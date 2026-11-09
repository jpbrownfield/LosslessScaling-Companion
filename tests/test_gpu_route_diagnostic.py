"""The shareable GPU diagnostic observes a live misroute without repairing it."""
import json
import tempfile
import unittest
import zipfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from companion.core.profile_manager import ProfileManager
from companion.core.state import AppState
from companion.services.automation import AutomationController
from companion.services.diagnostic_runner import DiagnosticRunner
from companion.services.gpu_router import GpuRouter, inventory_fingerprint


RTX_3090 = r"PCI\VEN_10DE&DEV_2203"
RTX_5050 = r"PCI\VEN_10DE&DEV_2D83"


@contextmanager
def diagnostic():
    with tempfile.TemporaryDirectory() as directory:
        manager = ProfileManager(Path(directory))
        config = manager.config
        config.preferred_scaling_gpu_device_id = RTX_5050
        config.preferred_scaling_gpu_name = "NVIDIA GeForce RTX 5050"
        config.auto_route_gpu_to_display = False
        config.ls_gpu_id_map = {RTX_3090: 1, RTX_5050: 6}
        router = GpuRouter(lambda: [
            {"deviceName": r"\\.\DISPLAY1", "name": "NVIDIA GeForce RTX 3090",
             "deviceId": RTX_3090, "primary": True},
            {"deviceName": r"\\.\DISPLAY2", "name": "NVIDIA GeForce RTX 5050",
             "deviceId": RTX_5050, "primary": False},
        ], lambda hwnd: r"\\.\DISPLAY2", ls_gpu_id_map=lambda: config.ls_gpu_id_map)
        config.ls_gpu_map_fingerprint = inventory_fingerprint(router.detect())
        state = AppState()
        state.is_scaling_active = True
        state.scaling_target_pid, state.scaling_target_hwnd = 42, 82
        state.current_active_profile = config.profiles[0]
        watcher, settings = Mock(), Mock()
        automation = AutomationController(manager, state, watcher, gpu_router=router, lossless_settings=settings)
        automation._adapter_report_process = Mock(return_value=(123, 100.0))
        automation._settings_route = Mock(return_value=(6, 2))
        automation._verify_active_route = Mock(return_value=True)
        automation.set_scaling = Mock()
        rows = [{"description": "NVIDIA GeForce RTX 3090", "vendorId": 0x10DE,
                 "deviceId": 0x2203, "luidLow": index + 1} for index in range(5)]
        rows.append({"description": "NVIDIA GeForce RTX 5050", "vendorId": 0x10DE,
                     "deviceId": 0x2D83, "luidLow": 6})
        automation._current_adapter_report = Mock(return_value={
            "schema": 1, "pid": 123, "hasDevice": True,
            "device": rows[-1], "enumAdapters": rows, "enumAdapters1": rows,
            "lsAdapterNames": [row["description"] for row in rows],
        })
        automation.refresh_gpu_selections = Mock(side_effect=AssertionError("Diagnostic must not remap selections"))
        manager.save_config = Mock(side_effect=AssertionError("Diagnostic must not save settings"))
        server = SimpleNamespace(profile_manager=manager, config=config, state=state,
                                 process_watcher=watcher, automation=automation, ls_settings=settings)
        yield DiagnosticRunner(server), automation, server


class GpuRouteDiagnosticTests(unittest.TestCase):
    def test_catalog_exposes_separate_test_and_runs_it_before_live_mutating_tests(self):
        with diagnostic() as (runner, _, _):
            catalog = {item["id"]: item for item in runner.catalog()}
            self.assertEqual(catalog["gpu_route_validation"]["dependencies"], ["configuration"])
            ids = [row[0] for row in runner._execution_plan(None)]
            self.assertLess(ids.index("gpu_route_validation"), ids.index("autoscale_behavior"))

    def test_correct_gpu_passes_and_raw_duplicate_entries_remain_intact(self):
        with diagnostic() as (runner, automation, server):
            before = server.config.model_dump()
            result = runner._gpu_route_validation()
            self.assertEqual(result["status"], "pass")
            self.assertEqual(result["details"]["requestedGpuDeviceId"], RTX_5050)
            self.assertEqual(result["details"]["actualGpuDeviceId"], RTX_5050)
            self.assertEqual(len(result["details"]["adapterReport"]["enumAdapters"]), 6)
            self.assertEqual(result["details"]["plannedRoute"]["lsGpuId"], 6)
            self.assertEqual(server.config.model_dump(), before)
            automation.refresh_gpu_selections.assert_not_called()
            automation.set_scaling.assert_not_called()
            server.profile_manager.save_config.assert_not_called()

    def test_healthy_frames_on_3090_fail_when_5050_was_requested(self):
        with diagnostic() as (runner, automation, _):
            report = automation._current_adapter_report.return_value
            report["device"] = report["enumAdapters"][0]
            result = runner._gpu_route_validation()
            self.assertEqual(result["status"], "fail")
            self.assertFalse(result["details"]["checks"]["renderingGpuMatchesRequestedGpu"])
            self.assertTrue(result["details"]["checks"]["framesFlowing"])
            self.assertEqual(result["details"]["actualGpuDeviceId"], RTX_3090)

    def test_missing_or_foreign_report_warns_instead_of_accepting_frame_output(self):
        for report in (None, {"pid": 999, "device": {"vendorId": 0x10DE, "deviceId": 0x2D83}}):
            with self.subTest(report=report), diagnostic() as (runner, automation, _):
                automation._current_adapter_report.return_value = report
                result = runner._gpu_route_validation()
                self.assertEqual(result["status"], "warn")
                self.assertIsNone(result["details"]["actualGpuDeviceId"])
                self.assertFalse(result["details"]["checks"]["reportMatchesConfiguredProcess"])

    def test_restart_during_capture_is_inconclusive(self):
        with diagnostic() as (runner, automation, _):
            automation._adapter_report_process.side_effect = [(123, 100), (123, 200)]
            result = runner._gpu_route_validation()
            self.assertEqual(result["status"], "warn")
            self.assertFalse(result["details"]["checks"]["sameProcessThroughoutCapture"])

    def test_no_active_session_warns_and_does_not_capture_frames(self):
        with diagnostic() as (runner, automation, server):
            server.state.is_scaling_active = False
            result = runner._gpu_route_validation()
            self.assertEqual(result["status"], "warn")
            automation._verify_active_route.assert_not_called()
            self.assertIn("Start scaling", result["summary"])

    def test_native_index_mismatch_is_visible_even_on_correct_gpu(self):
        with diagnostic() as (runner, automation, _):
            automation._settings_route.return_value = (2, 2)
            result = runner._gpu_route_validation()
            self.assertEqual(result["status"], "fail")
            self.assertFalse(result["details"]["checks"]["nativeIndexesMatchPlannedRoute"])

    def test_native_auto_is_reported_as_a_mismatch_with_an_explicit_plan(self):
        with diagnostic() as (runner, automation, server):
            # Use the real XML reader path so zero is preserved as evidence.
            del automation._settings_route
            title = server.state.current_active_profile.name
            server.state.current_active_profile.lossless_profile_title = title
            server.ls_settings.read.return_value = {"profiles": [{
                "Title": title, "PreferredGpuId": "0", "OutputDisplayId": "0"}]}
            result = runner._gpu_route_validation()
            self.assertEqual(result["status"], "fail")
            self.assertEqual(result["details"]["nativeProfileRoute"], (0, 0))
            self.assertFalse(result["details"]["checks"]["nativeIndexesMatchPlannedRoute"])

    def test_collection_error_preserves_other_evidence(self):
        with diagnostic() as (runner, automation, _):
            automation._settings_route.side_effect = OSError("Settings.xml locked")
            result = runner._gpu_route_validation()
            self.assertEqual(result["status"], "fail")
            self.assertIn("Settings.xml locked", result["details"]["errors"][0]["error"])
            self.assertEqual(result["details"]["adapterReport"]["pid"], 123)

    def test_selected_run_exports_evidence_without_resetting_or_stopping_ls(self):
        with diagnostic() as (runner, automation, server):
            server.config.ls_gpu_index_observations = {
                "2": {"gpuDeviceId": RTX_3090, "source": "initial_mismatch"},
                "6": {"gpuDeviceId": RTX_5050, "source": "correct"},
            }
            runner._reset_lossless_folder = Mock()
            result = runner.run("gpu_route_validation")
            runner._reset_lossless_folder.assert_not_called()
            automation.set_scaling.assert_not_called()
            server.process_watcher.stop_lossless_scaling.assert_not_called()
            self.assertTrue(server.state.is_scaling_active)
            with zipfile.ZipFile(result["archivePath"]) as archive:
                evidence = json.loads(archive.read("ls-companion-diagnostics/gpu_route_validation.log"))
                self.assertEqual(evidence["status"], "pass")
                self.assertEqual(evidence["details"]["targetPid"], 42)
                self.assertEqual(evidence["details"]["processBefore"], [123, 100])
                self.assertEqual(evidence["details"]["gpuIndexObservations"], server.config.ls_gpu_index_observations)

    def test_configuration_failure_does_not_hide_gpu_evidence(self):
        with diagnostic() as (runner, _, _):
            runner._configuration = Mock(return_value=runner._result("fail", "Inspect configuration"))
            runner._reset_lossless_folder = Mock()
            result = runner.run("gpu_route_validation")
            evidence = next(item for item in result["results"] if item["id"] == "gpu_route_validation")
            self.assertEqual(evidence["details"]["actualGpuDeviceId"], RTX_5050)
            runner._reset_lossless_folder.assert_not_called()


if __name__ == "__main__":
    unittest.main()
