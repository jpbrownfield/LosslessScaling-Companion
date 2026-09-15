"""Single-instance guard for LS Companion.

Uses a named Windows mutex (Global namespace so elevated and non-elevated
copies see each other) plus a stale-PID lock file fallback for other
platforms and for diagnosing who holds the instance.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("LSCompanion.SingleInstance")

MUTEX_NAME = "Global\\LosslessScalingCompanionSingleInstance"


def _lock_file_path(instance_name: str = "companion") -> Path:
    local_root = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(local_root) / "LosslessScalingHelper" / f"{instance_name}.lock"


class SingleInstanceGuard:
    """Hold a named mutex for the process lifetime; release on close."""

    def __init__(self, instance_name: str = "companion") -> None:
        if not instance_name or not instance_name.replace("-", "").isalnum():
            raise ValueError("instance name may contain only letters, numbers, and hyphens")
        self.instance_name = instance_name
        self.mutex_name = (
            MUTEX_NAME
            if instance_name == "companion"
            else f"{MUTEX_NAME}-{instance_name}"
        )
        self._mutex_handle = None
        self._lock_file: Optional[Path] = None
        self.holder_pid: Optional[int] = None

    @property
    def acquired(self) -> bool:
        return self._mutex_handle is not None or self._lock_file is not None

    def acquire(self) -> bool:
        """Return True if this is the only live instance, else False."""
        if os.name == "nt":
            if self._acquire_mutex():
                self._write_lock_file()
                return True
            self.holder_pid = self._read_lock_file_pid()
            return False
        return self._acquire_lock_file()

    def _acquire_mutex(self) -> bool:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        # Declare the HANDLE return explicitly: the ctypes default (c_long)
        # truncates 64-bit handles, making valid handles look NULL.
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_bool,
            ctypes.c_wchar_p,
        ]
        # ERROR_ALREADY_EXISTS == 183
        handle = kernel32.CreateMutexW(None, False, self.mutex_name)
        if not handle:
            error = kernel32.GetLastError()
            logger.error("Could not create single-instance mutex (WinError %s)", error)
            return False
        if kernel32.GetLastError() == 183:
            kernel32.CloseHandle(handle)
            return False
        self._mutex_handle = handle
        return True

    def _write_lock_file(self) -> None:
        try:
            path = _lock_file_path(self.instance_name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"pid": os.getpid(), "started": time.time()}),
                encoding="utf-8",
            )
        except OSError as error:
            logger.debug("Could not write companion lock file: %s", error)

    def _read_lock_file_pid(self) -> Optional[int]:
        try:
            data = json.loads(_lock_file_path(self.instance_name).read_text(encoding="utf-8"))
            pid = int(data.get("pid") or 0)
            return pid or None
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    def _acquire_lock_file(self) -> bool:
        import psutil

        path = _lock_file_path(self.instance_name)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            pid = int(data.get("pid") or 0)
            if pid and pid != os.getpid() and psutil.pid_exists(pid):
                self.holder_pid = pid
                return False
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"pid": os.getpid(), "started": time.time()}),
                encoding="utf-8",
            )
            self._lock_file = path
            return True
        except OSError as error:
            logger.error("Could not write companion lock file: %s", error)
            return False

    def release(self) -> None:
        if self._mutex_handle is not None and os.name == "nt":
            import ctypes

            try:
                ctypes.windll.kernel32.CloseHandle(self._mutex_handle)
            except Exception:
                pass
            self._mutex_handle = None
        if self._lock_file is not None:
            try:
                if self._lock_file.is_file():
                    self._lock_file.unlink()
            except OSError:
                pass
            self._lock_file = None
        elif os.name == "nt":
            # Best effort: only remove our own stale marker.
            try:
                path = _lock_file_path(self.instance_name)
                if path.is_file() and self._read_lock_file_pid() == os.getpid():
                    path.unlink()
            except OSError:
                pass

    def __enter__(self) -> "SingleInstanceGuard":
        if not self.acquire():
            raise RuntimeError("another instance")
        return self

    def __exit__(self, *args: object) -> None:
        self.release()
