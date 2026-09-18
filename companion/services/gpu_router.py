"""Windows display-adapter/GPU discovery and target-window routing for LS."""

from __future__ import annotations

import ctypes
import os
import re
from ctypes import wintypes
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


_DISPLAY_PATH_RE = re.compile(r"^\\\\\.\\DISPLAY\d+$", re.IGNORECASE)


class GpuRouter:
    """Map active Windows display adapters to the numeric IDs used by LS."""

    def __init__(
        self,
        display_enumerator: Optional[Callable[[], List[Dict]]] = None,
        window_device_resolver: Optional[Callable[[int], Optional[str]]] = None,
        monitor_enumerator: Optional[Callable[[str], List[Dict]]] = None,
    ):
        self._display_enumerator = display_enumerator or self._enumerate_windows_displays
        self._window_device_resolver = window_device_resolver or self._window_display_device
        if monitor_enumerator is not None:
            self._monitor_enumerator = monitor_enumerator
        elif display_enumerator is None:
            self._monitor_enumerator = self._enumerate_adapter_monitors
        else:
            # Injected adapter lists in tests stay hermetic: do not touch Win32.
            self._monitor_enumerator = lambda _adapter: []

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
    def _enumerate_adapter_monitors(adapter_device_name: str) -> List[Dict]:
        """Return friendly monitor names for one adapter (second-level enum)."""
        if os.name != "nt" or not adapter_device_name:
            return []
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
        except OSError:
            return []
        enum_devices = user32.EnumDisplayDevicesW
        enum_devices.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.POINTER(_DisplayDeviceW), ctypes.c_uint32]
        enum_devices.restype = ctypes.c_bool
        monitors: List[Dict] = []
        index = 0
        while True:
            device = _DisplayDeviceW()
            device.cb = ctypes.sizeof(_DisplayDeviceW)
            if not enum_devices(adapter_device_name, index, ctypes.byref(device), 0):
                break
            flags = int(device.StateFlags)
            if flags & DISPLAY_DEVICE_ATTACHED_TO_DESKTOP:
                monitors.append({
                    "name": device.DeviceString or device.DeviceID or adapter_device_name,
                    "deviceId": device.DeviceID or "",
                })
            index += 1
            if index > 16:
                break
        return monitors

    @staticmethod
    def _monitor_for_window_bounds(
        window_bounds: tuple[int, int, int, int], monitors: List[Dict]
    ) -> Optional[str]:
        """Select the display containing the largest area of a window."""
        left, top, right, bottom = window_bounds
        if right <= left or bottom <= top:
            return None
        center_x = (left + right) / 2
        center_y = (top + bottom) / 2
        best: Optional[tuple[int, bool, bool, str]] = None
        for monitor in monitors:
            bounds = monitor.get("bounds") or ()
            if len(bounds) != 4:
                continue
            m_left, m_top, m_right, m_bottom = map(int, bounds)
            width = max(0, min(right, m_right) - max(left, m_left))
            height = max(0, min(bottom, m_bottom) - max(top, m_top))
            area = width * height
            if not area:
                continue
            contains_center = (
                m_left <= center_x < m_right and m_top <= center_y < m_bottom
            )
            candidate = (
                area,
                contains_center,
                bool(monitor.get("primary")),
                str(monitor.get("deviceName") or ""),
            )
            if best is None or candidate[:3] > best[:3]:
                best = candidate
        return best[3] if best and best[3] else None

    @staticmethod
    def _window_display_device(hwnd: int) -> Optional[str]:
        if os.name != "nt" or not hwnd:
            return None
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        if not user32.IsWindow(ctypes.c_void_p(hwnd)):
            return None
        monitor_from_window = user32.MonitorFromWindow
        monitor_from_window.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        monitor_from_window.restype = ctypes.c_void_p
        get_monitor_info = user32.GetMonitorInfoW
        get_monitor_info.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MonitorInfoExW)]
        get_monitor_info.restype = ctypes.c_bool

        window_rect = _Rect()
        bounds_found = False
        try:
            dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
            get_extended_bounds = dwmapi.DwmGetWindowAttribute
            get_extended_bounds.argtypes = [
                ctypes.c_void_p,
                wintypes.DWORD,
                ctypes.c_void_p,
                wintypes.DWORD,
            ]
            get_extended_bounds.restype = ctypes.c_long
            # Extended frame bounds exclude invisible resize borders and more
            # closely match the application pixels occupying each monitor.
            bounds_found = (
                get_extended_bounds(
                    ctypes.c_void_p(hwnd),
                    9,  # DWMWA_EXTENDED_FRAME_BOUNDS
                    ctypes.byref(window_rect),
                    ctypes.sizeof(window_rect),
                )
                == 0
            )
        except OSError:
            bounds_found = False
        if not bounds_found:
            bounds_found = bool(
                user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(window_rect))
            )

        monitors: List[Dict] = []
        monitor_callback_type = ctypes.WINFUNCTYPE(
            ctypes.c_bool,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(_Rect),
            wintypes.LPARAM,
        )

        def collect_monitor(handle, _dc, rect, _data):
            info = _MonitorInfoExW()
            info.cbSize = ctypes.sizeof(_MonitorInfoExW)
            if get_monitor_info(handle, ctypes.byref(info)):
                monitors.append({
                    "deviceName": info.szDevice,
                    "bounds": (
                        int(info.rcMonitor.left),
                        int(info.rcMonitor.top),
                        int(info.rcMonitor.right),
                        int(info.rcMonitor.bottom),
                    ),
                    "primary": bool(info.dwFlags & 1),
                })
            return True

        callback = monitor_callback_type(collect_monitor)
        if bounds_found:
            enum_monitors = user32.EnumDisplayMonitors
            enum_monitors.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(_Rect),
                monitor_callback_type,
                wintypes.LPARAM,
            ]
            enum_monitors.restype = ctypes.c_bool
            enum_monitors(None, None, callback, 0)
            selected = GpuRouter._monitor_for_window_bounds(
                (
                    int(window_rect.left),
                    int(window_rect.top),
                    int(window_rect.right),
                    int(window_rect.bottom),
                ),
                monitors,
            )
            if selected:
                return selected

        # Minimized, cloaked, and wholly off-screen windows may not intersect
        # the desktop layout. Retain the nearest-monitor API as a fallback.
        monitor = monitor_from_window(ctypes.c_void_p(hwnd), MONITOR_DEFAULTTONEAREST)
        if not monitor:
            return None
        info = _MonitorInfoExW()
        info.cbSize = ctypes.sizeof(_MonitorInfoExW)
        if not get_monitor_info(monitor, ctypes.byref(info)):
            return None
        return info.szDevice

    def detect(self) -> Dict:
        # First-level EnumDisplayDevices enumerates display *adapters* (GPUs),
        # not physical monitors. Group adapter entries by PCI device ID into
        # GPUs, then resolve each adapter's child monitors for friendly
        # display names. Never expose raw \\.\DISPLAYx paths in UI names.
        displays: List[Dict] = []
        gpus: List[Dict] = []
        gpu_by_device: Dict[str, Dict] = {}
        for display_index, raw in enumerate(self._display_enumerator(), start=1):
            adapter_name = str(raw.get("deviceName") or "").strip()
            adapter_label = str(raw.get("name") or "").strip()
            if not adapter_label or _DISPLAY_PATH_RE.match(adapter_label):
                adapter_label = "Unknown GPU"
            device_id = str(raw.get("deviceId") or adapter_name or "").strip()
            key = device_id.casefold()
            gpu = gpu_by_device.get(key)
            if gpu is None:
                gpu = {
                    "deviceId": device_id,
                    "name": adapter_label,
                    "vendor": _vendor(device_id, adapter_label),
                    "lsGpuId": len(gpus) + 1,
                    "connectedDisplays": [],
                }
                gpu_by_device[key] = gpu
                gpus.append(gpu)
            friendly_monitors: List[str] = []
            explicit_display = str(raw.get("displayName") or "").strip()
            if explicit_display and not _DISPLAY_PATH_RE.match(explicit_display):
                friendly_monitors.append(explicit_display)
            else:
                try:
                    for monitor in self._monitor_enumerator(adapter_name):
                        label = str(monitor.get("name") or "").strip()
                        if label and not _DISPLAY_PATH_RE.match(label):
                            friendly_monitors.append(label)
                except Exception:
                    friendly_monitors = []
            display_name = friendly_monitors[0] if friendly_monitors else f"Display {display_index}"
            display = {
                "deviceName": adapter_name,
                "name": display_name,
                "gpuDeviceId": gpu["deviceId"],
                "lsGpuId": gpu["lsGpuId"],
                "lsDisplayId": display_index,
                "primary": bool(raw.get("primary")),
            }
            displays.append(display)
            gpu["connectedDisplays"].append(display["name"])
            for extra in friendly_monitors[1:]:
                if extra not in gpu["connectedDisplays"]:
                    gpu["connectedDisplays"].append(extra)
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

    def route_for_ls_ids(self, gpu_id: int, display_id: int) -> Dict:
        """Resolve LS numeric IDs back to the corresponding Windows devices."""
        inventory = self.detect()
        selected_display = next(
            (
                item for item in inventory["displays"]
                if display_id and int(item.get("lsDisplayId") or 0) == display_id
            ),
            None,
        )
        selected_gpu = next(
            (
                item for item in inventory["gpus"]
                if gpu_id and int(item.get("lsGpuId") or 0) == gpu_id
            ),
            None,
        )
        if selected_display and not selected_gpu:
            selected_gpu = next(
                (
                    item for item in inventory["gpus"]
                    if item.get("deviceId") == selected_display.get("gpuDeviceId")
                ),
                None,
            )
        return {
            "gpuDeviceId": selected_gpu.get("deviceId") if selected_gpu else None,
            "lsGpuId": int(selected_gpu.get("lsGpuId") or 0) if selected_gpu else gpu_id,
            "lsDisplayId": int(selected_display.get("lsDisplayId") or 0) if selected_display else display_id,
            "deviceName": selected_display.get("deviceName", "") if selected_display else "",
        }
