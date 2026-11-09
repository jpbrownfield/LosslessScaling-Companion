"""Post-scale verification uses fresh DLL identity, without rendering probes."""
import os
import subprocess
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

from companion.services import adapter_report
from tests.test_gpu_route_diagnostic import diagnostic, RTX_3090, RTX_5050


TOKEN = "a" * 64


@contextmanager
def available(*_args):
    yield True


class RuntimeRequestTests(unittest.TestCase):
    def request(self, report, instance_check=None):
        with patch.object(adapter_report, "_runtime_request", available), \
                patch.object(adapter_report.secrets, "token_hex", return_value=TOKEN), \
                patch.object(adapter_report, "load_latest", return_value=report):
            return adapter_report.request_runtime_report(adapter_report.report_directory(), (123, 100),
                process_check=instance_check or (lambda: (123, 100)), timeout=0)

    def test_unique_request_token_and_pid_are_both_required(self):
        for pid, token in ((123, TOKEN), (999, TOKEN), (123, "b" * 64)):
            with self.subTest(pid=pid, token=token):
                report = {"pid": pid, "requestId": token}
                self.assertEqual(self.request(report), report if pid == 123 and token == TOKEN else None)

    def test_reused_pid_discards_response(self):
        self.assertIsNone(self.request({"pid": 123, "requestId": TOKEN}, lambda: (123, 200)))

    def test_old_or_missing_dll_does_not_read_cached_report(self):
        @contextmanager
        def missing(*_args):
            yield False
        with patch.object(adapter_report, "_runtime_request", missing), \
                patch.object(adapter_report, "load_latest") as load:
            self.assertIsNone(adapter_report.request_runtime_report(Path("reports"), (123, 100),
                process_check=lambda: (123, 100)))
        load.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows shared-memory request protocol")
    def test_real_dll_answers_two_python_requests_without_a_rendering_device(self):
        host = Path(__file__).resolve().parents[1] / "build/native/LSC-AdapterReport/LSC_AdapterReportLifetimeHost.exe"
        if not host.is_file():
            self.skipTest("Build native adapter reporter and lifetime fixture first")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "LosslessScalingHelper").mkdir()
            output = root / "host.log"
            with output.open("w", encoding="utf-8") as stream:
                process = subprocess.Popen([str(host), "listen"], stdin=subprocess.PIPE, stdout=stream,
                    stderr=stream, env={**os.environ, "LOCALAPPDATA": str(root)},
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                try:
                    deadline = time.monotonic() + 10
                    while "READY" not in output.read_text(encoding="utf-8", errors="replace"):
                        self.assertIsNone(process.poll(), output.read_text(encoding="utf-8", errors="replace"))
                        self.assertLess(time.monotonic(), deadline, "Reporter fixture never became ready")
                        time.sleep(0.01)
                    instance = (process.pid, 0)
                    check = lambda: instance if process.poll() is None else None
                    reports = [adapter_report.request_runtime_report(root / "LosslessScalingHelper/diagnostics",
                               instance, process_check=check, timeout=2) for _ in range(2)]
                    for report in reports:
                        self.assertIsNotNone(report)
                        self.assertEqual(report["pid"], process.pid)
                        self.assertFalse(report["hasDevice"])
                    self.assertNotEqual(reports[0]["requestId"], reports[1]["requestId"])
                finally:
                    try:
                        process.communicate(b"\n", timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate()
                self.assertEqual(process.returncode, 0, output.read_text(encoding="utf-8", errors="replace"))


class PostScaleVerificationTests(unittest.TestCase):
    def test_normal_activation_uses_dll_check_and_never_enters_presentmon_repair(self):
        from tests.test_scale_controls import controller
        automation, state, _ = controller()
        automation._verify_gpu_after_scaling = Mock(return_value="verified")
        automation._route_verification_due = Mock(return_value=True)
        automation._verify_and_repair_route = Mock()
        self.assertTrue(automation.set_scaling(True, reason="manual", force=True))
        self.assertTrue(state.is_scaling_active)
        automation._verify_gpu_after_scaling.assert_called_once_with(None, None)
        automation._route_verification_due.assert_not_called()
        automation._verify_and_repair_route.assert_not_called()

    def check(self, runner, automation, server, report):
        automation._adapter_report_process.return_value = (123, 100)
        automation.gpu_index_refresh_requested = Mock()
        automation._verify_and_repair_route = Mock()
        automation.calibrate_gpu_map = Mock()
        automation._repair_route = Mock()
        automation._correct_current_gpu_index = Mock(return_value="corrected")
        with patch.object(adapter_report, "request_runtime_report", return_value=report):
            outcome = automation._verify_gpu_after_scaling(server.state.current_active_profile, 82)
        automation._verify_active_route.assert_not_called()
        automation._verify_and_repair_route.assert_not_called()
        automation.calibrate_gpu_map.assert_not_called()
        automation._repair_route.assert_not_called()
        automation.set_scaling.assert_not_called()
        return outcome

    def test_matching_gpu_needs_no_calibration_even_if_map_was_partial(self):
        with diagnostic() as (runner, automation, server):
            report = automation._current_adapter_report.return_value
            server.config.ls_gpu_id_map.pop(RTX_3090)
            outcome = self.check(runner, automation, server, report)
            self.assertEqual(outcome, "verified")
            self.assertEqual(server.state.gpu_mapping_status["state"], "runtime_verified")
            automation.gpu_index_refresh_requested.assert_not_called()
            automation._current_adapter_report.assert_not_called()

    def test_mismatch_starts_only_current_target_correction(self):
        with diagnostic() as (runner, automation, server):
            report = automation._current_adapter_report.return_value
            report["device"] = report["enumAdapters"][0]
            before = server.config.model_dump()
            self.assertEqual(self.check(runner, automation, server, report), "corrected")
            automation.gpu_index_refresh_requested.assert_not_called()
            automation._correct_current_gpu_index.assert_called_once_with(server.state.current_active_profile, 82, RTX_5050, report)
            self.assertEqual(server.config.model_dump(), before)

    def test_missing_report_or_foreign_pid_stays_unverified_without_calibration(self):
        for report in (None, {"pid": 999, "device": {"vendorId": 0x10DE, "deviceId": 0x2D83}}):
            with self.subTest(report=report), diagnostic() as (runner, automation, server):
                self.assertEqual(self.check(runner, automation, server, report), "unverified")
                automation.gpu_index_refresh_requested.assert_not_called()
                self.assertEqual(server.state.gpu_mapping_status["state"], "runtime_unverified")


if __name__ == "__main__":
    unittest.main()
