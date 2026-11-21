import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from companion.core.models import AppConfig, Profile, ReshadeConfig
from companion.core.state import AppState

from companion.services.diagnostic_runner import DiagnosticRunner


class CaptureTraceCommandTests(unittest.TestCase):
    def payload(self, **changes):
        return {"schema": 1, "pid": 123, "createdAt": 900.0, "sampledAt": 1000.0,
                "headless": True, "hostVersion": "build-7-lsc.9", "addons": [],
                "capture": {"traceVersion": 1, "progress": {"handoffAttempts": 7}},
                "presentationPacing": {"version": 1, "intervals": 120, "relativeStalls": 2, "measurementOnly": True},
                "metrics": {"DLSSNR02": {"accepted": 7}, "other": {"unused": 1}}, **changes}

    def test_read_only_trace_preserves_progress_and_partial_errors(self):
        from run_live_diagnostics import capture_trace_report
        with patch("psutil.Process") as process, patch("time.sleep") as pause, \
                patch("companion.services.addon_host.time.time", return_value=1000.0), \
                patch("companion.services.addon_host._read_pipe", side_effect=[self.payload(), OSError("pipe busy"), self.payload()]):
            process.return_value.create_time.return_value = 900.0
            report = capture_trace_report(123)
        self.assertTrue(report["traceSupported"])
        self.assertEqual(len(report["samples"]), 3)
        self.assertEqual(report["samples"][0]["capture"]["progress"]["handoffAttempts"], 7)
        self.assertEqual(report["samples"][0]["metrics"], {"DLSSNR02": {"accepted": 7}})
        self.assertEqual(report["samples"][0]["presentationPacing"], self.payload()["presentationPacing"])
        self.assertEqual(report["samples"][1], {"available": False, "error": "pipe busy"})
        self.assertEqual(pause.call_count, 2)

    def test_trace_rejects_another_process_and_reports_legacy_host(self):
        from run_live_diagnostics import capture_trace_report
        for payload, available in ((self.payload(pid=456), False), (self.payload(capture={}), True)):
            with self.subTest(available=available), patch("psutil.Process") as process, \
                    patch("time.sleep"), patch("companion.services.addon_host.time.time", return_value=1000.0), \
                    patch("companion.services.addon_host._read_pipe", return_value=payload):
                process.return_value.create_time.return_value = 900.0
                report = capture_trace_report(123)
                self.assertFalse(report["traceSupported"])
                self.assertTrue(all(sample["available"] == available for sample in report["samples"]))

    def test_trace_mode_bypasses_elevation_and_application_startup(self):
        import run_live_diagnostics
        report = {"samples": [{"available": True}]}
        with patch.object(run_live_diagnostics, "parse_args", return_value=SimpleNamespace(capture_trace=123)), \
                patch.object(run_live_diagnostics, "capture_trace_report", return_value=report) as capture, \
                patch.object(run_live_diagnostics, "_ensure_admin") as elevate, patch("builtins.print"):
            self.assertEqual(run_live_diagnostics.main(), 0)
        capture.assert_called_once_with(123)
        elevate.assert_not_called()

    def test_trace_refuses_reused_pid_before_reading_telemetry(self):
        from companion.services.addon_host import capture_trace_report
        with patch("psutil.Process") as process, patch("companion.services.addon_host._read_pipe") as read:
            process.return_value.create_time.return_value = 901.0
            with self.assertRaisesRegex(ValueError, "restarted"):
                capture_trace_report(123, expected_created_at=900.0)
        read.assert_not_called()


class DiagnosticRunnerTests(unittest.TestCase):
    def test_nr_reproduction_profile_enables_nr_and_matches_local_capture_settings(self):
        default = Profile(name="Default", is_default=True,
                          native_scaling_settings={"FrameGeneration": "Off", "CaptureApi": "DXGI",
                                                   "HdrSupport": "false", "ScalingType": "NIS"})
        runner = DiagnosticRunner.__new__(DiagnosticRunner)
        runner.server = SimpleNamespace(config=SimpleNamespace(profiles=[default]), asset_store=Mock())
        runner.server.asset_store.list_packages.return_value = [{"provider": "ls-addon-manager", "version": "build-7-lsc.9"}]
        runner.server.asset_store.list_imports.return_value = [{"filename": "nvngx_dlssnr.dll", "architecture": "x64", "sha256": "a" * 64}]
        profile = runner._nr_reproduction_profile()
        self.assertEqual(profile.graphics.neural_render.implementation, "lsp_neural_render")
        self.assertEqual(profile.graphics.neural_render.runtime_asset_sha256, "a" * 64)
        self.assertTrue(profile.addon_config["addons"]["DLSSNR02"]["_enabled"])
        self.assertEqual(profile.native_scaling_settings, {"FrameGeneration": "LSFG3", "CaptureApi": "WGC",
                                                         "HdrSupport": "true", "ScalingType": "Off"})
        self.assertEqual(default.native_scaling_settings["CaptureApi"], "DXGI")
        self.assertEqual(default.native_scaling_settings["HdrSupport"], "false")

    def test_nr_reproduction_requires_imported_runtime_and_host(self):
        runner = DiagnosticRunner.__new__(DiagnosticRunner)
        runner.server = SimpleNamespace(config=SimpleNamespace(profiles=[]), asset_store=Mock())
        runner.server.asset_store.list_packages.return_value = [{"provider": "ls-addon-manager", "version": "build-7-lsc.9"}]
        runner.server.asset_store.list_imports.return_value = []
        with self.assertRaisesRegex(ValueError, "nvngx_dlssnr.dll"):
            runner._nr_reproduction_profile()

    def test_nr_settings_resolve_implicit_default_title_using_real_xml(self):
        from companion.services.ls_settings import LosslessSettingsXml
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Settings.xml"
            path.write_text("<Settings><GameProfiles><Profile><Title>Native Default</Title>"
                            "<ScalingType>NIS</ScalingType></Profile><Profile><Title>Other</Title>"
                            "<ScalingType>NIS</ScalingType></Profile></GameProfiles></Settings>", encoding="utf-8")
            settings = LosslessSettingsXml(str(path))
            runner = DiagnosticRunner.__new__(DiagnosticRunner)
            runner.server = SimpleNamespace(ls_settings=settings)
            values = {"FrameGeneration": "LSFG3", "CaptureApi": "WGC", "HdrSupport": "true", "ScalingType": "Off"}
            runner._apply_nr_reproduction_settings(Profile(name="Test", native_scaling_settings=values))
            native = settings.get_profile(None)
            self.assertEqual({key: native[key] for key in values}, values)
            self.assertEqual(settings.get_profile("Other")["ScalingType"], "NIS")

    def test_nr_benchmark_captures_before_stop_and_restores_after_success_or_waiting(self):
        from companion.services.asset_store import AssetStore
        for processed, slow_start, trace_error, actual_gpu in (
            (True, False, False, None), (False, False, False, None),
            (True, True, False, None), (True, False, True, None),
            (True, False, False, "NVIDIA GeForce RTX 5050"),
            (True, False, False, "NVIDIA GeForce RTX 3090 Ti"),
            (True, False, False, ""),
        ):
            with self.subTest(processed=processed, slow_start=slow_start, trace_error=trace_error), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                executable, workload, settings = root / "LosslessScaling.exe", root / "LSBenchmark.exe", root / "Settings.xml"
                executable.write_bytes(b"fixture")
                workload.write_bytes(b"fixture")
                settings.write_bytes(b"original settings")
                previous = Profile(name="Previous")
                profile = Profile(name="NR", native_scaling_settings={"FrameGeneration": "LSFG3", "CaptureApi": "WGC",
                                                                      "HdrSupport": "true", "ScalingType": "Off"})
                profile.graphics.neural_render.implementation = "lsp_neural_render"
                state = AppState()
                state.current_active_profile = previous
                state.auto_scale_enabled = True
                state.benchmark_mode_active = False
                running = {"value": True}
                events = []
                watcher = Mock()
                watcher.check_is_lossless_scaling_running.side_effect = lambda: running["value"]
                def stop_host():
                    running["value"] = False
                    return True
                def launch_host(**kwargs):
                    running["value"] = True
                    return True
                watcher.stop_lossless_scaling.side_effect = stop_host
                watcher.launch_lossless_scaling.side_effect = launch_host
                automation = Mock()
                automation._desired_gpu_route.return_value = {"gpuName": "NVIDIA GeForce RTX 5050"} if actual_gpu is not None else {}
                automation.resolve_deployment_files.return_value = [{"relative_path": "fixture"}]
                automation.deployment_manager.apply.return_value = {"files": [{"relative_path": executable.name,
                    "deployed_sha256": AssetStore.sha256(executable)}]}
                def set_scaling(enabled, *args, **kwargs):
                    events.append("start" if enabled else "stop")
                    state.is_scaling_active = enabled
                    return True
                automation.set_scaling.side_effect = set_scaling
                native_settings = Mock(path=settings)
                native_settings.get_profile.return_value = {"Title": "Native Default"}
                native_settings.gpu_route_changes_required.return_value = False
                def update_profile(*args, **kwargs):
                    settings.write_bytes(b"temporary NR settings")
                    return True
                native_settings.update_profile.side_effect = update_profile
                server = SimpleNamespace(automation=automation, state=state, process_watcher=watcher,
                    ls_settings=native_settings, _embedded_benchmark_executable=lambda: workload,
                    config=SimpleNamespace(lossless_scaling_exe_path=str(executable),
                        lossless_settings_xml_path=str(settings), addon_manager_enabled=False,
                        addon_manager=SimpleNamespace(enabled=False)))
                runner = DiagnosticRunner.__new__(DiagnosticRunner)
                runner.server = server
                runner._nr_reproduction_profile = Mock(return_value=profile)
                runner._wait_until = Mock(side_effect=lambda predicate, timeout, interval=0.2: bool(predicate()))
                runner._runtime_log_paths = Mock(return_value=[])
                runner._log_delta = Mock(return_value={"fixture.log": "fresh runtime evidence"})
                runner._runtime_log_errors = Mock(return_value=[])
                runner._runtime_log_evidence = Mock(return_value={})
                runner._runtime_gpu_names = Mock(return_value=[])
                runner._lossless_loaded_modules = Mock(return_value=([], None))
                runner._addon_runtime_checks = Mock(return_value={"neuralFramesProcessed": processed})
                trace = {"status": "pass", "details": {"trace": {"samples": [{"capture": {"captures": 7 if processed else 0}}]}}}
                def collect():
                    self.assertTrue(state.is_scaling_active)
                    events.append("trace")
                    if trace_error:
                        raise RuntimeError("trace fixture failure")
                    return trace
                runner._collect_nr_capture_trace = Mock(side_effect=collect)
                process = Mock(pid=456)
                process.poll.return_value = None
                with patch("companion.services.managed_runtime.assert_private_settings"), \
                        patch("scripts.benchmark_lossless_scaling.find_largest_window", return_value=789), \
                        patch("scripts.benchmark_lossless_scaling.focus_window", return_value=True), \
                        patch("companion.services.ls_inspector.LosslessScalingInspector") as inspector, \
                        patch("companion.services.diagnostic_runner.subprocess.Popen", return_value=process) as launch, \
                        patch("companion.services.diagnostic_runner.runtime_status", return_value={
                            "capture": {"adapter": actual_gpu, "captures": 7 if processed else 0}}), \
                        patch("companion.services.diagnostic_runner.time.sleep"):
                    inspector.return_value._find_log_file.return_value = None
                    observation = SimpleNamespace(is_active=True, to_dict=lambda: {"is_active": True})
                    inspector.return_value.inspect_current_scaling_target.side_effect = (
                        [SimpleNamespace(is_active=not slow_start), observation, observation])
                    result = runner._nr_capture_trace()
                expected_pass = processed and not trace_error and actual_gpu in (None, "NVIDIA GeForce RTX 5050")
                self.assertEqual(result["status"], "pass" if expected_pass else "fail")
                self.assertEqual(events, ["start", "trace", "stop"])
                launch.assert_called_once_with([str(workload), "--duration", "300"], cwd=str(root))
                native_settings.update_profile.assert_called_once_with("Native Default", profile.native_scaling_settings, backup=False)
                self.assertEqual(runner._wait_until.call_args.args[1], 30.0)
                attempt = result["details"]["testedProfiles"][0]
                if trace_error:
                    self.assertIn("trace fixture failure", attempt["error"])
                else:
                    self.assertEqual(attempt["captureTrace"], trace)
                    self.assertEqual(attempt["runtimeChecks"]["neuralFramesProcessed"], processed)
                    if actual_gpu is not None:
                        self.assertEqual(attempt["gpuRouteMatches"], actual_gpu == "NVIDIA GeForce RTX 5050")
                self.assertEqual(settings.read_bytes(), b"original settings")
                self.assertIs(state.current_active_profile, previous)
                self.assertTrue(state.auto_scale_enabled)
                self.assertFalse(state.benchmark_mode_active)
                self.assertIsNone(state.diagnostic_scaling_target)
                self.assertFalse(server.config.addon_manager_enabled)
                self.assertFalse(server.config.addon_manager.enabled)
                self.assertEqual(automation.deployment_manager.apply.call_args.kwargs["profile_id"], previous.id)
                process.terminate.assert_called_once_with()

    def test_nr_benchmark_reproduction_is_dedicated_and_mutating(self):
        runner = DiagnosticRunner.__new__(DiagnosticRunner)
        entry = next(item for item in runner.catalog() if item["id"] == "nr_capture_trace")
        self.assertFalse(entry["readOnly"])
        self.assertIn("benchmark reproduction", entry["name"])
        self.assertIn("nr_capture_trace", runner.LS_MUTATING_TESTS)
        self.assertEqual(entry["dependencies"], [])
        self.assertEqual([item[0] for item in runner._execution_plan("nr_capture_trace")], ["nr_capture_trace"])

    def test_nr_benchmark_reproduction_exports_snapshots_and_runs_cleanup(self):
        report = {"traceSupported": True, "samples": [{"available": True, "capture": {"traceVersion": 1}}] * 3}
        with tempfile.TemporaryDirectory() as directory:
            server = SimpleNamespace(profile_manager=SimpleNamespace(config_dir=Path(directory)),
                state=SimpleNamespace(simulation_mode=False), process_watcher=None,
                config=SimpleNamespace(lossless_scaling_exe_path="LosslessScaling.exe"))
            runner = DiagnosticRunner(server)
            profile = Profile(name="NR reproduction")
            runner._nr_reproduction_profile = Mock(return_value=profile)
            runner._run_live_addon_deployment_behavior = Mock(return_value=runner._result(
                "pass", "done", testedProfiles=[{"captureTrace": {"details": {"trace": report}}}]))
            runner._reset_lossless_folder = Mock(return_value=runner._result("pass", "reset"))
            with patch("companion.services.adapter_report.configured_process", return_value=(123, 900.0)):
                result = runner.run("nr_capture_trace")
            runner._run_live_addon_deployment_behavior.assert_called_once_with(profiles=[profile], capture_neural_trace=True)
            self.assertEqual(runner._reset_lossless_folder.call_count, 2)
            with zipfile.ZipFile(result["archivePath"]) as bundle:
                saved = json.loads(bundle.read("ls-companion-diagnostics/nr_capture_trace.log"))
            self.assertEqual(saved["status"], "pass")
            self.assertEqual(saved["details"]["testedProfiles"][0]["captureTrace"]["details"]["trace"], report)
            self.assertEqual([item["id"] for item in result["results"]],
                             ["nr_capture_trace", "environment_reset"])

    def test_nr_capture_trace_reports_missing_host_and_partial_evidence(self):
        runner = DiagnosticRunner.__new__(DiagnosticRunner)
        runner.server = SimpleNamespace(config=SimpleNamespace(lossless_scaling_exe_path="LosslessScaling.exe"))
        with patch("companion.services.adapter_report.configured_process", return_value=None), \
                patch("companion.services.diagnostic_runner.capture_trace_report") as capture:
            self.assertEqual(runner._collect_nr_capture_trace()["status"], "warn")
        capture.assert_not_called()
        reports = (
            ({"traceSupported": False, "samples": [{"available": True}]}, "warn"),
            ({"traceSupported": False, "samples": [{"available": False}]}, "fail"),
            ({"traceSupported": True, "samples": [{"available": True}, {"available": False}]}, "warn"),
        )
        for report, expected in reports:
            with self.subTest(expected=expected), \
                    patch("companion.services.adapter_report.configured_process", return_value=(123, 900.0)), \
                    patch("companion.services.diagnostic_runner.capture_trace_report", return_value=report):
                result = runner._collect_nr_capture_trace()
                self.assertEqual(result["status"], expected)
                self.assertEqual(result["details"]["trace"], report)

    def test_overlay_requires_listener_even_without_scaling_override(self):
        runner = DiagnosticRunner.__new__(DiagnosticRunner)
        config = AppConfig(override_lossless_hotkey=False)
        runner.server = SimpleNamespace(config=config, hotkey_listener=None)
        result = runner._hotkeys()
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["details"]["overlayHotkeys"], config.overlay_hotkeys)
        config.overlay_indicator_enabled = False
        self.assertEqual(runner._hotkeys()["status"], "pass")

    def test_hotkey_diagnostic_reports_unregistered_overlay_binding(self):
        runner = DiagnosticRunner.__new__(DiagnosticRunner)
        listener = SimpleNamespace(_thread=Mock(),
            _overlay_actions={"overlay_toggle": {"binding": "ctrl+home"}}, overlay_registered={})
        listener._thread.is_alive.return_value = True
        runner.server = SimpleNamespace(config=AppConfig(), hotkey_listener=listener)
        result = runner._hotkeys()
        self.assertEqual(result["status"], "warn")
        self.assertEqual(result["details"]["overlayUnavailable"], {"overlay_toggle": "ctrl+home"})
        listener.overlay_registered["overlay_toggle"] = "ctrl+home"
        self.assertEqual(runner._hotkeys()["status"], "pass")

    def test_reshade_requires_deployed_module_and_fresh_runtime_initialization(self):
        root = Path("diagnostic-ls").resolve()
        profile = Profile(name="ReShade", reshade=ReshadeConfig(enabled=True))
        files = [{"relative_path": "dxgi.dll", "source_package": "reshade/6.8/hash"}]
        logs = {"LSAddonManager.log": "Addon Manager for Lossless Scaling Build 5 starting",
                "ReShade.log": "Recreated runtime environment on runtime 000001234567 ('ReShade.ini')."}
        checks = DiagnosticRunner._addon_runtime_checks(profile, root, files,
            [str(Path("C:/Windows/System32/dxgi.dll"))], logs)
        self.assertFalse(checks["reshadeModulesLoaded"])
        checks = DiagnosticRunner._addon_runtime_checks(profile, root, files, [str(root / "dxgi.dll")], logs)
        self.assertTrue(all(checks.values()))
        logs["ReShade.log"] = "Initializing crosire's ReShade version 6.8"
        checks = DiagnosticRunner._addon_runtime_checks(profile, root, files, [str(root / "dxgi.dll")], logs)
        self.assertFalse(checks["reshadeRuntimeInitialized"])

    def test_special_k_requires_its_module_and_own_runtime_evidence(self):
        root = Path("diagnostic-ls").resolve()
        profile = Profile(name="Special K")
        profile.graphics.special_k.enabled = True
        files = [{"relative_path": "dxgi.dll", "source_package": "special-k/1/hash"}]
        logs = {"LSAddonManager.log": "Addon Manager for Lossless Scaling Build 5 starting"}
        checks = DiagnosticRunner._addon_runtime_checks(profile, root, files, [str(root / "dxgi.dll")], logs)
        self.assertFalse(checks["specialKRuntimeInitialized"])
        logs["dxgi.log"] = "Special K initialized"
        self.assertTrue(all(DiagnosticRunner._addon_runtime_checks(profile, root, files,
            [str(root / "dxgi.dll")], logs).values()))

    def test_current_neural_recipe_requires_processed_frames_and_healthy_telemetry(self):
        root = Path("diagnostic-ls").resolve()
        profile = Profile(name="NR")
        profile.graphics.neural_render.implementation = "lsp_neural_render"
        files = [{"relative_path": name, "source_package": "ls-addon-manager/build-5/hash"}
                 for name in ("addons/DLSSNR02/DLSSNR02.dll", "addons/DLSSNR02/NeuralRendering_nvngx.dll")]
        files.append({"relative_path": "nvngx_dlssnr.dll", "role": "neural_runtime"})
        modules = [str(root / item["relative_path"]) for item in files]
        logs = {"LSAddonManager.log": "Addon Manager for Lossless Scaling Build 5 starting"}
        telemetry = {"available": True, "addons": [{"id": "DLSSNR02", "loaded": True, "enabled": True}],
                     "capture": {"captures": 10}, "metrics": {"DLSSNR02": {"accepted": 5, "model_ms": 2.0}}}
        check = lambda: DiagnosticRunner._addon_runtime_checks(profile, root, files, modules, logs, telemetry)
        self.assertTrue(all(check().values()))
        telemetry["metrics"]["DLSSNR02"]["accepted"] = 0
        self.assertFalse(check()["neuralFramesProcessed"])
        telemetry["addons"][0]["faulted"] = True
        self.assertFalse(check()["neuralAddonHealthy"])
        modules.pop()
        self.assertFalse(check()["neuralModulesLoaded"])

    def test_saved_gpu_map_does_not_hide_failed_idle_discovery(self):
        runner = DiagnosticRunner.__new__(DiagnosticRunner)
        runner.server = SimpleNamespace(
            state=AppState(), profile_manager=SimpleNamespace(config_dir=Path("missing")),
            automation=Mock(),
        )
        runner.server.automation.gpu_map_status.return_value = {"state": "ok"}
        runner.server.automation._desired_gpu_route.return_value = {}
        runner.server.state.gpu_mapping_status = {"state": "error", "message": "empty adapter names"}
        with patch("companion.services.adapter_report.load_latest", return_value=None):
            result = runner._gpu_routing()
        self.assertEqual(result["status"], "warn")
        self.assertFalse(result["details"]["liveRoutingVerified"])
        self.assertEqual(result["details"]["evidenceScope"], "saved_map_consistency")

    def test_live_cleanup_does_not_restore_files_when_host_will_not_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable, workload, settings_path = root / "LosslessScaling.exe", root / "LSBenchmark.exe", root / "Settings.xml"
            for path in (executable, workload, settings_path):
                path.write_bytes(b"original")
            config = AppConfig(lossless_scaling_exe_path=str(executable))
            manager, watcher, automation = Mock(config=config), Mock(), Mock()
            watcher.check_is_lossless_scaling_running.return_value = True
            watcher.stop_lossless_scaling.return_value = False
            runner = DiagnosticRunner.__new__(DiagnosticRunner)
            runner.server = SimpleNamespace(config=config, profile_manager=manager, state=AppState(),
                process_watcher=watcher, automation=automation, ls_settings=SimpleNamespace(path=settings_path),
                hotkey_listener=None, _embedded_benchmark_executable=lambda: str(workload))
            runner._path = str
            with patch("companion.services.ls_inspector.LosslessScalingInspector") as inspector, patch(
                "companion.services.diagnostic_runner.subprocess.Popen", side_effect=RuntimeError("workload unavailable")
            ):
                inspector.return_value._find_log_file.return_value = None
                results = runner._run_live_scaling_workflow()
            automation.deployment_manager.apply.assert_not_called()
            self.assertEqual(settings_path.read_bytes(), b"original")
            self.assertFalse(runner.server.state.benchmark_mode_active)
            self.assertTrue(all("restoration was not attempted" in result["details"]["cleanupError"] for result in results.values()))

    def test_echo_106_crash_is_a_fatal_runtime_error(self):
        logs = {"LSAddonManager.log": "[16:44:43.832] [ERROR] [Crash] unhandled exception\n"
                "[16:44:43.832] [ERROR] [Crash] code 0xC0000005 at d3d11.dll+0x988FD\n"}
        errors = DiagnosticRunner._runtime_log_errors(logs)
        self.assertEqual(len(errors), 2)
        self.assertIn("0xC0000005", errors[-1])
        self.assertTrue(DiagnosticRunner._runtime_log_evidence(logs)["runtimeCrashed"])

    def test_live_profiles_inherit_gpu_identity_without_enabling_default_addons(self):
        default = Profile(name="Default", is_default=True, lossless_profile_title="Native",
                          preferred_gpu_device_id="gpu-two", preferred_gpu_name="RTX 5050",
                          native_scaling_settings={"PreferredGpuId": "2", "OutputDisplayId": "1", "ScalingType": "NIS"},
                          addon_config={"addons": {"DLSSNR02": {"_enabled": True}}})
        runner = DiagnosticRunner.__new__(DiagnosticRunner)
        runner.server = SimpleNamespace(config=AppConfig(profiles=[default]))
        profile = runner._live_test_profile("scale")
        self.assertEqual(profile.preferred_gpu_device_id, "gpu-two")
        self.assertEqual(profile.preferred_gpu_name, "RTX 5050")
        self.assertEqual(profile.native_scaling_settings, {"PreferredGpuId": "2", "OutputDisplayId": "1"})
        self.assertEqual(profile.addon_config["addons"], {})

    def test_runtime_gpu_is_dispatch_device_not_preflight_or_gui_card(self):
        names = DiagnosticRunner._runtime_gpu_names({"addon.log":
            "requirements: Graphics card: NVIDIA GeForce RTX 5050 (8 GB)\n"
            "Running on NVIDIA GeForce RTX 5050 Driver 617.42\n"
            "dispatching device 00000139941F9BC0 on NVIDIA GeForce RTX 3090 Ti (LUID 000149e3) -> TAPPED\n"})
        self.assertEqual(names, ["NVIDIA GeForce RTX 3090 Ti"])

    def test_cleanup_refuses_running_or_relaunched_host(self):
        runner = DiagnosticRunner.__new__(DiagnosticRunner)
        watcher = Mock()
        runner.server = SimpleNamespace(process_watcher=watcher)
        watcher.check_is_lossless_scaling_running.return_value = True
        watcher.stop_lossless_scaling.return_value = False
        with self.assertRaisesRegex(RuntimeError, "restoration was not attempted"):
            runner._require_stopped_for_deployment()
        watcher.stop_lossless_scaling.return_value = True
        with self.assertRaisesRegex(RuntimeError, "restarted during cleanup"):
            runner._require_stopped_for_deployment()

    def test_lossless_settings_log_exposes_schema_without_titles_or_paths(self):
        runner = DiagnosticRunner.__new__(DiagnosticRunner)
        settings = Mock()
        settings.path = Mock()
        settings.path.is_file.return_value = True
        settings.path.__str__ = Mock(return_value="Settings.xml")
        settings.read.return_value = {
            "profiles": [{
                "Title": "Private Game",
                "Path": r"D:\Private\Game.exe",
                "FrameGeneration": "LSFG3",
                "LSFG3Multiplier": "2",
            }]
        }
        settings.initial_backup_path = Mock()
        settings.initial_backup_path.is_file.return_value = True
        settings.native_auto_scale_enabled.return_value = False
        runner.server = SimpleNamespace(ls_settings=settings)
        runner._path = lambda value: str(value)

        result = runner._lossless_settings()

        schema = result["details"]["nativeProfileSchemas"][0]["settings"]
        self.assertEqual(schema, {"FrameGeneration": "LSFG3", "LSFG3Multiplier": "2"})
        self.assertNotIn("Private Game", json.dumps(result))
        self.assertNotIn("Game.exe", json.dumps(result))

    def test_isolated_component_helpers_execute_production_code(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = DiagnosticRunner.__new__(DiagnosticRunner)
            runner.root = Path(directory)
            runner._behavior_root = runner.root / "behavior"
            runner._behavior_root.mkdir()

            results = [
                runner._reshade_move_behavior(),
                runner._deployment_behavior(),
                runner._benchmark_cli_behavior(),
            ]

            self.assertEqual([result["status"] for result in results], ["pass"] * 3, results)

    def test_reshade_diagnostic_rejects_original_missing_and_corrupted_presets(self):
        import configparser
        from companion.services.reshade_manager import ReshadeManager

        swap = ReshadeManager.swap_preset
        for fault in ("original", "missing", "corrupted", "mismatched-selection"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                runner = DiagnosticRunner.__new__(DiagnosticRunner)
                runner._behavior_root = Path(directory)

                def broken_swap(active, preset):
                    self.assertTrue(swap(active, preset))
                    ini = configparser.ConfigParser(interpolation=None)
                    ini.read(active, encoding="utf-8")
                    selected = Path(ini["GENERAL"]["PresetPath"])
                    if fault == "missing":
                        selected.unlink()
                    elif fault == "corrupted":
                        selected.write_text("Techniques=Wrong@Wrong.fx\n", encoding="utf-8")
                    else:
                        keys = ("PresetPath", "CurrentPresetPath", "StartupPresetPath") if fault == "original" else ("StartupPresetPath",)
                        for key in keys:
                            ini["GENERAL"][key] = preset
                        with open(active, "w", encoding="utf-8") as output:
                            ini.write(output)
                    return True

                with patch.object(ReshadeManager, "swap_preset", side_effect=broken_swap):
                    result = runner._reshade_move_behavior()
                self.assertEqual(result["status"], "fail", result)
                self.assertFalse(result["details"]["presetSwapped"])
                self.assertTrue(result["details"]["backupPreserved"])
                self.assertTrue(result["details"]["originalRestored"])

    def test_live_scaling_results_run_once_and_are_returned_individually(self):
        runner = DiagnosticRunner.__new__(DiagnosticRunner)
        runner._live_scaling_results = None
        expected = {
            key: runner._result("pass", key)
            for key in ("autoscale", "hotkey_override", "lossless_log")
        }
        runner._run_live_scaling_workflow = Mock(return_value=expected)

        self.assertIs(runner._autoscale_behavior(), expected["autoscale"])
        self.assertIs(runner._hotkey_override_behavior(), expected["hotkey_override"])
        self.assertIs(runner._lossless_scaling_log_behavior(), expected["lossless_log"])
        runner._run_live_scaling_workflow.assert_called_once_with()

    def test_reshade_runtime_uses_the_cached_live_deployment_attempt(self):
        runner = DiagnosticRunner.__new__(DiagnosticRunner)
        runner._live_addon_result = None
        deployment = runner._result(
            "fail", "one failed",
            testedProfiles=[{
                "profile": "Experimental Live reshade-proxy",
                "status": "fail",
                "runtimeLogFiles": [],
                "error": "scaling was not confirmed",
            }],
            failures=[{"profile": "Experimental Live reshade-proxy"}],
        )
        runner._run_live_addon_deployment_behavior = Mock(return_value=deployment)

        result = runner._reshade_runtime_behavior()

        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["details"]["attempts"][0]["profile"], "Experimental Live reshade-proxy")
        self.assertIs(runner._live_addon_deployment_behavior(), deployment)
        runner._run_live_addon_deployment_behavior.assert_called_once_with()

    def test_reshade_runtime_still_interprets_a_failed_deployment_dependency(self):
        runner = DiagnosticRunner.__new__(DiagnosticRunner)
        runner.root = Path(tempfile.mkdtemp())
        runner._live_scaling_results = {"stale": {}}
        runner._live_addon_result = {"stale": True}
        runner.server = SimpleNamespace(
            profile_manager=SimpleNamespace(config_dir=runner.root),
            state=SimpleNamespace(simulation_mode=False),
            process_watcher=None,
        )
        runner._tests = lambda: (
            ("deployment_behavior", "Deployment", lambda: runner._result("fail", "failed")),
            ("reshade_runtime", "ReShade", lambda: runner._result("fail", "interpreted")),
        )
        runner.DEPENDENCIES = {"reshade_runtime": ("deployment_behavior",)}
        runner._reset_lossless_folder = lambda _running: runner._result("pass", "reset")
        try:
            result = runner.run("reshade_runtime")
        finally:
            import shutil
            shutil.rmtree(runner.root, ignore_errors=True)

        summaries = {item["id"]: item["summary"] for item in result["results"]}
        self.assertEqual(summaries["reshade_runtime"], "interpreted")

    def test_run_writes_one_log_per_test_and_downloadable_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory) / "config"
            server = SimpleNamespace(
                profile_manager=SimpleNamespace(config_dir=config_dir),
                state=SimpleNamespace(simulation_mode=True),
                process_watcher=None,
            )
            runner = DiagnosticRunner(server)
            runner._tests = lambda: (
                ("first", "First", lambda: runner._result("pass", "worked")),
                ("second", "Second", lambda: runner._result("warn", "inspect")),
            )
            runner._reset_lossless_folder = lambda _running: runner._result("pass", "reset")

            result = runner.run()

            archive = Path(result["archivePath"])
            self.assertTrue(archive.is_file())
            self.assertEqual(result["counts"], {"pass": 2, "warn": 1, "fail": 0})
            with zipfile.ZipFile(archive) as bundle:
                names = set(bundle.namelist())
                self.assertIn("ls-companion-diagnostics/first.log", names)
                self.assertIn("ls-companion-diagnostics/second.log", names)
                self.assertIn("ls-companion-diagnostics/environment_reset.log", names)
                summary = json.loads(bundle.read("ls-companion-diagnostics/summary.json"))
            self.assertEqual(
                [item["id"] for item in summary["results"]],
                ["first", "second", "environment_reset"],
            )

    def test_individual_execution_plan_includes_dependencies_first(self):
        runner = DiagnosticRunner.__new__(DiagnosticRunner)
        runner._tests = lambda: (
            ("configuration", "Configuration", lambda: {}),
            ("profile_integrity", "Profiles", lambda: {}),
            ("profile_matching", "Matching", lambda: {}),
        )

        plan = runner._execution_plan("profile_matching")

        self.assertEqual([item[0] for item in plan], [
            "configuration", "profile_integrity", "profile_matching",
        ])

    def test_adaptive_acceptance_benchmark_is_last_and_requires_setup(self):
        runner = DiagnosticRunner.__new__(DiagnosticRunner)
        self.assertEqual(runner.catalog()[-1]["id"], "rtss_adaptive_benchmark")
        plan=[item[0] for item in runner._execution_plan("rtss_adaptive_benchmark")]
        self.assertEqual(plan[-1], "rtss_adaptive_benchmark")
        self.assertIn("benchmark",plan)
        self.assertIn("rtss",plan)
        self.assertIn("rtss_adaptive_benchmark",runner.LS_MUTATING_TESTS)

    def test_run_all_includes_lilium_diagnostic(self):
        runner = DiagnosticRunner.__new__(DiagnosticRunner)

        catalog_ids = {item["id"] for item in runner.catalog()}
        run_all_ids = [item[0] for item in runner._execution_plan(None)]

        self.assertIn("reshade_lilium", catalog_ids)
        self.assertIn("reshade_lilium", run_all_ids)
        self.assertLess(run_all_ids.index("reshade"), run_all_ids.index("reshade_lilium"))

    def test_lilium_diagnostic_captures_relevant_compiler_error_and_shader_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install = root / "lossless"
            install.mkdir()
            executable = install / "LosslessScaling.bin"
            executable.write_bytes(b"test")
            source = root / "config" / "reshade-profiles" / "hdr" / "packages" / "22"
            deployed = install / "reshade-shaders" / "LSCompanion" / "hdr" / "packages" / "22"
            source.mkdir(parents=True)
            deployed.mkdir(parents=True)
            (source / "lilium__cas_hdr.fx").write_text("shader", encoding="utf-8")
            (deployed / "lilium__cas_hdr.fx").write_text("shader", encoding="utf-8")
            (install / "ReShade.log").write_text(
                'Loading effect "lilium__cas_hdr.fx"\nerror X3004: undeclared identifier\n',
                encoding="utf-8",
            )
            managed = SimpleNamespace(id="hdr", name="HDR", shaders={"22": ["lilium__cas_hdr.fx"]})
            active = SimpleNamespace(reshade=SimpleNamespace(enabled=True, managed_profile_id="hdr"))
            server = SimpleNamespace(
                config=SimpleNamespace(lossless_scaling_exe_path=str(executable), reshade_profiles=[managed]),
                state=SimpleNamespace(current_active_profile=active),
                profile_manager=SimpleNamespace(config_dir=root / "config"),
            )

            result = DiagnosticRunner(server)._reshade_lilium()

            self.assertEqual(result["status"], "fail")
            self.assertEqual(len(result["details"]["compilationErrors"]), 1)
            shader = result["details"]["profiles"][0]["shaders"][0]
            self.assertEqual(shader["managedSha256"], shader["deployedSha256"])

    def test_selected_lilium_diagnostic_does_not_reset_lossless_scaling(self):
        with tempfile.TemporaryDirectory() as directory:
            server = SimpleNamespace(
                profile_manager=SimpleNamespace(config_dir=Path(directory)),
                state=SimpleNamespace(simulation_mode=False),
                process_watcher=Mock(),
            )
            runner = DiagnosticRunner(server)
            runner._tests = lambda: (("reshade_lilium", "Lilium", lambda: runner._result("warn", "inspect")),)
            runner._reset_lossless_folder = Mock()

            result = runner.run("reshade_lilium")

            runner._reset_lossless_folder.assert_not_called()
            self.assertIn("not reset", result["results"][-1]["summary"])

    def test_lilium_failure_on_earlier_runtime_with_later_success_is_reported_as_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "LosslessScaling.exe"
            executable.write_bytes(b"test")
            source = root / "config" / "reshade-profiles" / "hdr" / "packages" / "22"
            source.mkdir(parents=True)
            (source / "lilium__inverse_tone_mapping.fx").write_text("shader", encoding="utf-8")
            (root / "ReShade.log").write_text(
                "Failed to compile 'lilium__inverse_tone_mapping.fx':\n"
                "error X4530: pixel shader must minimally write all four components of COLOR0\n"
                "Successfully compiled 'lilium__inverse_tone_mapping.fx' in 0.036 seconds.\n",
                encoding="utf-8",
            )
            managed = SimpleNamespace(id="hdr", name="HDR", shaders={"22": ["lilium__inverse_tone_mapping.fx"]})
            server = SimpleNamespace(
                config=SimpleNamespace(lossless_scaling_exe_path=str(executable), reshade_profiles=[managed]),
                state=SimpleNamespace(current_active_profile=None),
                profile_manager=SimpleNamespace(config_dir=root / "config"),
            )

            result = DiagnosticRunner(server)._reshade_lilium()

            self.assertEqual(result["status"], "warn")
            self.assertEqual(result["details"]["evidenceScope"], "historical")
            self.assertTrue(result["details"]["compilationErrors"][0]["laterSuccessfulCompilation"])

    def test_log_delta_reads_restarted_truncated_log_from_the_beginning(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "LosslessProxy.log"
            path.write_text("old output that is considerably longer", encoding="utf-8")
            snapshot = DiagnosticRunner._log_snapshot([path])
            path.write_text("new output", encoding="utf-8")

            delta = DiagnosticRunner._log_delta(snapshot, [path])

            self.assertEqual(delta[str(path)], "new output")

    def test_log_delta_detects_a_truncated_log_that_regrew_past_its_old_size(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "LosslessProxy.log"
            path.write_text("old-prefix\n" + "x" * 20, encoding="utf-8")
            snapshot = DiagnosticRunner._log_snapshot([path])
            path.write_text("new-prefix\n" + "y" * 80, encoding="utf-8")

            delta = DiagnosticRunner._log_delta(snapshot, [path])

            self.assertTrue(delta[str(path)].startswith("new-prefix"))

    def test_runtime_log_errors_ignores_ngx_fallback_warnings_after_ready(self):
        errors = DiagnosticRunner._runtime_log_errors({
            "LSP-NeuralRender.log": (
                "error: failed to load NGXCore: 126 (_nvngx.dll)\n"
                "nvLoadSignedLibraryW() failed: missing or corrupted\n"
                "NrEngine ready (float slot 6)\n"
            )
        })

        self.assertEqual(errors, [])

    def test_runtime_log_errors_reports_explicit_neural_engine_failure(self):
        errors = DiagnosticRunner._runtime_log_errors({
            "LSP-NeuralRender.log": "NrEngine FAILED: CreateFeature(18): FeatureNotSupported\n"
        })

        self.assertEqual(len(errors), 1)
        self.assertIn("FeatureNotSupported", errors[0])

    def test_runtime_log_evidence_requires_model_work_beyond_engine_ready(self):
        ready = DiagnosticRunner._runtime_log_evidence({
            "LSP-NeuralRender.log": "NrEngine ready (float slot 6)\nticks 0, taps 0\n"
        })
        exercised = DiagnosticRunner._runtime_log_evidence({
            "LSP-NeuralRender.log": "NrEngine ready (float slot 6)\nPrepare: frame 1280x720\ntaps 1\n"
        })

        self.assertTrue(ready["neuralEngineReady"])
        self.assertFalse(ready["neuralModelPrepared"])
        self.assertFalse(ready["neuralTapObserved"])
        self.assertTrue(exercised["neuralModelPrepared"])
        self.assertTrue(exercised["neuralTapObserved"])

    def test_runtime_log_extracts_lossless_proxy_self_reported_version(self):
        version = DiagnosticRunner._reported_lossless_proxy_version({
            "LosslessProxy.log": "[INFO] [Core] LosslessProxy v0.2.0 starting...\n"
        })

        self.assertEqual(version, "0.2.0")

    def test_proxy_version_label_is_not_a_runtime_error(self):
        errors = DiagnosticRunner._runtime_log_errors({
            "LosslessProxy.log": "[INFO] [Core] LosslessProxy v0.2.0 starting...\n"
        })

        self.assertEqual(errors, [])

    def test_runtime_log_paths_include_special_k_nested_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root_log = root / "LosslessProxy.log"
            nested_log = root / "logs" / "crash.log"
            nested_log.parent.mkdir()
            root_log.write_text("proxy", encoding="utf-8")
            nested_log.write_text("special k", encoding="utf-8")

            paths = DiagnosticRunner._runtime_log_paths(root)

            self.assertEqual(set(paths), {root_log, nested_log})


if __name__ == "__main__":
    unittest.main()
