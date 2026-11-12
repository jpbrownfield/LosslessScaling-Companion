"""Temporary HDR policy via stock Gloam's per-display running-app profiles.

Only idle Gloam settings are edited. Runtime switching uses helper lifetimes;
saved desktop preferences and other application profiles are never replaced.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import json
import logging
import math
import mmap
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import threading
import time

import psutil

from .adapter_report import configured_process
from .addon_host import runtime_status
from .hdr_display import HdrDisplayDetector

log = logging.getLogger("LSCompanion.Gloam")


def tick_ms():
    if os.name != "nt":
        return int(time.monotonic() * 1000)
    kernel = ctypes.WinDLL("kernel32")
    kernel.GetTickCount64.restype = ctypes.c_uint64
    return kernel.GetTickCount64()


def monitor_snapshot():
    """GDI name -> the same DisplayConfig monitor interface path Gloam stores."""
    if os.name != "nt":
        return []
    class Luid(ctypes.Structure):
        _fields_ = [("low", wintypes.DWORD), ("high", wintypes.LONG)]
    class Source(ctypes.Structure):
        _fields_ = [("adapter", Luid), ("id", wintypes.UINT), ("mode", wintypes.UINT), ("flags", wintypes.UINT)]
    class Target(ctypes.Structure):
        _fields_ = [("adapter", Luid), ("id", wintypes.UINT), ("mode", wintypes.UINT),
                    ("technology", wintypes.UINT), ("rotation", wintypes.UINT), ("scaling", wintypes.UINT),
                    ("numerator", wintypes.UINT), ("denominator", wintypes.UINT),
                    ("scan", wintypes.UINT), ("available", wintypes.BOOL), ("flags", wintypes.UINT)]
    class DisplayPath(ctypes.Structure):
        _fields_ = [("source", Source), ("target", Target), ("flags", wintypes.UINT)]
    class Header(ctypes.Structure):
        _fields_ = [("type", wintypes.UINT), ("size", wintypes.UINT), ("adapter", Luid), ("id", wintypes.UINT)]
    class SourceName(ctypes.Structure):
        _fields_ = [("header", Header), ("name", ctypes.c_wchar * 32)]
    class TargetName(ctypes.Structure):
        _fields_ = [("header", Header), ("flags", wintypes.UINT), ("technology", wintypes.UINT),
                    ("manufacturer", wintypes.WORD), ("product", wintypes.WORD), ("connector", wintypes.UINT),
                    ("name", ctypes.c_wchar * 64), ("path", ctypes.c_wchar * 128)]
    class White(ctypes.Structure):
        _fields_ = [("header", Header), ("level", wintypes.UINT)]
    user = ctypes.WinDLL("user32")
    user.GetDisplayConfigBufferSizes.argtypes = [wintypes.UINT, ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT)]
    user.QueryDisplayConfig.argtypes = [wintypes.UINT, ctypes.POINTER(wintypes.UINT), ctypes.c_void_p,
                                      ctypes.POINTER(wintypes.UINT), ctypes.c_void_p, ctypes.c_void_p]
    user.DisplayConfigGetDeviceInfo.argtypes = [ctypes.c_void_p]
    paths, modes = wintypes.UINT(), wintypes.UINT()
    outputs = HdrDisplayDetector._enumerate_dxgi_outputs()
    # Topology may change between sizing and reading; retry ERROR_INSUFFICIENT_BUFFER.
    for _ in range(3):
        if user.GetDisplayConfigBufferSizes(2, ctypes.byref(paths), ctypes.byref(modes)):
            return []
        if paths.value > 256 or modes.value > 1024:
            return []
        rows = (DisplayPath * paths.value)()
        mode_buffer = ctypes.create_string_buffer(64 * max(1, modes.value))
        result = user.QueryDisplayConfig(2, ctypes.byref(paths), rows, ctypes.byref(modes), mode_buffer, None)
        if result == 122:
            continue
        if result:
            return []
        selected = []
        for row in rows[:paths.value]:
            source, target, white = SourceName(), TargetName(), White()
            source.header = Header(1, ctypes.sizeof(source), row.source.adapter, row.source.id)
            target.header = Header(2, ctypes.sizeof(target), row.target.adapter, row.target.id)
            white.header = Header(11, ctypes.sizeof(white), row.target.adapter, row.target.id)
            if user.DisplayConfigGetDeviceInfo(ctypes.byref(source)) or user.DisplayConfigGetDeviceInfo(ctypes.byref(target)):
                continue
            white_ok = user.DisplayConfigGetDeviceInfo(ctypes.byref(white)) == 0
            output = next((item for item in outputs if item.get("deviceName", "").casefold() == source.name.casefold()), {})
            if target.path:
                selected.append({"deviceName": source.name, "monitorId": target.path,
                                 "name": target.name or source.name,
                                 "hdr": output.get("colorSpace") == 12,
                                 "sdrWhiteNits": white.level * 80 / 1000 if white_ok else None})
        return selected
    return []


def gloam_processes():
    found = []
    for process in psutil.process_iter(["name", "exe"]):
        info = process.info
        if str(info.get("name") or "").casefold() == "gloam.exe":
            found.append(info.get("exe") or "")
    return found


def helper_source():
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    candidates = [root / "tools/LSHdrSession.exe", root / "build/native/LSHdrSession.exe"]
    return next((item for item in candidates if item.is_file()), None)


def profile_for(monitor, executable):
    return {"AppName": executable.name, "ExecutablePath": str(executable.resolve()),
            "DisplayName": f"Lossless Scaling HDR: {monitor['name']}", "Enabled": True,
            "ApplyWhileRunning": True, "PictureIntent": "CinematicHdr", "DisplayScope": "SpecificDisplay",
            "MonitorDevicePath": monitor["monitorId"], "HdrExpectation": "RequireHdr",
            "OverrideGamma": True, "GammaMode": "WindowsDefault", "ShadowDetailStrength": 0.0,
            "NightPolicy": "ForceDaylight", "GameplayLock": True}


def provision(root, settings_path, monitors, source, running):
    """Idempotent offline merge. Refuse newer schemas, collisions and concurrent edits."""
    root, settings_path = Path(root), Path(settings_path)
    if not source or not Path(source).is_file():
        return {"state": "missing_helper", "message": "The HDR session helper is missing from this build."}
    root.mkdir(parents=True, exist_ok=True)
    receipt_path = root / "helpers.json"
    receipt = json.loads(receipt_path.read_text()) if receipt_path.is_file() else {}
    if not isinstance(receipt, dict):
        raise ValueError("HDR helper ownership receipt is malformed")
    desired = []
    digest = hashlib.sha256(Path(source).read_bytes()).hexdigest()
    for monitor in monitors:
        key = hashlib.sha256(monitor["monitorId"].casefold().encode()).hexdigest()[:16]
        executable = root / f"LSHdrSession_{key}.exe"
        if not executable.is_file():
            shutil.copy2(source, executable)
        elif hashlib.sha256(executable.read_bytes()).hexdigest() != digest:
            actual = hashlib.sha256(executable.read_bytes()).hexdigest()
            if receipt.get(executable.name) != actual:
                raise ValueError("An HDR session executable was changed outside Companion; it was preserved")
            # Windows refuses replacement while the old executable is running.
            # The caller releases its owned marker on error and retries later.
            shutil.copyfile(source, executable)
        receipt[executable.name] = digest
        desired.append(profile_for(monitor, executable))
    receipt_path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    if not settings_path.is_file():
        return {"state": "missing_gloam", "message": "Install and start Gloam once to initialize its settings."}
    if settings_path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("Gloam settings exceed the supported size")
    original = settings_path.read_bytes()
    data = json.loads(original.decode("utf-8-sig"))
    if not isinstance(data, dict) or type(data.get("SchemaVersion")) is not int or data["SchemaVersion"] != 4:
        raise ValueError("Unsupported Gloam settings schema; profiles were preserved")
    profiles = data.get("GamerProfiles")
    if not isinstance(profiles, list) or any(not isinstance(item, dict) for item in profiles):
        raise ValueError("Gloam game profiles are malformed")
    missing = []
    for wanted in desired:
        existing = [item for item in profiles if str(item.get("AppName", "")).casefold() == wanted["AppName"].casefold()]
        if existing and (len(existing) != 1 or any(existing[0].get(k) != v for k, v in wanted.items())):
            return {"state": "profile_conflict", "message": "A managed Gloam HDR profile was edited. Its settings were preserved."}
        if not existing:
            missing.append(wanted)
    if not missing:
        return {"state": "ready" if data.get("GamerModeEnabled") else "paused",
                "message": "Gloam HDR profiles are ready." if data.get("GamerModeEnabled") else "Enable Game Mode in Gloam to resume HDR switching.",
                "profiles": desired}
    if running():
        return {"state": "awaiting_gloam_exit", "message": "Quit Gloam once to finish automatic profile setup, then reopen it."}
    backup = settings_path.with_name(f"settings.before-ls-hdr-{hashlib.sha256(original).hexdigest()[:16]}.json")
    if not backup.exists():
        backup.write_bytes(original)
    data["GamerProfiles"] = profiles + missing
    data["GamerModeEnabled"] = True
    temporary = settings_path.with_name("settings.ls-hdr.tmp")
    try:
        temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        if running() or settings_path.read_bytes() != original:
            return {"state": "retry", "message": "Gloam settings changed; automatic setup will retry."}
        temporary.replace(settings_path)
    finally:
        temporary.unlink(missing_ok=True)
    return {"state": "ready", "message": "Gloam HDR profiles created. Start Gloam to use automatic HDR switching.", "profiles": desired}


class HdrControl:
    def __init__(self):
        self.mapping = None
        self.pid = None

    def set(self, pid, enabled, sample=True, white=400):
        if self.pid != pid:
            self.close()
            if pid is not None and os.name == "nt":
                self.mapping = mmap.mmap(-1, 24, tagname=f"Local\\LSCHdrProbe-{pid}")
                self.pid = pid
        if self.mapping:
            # Clear enabled first; publish enabled only after the complete packet.
            self.mapping[4:8] = struct.pack("<I", 0)
            self.mapping[:] = struct.pack("<IIIfQ", 1, 0, int(sample), white, tick_ms())
            self.mapping[4:8] = struct.pack("<I", int(enabled))

    def close(self):
        if self.mapping:
            self.mapping[4:8] = bytes(4)
            self.mapping.close()
        self.mapping = self.pid = None


class GloamIntegration:
    def __init__(self, manager, state, *, monitors=monitor_snapshot, running=gloam_processes,
                 source=helper_source, telemetry=runtime_status, process=configured_process,
                 control=None, spawn=subprocess.Popen, clock=time.monotonic, settings_path=None):
        self.manager, self.state = manager, state
        self.monitors, self.running, self.source = monitors, running, source
        self.telemetry, self.process = telemetry, process
        self.control, self.spawn, self.clock = control or HdrControl(), spawn, clock
        self.settings_path = Path(settings_path or Path(os.environ.get("LOCALAPPDATA", "")) / "Gloam/settings.json")
        self.root = Path(manager.config_dir) / "gloam-sessions"
        self.lock = threading.RLock()
        self.child = None
        self.selected = self.confirmed = None
        self.observed_monitor = None
        self.instance = None
        self.hits = 0
        self.last_sample = 0
        self.next_setup = 0
        self.profiles = []
        self.status = {"state": "disabled", "message": "Gloam HDR integration is off."}

    def snapshot(self):
        # Dashboard snapshots must not wait on disk provisioning or pipe I/O.
        child = self.child
        return {**self.status, "helperRunning": child is not None and child.poll() is None,
                "monitorId": self.selected, "confirmed": self.confirmed is not None}

    def _stop(self):
        if self.child:
            if self.child.stdin:
                try:
                    self.child.stdin.close()  # EOF exits the helper; Gloam restores policy.
                except (OSError, ValueError):
                    pass
            try:
                self.child.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.child.terminate()  # Only our owned marker process, never Gloam.
                self.child.wait(timeout=1)
            self.child = None
        self.selected = self.confirmed = None
        self.hits = self.last_sample = 0

    def close(self):
        with self.lock:
            self.control.close()
            self._stop()

    def poll(self):
        with self.lock:
            if not self.manager.config.gloam_hdr_enabled:
                self.close()
                self.next_setup = 0
                self.status = {"state": "disabled", "message": "Gloam HDR integration is off; no HDR scans run."}
                return
            try:
                self._poll()
            except (OSError, ValueError, psutil.Error, subprocess.SubprocessError) as error:
                self.close()
                self.status = {"state": "error", "message": str(error)}
                log.warning("Gloam HDR integration: %s", error)

    def _poll(self):
        now = self.clock()
        if now >= self.next_setup:
            self.next_setup = now + 5
            monitors = self.monitors()
            if not monitors:
                self.close()
                self.status = {"state": "no_monitors", "message": "No identified output monitors are available."}
                return
            self.outputs = monitors
            setup = provision(self.root, self.settings_path, monitors, self.source(), self.running)
            self.profiles = setup.get("profiles", [])
            self.status = setup
        if self.status.get("state") not in {"ready", "watching", "active", "idle"}:
            self.close()
            return
        active = bool(self.state.is_scaling_active and not self.state.benchmark_mode_active
                      and not self.state.update_in_progress and not self.state.gpu_mapping_in_progress)
        if not active or not self.running():
            self.close()
            self.status = {"state": "idle", "message": "Waiting for Gloam and an active HDR scaling session."}
            return
        executable = self.manager.config.lossless_scaling_exe_path
        instance = self.process(executable)
        if not instance:
            self.close()
            return
        if instance != self.instance:
            self.close()
            self.instance = instance
        status = self.telemetry(executable)
        probe = status.get("hdrProbe") if status.get("available") and status.get("pid") == instance[0] else None
        if not isinstance(probe, dict):
            self.close()
            self.status = {"state": "watching", "message": "Waiting for the updated Companion host HDR observer."}
            return
        matches = [item for item in self.outputs if item["deviceName"].casefold() == str(probe.get("deviceName", "")).casefold()]
        # While waiting for the first Present, enable observation without guessing a monitor.
        monitor = matches[0] if len(matches) == 1 else None
        if monitor is None or not monitor["hdr"] or (probe.get("colorSpace") != 12 and not monitor.get("sdrWhiteNits")):
            self._stop()
            self.control.set(instance[0], True, False)
            self.status = {"state": "watching", "message": "Waiting for an identified HDR presentation and SDR-white level."}
            return
        if self.observed_monitor != monitor["monitorId"]:
            self._stop()
            self.observed_monitor = monitor["monitorId"]
        current = tick_ms()
        fresh = 0 <= current - int(probe.get("presentTick", 0)) < 3000
        if not fresh or probe.get("colorSpace") not in (1, 12):
            self._stop()
        if probe.get("colorSpace") == 1 and probe.get("sampledTick") != self.last_sample:
            self.last_sample = probe.get("sampledTick")
            pixels = probe.get("pixels", 0)
            evidence = (pixels >= 1000 and probe.get("highlights", 0) >= max(32, pixels * .001)
                        and str(probe.get("sampleDeviceName", "")).casefold() == monitor["deviceName"].casefold()
                        and math.isclose(probe.get("sampleWhiteNits", 0), monitor["sdrWhiteNits"], rel_tol=1e-5)
                        and 0 <= current - int(probe.get("sampledTick", 0)) < 3000)
            self.hits = self.hits + 1 if evidence else 0
        if fresh and (probe.get("colorSpace") == 12 or self.hits >= 3):
            self.confirmed = monitor["monitorId"]
        if self.confirmed and fresh:
            if not self.child or self.child.poll() is not None:
                wanted = next((p for p in self.profiles if p["MonitorDevicePath"] == monitor["monitorId"]), None)
                if not wanted:
                    self._stop()
                    return
                self.child = self.spawn([wanted["ExecutablePath"], str(os.getpid())], stdin=subprocess.PIPE,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                self.selected = monitor["monitorId"]
            try:
                self.child.stdin.write(b"1")
                self.child.stdin.flush()
            except (OSError, ValueError):
                self._stop()
                self.status = {"state": "watching", "message": "Restarting the HDR session helper."}
                return
            self.status = {"state": "active", "message": f"Cinematic HDR requested on {monitor['name']}. Foreground Gloam profiles take priority."}
        else:
            self.status = {"state": "watching", "message": probe.get("error") or "Checking HDR highlights twice per second."}
        self.control.set(instance[0], True, self.confirmed is None, monitor.get("sdrWhiteNits") or 400)
