import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from companion.services.benchmark_metrics import (
    read_presentmon_csv,
    summarize_dynamic_limiter,
    summarize_presentmon_files,
    summarize_presentmon_rows,
)
from companion.services.performance_benchmark import PresentMonCapture, focus_window
from companion.core.models import Profile, RtssLimiterConfig
from scripts.benchmark_lossless_scaling import (
    default_profile_limiter_settings,
    main as run_benchmark,
    monitor_workload_bounds,
    parse_args,
    limiter_profile_readback,
    resolve_benchmark_executable,
)


class BenchmarkMetricTests(unittest.TestCase):
    def test_missing_rtss_fails_before_launching_or_scaling(self):
        manager = Mock()
        manager.config.rtss_install_path = None
        with patch('scripts.benchmark_lossless_scaling.resolve_benchmark_executable', return_value=(Path('C:/tools/LSBenchmark.exe'), True)), \
                patch('scripts.benchmark_lossless_scaling.ProfileManager', return_value=manager), \
                patch('scripts.benchmark_lossless_scaling.RtssProfileManager') as rtss, \
                patch('scripts.benchmark_lossless_scaling.subprocess.Popen') as launch:
            rtss.return_value.status.return_value = {'installed': False, 'running': False}
            self.assertEqual(run_benchmark(['--dynamic-limiter', '--duration', '300', '--yes']), 2)
        launch.assert_not_called()

    def test_monitor_resolution_preserves_physical_bounds_and_restores_dpi_context(self):
        native = Mock()
        native.SetThreadDpiAwarenessContext.return_value = 123
        native.MonitorFromWindow.return_value = 99

        def monitor_info(_handle, pointer):
            info = pointer._obj
            info.monitor.left = -3840
            info.monitor.top = 0
            info.monitor.right = 0
            info.monitor.bottom = 2160
            return True

        native.GetMonitorInfoW.side_effect = monitor_info
        with patch('scripts.benchmark_lossless_scaling.ctypes.windll.user32', native):
            bounds = monitor_workload_bounds()
        self.assertEqual(bounds, {'left': -3840, 'top': 0, 'width': 3840, 'height': 2160})
        self.assertEqual(native.SetThreadDpiAwarenessContext.call_args.args, (123,))

    def test_dynamic_benchmark_options_and_rtss_readback(self):
        args = parse_args(['--dynamic-limiter', '--monitor-resolution', '--duration', '300', '--ls-gpu-target', '25'])
        self.assertTrue(args.monitor_resolution)
        self.assertEqual(args.ls_gpu_target, 25)
        rtss = Mock()
        rtss._profiles_dir.return_value = Path('C:/RTSS/Profiles')
        rtss._get_value.return_value = '91'
        profile = Profile(name='Benchmark', rtss=RtssLimiterConfig(managed_install_path='C:/RTSS', managed_target_process='LSBenchmark.exe'))
        self.assertEqual(limiter_profile_readback(rtss, profile), 91)
        rtss._get_value.return_value = 'invalid'
        self.assertIsNone(limiter_profile_readback(rtss, profile))

    def test_dynamic_target_requires_sustained_tail_not_a_single_peak(self):
        samples = [{"elapsed_seconds": second, "gpu_headroom_percent": 30 if second >= 35 else 5,
                    "current_limit": 120 - min(second, 10), "state": "calibrated"}
                   for second in range(60)]
        summary = summarize_dynamic_limiter(samples, target_percent=30, duration_seconds=60)
        self.assertEqual(summary["status"], "fail")
        for sample in samples:
            sample["gpu_headroom_percent"] = 30 if sample["elapsed_seconds"] >= 30 else 5
        summary = summarize_dynamic_limiter(samples, target_percent=30, duration_seconds=60)
        self.assertEqual(summary["status"], "pass")
        self.assertEqual(summary["first_target_hit_seconds"], 30)
        self.assertEqual(summary["adjustment_count"], 10)
        self.assertEqual(summary["final_limit"], 110)

    def test_dynamic_missing_telemetry_short_runs_and_no_response_do_not_pass(self):
        samples = [{"elapsed_seconds": second, "gpu_headroom_percent": 30,
                    "current_limit": 100, "state": "calibrated"} for second in range(60)]
        for rows, duration in (([], 60), (samples[:20], 20), (samples[:40], 60),
                               (samples[:-1] + [{**samples[-1], "state": "no_response"}], 60)):
            with self.subTest(duration=duration, count=len(rows)):
                self.assertEqual(summarize_dynamic_limiter(rows, target_percent=30, duration_seconds=duration)["status"], "fail")

    def test_extra_headroom_and_low_ls_demand_are_healthy(self):
        samples = [{"elapsed_seconds": second, "gpu_headroom_percent": 70, "lossless_gpu_percent": 5,
                    "current_limit": 100, "state": "calibrated"} for second in range(60)]
        result = summarize_dynamic_limiter(samples, target_percent=30, duration_seconds=60)
        self.assertTrue(result["sustained_target_reached"])
        self.assertEqual(result["settling_within_target_band_fraction"], 0)
        self.assertEqual(result["maximum_headroom_shortfall_percent"], 0)
        self.assertEqual(result["policy_version"], 1)
        self.assertFalse(result["frame_deadlines_verified"])

    def test_old_process_only_telemetry_cannot_pass_the_headroom_benchmark(self):
        samples = [{"elapsed_seconds": second, "lossless_gpu_percent": 70,
                    "current_limit": 100, "state": "calibrated"} for second in range(60)]
        self.assertEqual(summarize_dynamic_limiter(samples, target_percent=15, duration_seconds=60)['status'], 'fail')

    def test_process_filter_excludes_other_ls_instances_before_aggregation(self):
        with tempfile.TemporaryDirectory() as directory:
            csv_file = Path(directory) / "presentmon.csv"
            csv_file.write_text(
                "Application,ProcessID,TimeInSeconds,DisplayedTime\n"
                "LosslessScaling.exe,7,0,500\n"
                "LosslessScaling.exe,8,0,5\n"
                "LosslessScaling.exe,8,1,5\n"
                "LosslessScaling.exe,8,2,5\n"
                "LosslessScaling.exe,7,2,500\n", encoding="utf-8")
            result = summarize_presentmon_files([csv_file], warmup_seconds=1, process_id=7)
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["rows"], 1)
            self.assertEqual(result[0]["process_id"], 7)
            self.assertEqual(result[0]["metrics"]["displayed_fps"]["median"], 2)
            self.assertEqual(summarize_presentmon_files([csv_file], process_id=9), [])

    def test_focus_window_retries_with_attached_windows_input_threads(self):
        native = Mock()
        native.IsWindow.return_value = True
        native.IsIconic.return_value = False
        native.GetForegroundWindow.side_effect = [99, 99, 42]
        native.GetWindowThreadProcessId.side_effect = [7, 8]
        native.AttachThreadInput.return_value = True
        kernel = Mock()
        kernel.GetCurrentThreadId.return_value = 6

        with (
            patch("companion.services.performance_benchmark.user32", native),
            patch("companion.services.performance_benchmark.kernel32", kernel),
        ):
            self.assertTrue(focus_window(42, timeout=0.01))

        native.AttachThreadInput.assert_any_call(6, 7, True)
        native.AttachThreadInput.assert_any_call(6, 8, True)
        native.AttachThreadInput.assert_any_call(6, 8, False)
        native.AttachThreadInput.assert_any_call(6, 7, False)
        self.assertEqual(native.SetForegroundWindow.call_count, 2)

    def test_default_profile_limiter_uses_saved_limit_without_mutation(self):
        profile = Profile(
            name="Default",
            is_default=True,
            rtss=RtssLimiterConfig(
                enabled=True,
                limit_mode="dynamic",
                learned_framerate_limit=73,
                maximum_framerate_limit=120,
                limit_method="async",
            ),
        )
        before = profile.model_dump()
        settings = default_profile_limiter_settings(
            profile, globally_enabled=True, global_mode="static"
        )
        self.assertEqual(settings["configured_limit"], 73)
        self.assertEqual(settings["effective_mode"], "dynamic")
        self.assertEqual(settings["limit_method"], "async")
        self.assertEqual(profile.model_dump(), before)

    def test_downloaded_project_workload_keeps_latency_marker_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            workload = Path(directory) / "LSBenchmark-x64.exe"
            workload.write_bytes(b"test")
            resolved, project_workload = resolve_benchmark_executable(str(workload))
        self.assertEqual(resolved.name, "LSBenchmark-x64.exe")
        self.assertTrue(project_workload)

    def test_summarizes_v2_metrics_and_discards_warmup(self):
        rows = [
            {
                "Application": "Asteroids.exe",
                "ProcessID": "42",
                "TimeInSeconds": str(second),
                "FrameTime": str(frame),
                "DisplayedTime": str(frame),
                "DisplayLatency": "12",
                "MsClickToPhotonLatency": "18" if second > 1 else "NA",
                "GPUBusy": "7",
            }
            for second, frame in ((0, 100), (1, 50), (2, 10), (3, 20))
        ]

        result = summarize_presentmon_rows(rows, warmup_seconds=2)

        self.assertEqual(result["rows"], 2)
        self.assertEqual(result["application"], "Asteroids.exe")
        self.assertEqual(result["process_id"], 42)
        self.assertEqual(result["metrics"]["frame_time_ms"]["median"], 15)
        self.assertAlmostEqual(result["metrics"]["displayed_fps"]["average"], 1000 / 15)
        self.assertAlmostEqual(result["metrics"]["displayed_fps"]["median"], 1000 / 15)
        self.assertEqual(result["metrics"]["click_to_visible_ms"]["count"], 2)

    def test_reads_utf8_bom_csv_and_ignores_na(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.csv"
            path.write_text(
                "\ufeffApplication,ProcessID,MsBetweenPresents,MsUntilDisplayed\n"
                "Game.exe,7,16.5,NA\n",
                encoding="utf-8",
            )
            rows = read_presentmon_csv(path)
            result = summarize_presentmon_rows(rows)
            self.assertEqual(result["metrics"]["frame_time_ms"]["median"], 16.5)
            self.assertEqual(result["metrics"]["display_latency_ms"]["count"], 0)

    def test_presentmon_command_targets_both_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "PresentMon-2.4.1-x64.exe"
            executable.write_bytes(b"placeholder")
            capture = PresentMonCapture(executable, root / "output")

            command = capture.command(["Asteroids.exe", "LosslessScaling.exe"], 30)

            self.assertEqual(command.count("--process_name"), 2)
            self.assertIn("--multi_csv", command)
            self.assertIn("--terminate_after_timed", command)
            self.assertIn("30", command)

if __name__ == "__main__":
    unittest.main()
