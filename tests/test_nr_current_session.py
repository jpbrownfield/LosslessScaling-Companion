import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from companion.core.models import AppConfig, Profile
from companion.services.diagnostic_runner import DiagnosticRunner


class CurrentNrSessionTests(unittest.TestCase):
    def runner(self):
        runner = DiagnosticRunner.__new__(DiagnosticRunner)
        profile = Profile(name="Failing game", native_scaling_settings={"CaptureApi": "WGC", "FrameGeneration": "LSFG3"},
                          preferred_gpu_name="NVIDIA GeForce RTX 5050")
        runner.server = SimpleNamespace(state=SimpleNamespace(current_active_profile=profile), config=AppConfig())
        first = {"available": True, "capture": {"adapter": "NVIDIA GeForce RTX 5050", "support": "Ready",
                 "configured": True, "captureApi": 1, "frameGeneration": 1, "scaler": 0, "hdr": True,
                 "captures": 143, "dispatches": 13379, "progress": {"captureShaderMatches": 143}},
                 "addons": [{"id": "DLSSNR02", "enabled": True, "loaded": True, "status": "Running"}],
                 "metrics": {"DLSSNR02": {"accepted": 138, "model_ms": 2.24}}}
        last = copy.deepcopy(first)
        last["capture"].update(captures=502, dispatches=47125)
        last["metrics"]["DLSSNR02"]["accepted"] = 497
        result = runner._result("pass", "collected", trace={"traceSupported": True, "samples": [first, last]})
        runner._collect_nr_capture_trace = Mock(return_value=result)
        return runner, first, last

    def test_current_processing_on_5050_is_verified_without_replacing_the_profile(self):
        runner, _, _ = self.runner()
        result = runner._nr_capture_current()
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["details"]["captureAdapter"], "NVIDIA GeForce RTX 5050")
        self.assertEqual(result["details"]["activeProfile"]["name"], "Failing game")
        self.assertEqual(result["details"]["observedSettings"]["frameGeneration"], 1)

    def test_loaded_addon_without_a_capture_is_not_a_processing_pass(self):
        for stage, expected in (("no_dispatch", "No LS compute dispatches"),
                                ("no_callback", "no real-frame callback"),
                                ("unmatched", "none matched a supported capture")):
            with self.subTest(stage=stage):
                runner, first, last = self.runner()
                for sample in (first, last):
                    sample["capture"].update(captures=0, dispatches=0 if stage == "no_dispatch" else 100)
                    sample["capture"]["progress"] = ({"dispatchesWithoutCallback": 100} if stage == "no_callback"
                        else {"dispatchesWithCallback": 100} if stage == "unmatched" else {})
                    sample["metrics"]["DLSSNR02"] = {}
                result = runner._nr_capture_current()
                self.assertEqual(result["status"], "warn")
                self.assertIn(expected, result["summary"])

    def test_processing_on_3090_does_not_validate_a_requested_5050(self):
        runner, _, last = self.runner()
        last["capture"]["adapter"] = "NVIDIA GeForce RTX 3090 Ti"
        result = runner._nr_capture_current()
        self.assertEqual(result["status"], "warn")
        self.assertIn("3090 Ti", result["summary"])
        self.assertIn("5050 was requested", result["summary"])

    def test_old_successful_metrics_do_not_prove_new_frames_are_processing(self):
        runner, first, last = self.runner()
        last["metrics"] = copy.deepcopy(first["metrics"])
        result = runner._nr_capture_current()
        self.assertEqual(result["status"], "warn")
        self.assertIn("No new NR frames", result["summary"])

    def test_profile_support_error_explains_a_waiting_pipeline(self):
        runner, first, last = self.runner()
        for sample in (first, last):
            sample["metrics"] = {}
            sample["capture"]["support"] = "Capture paused: DXGI/other capture is unsupported"
        result = runner._nr_capture_current()
        self.assertEqual(result["status"], "warn")
        self.assertIn("DXGI/other capture is unsupported", result["summary"])

    def test_selected_current_session_diagnostic_does_not_reset_or_restart_ls(self):
        with tempfile.TemporaryDirectory() as directory:
            server = SimpleNamespace(profile_manager=SimpleNamespace(config_dir=Path(directory)),
                state=SimpleNamespace(simulation_mode=False), process_watcher=Mock())
            runner = DiagnosticRunner(server)
            runner._tests = lambda: (("nr_capture_current", "Current NR session", lambda: runner._result("warn", "inspect")),)
            runner._reset_lossless_folder = Mock()
            result = runner.run("nr_capture_current")
            runner._reset_lossless_folder.assert_not_called()
            server.process_watcher.stop_lossless_scaling.assert_not_called()
            server.process_watcher.launch_lossless_scaling.assert_not_called()
            self.assertIn("not reset", result["results"][-1]["summary"])
            self.assertIn("nr_capture_current", DiagnosticRunner.READ_ONLY_TESTS)
