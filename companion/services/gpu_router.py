"""Windows display/GPU discovery and target-window routing for Lossless Scaling."""

from __future__ import annotations

import ctypes
import os
from typing import Callable, Dict, List, Optional


DISPLAY_DEVICE_ATTACHED_TO_DESKTOP = 0x00000001
DISPLAY_DEVICE_PRIMARY_DEVICE = 0x00000004
DISPLAY_DEVICE_MIRRORING_DRIVER = 0x00000008
DISPLAY_DEVICE_REMOTE = 0x04000000
MONITOR_DEFAULTTONEAREST = 2


class _DisplayDeviceW(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("DeviceName", ctypes.c_wchar * 32),
        ("DeviceString", ctypes.c_wchar * 128),
        ("StateFlags", ctypes.c_uint32),
        ("DeviceID", ctypes.c_wchar * 128),
        ("DeviceKey", ctypes.c_wchar * 128),
    ]


class _Rect(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _MonitorInfoExW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("rcMonitor", _Rect),
        ("rcWork", _Rect),
        ("dwFlags", ctypes.c_uint32),
        ("szDevice", ctypes.c_wchar * 32),
    ]


def _vendor(device_id: str, name: str) -> str:
    marker = f"{device_id} {name}".upper()
    if "VEN_10DE" in marker or "NVIDIA" in marker:
        return "NVIDIA"
    if "VEN_1002" in marker or "VEN_1022" in marker or "AMD" in marker or "RADEON" in marker:
        return "AMD"
    if "VEN_8086" in marker or "INTEL" in marker:
        return "Intel"
    return "Other"


class GpuRouter:
    """Map active Windows display adapters to the numeric IDs used by LS."""

    def __init__(
        self,
        display_enumerator: Optional[Callable[[], List[Dict]]] = None,
        window_device_resolver: Optional[Callable[[int], Optional[str]]] = None,
    ):
        self._display_enumerator = display_enumerator or self._enumerate_windows_displays
        self._window_device_resolver = window_device_resolver or self._window_display_device

    @staticmethod
    def _enumerate_windows_displays() -> List[Dict]:
        if os.name != "nt":
            return []
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        enum_devices = user32.EnumDisplayDevicesW
        enum_devices.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.POINTER(_DisplayDeviceW), ctypes.c_uint32]
        enum_devices.restype = ctypes.c_bool
        results: List[Dict] = []
        index = 0
        while True:
            device = _DisplayDeviceW()
            device.cb = ctypes.sizeof(_DisplayDeviceW)
            if not enum_devices(None, index, ctypes.byref(device), 0):
                break
            flags = int(device.StateFlags)
            if (
                flags & DISPLAY_DEVICE_ATTACHED_TO_DESKTOP
                and not flags & (DISPLAY_DEVICE_MIRRORING_DRIVER | DISPLAY_DEVICE_REMOTE)
            ):
                results.append({
                    "deviceName": device.DeviceName,
                    "name": device.DeviceString or device.DeviceName,
                    "deviceId": device.DeviceID or device.DeviceName,
                    "primary": bool(flags & DISPLAY_DEVICE_PRIMARY_DEVICE),
                })
            index += 1
        return results

    @staticmethod
    def _window_display_device(hwnd: int) -> Optional[str]:
        if os.name != "nt" or not hwnd:
            return None
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        monitor_from_window = user32.MonitorFromWindow
        monitor_from_window.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        monitor_from_window.restype = ctypes.c_void_p
        get_monitor_info = user32.GetMonitorInfoW
        get_monitor_info.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MonitorInfoExW)]
        get_monitor_info.restype = ctypes.c_bool
        monitor = monitor_from_window(ctypes.c_void_p(hwnd), MONITOR_DEFAULTTONEAREST)
        if not monitor:
            return None
        info = _MonitorInfoExW()
        info.cbSize = ctypes.sizeof(_MonitorInfoExW)
        if not get_monitor_info(monitor, ctypes.byref(info)):
            return None
        return info.szDevice

    def detect(self) -> Dict:
        displays: List[Dict] = []
        gpus: List[Dict] = []
        gpu_by_device: Dict[str, Dict] = {}
        for display_index, raw in enumerate(self._display_enumerator(), start=1):
            device_id = str(raw.get("deviceId") or raw.get("deviceName") or "").strip()
            key = device_id.casefold()
            gpu = gpu_by_device.get(key)
            if gpu is None:
                gpu = {
                    "deviceId": device_id,
                    "name": str(raw.get("name") or "Unknown GPU"),
                    "vendor": _vendor(device_id, str(raw.get("name") or "")),
                    "lsGpuId": len(gpus) + 1,
                    "connectedDisplays": [],
                }
                gpu_by_device[key] = gpu
                gpus.append(gpu)
            display = {
                "deviceName": str(raw.get("deviceName") or ""),
                "name": str(raw.get("displayName") or raw.get("deviceName") or f"Display {display_index}"),
                "gpuDeviceId": gpu["deviceId"],
                "lsGpuId": gpu["lsGpuId"],
                "lsDisplayId": display_index,
                "primary": bool(raw.get("primary")),
            }
            displays.append(display)
            gpu["connectedDisplays"].append(display["name"])
        return {
            "gpus": gpus,
            "displays": displays,
            "hasNvidia": any(gpu["vendor"] == "NVIDIA" for gpu in gpus),
        }

    def route_for_window(self, hwnd: int) -> Optional[Dict]:
        device_name = self._window_device_resolver(hwnd)
        if not device_name:
            return None
        inventory = self.detect()
        for display in inventory["displays"]:
            if display["deviceName"].casefold() == device_name.casefold():
                return dict(display)
        return None

    def route_for_gpu(self, device_id: Optional[str]) -> Dict:
        if not device_id:
            return {"gpuDeviceId": None, "lsGpuId": 0, "lsDisplayId": 0, "deviceName": ""}
        for gpu in self.detect()["gpus"]:
            if gpu["deviceId"].casefold() == device_id.casefold():
                return {"gpuDeviceId": gpu["deviceId"], "lsGpuId": gpu["lsGpuId"], "lsDisplayId": 0, "deviceName": ""}
        return {"gpuDeviceId": None, "lsGpuId": 0, "lsDisplayId": 0, "deviceName": ""}
