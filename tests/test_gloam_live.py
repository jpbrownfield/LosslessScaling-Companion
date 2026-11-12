"""Explicit native fixtures; no Gloam settings or desktop color policy are changed."""
import os
from pathlib import Path
import subprocess
import time
import queue
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1] / "build/native"


@unittest.skipUnless(os.name == "nt" and (ROOT / "LSHdrSession.exe").is_file(), "Build the native HDR session helper first")
class GloamNativeTests(unittest.TestCase):
    def marker(self, parent_pid=None):
        child = subprocess.Popen([str(ROOT / "LSHdrSession.exe"), str(parent_pid or os.getpid())], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        def cleanup():
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)
            child.stdout.close()
        self.addCleanup(cleanup)
        ready = queue.Queue()
        threading.Thread(target=lambda: ready.put(child.stdout.read(1)), daemon=True).start()
        self.assertEqual(ready.get(timeout=30), b"1")
        return child

    def test_marker_lives_for_heartbeats_and_exits_on_eof(self):
        child = self.marker()
        child.stdin.write(b"1")
        child.stdin.flush()
        time.sleep(.25)
        self.assertIsNone(child.poll())
        child.stdin.close()
        self.assertEqual(child.wait(timeout=2), 0)

    def test_marker_expires_without_heartbeats(self):
        child = self.marker()
        try:
            self.assertEqual(child.wait(timeout=10), 0)
        finally:
            child.stdin.close()

    def test_marker_exits_when_its_parent_exits(self):
        import sys
        parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                  creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            child = self.marker(parent.pid)
            parent.terminate()
            parent.wait(timeout=5)
            self.assertEqual(child.wait(timeout=2), 0)
            child.stdin.close()
        finally:
            if parent.poll() is None:
                parent.terminate()
                parent.wait(timeout=5)

    @unittest.skipUnless((ROOT / "LSC_HDRProbeTest.exe").is_file(), "Build the HDR GPU fixture first")
    def test_real_gpu_sdr_white_hdr_highlights_disabled_gate_and_expired_lease(self):
        result = subprocess.run([str(ROOT / "LSC_HDRProbeTest.exe")], capture_output=True, text=True,
                                creationflags=subprocess.CREATE_NO_WINDOW, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("HDR GPU fixture passed", result.stdout)

    @unittest.skipUnless((ROOT / "LSC_HDRProbeTest.exe").is_file(), "Build the HDR GPU fixture first")
    def test_live_module_executes_native_checks_without_applying_a_gloam_profile(self):
        from companion.services.diagnostic_runner import DiagnosticRunner
        integration = Mock()
        integration.snapshot.return_value = {"state": "missing_gloam", "message": "Gloam is not installed."}
        runner = DiagnosticRunner.__new__(DiagnosticRunner)
        runner.server = SimpleNamespace(config=SimpleNamespace(gloam_hdr_enabled=True),
                                        automation=SimpleNamespace(gloam_integration=integration))
        result = runner._gloam_hdr()
        self.assertEqual(result["status"], "warn", result)
        self.assertTrue(result["details"]["gpuFixturePassed"])
        self.assertTrue(result["details"]["helperLifetimePassed"])
