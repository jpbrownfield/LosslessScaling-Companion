"""Idle LS index discovery. Never emits scaling hotkeys or creates a D3D device."""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET

from . import adapter_report, startup_guard
from .external_process import external_dll_search
from .gpu_router import inventory_fingerprint, pci_ven_dev
from .gpu_selections import match_device

logger = logging.getLogger("LSCompanion.GpuIndexRefresh")
CHECK_SECONDS = 3 * 3600
# Inspected managed UI: cdecl GetAdapterNames() -> owned BSTR; Split(';',
# RemoveEmptyEntries); Auto inserted at index zero; SelectedIndex saved verbatim.
SUPPORTED_UI_HASHES = {"e4ea2dbb1371ea1920d73c87202f19b6f095ef6f521a8a7a139d19f34f1fe866"}


def _name(value):
    return " ".join(str(value or "").casefold().split())


def _slot(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def snapshot_fingerprint(report):
    """Order and LUIDs matter even if model names and adapter counts do not change."""
    arrays = []
    for key in ("enumAdapters", "enumAdapters1"):
        rows = report.get(key)
        if not isinstance(rows, list) or not rows or len(rows) >= 256:
            raise ValueError("DXGI adapter enumeration is unavailable")
        arrays.append([tuple(row.get(k) for k in (
            "description", "vendorId", "deviceId", "subSysId", "revision",
            "luidHigh", "luidLow", "isSoftware")) for row in rows])
    return hashlib.sha256(json.dumps(arrays, sort_keys=True).encode()).hexdigest()


def probe_path():
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "tools/LSC_AdapterProbe.exe"
    return Path(__file__).resolve().parents[2] / "build/native/LSC-AdapterReport/LSC_AdapterProbe.exe"


def fast_snapshot():
    executable = probe_path()
    if not executable.is_file():
        raise RuntimeError("Native DXGI probe is unavailable; rebuild or update Companion")
    with external_dll_search():
        result = subprocess.run([str(executable)], capture_output=True, timeout=10,
                                creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                               | getattr(subprocess, "IDLE_PRIORITY_CLASS", 0)))
    if result.returncode or len(result.stdout) > 1024 * 1024:
        raise RuntimeError("Native DXGI probe failed")
    report = json.loads(result.stdout.decode("utf-8"))
    snapshot_fingerprint(report)
    return report


def mapping_plan(config, inventory, report, native_profiles):
    """Only map uniquely identified physical selections; preserve Auto and unknowns."""
    names = report.get("lsAdapterNames")
    if not isinstance(names, list) or not names or len(names) > 256:
        raise ValueError("LS did not provide its own adapter-name list")
    if len(names) == 1:
        return {"mapping": {}, "profiles": {}, "xml": {}, "skipped": []}
    gpus = inventory.get("gpus", [])
    known = {}
    skipped = []
    for gpu in gpus:
        name = _name(gpu.get("name"))
        if not name or sum(_name(g.get("name")) == name for g in gpus) != 1:
            skipped.append(str(gpu.get("name") or "Ambiguous GPU"))
            continue
        # Prove the label belongs to this hardware model in the same snapshot.
        hardware = pci_ven_dev(gpu.get("deviceId"))
        adapters = report.get("enumAdapters") or []
        if not hardware or not any(
            _name(a.get("description")) == name
            and (int(a.get("vendorId", -1)), int(a.get("deviceId", -1)))
                == tuple(int(part, 16) for part in hardware)
            and not a.get("isSoftware") for a in adapters
        ):
            skipped.append(gpu.get("name"))
            continue
        slots = [index for index, label in enumerate(names, 1) if _name(label) == name]
        if slots:
            old = config.ls_gpu_id_map.get(gpu["deviceId"])
            known[gpu["deviceId"]] = old if old in slots else slots[0]
        else:
            skipped.append(gpu.get("name"))

    def slot_for(device_id, name):
        gpu = match_device(gpus, device_id, name, gpu=True)
        return known.get(gpu["deviceId"]) if gpu else None

    def old_identity(slot):
        candidates = {pci_ven_dev(key) for key, value in config.ls_gpu_id_map.items() if value == slot}
        candidates.discard(None)
        if len(candidates) != 1:
            return None
        physical = [g for g in gpus if pci_ven_dev(g.get("deviceId")) in candidates]
        return physical[0] if len(physical) == 1 else None

    profiles, xml = {}, {}
    by_title = {}
    for profile in config.profiles:
        current = _slot(profile.native_scaling_settings.get("PreferredGpuId"))
        if not current:
            continue
        old = old_identity(current)
        slot = slot_for(profile.preferred_gpu_device_id or (old or {}).get("deviceId"),
                        profile.preferred_gpu_name or (old or {}).get("name"))
        if slot is None:
            skipped.append(profile.name)
            continue
        profiles[profile.id] = slot
        if profile.lossless_profile_title:
            by_title.setdefault(profile.lossless_profile_title.casefold(), []).append(slot)
    for native in native_profiles:
        title = str(native.get("Title") or "")
        if not title or sum(_name(p.get("Title")) == _name(title) for p in native_profiles) != 1:
            skipped.append(title or "Untitled LS profile")
            continue
        current = _slot(native.get("PreferredGpuId"))
        if not current:
            continue
        choices = by_title.get(title.casefold(), [])
        old = old_identity(current)
        slot = choices[0] if len(choices) == 1 else (
            known.get((old or {}).get("deviceId")) if not choices else None)
        if slot is not None and slot != current:
            xml[title] = slot
        elif slot is None:
            skipped.append(title)
    return {"mapping": known, "profiles": profiles, "xml": xml, "skipped": skipped}


class IdleGpuIndexRefresh:
    def __init__(self, server, *, fast_probe=fast_snapshot, process_lookup=adapter_report.configured_process, clock=time.monotonic):
        self.server = server
        self.fast_probe, self.process_lookup, self.clock = fast_probe, process_lookup, clock
        self.next_check = 0.0
        self.fingerprint = None
        self.observed = None
        self.retry_after = 0.0
        self.process_instance = None
        self.pending = True
        self.stopping = threading.Event()

    def stop(self):
        self.stopping.set()

    def request_refresh(self):
        """A fresh runtime mismatch invalidates the periodic-check shortcut."""
        self.pending = True
        self.observed = None
        self.retry_after = 0.0

    def status(self, state, message, **details):
        value = {"state": state, "message": message, **details}
        if value != self.server.state.gpu_mapping_status:
            logger.info("%s", message)
        self.server.state.gpu_mapping_status = value

    def idle(self, *, cleanup=False):
        server = self.server
        state = server.state
        if state.is_scaling_active:
            return False
        if not cleanup and (state.benchmark_mode_active or state.update_in_progress
                or server.profile_manager.repair_session or server._editing_clients
                or server._active_mutations or getattr(server, "_active_requests", 0)
                or server.component_install_lock.locked()
                or (server.benchmark_task and not server.benchmark_task.done())
                or (server.diagnostics_task and not server.diagnostics_task.done())
                or state.scaling_control_status.get("state") == "pending"
                or (server._gpu_calibration_task and not server._gpu_calibration_task.done())):
            return False
        probe = server.automation.scaling_state_probe
        if server.process_watcher.check_is_lossless_scaling_running():
            if not probe:
                return False
            observation = probe()
            active = observation.get("isActive", observation.get("is_active")) if isinstance(observation, dict) else observation
            if active is not False:
                return False
        return True

    def check(self):
        server = self.server
        config = server.profile_manager.config
        executable = Path(config.lossless_scaling_exe_path or "")
        if self.stopping.is_set() or server.state.simulation_mode or server.process_watcher is None or not executable.is_file():
            return
        now = self.clock()
        if now < self.retry_after:
            return
        instance = self.process_lookup(executable)
        changed_process = instance != self.process_instance
        if changed_process:
            self.pending = True
            self.process_instance = instance
        if not self.pending and now < self.next_check:
            return
        try:
            # Enumeration loads graphics-driver code too. Defer even the fast
            # helper during scaling/tests/editing rather than adding contention
            # to a live session merely to decide whether a refresh is needed.
            if not self.idle():
                self.status("deferred", "GPU index checks wait until scaling, editing, tests, and updates are idle")
                return
            if now >= self.next_check or self.observed is None or changed_process:
                fast = self.fast_probe()
                inventory = server.automation.gpu_router.detect()
                signature = (snapshot_fingerprint(fast), inventory_fingerprint(inventory),
                             executable.with_suffix('.dll').stat().st_mtime_ns,
                             (executable.parent / 'Lossless.dll').stat().st_mtime_ns)
                self.next_check = now + CHECK_SECONDS
                self.observed = signature
                if signature != self.fingerprint:
                    self.pending = True
                if not self.pending:
                    self.status("current", "DXGI adapter layout is unchanged")
                    return
            else:
                signature = self.observed
                inventory = server.automation.gpu_router.detect()
            if not self.idle():
                self.status("deferred", "GPU index refresh waits until scaling, editing, tests, and updates are idle")
                return
            if (not config.lossless_control_configured or (
                    not config.disable_native_auto_scale and server.ls_settings.native_auto_scale_enabled() is not False)):
                self.status("deferred", "GPU index refresh requires Companion's native auto-scale control")
                return
            ui = executable.with_suffix('.dll')
            if hashlib.sha256(ui.read_bytes()).hexdigest() not in SUPPORTED_UI_HASHES:
                self.status("needs_validation", "This LS UI build needs local validation before automatic GPU remapping")
                self.process_instance, self.pending = instance, False
                self.fingerprint = signature
                return
            import ctypes
            library = ctypes.WinDLL(str(startup_guard.guard_path()), use_last_error=True)
            if not hasattr(library, "LSCLaunchHiddenWithReport"):
                raise RuntimeError("The installed startup guard lacks idle GPU probing; rebuild or update Companion")
            with server.automation._lock:
                if not self.idle():
                    return
                with server._gpu_activity_lock:
                    if server._active_requests:
                        return
                    server.state.gpu_mapping_in_progress = True
                try:
                    if not self.idle():
                        return
                    self.status("checking", "Reading LS GPU indexes while idle")
                    self._remap(executable, inventory)
                finally:
                    with server._gpu_activity_lock:
                        server.state.gpu_mapping_in_progress = False
            self.fingerprint, self.pending = signature, False
            self.process_instance = self.process_lookup(executable)
        except Exception as error:
            logger.exception("Idle GPU index refresh failed")
            self.status("error", str(error))
            self.retry_after = self.clock() + 300
            self.observed = None
            self.pending = True

    def _launch_probe(self, executable):
        settings = self.server.ls_settings
        if settings.native_auto_scale_enabled() is not False:
            # Do not bypass ProcessWatcher's native-auto-scale safety check.
            # Only write while the configured process is confirmed stopped.
            if self.process_lookup(executable) is not None:
                raise RuntimeError("LS must close before disabling native Auto Scale")
            if not self.server.profile_manager.config.disable_native_auto_scale:
                raise RuntimeError("Native Auto Scale must be disabled before idle GPU discovery")
            if not settings.disable_native_auto_scale() or settings.native_auto_scale_enabled() is not False:
                raise RuntimeError("Cannot confirm native Auto Scale is disabled; idle probe aborted")
        directory = adapter_report.report_directory(self.server.profile_manager.config_dir)
        directory.mkdir(parents=True, exist_ok=True)
        token = secrets.token_hex(32)
        started = time.time()
        pid = startup_guard.launch_hidden(executable, adapter_report_dir=directory, probe_token=token)
        if pid is None:
            raise RuntimeError("Could not launch idle LS adapter discovery")
        deadline = time.monotonic() + 9
        while time.monotonic() < deadline:
            if self.stopping.is_set():
                raise RuntimeError("GPU index refresh cancelled during shutdown")
            report = adapter_report.load_latest(directory, pid=pid, max_age_seconds=15, created_after=started - 2)
            instance = self.process_lookup(executable)
            if (report and report.get("requestId") == token and int(report.get("pid") or 0) == pid
                    and instance and instance[0] == pid and instance[1] >= started - 2
                    and report.get("lsAdapterNames")):
                return report
            self.stopping.wait(0.1)
        raise RuntimeError("No fresh PID-matched LS adapter list arrived; saved GPU selections were preserved")

    def _stop(self):
        if not self.idle(cleanup=True) or not self.server.process_watcher.stop_lossless_scaling():
            raise RuntimeError("LS must remain idle for GPU index refresh")

    def _remap(self, executable, inventory):
        server = self.server
        watcher, settings, config = server.process_watcher, server.ls_settings, server.profile_manager.config
        was_running = watcher.check_is_lossless_scaling_running()
        xml_backup = None
        recovery = settings.path.with_suffix(".xml.gpu-refresh-recovery")
        old_map = copy.deepcopy(config.ls_gpu_id_map)
        old_previous = copy.deepcopy(config.ls_gpu_id_map_previous)
        old_fingerprint = config.ls_gpu_map_fingerprint
        profile_backup = {p.id: dict(p.native_scaling_settings) for p in config.profiles}
        config_written = False
        try:
            # A previous interrupted transaction must be recovered before a new
            # map can be accepted. Never overwrite this recovery copy.
            if recovery.exists():
                self._stop()
                settings._write_tree(ET.ElementTree(ET.fromstring(recovery.read_bytes())), backup=False)
                recovery.unlink()
            if was_running:
                self._stop()
            report = self._launch_probe(executable)
            plan = mapping_plan(config, inventory, report, settings.read().get("profiles", []))
            if plan["xml"]:
                self._stop()
                xml_backup = settings.path.read_bytes()
                with recovery.open("xb") as backup:
                    backup.write(xml_backup)
                    backup.flush()
                    os.fsync(backup.fileno())
                # Read again after LS closes and saves its own settings.
                plan = mapping_plan(config, inventory, report, settings.read().get("profiles", []))
                settings.update_gpu_indexes(plan["xml"])
                confirmed = self._launch_probe(executable)
                if (confirmed["lsAdapterNames"] != report["lsAdapterNames"]
                        or snapshot_fingerprint(confirmed) != snapshot_fingerprint(report)):
                    raise RuntimeError("LS adapter ordering changed during refresh; remapping was rolled back")
            if self.stopping.is_set() or not self.idle(cleanup=True):
                raise RuntimeError("LS did not remain idle during GPU index refresh")
            if plan["mapping"]:
                # Replace aliases only for identities actually proven by LS;
                # keep unrelated/ambiguous mappings and selections untouched.
                hardware = {pci_ven_dev(key) for key in plan["mapping"]}
                new_map = {key: slot for key, slot in old_map.items() if pci_ven_dev(key) not in hardware}
                new_map.update(plan["mapping"])
                if new_map != old_map:
                    config.ls_gpu_id_map_previous = old_map
                config.ls_gpu_id_map = new_map
                config.ls_gpu_map_fingerprint = inventory_fingerprint(inventory)
                for profile in config.profiles:
                    if profile.id in plan["profiles"]:
                        profile.native_scaling_settings["PreferredGpuId"] = str(plan["profiles"][profile.id])
                server.profile_manager.save_config()
                config_written = True
            if recovery.exists():
                recovery.unlink()
            self.status("partial" if plan["skipped"] or not plan["mapping"] else "mapped_idle",
                        "GPU indexes checked while idle; ambiguous selections preserved" if plan["skipped"] or not plan["mapping"]
                        else "GPU profile indexes refreshed without starting scaling",
                        mappedGpus=len(plan["mapping"]), skipped=plan["skipped"])
        except Exception:
            config.ls_gpu_id_map, config.ls_gpu_id_map_previous = old_map, old_previous
            config.ls_gpu_map_fingerprint = old_fingerprint
            for profile in config.profiles:
                profile.native_scaling_settings = profile_backup[profile.id]
            if config_written:
                server.profile_manager.save_config()
            if xml_backup is not None:
                self._stop()
                settings._write_tree(ET.ElementTree(ET.fromstring(xml_backup)), backup=False)
                recovery.unlink(missing_ok=True)
            raise
        finally:
            # The probe must not leave a newly launched LS instance running if
            # the user had it closed. Restore a pre-existing idle host on failure.
            if not was_running:
                self._stop()
            elif not watcher.check_is_lossless_scaling_running():
                watcher.launch_lossless_scaling(force=True)
