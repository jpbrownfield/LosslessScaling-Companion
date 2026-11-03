"""Run the REAL application diagnostics (live Lossless Scaling behavior).

Unlike pytest (mocks/temp dirs), this boots the real companion stack and
executes DiagnosticRunner against the configured LosslessScaling.exe:
benchmark workload, hotkeys, deployments, log evidence. Minutes-long.

Usage:
    python run_live_diagnostics.py --list
    python run_live_diagnostics.py
    python run_live_diagnostics.py autoscale_behavior --no-elevate

Stop the tray Companion first; this takes the single-instance guard.
Copies the resulting ls-companion-diagnostics-*.zip to the repo root.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent
sys.path.insert(0, str(WORKSPACE))


def _ensure_admin(args) -> None:
    if os.name != "nt" or getattr(sys, "frozen", False):
        return
    if args.no_elevate or os.environ.get("LS_COMPANION_NO_ELEVATE") == "1":
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
        None, "runas", sys.executable, params, str(WORKSPACE), 1)
    if rc and int(rc) > 32:
        raise SystemExit(0)
    print("Elevation declined; live LS stops/deploys may fail.", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("test_id", nargs="?", default=None,
                        help="Single application test id (default: full suite).")
    parser.add_argument("--list", action="store_true",
                        help="List application test ids and exit (no live changes).")
    parser.add_argument("--no-elevate", action="store_true",
                        help="Skip the UAC elevation prompt.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    _ensure_admin(args)
    if os.environ.get("LOSSLESS_COMPANION_SIMULATION") == "1":
        print("Live diagnostics refuse simulation mode; unset LOSSLESS_COMPANION_SIMULATION.",
              flush=True)
        return 2

    from companion.main import CompanionApplication
    from companion.services.diagnostic_runner import DiagnosticRunner
    from companion.services.single_instance import SingleInstanceGuard

    guard = SingleInstanceGuard("companion")
    if not guard.acquire():
        holder = guard.holder_pid
        detail = f" (PID {holder})" if holder else ""
        print(f"Stop the running LS Companion tray instance first{detail}; exiting.",
              flush=True)
        return 2
    app = None
    try:
        from companion.services.test_binaries import sync as _sync_test_binaries

        _sync_test_binaries()
    except Exception as error:
        print(f"Test binary sync skipped ({error}); using build/native.", flush=True)

    try:
        # No gh CLI / network here: mirror the installed 0.5.5 tools into the
        # gitignored cache so source runs resolve the same binaries as CI.
        from companion.services.test_binaries import TEST_BINARIES_ROOT

        _installed = Path(r"C:\Program Files\LS Companion\_internal")
        _mirror = {
            "tools/LSBenchmark.exe": "tools/LSBenchmark.exe",
            "tools/LSC_StartupGuard.dll": "tools/LSC_StartupGuard.dll",
            "addons/LSP-ReShade/LSC_ReShadeBridge.dll": "addons/LSP-ReShade/LSC_ReShadeBridge.dll",
            "addons/LSP-ReShade/addon.json": "addons/LSP-ReShade/addon.json",
        }
        for _relative, _src_relative in _mirror.items():
            _dest = TEST_BINARIES_ROOT / _relative.replace("/", os.sep)
            _src = _installed / _src_relative.replace("/", os.sep)
            if not _dest.is_file() and _src.is_file():
                _dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(_src, _dest)
                print(f"Mirrored {_src.name} from installed Companion.", flush=True)
    except Exception as error:
        print(f"Installed-tool mirror skipped ({error}).", flush=True)

    try:
        import threading

        app = CompanionApplication()
        # app.start() is what normally starts these; headless runs must do it
        # explicitly or the hotkey/override tests fail with listenerRunning=False
        # and the foreground monitor never fires autoscale.
        app.running = True
        try:
            app.hotkey_listener.start()
        except Exception as error:
            print(f"Hotkey listener could not start: {error}", flush=True)
        app.monitor_thread = threading.Thread(
            target=app._run_process_monitor, daemon=True, name="MonitorThread")
        app.monitor_thread.start()
        runner = DiagnosticRunner(app.server)
        if args.list:
            for entry in runner.catalog():
                deps = ",".join(entry.get("dependencies", ()))
                print(f"{entry['id']}  [{deps}]  {entry['name']}", flush=True)
            return 0
        if args.test_id and args.test_id not in {e["id"] for e in runner.catalog()}:
            print(f"Unknown application test: {args.test_id}", flush=True)
            return 2
        print(f"Running live diagnostics"
              f"{f' [{args.test_id}]' if args.test_id else ' [full suite]'}...",
              flush=True)
        result = runner.run(args.test_id)
        counts = result.get("counts", {})
        for item in result.get("results", []):
            print(f"[{item.get('status', '?').upper()}] {item.get('id')}: "
                  f"{item.get('summary', '')}", flush=True)
        print(f"Result: {counts.get('pass', 0)} passed, "
              f"{counts.get('warn', 0)} warnings, {counts.get('fail', 0)} failed",
              flush=True)
        archive = (Path(app.profile_manager.config_dir)
                   / "diagnostics" / "application-tests" / result.get("archiveName", ""))
        if archive.is_file():
            dest = WORKSPACE / archive.name
            shutil.copy2(archive, dest)
            print(f"Archive: {dest}", flush=True)
        else:
            print(f"Session: {result.get('directory')}", flush=True)
        return 1 if counts.get("fail") else 0
    finally:
        try:
            if app is not None:
                app.running = False
                try:
                    app.hotkey_listener.stop()
                except Exception:
                    pass
                if app.monitor_thread is not None:
                    app.monitor_thread.join(timeout=3)
        except Exception:
            pass
        guard.release()


if __name__ == "__main__":
    raise SystemExit(main())
