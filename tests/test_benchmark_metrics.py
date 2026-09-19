import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from companion.services.benchmark_metrics import (
    read_presentmon_csv,
    summarize_presentmon_rows,
)
from companion.services.performance_benchmark import PresentMonCapture, focus_window
from companion.core.models import Profile, RtssLimiterConfig
from scripts.benchmark_lossless_scaling import (
    default_profile_limiter_settings,
    resolve_benchmark_executable,
)


class BenchmarkMetricTests(unittest.TestCase):
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
