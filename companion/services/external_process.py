"""Keep external programs from inheriting PyInstaller's private DLL directory."""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import os
import sys
import threading
import logging


_launch_lock = threading.RLock()


@contextmanager
def external_dll_search():
    # SetDllDirectory is process-global, not an environment variable. Serialize
    # our launch windows and restore it immediately, including on launch errors.
    if os.name != "nt" or not getattr(sys, "frozen", False):
        yield
        return
    with _launch_lock:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetDllDirectoryW.argtypes = [wintypes.DWORD, wintypes.LPWSTR]
        kernel.GetDllDirectoryW.restype = wintypes.DWORD
        kernel.SetDllDirectoryW.argtypes = [wintypes.LPCWSTR]
        kernel.SetDllDirectoryW.restype = wintypes.BOOL
        buffer = ctypes.create_unicode_buffer(32768)
        ctypes.set_last_error(0)
        size = kernel.GetDllDirectoryW(len(buffer), buffer)
        if not size and ctypes.get_last_error():
            raise ctypes.WinError(ctypes.get_last_error())
        if size >= len(buffer):
            raise OSError("The parent DLL search directory is too long to preserve")
        original = buffer.value if size else None
        if not kernel.SetDllDirectoryW(None):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            yield
        finally:
            if not kernel.SetDllDirectoryW(original):
                # The child may already be running. Never turn restoration
                # failure into a signal to launch a duplicate fallback child.
                logging.getLogger(__name__).error("Could not restore Companion's DLL search directory (WinError %s)", ctypes.get_last_error())
