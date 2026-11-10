import ctypes
import json
from ctypes import wintypes
import os
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from companion.services import startup_guard


class StartupGuardTests(unittest.TestCase):
    def setUp(self):
        environment = patch.object(startup_guard, "launch_environment", return_value=(os.environ.copy(), None, None))
        environment.start()
        self.addCleanup(environment.stop)

    def tearDown(self):
        startup_guard._libraries.clear()

    def test_missing_dll_uses_fallback(self):
        with patch.object(startup_guard, "guard_path", return_value=Path("missing-guard.dll")):
            self.assertIsNone(startup_guard.launch_hidden(Path("LosslessScaling.exe")))

    def test_native_failure_uses_fallback_and_does_not_schedule_cleanup(self):
        library = SimpleNamespace(LSCLaunchHidden=Mock(return_value=False), LSCReleaseStartupGuard=Mock())
        with (
            patch.object(Path, "is_file", return_value=True),
            patch.object(startup_guard.ctypes, "WinDLL", return_value=library),
            patch.object(startup_guard.threading, "Timer") as timer,
        ):
            self.assertIsNone(startup_guard.launch_hidden(Path("LosslessScaling.exe")))
        timer.assert_not_called()
        library.LSCReleaseStartupGuard.assert_not_called()

    def test_success_returns_pid_and_releases_only_hook_not_process(self):
        def launch(_path, _duration, pid, session):
            pid._obj.value = 42
            session._obj.value = 123
            return True

        library = SimpleNamespace(LSCLaunchHidden=Mock(side_effect=launch), LSCReleaseStartupGuard=Mock())
        with (
            patch.object(Path, "is_file", return_value=True),
            patch.object(startup_guard.ctypes, "WinDLL", return_value=library),
            patch.object(startup_guard.threading, "Timer") as timer,
        ):
            self.assertEqual(startup_guard.launch_hidden(Path("LosslessScaling.exe")), 42)
            self.assertEqual(timer.call_args.args[0], 7.0)
            timer.call_args.args[1]()
        self.assertEqual(library.LSCReleaseStartupGuard.call_args.args[0].value, 123)

    def test_timer_failure_does_not_launch_second_instance(self):
        def launch(_path, _duration, pid, session):
            pid._obj.value = 42
            session._obj.value = 123
            return True

        library = SimpleNamespace(LSCLaunchHidden=Mock(side_effect=launch), LSCReleaseStartupGuard=Mock())
        with (
            patch.object(Path, "is_file", return_value=True),
            patch.object(startup_guard.ctypes, "WinDLL", return_value=library),
            patch.object(startup_guard.threading, "Timer") as timer,
        ):
            timer.return_value.start.side_effect = RuntimeError("shutdown")
            self.assertEqual(startup_guard.launch_hidden(Path("LosslessScaling.exe")), 42)
        library.LSCReleaseStartupGuard.assert_called_once()


@unittest.skipUnless(os.environ.get("LSC_TEST_NATIVE_STARTUP_GUARD") == "1", "Opt-in native GUI integration test")
class NativeStartupGuardTests(unittest.TestCase):
    def test_standalone_dxgi_probe_has_no_rendering_device_or_ls_names(self):
        from companion.services.gpu_index_refresh import fast_snapshot
        report = fast_snapshot()
        self.assertFalse(report["hasDevice"])
        self.assertEqual(report["lsAdapterNames"], [])
        self.assertTrue(report["enumAdapters"])
        self.assertTrue(report["enumAdapters1"])

    def test_idle_adapter_report_uses_child_pid_nonce_and_real_bstr_abi(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            token = "a1" * 32
            with patch.dict(os.environ, {"LSC_ADAPTER_REPORT_TOKEN": "inherited-untrusted-token"}):
                pid = self.run_host(root / "build/native/startup-guard-wpf-test/LosslessScaling.exe",
                                    4000, Path(directory), token)
                self.assertEqual(os.environ["LSC_ADAPTER_REPORT_TOKEN"], "inherited-untrusted-token")
            report = json.loads((Path(directory) / f"ls-adapter-report-{pid}.json").read_text(encoding="utf-8"))
            self.assertEqual(report["pid"], pid)
            self.assertEqual(report["requestId"], token)
            idle = json.loads((Path(directory) / f"ls-idle-adapter-report-{pid}.json").read_text(encoding="utf-8"))
            self.assertEqual(idle["requestId"], token)
            self.assertEqual(idle["lsAdapterStatus"], "ok")
            self.assertEqual(idle["lsAdapterError"], 0)
            self.assertEqual(report["lsAdapterNames"], ["NVIDIA GeForce RTX 3090"] * 2 + ["NVIDIA GeForce RTX 5050", "Test GPU \u03a9"])
            self.assertFalse(report["hasDevice"])
            self.assertTrue(report["enumAdapters"])
            self.assertTrue(report["enumAdapters1"])

    def test_private_special_k_environment_is_only_applied_to_the_child(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            host = Path(directory) / "LosslessScaling.exe"
            shutil.copy2(root / "build/native/startup-guard-test/LosslessScaling.exe", host)
            config = Path(directory) / ".companion-special-k/Global/osd.ini"
            config.parent.mkdir(parents=True)
            config.write_text("[SpecialK.OSD]\nShow=false", encoding="utf-8")
            with patch.dict(os.environ, {"SPECIALK_PATH": "external-special-k-settings"}):
                self.run_host(host, 1200)
                self.assertEqual(os.environ["SPECIALK_PATH"], "external-special-k-settings")

    def test_real_cross_process_hook_prevents_show_and_expires(self):
        # This is our compiled fixture, never the installed Lossless Scaling.
        root = Path(__file__).resolve().parents[1]
        host = root / "build/native/startup-guard-test/LosslessScaling.exe"
        self.run_host(host, 1200)

    def test_real_wpf_window_stays_hidden_then_opens_normally(self):
        root = Path(__file__).resolve().parents[1]
        self.run_host(root / "build/native/startup-guard-wpf-test/LosslessScaling.exe", 4000)

    def run_host(self, host, duration, directory=None, token=None):
        self.assertTrue(host.is_file())
        library = ctypes.WinDLL(str(startup_guard.guard_path()), use_last_error=True)
        library.LSCLaunchHidden.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                           ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(ctypes.c_void_p)]
        library.LSCLaunchHidden.restype = wintypes.BOOL
        library.LSCReleaseStartupGuard.argtypes = [ctypes.c_void_p]
        library.LSCReleaseStartupGuard.restype = None
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        pid = wintypes.DWORD()
        session = ctypes.c_void_p()
        if directory is not None:
            library.LSCLaunchHiddenWithReport.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPCWSTR,
                                                        wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD),
                                                        ctypes.POINTER(ctypes.c_void_p)]
            library.LSCLaunchHiddenWithReport.restype = wintypes.BOOL
            launched = library.LSCLaunchHiddenWithReport(str(host), duration, str(directory), token,
                                                        ctypes.byref(pid), ctypes.byref(session))
        else:
            launched = library.LSCLaunchHidden(str(host), duration, ctypes.byref(pid), ctypes.byref(session))
        error = ctypes.get_last_error()
        library.LSCStartupGuardLastStage.restype = wintypes.LPCWSTR
        self.assertTrue(launched, f"{library.LSCStartupGuardLastStage()}: {ctypes.WinError(error)}")
        process = kernel.OpenProcess(0x100000 | 0x1000 | 0x1, False, pid.value)
        try:
            self.assertTrue(process)
            self.assertEqual(kernel.WaitForSingleObject(process, 10000), 0)
            code = wintypes.DWORD()
            self.assertTrue(kernel.GetExitCodeProcess(process, ctypes.byref(code)))
            self.assertEqual(code.value, 0, f"Native fixture failed at checkpoint {code.value}")
        finally:
            library.LSCReleaseStartupGuard(session)
            if process:
                # Only our compiled fixture; do not leave it behind if an
                # assertion or a stalled WPF dispatcher prevented normal exit.
                if kernel.WaitForSingleObject(process, 0) == 258:
                    kernel.TerminateProcess(process, 99)
                    kernel.WaitForSingleObject(process, 2000)
                kernel.CloseHandle(process)
        return pid.value


if __name__ == "__main__":
    unittest.main()
