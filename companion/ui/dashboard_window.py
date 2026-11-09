"""Own a WebView2 dashboard helper without borrowing a browser process."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
from typing import Optional

logger = logging.getLogger("LSCompanion.DashboardWindow")
_open_lock = threading.RLock()
_host = None


class DashboardHost:
    def __init__(self):
        self.hwnd = None
        self.error = None
        self.ready = threading.Event()
        self._write_lock = threading.Lock()
        command = [sys.executable]
        if not getattr(sys, "frozen", False):
            command.append(str(Path(__file__).resolve().parents[2] / "run_companion.py"))
        command.append("--dashboard-host")
        self.process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
            close_fds=True, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        threading.Thread(target=self._read_events, daemon=True, name="DashboardEvents").start()

    def _read_events(self):
        try:
            for line in self.process.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("event") == "ready":
                    self.hwnd = int(event["hwnd"])
                    self.ready.set()
                elif event.get("event") == "error":
                    self.error = str(event.get("message"))
                    logger.error("Dashboard could not start: %s", self.error)
                    self.ready.set()
                    if os.name == "nt":
                        ctypes.windll.user32.MessageBoxW(None, self.error, "LS Companion dashboard", 0x10)
        finally:
            self.ready.set()
            self.process.stdout.close()

    def send(self, command):
        with self._write_lock:
            if self.process.poll() is not None:
                return False
            try:
                self.process.stdin.write(json.dumps(command) + "\n")
                self.process.stdin.flush()
                return True
            except (OSError, ValueError):
                return False

    def close(self, *, wait=False):
        # EOF is the lifetime contract. Windows also closes our handles after
        # a fatal crash, so this works without running a cleanup handler.
        with self._write_lock:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        if wait:
            self._reap()
        else:
            threading.Thread(target=self._reap, daemon=True, name="DashboardClose").start()

    def _reap(self):
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()


def open_dashboard_window(url: str) -> bool:
    global _host
    with _open_lock:
        try:
            if _host is not None and _host.send({"command": "open", "url": url}):
                return True
            if _host is not None:
                _host.close()
            _host = DashboardHost()
            return _host.send({"command": "open", "url": url})
        except OSError:
            logger.exception("Could not launch the Python dashboard host")
            return False


def close_dashboard_window(*, wait: bool = False) -> bool:
    global _host
    with _open_lock:
        if _host is None:
            return False
        host, _host = _host, None
        if wait:
            host.close(wait=True)
        else:
            host.close()
        return True


def dashboard_window_handle() -> Optional[int]:
    with _open_lock:
        if _host is None or _host.process.poll() is not None:
            return None
        return _host.hwnd


def dashboard_css_rect_to_client_pixels(rect: dict) -> Optional[tuple[int, int, int, int, int]]:
    """Map the embedded viewport's CSS rectangle into native client pixels."""
    hwnd = dashboard_window_handle()
    if not hwnd or os.name != "nt":
        return None
    viewport_width = float(rect.get("viewportWidth") or 0)
    viewport_height = float(rect.get("viewportHeight") or 0)
    if viewport_width <= 0 or viewport_height <= 0:
        return None
    client = wintypes.RECT()
    if not ctypes.windll.user32.GetClientRect(hwnd, ctypes.byref(client)):
        return None
    scale_x = max(1, client.right - client.left) / viewport_width
    scale_y = max(1, client.bottom - client.top) / viewport_height
    return (hwnd, round(float(rect.get("left") or 0) * scale_x),
            round(float(rect.get("top") or 0) * scale_y),
            max(1, round(float(rect.get("width") or 0) * scale_x)),
            max(1, round(float(rect.get("height") or 0) * scale_y)))
