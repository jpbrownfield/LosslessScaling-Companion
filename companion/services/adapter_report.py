"""Read the in-LS adapter report written by the LSC Adapter Report add-on.

The add-on runs inside the Lossless Scaling process, so its enumeration
reflects any in-process reshaping (hooks, preference-based ordering) that
host-side DXGI enumeration cannot see. The device entry is ground truth for
which physical GPU LS renders with; the enumeration indices bound the LS
numeric IDs without trial-and-error scaling runs.
"""
from __future__ import annotations

import json
import os
import time
import secrets
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List, Optional

REPORT_PREFIX = "ls-adapter-report-"
REPORT_SUFFIX = ".json"
REPORT_MAX_AGE_SECONDS = 24 * 3600


@contextmanager
def _runtime_request(pid: int, token: str):
    """Signal the loaded reporter; never inject or load a DLL into LS."""
    import ctypes
    import struct
    from ctypes import wintypes

    kernel = None
    handles = []
    view = None
    locked = False
    available = False
    try:
        if os.name == "nt":
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            for name, arguments, result in (
                ("OpenEventW", [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
                ("OpenFileMappingW", [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
                ("CreateMutexW", [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
                ("WaitForSingleObject", [wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
                ("MapViewOfFile", [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t], ctypes.c_void_p),
                ("SetEvent", [wintypes.HANDLE], wintypes.BOOL),
                ("ReleaseMutex", [wintypes.HANDLE], wintypes.BOOL),
                ("UnmapViewOfFile", [ctypes.c_void_p], wintypes.BOOL),
                ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
            ):
                function = getattr(kernel, name)
                function.argtypes, function.restype = arguments, result
            event = kernel.OpenEventW(2, False, f"Local\\LSCAdapterReportRefresh-{pid}")
            if event:
                handles.append(event)
                mapping = kernel.OpenFileMappingW(2, False, f"Local\\LSCAdapterReportRequest-{pid}")
                if mapping:
                    handles.append(mapping)
                    mutex = kernel.CreateMutexW(None, False, f"Local\\LSCAdapterReportClient-{pid}")
                    if mutex:
                        handles.append(mutex)
                        locked = kernel.WaitForSingleObject(mutex, 50) in (0, 128)
                        if locked:
                            view = kernel.MapViewOfFile(mapping, 2, 0, 0, 72)
                            if view:
                                packet = struct.pack("<I65s3x", 1, token.encode("ascii"))
                                ctypes.memmove(view, packet, len(packet))
                                available = bool(kernel.SetEvent(event))
    except (OSError, AttributeError, ValueError):
        available = False
    try:
        yield available
    finally:
        if kernel:
            if view:
                kernel.UnmapViewOfFile(view)
            if locked:
                kernel.ReleaseMutex(mutex)
            for handle in reversed(handles):
                kernel.CloseHandle(handle)


def request_runtime_report(directory, instance, *, process_check, timeout=0.75):
    """Fresh, nonce-matched device evidence for one configured LS activation.

    No renderer, benchmark, hotkey, or process restart. Older/unloaded reporters
    return None immediately; a loaded reporter has a short bounded response wait.
    """
    pid, created = instance
    token = secrets.token_hex(32)
    directories = [Path(directory)]
    default = report_directory()
    if default not in directories:
        directories.append(default)
    with _runtime_request(pid, token) as available:
        if not available:
            return None
        deadline = time.monotonic() + max(0, timeout)
        while True:
            if process_check() != instance:
                return None
            for location in directories:
                report = load_latest(location, pid=pid, created_after=created, max_age_seconds=15)
                if (report and report.get("pid") == pid and report.get("requestId") == token
                        and process_check() == instance):
                    return report
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.01)


def report_directory(config_dir: Optional[object] = None) -> Path:
    if config_dir is not None:
        return Path(str(config_dir)) / "diagnostics"
    local = os.environ.get("LOCALAPPDATA", "")
    return Path(local) / "LosslessScalingHelper" / "diagnostics"


def find_reports(directory: Path) -> List[Path]:
    try:
        candidates = sorted(
            (item for item in Path(directory).glob(f"{REPORT_PREFIX}*{REPORT_SUFFIX}")
             if item.is_file()),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return []
    return candidates


def load_latest(directory: Path, pid: Optional[int] = None,
                max_age_seconds: float = REPORT_MAX_AGE_SECONDS,
                created_after: Optional[float] = None) -> Optional[Dict]:
    for path in find_reports(directory):
        try:
            if pid is not None and path.stem != f"{REPORT_PREFIX}{pid}":
                continue
            modified = path.stat().st_mtime
            if (time.time() - modified > max_age_seconds
                    or (created_after is not None and modified < created_after)):
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("schema") == 1:
            if pid is not None and payload.get("pid") != pid:
                continue
            payload["_path"] = str(path)
            return payload
    return None


def load_idle_report(directory: Path, pid: int, created_after: float) -> Optional[Dict]:
    """Read the startup probe's separate result, untouched by runtime refreshes."""
    path = Path(directory) / f"ls-idle-adapter-report-{pid}.json"
    try:
        stat = path.stat()
        if stat.st_mtime < created_after or stat.st_size > 1024 * 1024:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and payload.get("schema") == 1 and payload.get("pid") == pid:
            return {**payload, "_path": str(path)}
    except (OSError, ValueError):
        pass
    return None


def configured_process(executable):
    """Resolve the configured LS instance, never an unrelated same-name copy."""
    import psutil
    if not executable:
        return None
    wanted = str(Path(executable).resolve()).casefold()
    for process in psutil.process_iter():
        try:
            # process_iter caches Process objects; another thread can replace
            # their .info with a different set of attributes at any time.
            info = process.as_dict(attrs=["name", "exe"])
            if (str(info.get("name") or "").casefold() == "losslessscaling.exe"
                    and info.get("exe")
                    and str(Path(info["exe"]).resolve()).casefold() == wanted):
                return (process.pid, process.create_time())
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError, ValueError):
            continue
    return None


def _pci_key(vendor_id: object, device_id: object) -> Optional[str]:
    try:
        return f"PCI\\VEN_{int(vendor_id):04X}&DEV_{int(device_id):04X}"
    except (TypeError, ValueError):
        return None


def device_pci(report: Dict) -> Optional[str]:
    device = report.get("device") if isinstance(report, dict) else None
    if not isinstance(device, dict):
        return None
    return _pci_key(device.get("vendorId"), device.get("deviceId"))


def settings_route_for(report: Dict, title: object) -> tuple:
    """(gpu, display) the report attributes to a Settings profile title.

    Prefers the per-title table (schema 2); falls back to the legacy
    top-level fields (schema 1). Returns (None, None) when unknown.
    """
    wanted = str(title or "").casefold()
    if isinstance(report, dict):
        for item in report.get("settingsProfiles") or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("title") or "").casefold() != wanted:
                continue
            try:
                gpu = int(item.get("gpu") or 0)
                disp = int(item.get("display") or 0)
            except (TypeError, ValueError):
                return (None, None)
            return (gpu if gpu > 0 else None, disp if disp > 0 else None)
        if report.get("hasSettingsIds"):
            try:
                gpu = int(report.get("settingsGpu") or 0)
                disp = int(report.get("settingsDisplay") or 0)
            except (TypeError, ValueError):
                return (None, None)
            return (gpu if gpu > 0 else None, disp if disp > 0 else None)
    return (None, None)


def candidate_gpu_ids(report: Dict, title: object = None,
                      limit: int = 4) -> List[int]:
    """LS GPU IDs most likely to address the report's device adapter.

    The title-matched Settings route comes first (it addresses this exact
    device while that route is active), followed by the device adapter's
    index in each reported enumeration.
    """
    ordered: List[int] = []

    def add(value: object) -> None:
        try:
            slot = int(value)
        except (TypeError, ValueError):
            return
        if slot >= 1 and slot not in ordered:
            ordered.append(slot)

    if isinstance(report, dict):
        gpu, _disp = settings_route_for(report, title)
        add(gpu)
        if title is not None and report.get("hasSettingsIds"):
            # A title-specific miss still leaves the legacy anchor useful.
            try:
                add(int(report.get("settingsGpu") or 0))
            except (TypeError, ValueError):
                pass
        for key in ("deviceIndexAdapters", "deviceIndexAdapters1"):
            try:
                add(int(report.get(key, -1)) + 1)
            except (TypeError, ValueError):
                continue
    return ordered[:max(1, limit)]


def attached_outputs(report: Dict) -> List[Dict]:
    outputs = report.get("deviceOutputs") if isinstance(report, dict) else []
    return [item for item in outputs
            if isinstance(item, dict) and item.get("attached")]


def same_device(first: object, second: object) -> bool:
    """Compare PCI identities tolerantly (full ID or VEN/DEV pair)."""
    if not isinstance(first, str) or not isinstance(second, str):
        return False
    if first.strip().casefold() == second.strip().casefold():
        return True
    from .gpu_router import pci_ven_dev

    left, right = pci_ven_dev(first), pci_ven_dev(second)
    return left is not None and left == right


RESHAPING_LAYER_NAMES = {
    "specialk64.dll",
    "specialk32.dll",
    "lsaddonmanager.dll",
    "reshade64.dll",
    "re shade64.dll",
}

RESHAPING_LAYER_HINTS = ("specialk", "lsaddonmanager", "reshade", "lsp-", "lsc_")


def _module_base_name(path: str) -> str:
    name = (path or "").replace("/", "\\").rsplit("\\", 1)[-1].casefold()
    return name


def detect_reshaping_layers(pid: Optional[int] = None,
                            process: object = None) -> List[str]:
    """Best-effort list of graphics-shaping modules loaded into LS.

    Special K, ReShade, and the manager add-on manager can reorder the adapter
    list LS itself sees, which invalidates host-side enumeration guesses.
    A plain system dxgi.dll is normal and never reported; only LS-directory
    copies and known hook DLLs are.
    """
    if process is None:
        if pid is None:
            return []
        try:
            import psutil

            process = psutil.Process(int(pid))
        except Exception:
            return []
    try:
        maps = process.memory_maps()
    except Exception:
        return []
    found: List[str] = []
    for entry in maps or []:
        path = str(getattr(entry, "path", "") or "")
        if not path:
            continue
        base = _module_base_name(path)
        if not base:
            continue
        lowered = path.casefold()
        system = ("\\windows\\system32\\" in lowered
                  or "\\windows\\syswow64\\" in lowered)
        if base in RESHAPING_LAYER_NAMES and base not in found:
            found.append(base)
        elif not system and base.endswith(".dll") and (
                base.startswith(RESHAPING_LAYER_HINTS)
                or "addon" in base or base == "dxgi.dll"):
            if base not in found:
                found.append(base)
    return sorted(found)
