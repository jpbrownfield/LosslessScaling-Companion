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
from pathlib import Path
from typing import Dict, List, Optional

REPORT_PREFIX = "ls-adapter-report-"
REPORT_SUFFIX = ".json"
REPORT_MAX_AGE_SECONDS = 24 * 3600


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
                max_age_seconds: float = REPORT_MAX_AGE_SECONDS) -> Optional[Dict]:
    for path in find_reports(directory):
        try:
            if pid is not None and path.stem != f"{REPORT_PREFIX}{pid}":
                continue
            if time.time() - path.stat().st_mtime > max_age_seconds:
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("schema") == 1:
            payload["_path"] = str(path)
            return payload
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

    Special K, ReShade, and the Echo add-on manager can reorder the adapter
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
