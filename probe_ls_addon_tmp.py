"""One-shot live test: deploy Echo proxy host + reporter, read in-LS report, restore."""
import json
import os
import sys
import tempfile
import time
from pathlib import Path


def _ensure_admin() -> None:
    if os.name != "nt" or getattr(sys, "frozen", False):
        return
    if os.environ.get("LS_COMPANION_NO_ELEVATE") == "1":
        return
    try:
        import ctypes

        if ctypes.windll.shell32.IsUserAnAdmin():
            return
    except Exception:
        return
    script = os.path.abspath(__file__)
    params = " ".join([f'"{script}"'] + [f'"{a}"' for a in sys.argv[1:]])
    rc = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, params,
        str(Path(__file__).resolve().parent), 1)
    if rc and int(rc) > 32:
        raise SystemExit(0)
    print("Elevation declined.", flush=True)
    raise SystemExit(2)


if "--restore-only" not in sys.argv:
    _ensure_admin()

sys.path.insert(0, str(Path(__file__).resolve().parent))

_LOG = open(Path(tempfile.gettempdir()) / "ls-adapter-probe.log",
            "w", encoding="utf-8")


_builtin_print = print


def log(*parts, **kwargs):
    line = " ".join(str(p) for p in parts)
    _builtin_print(line, flush=True)
    _LOG.write(line + "\n")
    _LOG.flush()

from companion.core.profile_manager import ProfileManager
from companion.core.state import AppState
from companion.services.asset_store import AssetStore
from companion.services.deployment_manager import DeploymentManager
from companion.services.process_watcher import ProcessWatcher


def _noop(*args, **kwargs):
    pass


print = log  # tee all output to the probe log for headless elevated runs

LS = Path(r"J:\Steam\steamapps\common\Lossless Scaling")
PROXY_DLL = next(
    (Path.home() / "AppData/Local/LosslessScalingHelper/assets/packages"
     / "lossless-proxy").rglob("Lossless.dll"))
BUILT = Path("build/native/LSC-AdapterReport")
PROFILE_ID = "adapter-report-probe"


def run_scaled_session(watcher, deployment, started, original_hash):
    """Launch the benchmark workload and toggle real LS scaling, then stop.

    Returns "scaled" if the overlay confirmed, "unscaled" otherwise. The
    reporter's retry thread picks up the D3D device while LS renders.
    """
    import subprocess

    from companion.services.input_simulator import InputSimulator
    from companion.services.ls_inspector import LosslessScalingInspector
    from companion.services.ls_settings import LosslessSettingsXml
    from companion.services.performance_benchmark import (
        find_largest_window, focus_window)
    from companion.services.window_manager import MonitorWindowManager

    settings_path = (Path.home() / "AppData/Local/Lossless Scaling/Settings.xml")
    settings_backup = None
    workload = None
    was_active = False
    try:
        if settings_path.is_file():
            settings_backup = settings_path.read_bytes()
        exe = (Path("test_binaries/tools/LSBenchmark.exe").resolve()
               if (Path("test_binaries/tools/LSBenchmark.exe").is_file())
               else (Path("build/native/LSBenchmark.exe").resolve()))
        ls_settings = LosslessSettingsXml()
        try:
            data = ls_settings.read()
            titles = [str(p.get("Title") or "")
                      for p in data.get("profiles", []) if p.get("Title")]
        except Exception:
            titles = ["Default"]
        for title in titles or ["Default"]:
            try:
                ls_settings.update_profile(
                    title, {"PreferredGpuId": 1, "OutputDisplayId": 2},
                    backup=True)
                print(f"  pinned profile '{title}' to GPU 1 / display 2",
                      flush=True)
            except Exception as error:
                print(f"  route write skipped for '{title}': {error}", flush=True)
        workload = subprocess.Popen(
            [str(exe), "--width", "1280", "--height", "720",
             "--shader-load", "64", "--duration", "120"],
            cwd=str(exe.parent))
        hwnd = find_largest_window(workload.pid, timeout=20.0)
        moved = MonitorWindowManager().move_window_to_display(
            hwnd, r"\\.\DISPLAY48")
        print(f"  workload hwnd={hwnd} moved_to_48={moved}", flush=True)
        if not focus_window(hwnd):
            print("  WARNING: foreground not granted", flush=True)
        inspector = LosslessScalingInspector()
        if inspector.detect_lossless_scaling_overlay_window()[0]:
            print("  already scaling; leaving session alone", flush=True)
            return "scaled"
        if not InputSimulator.trigger_hotkey([], "f24", hold_ms=80):
            print("  hotkey injection failed", flush=True)
            return "unscaled"
        import time as _time
        deadline = _time.monotonic() + 15.0
        active = False
        while _time.monotonic() < deadline:
            active, _ = inspector.detect_lossless_scaling_overlay_window()
            if active:
                break
            _time.sleep(0.5)
        print(f"  scaling confirmed: {active}", flush=True)
        if active:
            was_active = True
            print("  holding scaled session for device capture...", flush=True)
            _time.sleep(20.0)
            return "scaled"
        return "unscaled"
    except Exception as error:
        print(f"  scaled session failed: {type(error).__name__}: {error}",
              flush=True)
        return "unscaled"
    finally:
        if was_active:
            try:
                InputSimulator.trigger_hotkey([], "f24", hold_ms=80)
            except Exception:
                pass
        try:
            if workload is not None and workload.poll() is None:
                workload.terminate()
        except Exception:
            pass
        if settings_backup is not None:
            try:
                settings_path.write_bytes(settings_backup)
                print("  Settings.xml restored from backup", flush=True)
            except Exception as error:
                print(f"  Settings restore failed: {error}", flush=True)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restore-only", action="store_true",
                        help="Only remove probe deployment files and verify "
                             "vanilla state (use after manually closing LS).")
    probe_args = parser.parse_args()
    manager = ProfileManager()
    store = AssetStore(
        manager.config.asset_store_path or str(manager.config_dir / "managed-assets"))
    deployment = DeploymentManager(store)
    watcher = ProcessWatcher(manager, AppState())
    original_hash = AssetStore.sha256(LS / "Lossless.dll")
    print(f"vanilla Lossless.dll: {original_hash[:16]}", flush=True)

    if probe_args.restore_only:
        print("restore-only mode: removing probe deployment...", flush=True)
        reporter_dir = LS / "addons" / "LSC-AdapterReport"
        restore(deployment, watcher, original_hash)
        import shutil

        shutil.rmtree(reporter_dir, ignore_errors=True)
        print("reporter dir removed:",
              str(not reporter_dir.exists()), flush=True)
        return 0

    print("stopping LS...", flush=True)
    watcher.stop_lossless_scaling()

    tmpdir = Path(tempfile.mkdtemp(prefix="adapter-report-"))
    vanilla_copy = tmpdir / "Lossless-original.dll"
    vanilla_copy.write_bytes((LS / "Lossless.dll").read_bytes())
    config_path = tmpdir / "config.json"
    config_path.write_text(json.dumps({
        "global": {"ui": {"open_on_start": False},
                   "updates": {"check": False}, "auto_load": True},
        "addons": {
            "LSC-AdapterReport": {"_enabled": True},
            "DLSS4DLAA": {"_enabled": False},
            "DLSS5NR01": {"_enabled": False},
            "ECHOFG": {"_enabled": False},
            "FSR3UPSC": {"_enabled": False},
            "VSRUPSC": {"_enabled": False},
            "XESSUPSC": {"_enabled": False},
        },
    }), encoding="utf-8")
    files = [
        {"relative_path": "Lossless.dll", "source_path": str(PROXY_DLL),
         "role": "addon_manager_host"},
        {"relative_path": "Lossless_original.dll",
         "source_path": str(vanilla_copy),
         "role": "lossless_original_engine"},
        {"relative_path": "addons/config.json", "source_path": str(config_path),
         "role": "addon_manager_config"},
        {"relative_path": "addons/LSC-AdapterReport/LSC_AdapterReport.dll",
         "source_path": str(BUILT / "LSC_AdapterReport.dll"),
         "role": "diagnostics"},
        {"relative_path": "addons/LSC-AdapterReport/addon.json",
         "source_path": str(BUILT / "addon.json"), "role": "diagnostics"},
    ]
    print("deploying proxy host + reporter...", flush=True)
    manifest = deployment.apply(
        profile_id=PROFILE_ID,
        lossless_scaling_exe=str(LS / "LosslessScaling.exe"), files=files)
    print("deployed:", len(manifest.get("files", [])), "files", flush=True)

    started = time.time()
    print("launching LS...", flush=True)
    if not watcher.launch_lossless_scaling(force=True):
        print("LAUNCH FAILED", flush=True)
        restore(deployment, watcher, original_hash)
        return 2
    report_dir = (Path.home() / "AppData/Local/LosslessScalingHelper/diagnostics")

    def read_report(path):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def fresh_reports():
        candidates = sorted(report_dir.glob("ls-adapter-report-*.json"),
                            key=lambda p: p.stat().st_mtime, reverse=True)
        return [p for p in candidates if p.stat().st_mtime >= started - 5]

    found = None
    deadline = time.time() + 40
    while time.time() < deadline and found is None:
        for path in fresh_reports():
            payload = read_report(path)
            if payload is not None:
                found = path
                break
        time.sleep(2)
    if found is None:
        print("NO REPORT APPEARED (addon did not initialize)", flush=True)
        restore(deployment, watcher, original_hash)
        return 1
    payload = read_report(found)
    if not payload.get("hasDevice"):
        scaled = run_scaled_session(watcher, deployment, started, original_hash)
        if scaled == "scaled":
            deadline = time.time() + 60
            while time.time() < deadline:
                for path in fresh_reports():
                    payload = read_report(path)
                    if payload and payload.get("hasDevice"):
                        found = path
                        break
                else:
                    time.sleep(2)
                    continue
                break
        payload = read_report(found) or payload
    device = payload.get("device") or {}
    print(f"REPORT: {found.name}", flush=True)
    print(f"  device: {device.get('description')} "
          f"VEN_{device.get('vendorId', 0):04X} DEV_{device.get('deviceId', 0):04X}",
          flush=True)
    rprof = [x for x in (payload.get('settingsProfiles') or [])
             if isinstance(x, dict)][:8]
    print(f"  settingsProfiles={rprof}", flush=True)
    print(f"  rootGpuPreference={payload.get('rootGpuPreference')}", flush=True)
    print(f"  deviceIndexAdapters={payload.get('deviceIndexAdapters')} "
          f"deviceIndexAdapters1={payload.get('deviceIndexAdapters1')}",
          flush=True)
    print(f"  enumAdapters={[i.get('description', '')[:22] for i in payload.get('enumAdapters', [])]}",
          flush=True)
    restore(deployment, watcher, original_hash)
    return 0


def restore(deployment, watcher, original_hash) -> None:
    print("restoring vanilla LS...", flush=True)
    try:
        watcher.stop_lossless_scaling()
    except Exception as error:
        print(f"stop failed: {error}", flush=True)
    deployment.apply(profile_id=None,
                     lossless_scaling_exe=str(LS / "LosslessScaling.exe"),
                     files=[])
    current = AssetStore.sha256(LS / "Lossless.dll")
    print(f"restored Lossless.dll: {current[:16]} "
          f"({'MATCHES vanilla' if current == original_hash else 'MISMATCH!'})",
          flush=True)
    print("Lossless_original.dll removed:",
          str(not (LS / "Lossless_original.dll").exists()), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
