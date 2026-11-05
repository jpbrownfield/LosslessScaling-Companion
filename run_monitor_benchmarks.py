"""PresentMon A/B across monitor/GPU configs.

For each config: stop LS, pin the LS GPU route, launch LS, launch the
LSBenchmark workload, move its window to the config's display, engage LS
scaling on the config's GPU, capture PresentMon, then summarize displayed FPS.

Configs isolate same-GPU vs cross-GPU presentation:
  3090Ti-same-gpu : window DISPLAY44 + LS GPU 1 (same adapter)
  5050-same-gpu   : window DISPLAY48 + LS GPU 2 (same adapter)
  cross-gpu       : window DISPLAY48 + LS GPU 1 (old broken split)

Usage:
    python run_monitor_benchmarks.py --duration 20
    python run_monitor_benchmarks.py --duration 20 --config 5050-same-gpu

Restores the pinned route (GPU 2 / display 2) when finished. Requires
elevation (PresentMon ETW); re-launches itself via UAC unless --no-elevate.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent
sys.path.insert(0, str(WORKSPACE))

CONFIGS = [
    {"name": "3090Ti-same-gpu", "display": r"\\.\DISPLAY44", "gpu": 1, "ls_display": 1},
    {"name": "5050-same-gpu", "display": r"\\.\DISPLAY48", "gpu": 2, "ls_display": 2},
    {"name": "cross-gpu", "display": r"\\.\DISPLAY48", "gpu": 1, "ls_display": 1},
    {"name": "window44-ls5050", "display": r"\\.\DISPLAY44", "gpu": 2, "ls_display": 2},
    {"name": "gpu1-disp2", "display": r"\\.\DISPLAY48", "gpu": 1, "ls_display": 2},
    {"name": "gpu2-disp1", "display": r"\\.\DISPLAY48", "gpu": 2, "ls_display": 1},
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, default=20.0)
    parser.add_argument("--warmup", type=float, default=5.0)
    parser.add_argument("--config", action="append", default=[],
                        help="Run only this config name (repeatable)")
    parser.add_argument("--calibrate", action="store_true",
                        help="Probe every LS GPU ID on each display, save the "
                             "winning PCI->LS-ID map to settings, then exit")
    parser.add_argument("--calibrate-duration", type=float, default=8.0)
    parser.add_argument("--overlay-live", action="store_true",
                        help="Drive a real hotkey on/off scaling cycle and "
                             "verify the on-screen indicator sequence "
                             "(no PresentMon, no admin needed)")
    parser.add_argument("--overlay-log", default=None,
                        help="JSONL tap file for overlay events "
                             "(defaults next to the report dir)")
    parser.add_argument("--no-elevate", action="store_true")
    return parser.parse_args()


def ensure_admin(args) -> None:
    if os.name != "nt" or getattr(sys, "frozen", False):
        return
    if args.no_elevate or os.environ.get("LS_COMPANION_NO_ELEVATE") == "1":
        return
    import ctypes

    try:
        if ctypes.windll.shell32.IsUserAnAdmin():
            return
    except Exception:
        return
    params = " ".join([f'"{os.path.abspath(__file__)}"']
                      + [f'"{a}"' for a in sys.argv[1:]])
    rc = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, params, str(WORKSPACE), 1)
    if rc and int(rc) > 32:
        raise SystemExit(0)
    print("Elevation declined; PresentMon capture needs admin.", flush=True)
    raise SystemExit(2)


def run_config(config, *, duration, warmup, report_root) -> dict:
    from scripts.benchmark_lossless_scaling import (
        resolve_benchmark_executable, resolve_presentmon,
    )
    from companion.core.profile_manager import ProfileManager
    from companion.core.state import AppState
    from companion.services.automation import AutomationController
    from companion.services.benchmark_metrics import summarize_presentmon_files
    from companion.services.ls_inspector import LosslessScalingInspector
    from companion.services.performance_benchmark import (
        PresentMonCapture, GpuSampleRecorder, find_largest_window,
        focus_window, find_process_id,
    )
    from companion.services.process_watcher import ProcessWatcher

    name = config["name"]
    run_dir = report_root / name
    run_dir.mkdir(parents=True, exist_ok=True)

    manager = ProfileManager()
    state = AppState()
    watcher = ProcessWatcher(manager, state)
    inspector = LosslessScalingInspector(
        settings_xml_path=manager.config.lossless_settings_xml_path)
    automation = AutomationController(
        manager, state, process_watcher=watcher,
        scaling_state_probe=lambda: inspector.detect_lossless_scaling_overlay_window()[0],
    )
    workload = None
    capture = None
    recorder = None
    summary = {"config": name, "status": "fail"}
    try:
        from companion.services.test_binaries import candidate as test_binary

        local = test_binary("tools/LSBenchmark.exe")
        if local.is_file():
            benchmark_exe = local.resolve()
            bundled_workload = True
        else:
            benchmark_exe, bundled_workload = resolve_benchmark_executable(None)
        presentmon_exe = resolve_presentmon(
            type("Args", (), {"presentmon_exe": None, "download_presentmon": True})(),
            manager,
        )

        watcher.stop_lossless_scaling()
        route_needed = automation.lossless_settings.gpu_route_changes_required(
            "Default", config["gpu"], config["ls_display"])
        if route_needed is None:
            raise RuntimeError("LS GPU route could not be read for this config")
        if route_needed and not automation.lossless_settings.update_gpu_route(
                "Default", config["gpu"], config["ls_display"]):
            raise RuntimeError("LS GPU route could not be pinned for this config")
        if not watcher.launch_lossless_scaling(force=True):
            raise RuntimeError("Lossless Scaling could not be launched")
        time.sleep(2.0)

        workload = subprocess.Popen(
            [str(benchmark_exe), "--width", "1280", "--height", "720",
             "--shader-load", "64", "--duration", str(int(duration + 60))],
            cwd=benchmark_exe.parent,
        )
        hwnd = find_largest_window(workload.pid, timeout=20.0)
        if not automation.window_manager.move_window_to_display(hwnd, config["display"]):
            raise RuntimeError(f"Benchmark window could not move to {config['display']}")
        # Re-resolve: some engines recreate the window on display change.
        time.sleep(1.0)
        moved_hwnd = find_largest_window(workload.pid, timeout=10.0)
        hwnd = moved_hwnd or hwnd
        if not focus_window(hwnd):
            raise RuntimeError("Windows did not grant the benchmark foreground ownership")

        profile = manager.config.profiles[0].model_copy(deep=True)
        profile.id = f"monitor-benchmark-{name}"
        profile.name = f"Monitor benchmark {name}"
        profile.lossless_profile_title = "Default"
        # No explicit PreferredGpuId/OutputDisplayId here: the LS route is
        # pinned directly in Settings.xml above, and explicit profile numbers
        # would make set_scaling relocate the window we just placed.
        if not automation.set_scaling(
            True, profile, reason="monitor_benchmark", force=True,
            target_pid=workload.pid, target_hwnd=hwnd,
            park_runtime_on_stop=False,
        ):
            raise RuntimeError("Lossless Scaling did not confirm activation")
        time.sleep(warmup)

        lossless_pid = find_process_id("LosslessScaling.exe")
        capture = PresentMonCapture(presentmon_exe, run_dir)
        capture.start(["LSBenchmark.exe"] + (["LosslessScaling.exe"] if lossless_pid else []),
                      duration)
        recorder = GpuSampleRecorder(
            [workload.pid] + ([lossless_pid] if lossless_pid else []))
        recorder.start()
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            if workload.poll() is not None:
                raise RuntimeError("Benchmark workload exited during capture")
            time.sleep(0.5)
        recorder.stop()
        recorder = None
        capture.wait(timeout=duration + 30)
        capture = None

        summaries = summarize_presentmon_files(capture_csvs(run_dir), warmup_seconds=0.0)
        summary.update({
            "status": "pass",
            "display": config["display"],
            "ls_gpu": config["gpu"],
            "ls_display": config["ls_display"],
            "summaries": [
                {"application": s["application"], "process_id": s["process_id"],
                 "rows": s["rows"], "displayed_fps": s["metrics"].get("displayed_fps")}
                for s in summaries
            ],
        })
    except Exception as error:
        summary["error"] = str(error)
    finally:
        try:
            if recorder is not None:
                recorder.stop()
        except Exception:
            pass
        try:
            if capture is not None:
                capture.stop()
        except Exception:
            pass
        try:
            automation.set_scaling(False, reason="monitor_benchmark_done", force=True)
        except Exception:
            pass
        try:
            if workload is not None and workload.poll() is None:
                workload.terminate()
        except Exception:
            pass
        try:
            watcher.stop_lossless_scaling()
        except Exception:
            pass
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def capture_csvs(run_dir: Path):
    return sorted(run_dir.glob("presentmon*.csv"))


def restore_pinned_route() -> None:
    """Rewrite LS Settings.xml from the PCI-anchored pin via the calibrated map."""
    from companion.core.profile_manager import ProfileManager
    from companion.services.gpu_router import GpuRouter
    from companion.services.ls_settings import LosslessSettingsXml

    manager = ProfileManager()
    router = GpuRouter(
        ls_gpu_id_map=dict(getattr(manager.config, "ls_gpu_id_map", None) or {}))
    pin_id = manager.config.preferred_scaling_gpu_device_id
    pin_name = manager.config.preferred_scaling_gpu_name
    route = router.route_for_gpu(pin_id, pin_name)
    inventory = router.detect()
    target = next(
        (d for d in inventory["displays"]
         if pin_id and str(d.get("gpuDeviceId") or "").casefold() == pin_id.casefold()),
        None,
    ) or next((d for d in inventory["displays"] if d.get("primary")),
                inventory["displays"][0] if inventory["displays"] else None)
    if target is None:
        raise RuntimeError("no displays detected")
    LosslessSettingsXml().update_gpu_route(
        "Default", int(route["lsGpuId"]), int(target["lsDisplayId"]))
    print(f"Restored pinned route GPU {route['lsGpuId']} / "
          f"display {target['lsDisplayId']}.", flush=True)


def calibrate(report_root: Path, duration: float) -> int:
    """Probe every LS (GPU, display) pair per monitor; persist the winners."""
    import re

    from companion.core.profile_manager import ProfileManager
    from companion.services.gpu_router import GpuRouter, inventory_fingerprint

    router = GpuRouter()
    inventory = router.detect()
    displays = [d for d in inventory["displays"]]
    gpu_count = max(1, len(inventory["gpus"]))
    # Static wiring first: each target display is probed with its own GDI
    # display ID before any other LS output ID, since the window's monitor
    # is physically wired and GDI-enumerated.
    base_ids = list(range(1, len(displays) + 2))
    manager = ProfileManager()
    gpu_mapping: dict = dict(getattr(manager.config, "ls_gpu_id_map", None) or {})
    disp_mapping: dict = dict(
        getattr(manager.config, "ls_display_id_map", None) or {})
    for index, display in enumerate(displays):
        device = display["deviceName"]
        display_gpu = display["gpuDeviceId"]
        tag = re.sub(r"[^A-Za-z0-9]+", "_", device).strip("_")
        display_ids = [index + 1] + [i for i in base_ids if i != index + 1]
        print(f"--- calibrating {device} ({display['name']}) ---", flush=True)
        best_fps, best_gpu, best_disp = -1.0, 0, 0
        for ls_id in range(1, gpu_count + 1):
            for ls_disp in display_ids:
                config = {"name": f"cal-{tag}-{ls_id}x{ls_disp}",
                          "display": device, "gpu": ls_id,
                          "ls_display": ls_disp}
                summary = run_config(config, duration=duration, warmup=2.0,
                                     report_root=report_root)
                fps = -1.0
                if summary["status"] == "pass":
                    for item in summary["summaries"]:
                        if "LosslessScaling" in str(item["application"]):
                            median = (item["displayed_fps"] or {}).get("median")
                            if isinstance(median, (int, float)):
                                fps = max(fps, float(median))
                print(f"  LS GPU {ls_id} display {ls_disp}: "
                      f"median LS fps={fps}", flush=True)
                if fps > best_fps:
                    best_fps, best_gpu, best_disp = fps, ls_id, ls_disp
        if best_gpu and best_fps > 0:
            gpu_mapping[display_gpu] = best_gpu
            disp_mapping[device] = best_disp
            print(f"  winner: LS GPU {best_gpu} display {best_disp} "
                  f"-> {device}", flush=True)
        else:
            print(f"  no working LS route for {device}; left unmapped",
                  flush=True)
    manager.config.ls_gpu_id_map_previous = dict(
        getattr(manager.config, "ls_gpu_id_map", None) or {})
    manager.config.ls_gpu_id_map = gpu_mapping
    manager.config.ls_display_id_map_previous = dict(
        getattr(manager.config, "ls_display_id_map", None) or {})
    manager.config.ls_display_id_map = disp_mapping
    manager.config.ls_gpu_map_fingerprint = inventory_fingerprint(
        router.detect())
    manager.save_config()
    print(f"Saved ls_gpu_id_map: {gpu_mapping} "
          f"ls_display_id_map: {disp_mapping} "
          f"(fingerprint {manager.config.ls_gpu_map_fingerprint})", flush=True)
    from companion.core.state import AppState
    from companion.services.automation import AutomationController

    try:
        calibrator = AutomationController(manager, AppState())
        migrated = calibrator._migrate_ls_profile_routes()
        migrated.update(calibrator._migrate_stored_routes())
        print(f"Migrated stored routes: {migrated}", flush=True)
    except Exception as error:
        print(f"Stored-route migration skipped ({error}).", flush=True)
    check = AutomationController(ProfileManager(), AppState())
    route = check._desired_gpu_route(None, None)
    print(f"Pin now resolves to LS GPU {route.get('lsGpuId')} "
          f"({route.get('gpuName')})", flush=True)
    return 0


def run_overlay_live(report_root: Path, tap_path: Path,
                     duration: float = 8.0) -> int:
    """End-to-end overlay verification with a real scaling session.

    Hotkey scaling on, hold while the pill + maroon border are visible, then
    hotkey off. Asserts the tap log shows detection -> hotkey -> started ->
    active border -> stopped -> border cleared. Needs no PresentMon/admin.
    """
    import subprocess

    from companion.core.profile_manager import ProfileManager
    from companion.core.state import AppState
    from companion.services.automation import AutomationController
    from companion.services.ls_inspector import LosslessScalingInspector
    from companion.services.performance_benchmark import (
        find_largest_window, focus_window)
    from companion.services.process_watcher import ProcessWatcher
    from companion.services.test_binaries import candidate as test_binary
    from companion.services.window_manager import MonitorWindowManager

    os.environ["LS_OVERLAY_RECORD"] = str(tap_path)
    manager = ProfileManager()
    if not manager.config.override_lossless_hotkey:
        print("overlay-live needs the override hotkey enabled.", flush=True)
        return 2
    state = AppState()
    watcher = ProcessWatcher(manager, state)
    inspector = LosslessScalingInspector(
        settings_xml_path=manager.config.lossless_settings_xml_path)
    automation = AutomationController(
        manager, state, process_watcher=watcher,
        scaling_state_probe=lambda: inspector.detect_lossless_scaling_overlay_window()[0],
    )
    exe = test_binary("tools/LSBenchmark.exe")
    if not exe.is_file():
        print("LSBenchmark.exe not found.", flush=True)
        return 2
    try:
        Path(tap_path).unlink()
    except OSError:
        pass
    settings_path = (Path.home()
                     / "AppData/Local/Lossless Scaling/Settings.xml")
    settings_backup = (settings_path.read_bytes()
                       if settings_path.is_file() else None)
    workload = None
    try:
        if watcher.check_is_lossless_scaling_running():
            print("stopping running Lossless Scaling...", flush=True)
            watcher.stop_lossless_scaling()
        if not watcher.launch_lossless_scaling(force=True):
            print("Could not launch Lossless Scaling.", flush=True)
            return 2
        time.sleep(2.0)
        workload = subprocess.Popen(
            [str(exe), "--width", "1280", "--height", "720",
             "--shader-load", "64", "--duration", "120"],
            cwd=str(exe.parent))
        hwnd = find_largest_window(workload.pid, timeout=20.0)
        MonitorWindowManager().move_window_to_display(hwnd, r"\\.\DISPLAY48")
        time.sleep(1.0)
        hwnd = find_largest_window(workload.pid, timeout=10.0) or hwnd
        if not focus_window(hwnd):
            print("WARNING: benchmark foreground ownership denied.", flush=True)
        print("overlay-live: hotkey ON (expect armed -> hotkey -> started)...",
              flush=True)
        try:
            automation.handle_override_hotkey()
        except Exception as error:
            print(f"hotkey ON failed: {type(error).__name__}: {error}",
                  flush=True)
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            if workload.poll() is not None:
                print("workload exited early", flush=True)
                return 1
            time.sleep(0.5)
        if not automation.state.is_scaling_active:
            print("scaling did not engage; not toggling blindly", flush=True)
            return 1
        print(f"overlay-live: holding scaled session {duration:.0f}s...",
              flush=True)
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            if workload.poll() is not None:
                print("workload exited early", flush=True)
                return 1
            time.sleep(0.5)
        print("overlay-live: hotkey OFF...", flush=True)
        try:
            automation.handle_override_hotkey()
        except Exception as error:
            print(f"hotkey OFF failed: {type(error).__name__}: {error}",
                  flush=True)
        time.sleep(3.0)
    finally:
        try:
            if workload is not None and workload.poll() is None:
                workload.terminate()
        except Exception:
            pass
        try:
            watcher.stop_lossless_scaling()
        except Exception:
            pass
        if settings_backup is not None:
            try:
                settings_path.write_bytes(settings_backup)
                print("Settings.xml restored from backup.", flush=True)
            except Exception as error:
                print(f"Settings restore failed: {error}", flush=True)
    return check_overlay_tap(tap_path)


def check_overlay_tap(tap_path: Path) -> int:
    """Assert the expected live overlay behavior from a JSONL tap file."""
    import json as _json

    def load_frames():
        try:
            with open(tap_path, encoding="utf-8") as stream:
                for raw in stream:
                    try:
                        yield _json.loads(raw).get("payload")
                    except ValueError:
                        continue
        except OSError as error:
            print(f"tap log unreadable: {error}", flush=True)
            return

    frames = [payload for payload in load_frames()]
    if not frames:
        print("tap log is empty", flush=True)
        return 2

    def has_line(prefix: str) -> bool:
        return any(
            str(item.get("text") or "").startswith(prefix)
            for payload in frames if isinstance(payload, dict)
            for item in payload.get("lines", []))

    def border_seen(state: str) -> bool:
        return any(isinstance(payload, dict)
                   and payload.get("border") == state
                   for payload in frames)

    failures = []

    def require(desc: str, ok: bool) -> None:
        print(("PASS " if ok else "FAIL ") + desc, flush=True)
        if not ok:
            failures.append(desc)

    stopped_frames = [
        index for index, payload in enumerate(frames)
        if isinstance(payload, dict) and any(
            str(item.get("text") or "").startswith("Scaling stopped")
            for item in payload.get("lines", []))]
    cleared_after_stop = (
        any(not (isinstance(payload, dict) and payload.get("border"))
            for payload in frames[max(stopped_frames):])
        if stopped_frames else False)

    require("hotkey flash shown on start",
            has_line("Hotkey received: starting scaling"))
    require("game window detected + profile configured",
            has_line("Game window detected")
            and has_line("Configuring for profile"))
    require("scaling start confirmed", has_line("Scaling started"))
    require("blue pending border shown", border_seen("pending"))
    require("maroon border while scaling", border_seen("active"))
    require("second hotkey stops scaling with a stop line",
            has_line("Hotkey received: stopping scaling")
            and bool(stopped_frames))
    require("border cleared after stop", cleared_after_stop)
    ok = not failures
    print("OVERLAY LIVE TEST: " + ("PASS" if ok else "FAIL"), flush=True)
    return 0 if ok else 1


def main() -> int:
    args = parse_args()
    ensure_admin(args)
    selected = [c for c in CONFIGS if not args.config or c["name"] in args.config]
    if args.config and len(selected) != len(args.config):
        print(f"Unknown --config; choices: {[c['name'] for c in CONFIGS]}", flush=True)
        return 2
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    report_root = (WORKSPACE / "diagnostics" / "performance-benchmark"
                   / f"monitor-ab-{stamp}")
    report_root.mkdir(parents=True, exist_ok=True)
    (WORKSPACE / "diagnostics" / "performance-benchmark"
     / "monitor-ab-latest.txt").write_text(str(report_root), encoding="utf-8")
    if args.calibrate:
        try:
            return calibrate(report_root, args.calibrate_duration)
        finally:
            try:
                restore_pinned_route()
            except Exception as error:
                print(f"Route restore skipped ({error}).", flush=True)
    if args.overlay_live:
        tap_path = (Path(args.overlay_log) if args.overlay_log
                    else report_root / "overlay-tap.jsonl")
        return run_overlay_live(report_root, tap_path)
    results = []
    for config in selected:
        print(f"--- {config['name']}: window {config['display']}, "
              f"LS GPU {config['gpu']} display {config['ls_display']} ---", flush=True)
        summary = run_config(config, duration=args.duration,
                             warmup=args.warmup, report_root=report_root)
        results.append(summary)
        if summary["status"] == "pass":
            for item in summary["summaries"]:
                fps = item["displayed_fps"] or {}
                print(f"  {item['application']}: avg={fps.get('average')} "
                      f"median={fps.get('median')} 1%low={fps.get('one_percent_low')} "
                      f"(n={item['rows']})", flush=True)
        else:
            print(f"  FAILED: {summary.get('error')}", flush=True)

    try:
        restore_pinned_route()
    except Exception as error:
        print(f"Route restore skipped ({error}).", flush=True)
    print(f"Reports: {report_root}", flush=True)
    return 0 if all(r["status"] == "pass" for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
