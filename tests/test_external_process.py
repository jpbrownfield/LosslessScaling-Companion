import ctypes
from ctypes import wintypes
import os
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from companion.services import external_process


@unittest.skipUnless(os.name == "nt", "Windows DLL-directory isolation")
class ExternalProcessTests(unittest.TestCase):
    def fake_kernel(self):
        def get_directory(_length, buffer):
            buffer.value = "C:\\Companion\\_internal"
            return len(buffer.value)
        return SimpleNamespace(GetDllDirectoryW=Mock(side_effect=get_directory), SetDllDirectoryW=Mock(return_value=True))

    def test_frozen_launch_clears_then_restores_private_dll_directory(self):
        kernel = self.fake_kernel()
        with patch.object(sys, "frozen", True, create=True), patch.object(external_process.ctypes, "WinDLL", return_value=kernel):
            with external_process.external_dll_search():
                kernel.SetDllDirectoryW.assert_called_once_with(None)
        self.assertEqual([call.args[0] for call in kernel.SetDllDirectoryW.call_args_list], [None, "C:\\Companion\\_internal"])

    def test_launch_failure_also_restores_the_companion_directory(self):
        kernel = self.fake_kernel()
        with patch.object(sys, "frozen", True, create=True), patch.object(external_process.ctypes, "WinDLL", return_value=kernel):
            with self.assertRaisesRegex(RuntimeError, "launch failed"):
                with external_process.external_dll_search():
                    raise RuntimeError("launch failed")
        self.assertEqual(kernel.SetDllDirectoryW.call_args.args, ("C:\\Companion\\_internal",))

    def test_restoration_failure_does_not_request_a_duplicate_launch(self):
        kernel = self.fake_kernel()
        kernel.SetDllDirectoryW.side_effect = [True, False]
        with patch.object(sys, "frozen", True, create=True), patch.object(external_process.ctypes, "WinDLL", return_value=kernel):
            with self.assertLogs(external_process.__name__, level="ERROR"):
                with external_process.external_dll_search():
                    pass  # Child already launched successfully.

    def test_source_run_does_not_change_dll_search(self):
        with patch.object(sys, "frozen", False, create=True), patch.object(external_process.ctypes, "WinDLL") as load:
            with external_process.external_dll_search():
                pass
        load.assert_not_called()

    def test_real_child_does_not_inherit_private_directory_and_parent_is_restored(self):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetDllDirectoryW.argtypes = [wintypes.DWORD, wintypes.LPWSTR]
        kernel.GetDllDirectoryW.restype = wintypes.DWORD
        kernel.SetDllDirectoryW.argtypes = [wintypes.LPCWSTR]
        kernel.SetDllDirectoryW.restype = wintypes.BOOL
        buffer = ctypes.create_unicode_buffer(32768)
        kernel.GetDllDirectoryW(len(buffer), buffer)
        original = buffer.value or None
        probe = "import ctypes; b=ctypes.create_unicode_buffer(32768); ctypes.windll.kernel32.GetDllDirectoryW(len(b),b); print(b.value)"
        with tempfile.TemporaryDirectory() as private:
            self.assertTrue(kernel.SetDllDirectoryW(private))
            try:
                inherited = subprocess.check_output([sys.executable, "-c", probe], text=True).strip()
                self.assertEqual(inherited, private)
                with patch.object(sys, "frozen", True, create=True):
                    with external_process.external_dll_search():
                        child = subprocess.check_output([sys.executable, "-c", probe], text=True).strip()
                self.assertEqual(child, "")
                kernel.GetDllDirectoryW(len(buffer), buffer)
                self.assertEqual(buffer.value, private)
            finally:
                kernel.SetDllDirectoryW(original)
