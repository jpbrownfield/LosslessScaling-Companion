"""Process, PresentMon, and GPU telemetry primitives for LS benchmarks."""

from __future__ import annotations

import ctypes
import os
import subprocess
import threading
import time
import uuid
from ctypes import wintypes
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

import psutil

from .gpu_telemetry import WindowsGpuTelemetry


CREATE_NO_WINDOW = 0x08000000
SW_RESTORE = 9
user32 = ctypes.windll.user32 if os.name == "nt" else None
gdi32 = ctypes.windll.gdi32 if os.name == "nt" else None
kernel32 = ctypes.windll.kernel32 if os.name == "nt" else None
if user32 is not None:
    user32.IsWindow.argtypes = (wintypes.HWND,)
    user32.IsWindow.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = (wintypes.HWND,)
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetDC.argtypes = (wintypes.HWND,)
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = (wintypes.HWND, wintypes.HDC)
    user32.ReleaseDC.restype = ctypes.c_int
    gdi32.GetPixel.argtypes = (wintypes.HDC, ctypes.c_int, ctypes.c_int)
    gdi32.GetPixel.restype = wintypes.DWORD


def find_largest_window(process_id: int, timeout: float = 15.0) -> int:
    """Wait for and return a process's largest visible top-level window."""
    if user32 is None:
        raise RuntimeError("Window discovery requires Windows")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        matches: List[tuple[int, int]] = []

        def callback(raw_hwnd: int, _extra: int) -> bool:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(raw_hwnd, ctypes.byref(pid))
            if pid.value != process_id or not user32.IsWindowVisible(raw_hwnd):
                return True
            rect = wintypes.RECT()
            user32.GetWindowRect(raw_hwnd, ctypes.byref(rect))
            area = max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
            if area:
                matches.append((area, int(raw_hwnd)))
            return True

        callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        callback_ref = callback_type(callback)
        user32.EnumWindows(callback_ref, 0)
        if matches:
            return max(matches)[1]
        time.sleep(0.1)
    raise RuntimeError("Benchmark application did not create a visible window")


def focus_window(hwnd: int, timeout: float = 2.0) -> bool:
    if user32 is None or not user32.IsWindow(hwnd):
        return False
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)

    def focused() -> bool:
        return int(user32.GetForegroundWindow() or 0) == int(hwnd)

    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    if not focused():
        # Windows restricts foreground activation. An elevated companion is
        # especially likely to be denied even when the request originated in
        # the dashboard. Temporarily join the relevant UI input queues and
        # retry without injecting a synthetic keystroke into the workload.
        current_thread = int(kernel32.GetCurrentThreadId())
        foreground = int(user32.GetForegroundWindow() or 0)
        foreground_thread = (
            int(user32.GetWindowThreadProcessId(foreground, None))
            if foreground else 0
        )
        target_thread = int(user32.GetWindowThreadProcessId(hwnd, None))
        attached = []
        try:
            for thread_id in (foreground_thread, target_thread):
                if (
                    thread_id
                    and thread_id != current_thread
                    and thread_id not in attached
                    and user32.AttachThreadInput(current_thread, thread_id, True)
                ):
                    attached.append(thread_id)
            user32.ShowWindow(hwnd, SW_RESTORE)
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            for thread_id in reversed(attached):
                user32.AttachThreadInput(current_thread, thread_id, False)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if focused():
            return True
        time.sleep(0.025)
    return False


def window_center(hwnd: int) -> tuple[int, int]:
    if user32 is None or not user32.IsWindow(hwnd):
        raise RuntimeError("Latency output window is unavailable")
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise ctypes.WinError()
    return ((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)


def measure_visible_marker(
    hwnd: int, trigger, *, timeout: float = 0.75
) -> Dict:
    """Time input injection to a cyan/magenta marker in composed screen output.

    GetPixel observes the Windows-composed desktop, not physical panel emission.
    The bundled workload deliberately fills its frame with a marker for 120 ms.
    """
    if user32 is None:
        raise RuntimeError("Visible-output probing requires Windows")
    x, y = window_center(hwnd)
    desktop_dc = user32.GetDC(0)
    if not desktop_dc:
        raise ctypes.WinError()
    started = time.perf_counter()
    try:
        sent = bool(trigger())
        if not sent:
            return {"sent": False, "latency_ms": None, "color": None}
        deadline = started + max(0.05, timeout)
        while time.perf_counter() < deadline:
            color_ref = int(gdi32.GetPixel(desktop_dc, x, y))
            if color_ref not in (-1, 0xFFFFFFFF):
                red = color_ref & 0xFF
                green = (color_ref >> 8) & 0xFF
                blue = (color_ref >> 16) & 0xFF
                is_magenta = red >= 200 and green <= 80 and blue >= 200
                is_cyan = red <= 80 and green >= 200 and blue >= 200
                if is_magenta or is_cyan:
                    return {
                        "sent": True,
                        "latency_ms": (time.perf_counter() - started) * 1000.0,
                        "color": [red, green, blue],
                    }
            time.sleep(0.001)
        return {"sent": True, "latency_ms": None, "color": None}
    finally:
        user32.ReleaseDC(0, desktop_dc)


class PresentMonCapture:
    def __init__(self, executable: Path, output_dir: Path):
        self.executable = executable.resolve(strict=True)
        self.output_dir = output_dir.resolve()
        self.process: Optional[subprocess.Popen] = None
        self.session_name = f"LSHelperBenchmark-{uuid.uuid4().hex}"
        self._stderr_stream = None

    def command(self, process_names: Sequence[str], seconds: float) -> List[str]:
        if not process_names:
            raise ValueError("PresentMon capture requires at least one process name")
        output = self.output_dir / "presentmon.csv"
        command = [
            str(self.executable),
            "--no_console_stats",
            "--multi_csv",
            "--output_file",
            str(output),
            "--session_name",
            self.session_name,
            "--timed",
            str(max(1.0, seconds)),
            "--terminate_after_timed",
        ]
        for name in dict.fromkeys(process_names):
            if not name or name != Path(name).name:
                raise ValueError(f"Unsafe PresentMon process name: {name!r}")
            command.extend(("--process_name", name))
        return command

    def start(self, process_names: Sequence[str], seconds: float) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._stderr_stream = (self.output_dir / "presentmon.stderr.log").open(
            "w", encoding="utf-8"
        )
        self.process = subprocess.Popen(
            self.command(process_names, seconds),
            cwd=self.output_dir,
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
            stdout=subprocess.DEVNULL,
            stderr=self._stderr_stream,
            text=True,
        )
        time.sleep(0.5)
        if self.process.poll() is not None:
            self._close_stderr()
            error = (self.output_dir / "presentmon.stderr.log").read_text(
                encoding="utf-8", errors="replace"
            ).strip()
            raise RuntimeError(f"PresentMon exited before capture began: {error}")

    def wait(self, timeout: float) -> None:
        if self.process is None:
            return
        try:
            code = self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("PresentMon capture did not finish on time") from error
        self._close_stderr()
        if code != 0:
            error = (self.output_dir / "presentmon.stderr.log").read_text(
                encoding="utf-8", errors="replace"
            ).strip()
            raise RuntimeError(f"PresentMon exited with code {code}: {error}")

    def stop(self) -> None:
        if self.process is None:
            self._close_stderr()
            return
        if self.process.poll() is not None:
            self._close_stderr()
            return
        subprocess.run(
            [
                str(self.executable),
                "--session_name",
                self.session_name,
                "--terminate_existing_session",
            ],
            cwd=self.output_dir,
            creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
        finally:
            self._close_stderr()

    def _close_stderr(self) -> None:
        if self._stderr_stream is not None:
            self._stderr_stream.close()
            self._stderr_stream = None

    def csv_files(self) -> List[Path]:
        return list(self.output_dir.glob("presentmon*.csv"))


class GpuSampleRecorder:
    """Record Task-Manager-style busiest-engine utilization once per second."""

    def __init__(self, process_ids: Iterable[int], interval: float = 1.0):
        self.process_ids = {int(pid) for pid in process_ids if int(pid) > 0}
        self.interval = max(0.1, interval)
        self.telemetry = WindowsGpuTelemetry()
        self.samples: List[Dict] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._started_at = 0.0

    def start(self) -> None:
        if not self.telemetry.available:
            return
        self._started_at = time.monotonic()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            values = self.telemetry.sample_by_pid()
            self.samples.append(
                {
                    "elapsed_seconds": time.monotonic() - self._started_at,
                    "utilization": {
                        str(pid): values.get(pid) for pid in sorted(self.process_ids)
                    },
                }
            )

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.interval + 1)
        self.telemetry.close()


def find_process_id(name: str) -> Optional[int]:
    matches = []
    for process in psutil.process_iter(["name", "create_time"]):
        try:
            if (process.info.get("name") or "").casefold() == name.casefold():
                matches.append((float(process.info.get("create_time") or 0), process.pid))
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    return max(matches)[1] if matches else None


__all__ = [
    "GpuSampleRecorder",
    "PresentMonCapture",
    "find_largest_window",
    "find_process_id",
    "focus_window",
    "measure_visible_marker",
    "window_center",
]
