"""Inspect top-level windows and safely send a hotkey to a selected window.

This is an exploratory helper for Test-LosslessScalingMultiInstance.ps1. It
never emits input until the requested HWND has been made and verified as the
foreground window.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import sys
import time
from ctypes import wintypes
from pathlib import Path
from typing import Iterable, Optional


if sys.platform != "win32":
    raise SystemExit("This probe requires Windows.")

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from companion.services.input_simulator import InputSimulator  # noqa: E402


SW_RESTORE = 9
user32 = ctypes.windll.user32
EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

user32.EnumWindows.argtypes = (EnumWindowsProc, wintypes.LPARAM)
user32.EnumWindows.restype = wintypes.BOOL
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.IsWindow.argtypes = (wintypes.HWND,)
user32.IsWindow.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = (wintypes.HWND,)
user32.IsWindowVisible.restype = wintypes.BOOL
user32.IsIconic.argtypes = (wintypes.HWND,)
user32.IsIconic.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
user32.ShowWindow.restype = wintypes.BOOL


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


def _window_text(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, len(buffer))
    return buffer.value


def _window_class(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buffer, len(buffer))
    return buffer.value


def enumerate_windows(process_ids: Optional[Iterable[int]] = None) -> list[dict]:
    pid_filter = set(process_ids or [])
    results: list[dict] = []

    def callback(raw_hwnd: int, _lparam: int) -> bool:
        hwnd = int(raw_hwnd)
        process_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        if pid_filter and process_id.value not in pid_filter:
            return True
        if not user32.IsWindowVisible(hwnd):
            return True
        rect = RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        width = max(0, rect.right - rect.left)
        height = max(0, rect.bottom - rect.top)
        results.append(
            {
                "hwnd": hwnd,
                "hwndHex": f"0x{hwnd:016X}",
                "processId": process_id.value,
                "title": _window_text(hwnd),
                "className": _window_class(hwnd),
                "isMinimized": bool(user32.IsIconic(hwnd)),
                "bounds": {
                    "left": rect.left,
                    "top": rect.top,
                    "width": width,
                    "height": height,
                },
                "area": width * height,
            }
        )
        return True

    callback_ref = EnumWindowsProc(callback)
    if not user32.EnumWindows(callback_ref, 0):
        raise ctypes.WinError()
    return sorted(results, key=lambda item: item["area"], reverse=True)


def parse_hotkey(value: str) -> tuple[list[str], str]:
    parts = [part.strip().lower() for part in value.split("+") if part.strip()]
    if not parts:
        raise ValueError("hotkey cannot be empty")
    modifiers = parts[:-1]
    key = parts[-1]
    for name in parts:
        InputSimulator.vk_from_string(name)
    return modifiers, key


def choose_window(
    windows: list[dict], hwnd: Optional[int], process_id: Optional[int], title: Optional[str]
) -> dict:
    matches = windows
    if hwnd is not None:
        matches = [item for item in matches if item["hwnd"] == hwnd]
    if process_id is not None:
        matches = [item for item in matches if item["processId"] == process_id]
    if title:
        title_folded = title.casefold()
        matches = [item for item in matches if title_folded in item["title"].casefold()]
    if not matches:
        raise ValueError("no visible top-level window matched the requested target")
    if hwnd is None and process_id is None and title is None:
        raise ValueError("specify --hwnd, --process-id, or --title when sending input")
    return max(matches, key=lambda item: item["area"])


def focus_and_trigger(target: dict, hotkey: str, focus_timeout: float) -> dict:
    hwnd = target["hwnd"]
    previous = int(user32.GetForegroundWindow() or 0)
    if not user32.IsWindow(hwnd):
        raise ValueError("target HWND is no longer valid")
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    requested = bool(user32.SetForegroundWindow(hwnd))
    deadline = time.monotonic() + focus_timeout
    while time.monotonic() < deadline and int(user32.GetForegroundWindow() or 0) != hwnd:
        time.sleep(0.025)
    verified = int(user32.GetForegroundWindow() or 0) == hwnd
    if not verified:
        return {
            "sent": False,
            "setForegroundReturned": requested,
            "foregroundVerified": False,
            "previousForegroundHwnd": previous,
            "target": target,
            "error": "Windows did not grant foreground ownership; no input was emitted",
        }
    modifiers, key = parse_hotkey(hotkey)
    sent = InputSimulator.trigger_hotkey(modifiers, key)
    return {
        "sent": sent,
        "setForegroundReturned": requested,
        "foregroundVerified": True,
        "previousForegroundHwnd": previous,
        "target": target,
        "hotkey": hotkey,
    }


def int_auto(value: str) -> int:
    return int(value, 0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="list visible top-level windows")
    parser.add_argument("--hwnd", type=int_auto, help="exact target HWND, decimal or 0x-prefixed")
    parser.add_argument("--process-id", type=int, help="target a process's largest visible window")
    parser.add_argument("--title", help="case-insensitive title substring")
    parser.add_argument("--hotkey", help="hotkey such as ctrl+alt+f23")
    parser.add_argument("--focus-timeout", type=float, default=1.0)
    args = parser.parse_args()

    try:
        process_ids = [args.process_id] if args.process_id and args.list else None
        windows = enumerate_windows(process_ids)
        if args.list and not args.hotkey:
            print(json.dumps({"windows": windows}, indent=2))
            return 0
        if not args.hotkey:
            parser.error("--hotkey is required unless --list is used alone")
        target = choose_window(windows, args.hwnd, args.process_id, args.title)
        result = focus_and_trigger(target, args.hotkey, max(0.1, args.focus_timeout))
        print(json.dumps(result, indent=2))
        return 0 if result["sent"] else 2
    except Exception as error:
        print(json.dumps({"sent": False, "error": str(error)}, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
