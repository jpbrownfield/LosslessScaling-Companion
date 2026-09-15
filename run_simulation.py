"""Run LS Companion against local, unprivileged Windows simulator processes.

This creates an isolated fake Lossless Scaling installation and configuration
under ``.tmp/simulation``. It never reads or writes the user's real companion or
Lossless Scaling configuration.
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple

import psutil

from companion.core.models import Profile
from companion.core.profile_manager import ProfileManager


WORKSPACE = Path(__file__).resolve().parent
SIMULATION_ROOT = WORKSPACE / ".tmp" / "simulation"
INSTALL_ROOT = SIMULATION_ROOT / "lossless-scaling"
CONFIG_ROOT = SIMULATION_ROOT / "config"
SOURCE = WORKSPACE / "simulation" / "FakeApplication.cs"
LOSSLESS_EXE = INSTALL_ROOT / "LosslessScaling.exe"
TARGET_EXE = INSTALL_ROOT / "SimulationGame.exe"
SETTINGS_XML = INSTALL_ROOT / "Settings.xml"
COMPANION_LAUNCHER = WORKSPACE / "run_simulation.py"
WATCH_SUFFIXES = frozenset({".py", ".html", ".css", ".js", ".cs"})
IGNORED_PARTS = frozenset({"__pycache__", ".git", ".tmp", "build", "dist"})
FileState = Tuple[int, int]


def find_csharp_compiler() -> Path:
    windows = Path(os.environ.get("WINDIR", r"C:\Windows"))
    candidates = (
        windows / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe",
        windows / "Microsoft.NET" / "Framework" / "v4.0.30319" / "csc.exe",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError(
        "The Windows .NET Framework C# compiler was not found. "
        "Install/enable .NET Framework 4.x, which does not require Lossless Scaling."
    )


def build_simulators(force: bool = False) -> None:
    INSTALL_ROOT.mkdir(parents=True, exist_ok=True)
    compiler = find_csharp_compiler()
    for output in (LOSSLESS_EXE, TARGET_EXE):
        if not force and output.is_file() and output.stat().st_mtime_ns >= SOURCE.stat().st_mtime_ns:
            continue
        command = [
            str(compiler),
            "/nologo",
            "/target:winexe",
            "/reference:System.dll",
            "/reference:System.Drawing.dll",
            "/reference:System.Windows.Forms.dll",
            f"/out:{output}",
            str(SOURCE),
        ]
        completed = subprocess.run(command, cwd=str(WORKSPACE), check=False)
        if completed.returncode:
            raise RuntimeError(f"Could not build {output.name} (csc exit {completed.returncode})")


def prepare_configuration(reset: bool = False) -> None:
    if reset and CONFIG_ROOT.is_dir():
        shutil.rmtree(CONFIG_ROOT)
    CONFIG_ROOT.mkdir(parents=True, exist_ok=True)
    fixture = WORKSPACE / "tests" / "fixtures" / "lossless_scaling" / "Settings.xml"
    if reset or not SETTINGS_XML.is_file():
        shutil.copy2(fixture, SETTINGS_XML)
    (INSTALL_ROOT / "simulation.log").unlink(missing_ok=True)
    (INSTALL_ROOT / "simulation.command").unlink(missing_ok=True)
    original_engine = INSTALL_ROOT / "Lossless.dll"
    if not original_engine.exists():
        original_engine.write_bytes(b"LS Companion simulation engine placeholder\n")

    manager = ProfileManager(CONFIG_ROOT)
    config = manager.config
    config.port = 24893
    config.lossless_scaling_exe_path = str(LOSSLESS_EXE.resolve())
    config.lossless_settings_xml_path = str(SETTINGS_XML.resolve())
    config.asset_store_path = str((SIMULATION_ROOT / "managed-assets").resolve())
    config.auto_launch_lossless_scaling = False
    config.lossless_control_configured = False
    config.run_at_startup = False
    config.rtss_frame_limiting_enabled = False
    config.process_lasso_performance_mode_scaling = False
    simulation_profile = next(
        (item for item in config.profiles if item.id == "simulation-game"), None
    )
    if simulation_profile is None:
        simulation_profile = Profile(
            id="simulation-game",
            name="Simulation Game",
            target_process=TARGET_EXE.name,
            target_executable_path=str(TARGET_EXE.resolve()),
            auto_scale=False,
        )
        config.profiles.append(simulation_profile)
    else:
        simulation_profile.target_process = TARGET_EXE.name
        simulation_profile.target_executable_path = str(TARGET_EXE.resolve())
    config.active_profile_id = simulation_profile.id
    manager.save_config()


def iter_watched_files() -> Iterable[Path]:
    for root in (WORKSPACE / "companion", WORKSPACE / "simulation"):
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            relative_parts = path.relative_to(WORKSPACE).parts
            if any(part in IGNORED_PARTS for part in relative_parts):
                continue
            if path.is_file() and path.suffix.casefold() in WATCH_SUFFIXES:
                yield path
    yield COMPANION_LAUNCHER


def snapshot() -> Dict[str, FileState]:
    result: Dict[str, FileState] = {}
    for path in iter_watched_files():
        try:
            metadata = path.stat()
        except OSError:
            continue
        result[str(path)] = (metadata.st_mtime_ns, metadata.st_size)
    return result


def start_process(command: list[str], label: str) -> subprocess.Popen:
    print(f"[simulation] starting {label}", flush=True)
    return subprocess.Popen(
        command,
        cwd=str(WORKSPACE),
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )


def stop_process(process: Optional[subprocess.Popen], label: str) -> None:
    if process is None or process.poll() is not None:
        return
    print(f"[simulation] stopping {label} PID {process.pid}", flush=True)
    process.terminate()
    try:
        process.wait(timeout=4)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def stop_simulator_processes() -> None:
    """Stop only processes whose executable is one of this harness's exact outputs."""
    targets = {str(LOSSLESS_EXE.resolve()).casefold(), str(TARGET_EXE.resolve()).casefold()}
    matches = []
    for process in psutil.process_iter(["exe"]):
        try:
            executable = process.info.get("exe")
            if executable and str(Path(executable).resolve()).casefold() in targets:
                process.terminate()
                matches.append(process)
        except (OSError, psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    _, alive = psutil.wait_procs(matches, timeout=3)
    for process in alive:
        try:
            process.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass


def run_child() -> int:
    from companion.main import CompanionApplication

    os.environ["LOSSLESS_COMPANION_SIMULATION"] = "1"
    app = CompanionApplication(config_dir=CONFIG_ROOT)
    app.start()
    return 0


def run_self_test() -> int:
    from companion.services.ls_inspector import LosslessScalingInspector
    from companion.services.simulation_adapters import SimulationHotkeyTrigger

    build_simulators()
    prepare_configuration(reset=True)
    stop_simulator_processes()
    target = start_process([str(TARGET_EXE), "--target"], "fake target")
    lossless = start_process([str(LOSSLESS_EXE)], "fake Lossless Scaling")
    try:
        time.sleep(1.0)
        if psutil.Process(lossless.pid).name().casefold() != "losslessscaling.exe":
            raise RuntimeError("The fake runtime did not register as LosslessScaling.exe")
        trigger = SimulationHotkeyTrigger(INSTALL_ROOT / "simulation.command")
        if not trigger():
            raise RuntimeError("Could not send the simulation toggle command")
        inspector = LosslessScalingInspector(
            custom_log_path=str(INSTALL_ROOT / "simulation.log"),
            settings_xml_path=str(SETTINGS_XML),
            lossless_exe_path=str(LOSSLESS_EXE),
        )
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            overlay, _ = inspector.detect_lossless_scaling_overlay_window()
            if overlay:
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("The simulated scaling overlay was not detected")
        log_info = inspector.parse_log_updates()
        if not log_info or not log_info.is_active:
            raise RuntimeError("The simulated scaling-start log was not parsed")
        trigger()
        print("[simulation] self-test passed: process, command, overlay, and log inspection", flush=True)
        return 0
    finally:
        stop_process(lossless, "fake Lossless Scaling")
        stop_process(target, "fake target")
        stop_simulator_processes()


def run_reloader(poll_interval: float, debounce: float, reset: bool) -> int:
    build_simulators()
    prepare_configuration(reset=reset)
    stop_simulator_processes()
    state = snapshot()
    target = start_process([str(TARGET_EXE), "--target"], "fake target")
    lossless = start_process([str(LOSSLESS_EXE)], "fake Lossless Scaling")
    companion: Optional[subprocess.Popen] = None
    pending_since: Optional[float] = None
    pending_paths: set[str] = set()
    try:
        companion = start_process(
            [sys.executable, str(COMPANION_LAUNCHER), "--child"], "companion"
        )
        print("[simulation] dashboard: http://127.0.0.1:24893/dashboard", flush=True)
        while True:
            time.sleep(poll_interval)
            current = snapshot()
            changed = [path for path in set(state) | set(current) if state.get(path) != current.get(path)]
            state = current
            if changed:
                pending_paths.update(changed)
                pending_since = time.monotonic()
            if pending_since is not None and time.monotonic() - pending_since >= debounce:
                stop_process(companion, "companion")
                if any(Path(path).suffix.casefold() == ".cs" for path in pending_paths):
                    stop_process(lossless, "fake Lossless Scaling")
                    stop_process(target, "fake target")
                    build_simulators(force=True)
                    target = start_process([str(TARGET_EXE), "--target"], "fake target")
                    lossless = start_process([str(LOSSLESS_EXE)], "fake Lossless Scaling")
                companion = start_process(
                    [sys.executable, str(COMPANION_LAUNCHER), "--child"], "companion"
                )
                pending_since = None
                pending_paths.clear()
            if companion is not None and companion.poll() is not None and pending_since is None:
                print(f"[simulation] companion exited with code {companion.returncode}", flush=True)
                companion = None
    except KeyboardInterrupt:
        print("\n[simulation] shutting down", flush=True)
        return 0
    finally:
        stop_process(companion, "companion")
        stop_process(lossless, "fake Lossless Scaling")
        stop_process(target, "fake target")
        stop_simulator_processes()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the unprivileged LS Companion simulation.")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--self-test", action="store_true", help="Verify the simulator command and overlay, then exit.")
    parser.add_argument("--reset", action="store_true", help="Reset the isolated simulation settings.")
    parser.add_argument("--poll-interval", type=float, default=0.5)
    parser.add_argument("--debounce", type=float, default=0.6)
    args = parser.parse_args()
    if args.poll_interval <= 0 or args.debounce < 0:
        parser.error("--poll-interval must be positive and --debounce cannot be negative")
    return args


def main() -> int:
    args = parse_args()
    if args.child:
        return run_child()
    if args.self_test:
        return run_self_test()
    return run_reloader(args.poll_interval, args.debounce, args.reset)


if __name__ == "__main__":
    raise SystemExit(main())
