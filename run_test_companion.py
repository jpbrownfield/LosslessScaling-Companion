"""Launch the tray companion and reload it when development files change."""


from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple


WORKSPACE = Path(__file__).resolve().parent
COMPANION_LAUNCHER = WORKSPACE / "run_companion.py"
WATCH_SUFFIXES = frozenset({".py", ".html", ".css", ".js"})
IGNORED_PARTS = frozenset({"__pycache__", ".git", ".tmp", "build", "dist"})
FileState = Tuple[int, int]


def iter_watched_files(include_extension: bool = False) -> Iterable[Path]:
    roots = [WORKSPACE / "companion"]
    if include_extension:
        roots.append(WORKSPACE / "extension")
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            relative_parts = path.relative_to(WORKSPACE).parts
            if any(part in IGNORED_PARTS for part in relative_parts):
                continue
            if path.is_file() and path.suffix.casefold() in WATCH_SUFFIXES:
                yield path
    yield COMPANION_LAUNCHER


def snapshot(include_extension: bool = False) -> Dict[str, FileState]:
    result: Dict[str, FileState] = {}
    for path in iter_watched_files(include_extension):
        try:
            metadata = path.stat()
        except OSError:
            continue
        result[str(path)] = (metadata.st_mtime_ns, metadata.st_size)
    return result


def changed_paths(previous: Dict[str, FileState], current: Dict[str, FileState]) -> list[str]:
    return sorted(
        path
        for path in set(previous) | set(current)
        if previous.get(path) != current.get(path)
    )


def launch_companion() -> subprocess.Popen:
    if not COMPANION_LAUNCHER.is_file():
        raise FileNotFoundError(f"Companion launcher not found: {COMPANION_LAUNCHER}")
    environment = os.environ.copy()
    environment["LOSSLESS_COMPANION_DEV_RELOAD"] = "1"
    print("[reload] launching tray companion", flush=True)
    return subprocess.Popen(
        [sys.executable, str(COMPANION_LAUNCHER)],
        cwd=str(WORKSPACE),
        env=environment,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )


def stop_companion(process: Optional[subprocess.Popen], timeout: float = 5.0) -> None:
    if process is None or process.poll() is not None:
        return
    print(f"[reload] stopping companion PID {process.pid}", flush=True)
    try:
        if sys.platform == "win32":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            process.terminate()
    except (OSError, ValueError):
        process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"[reload] companion PID {process.pid} did not exit gracefully; terminating it", flush=True)
        process.terminate()
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            print(f"[reload] companion PID {process.pid} still did not exit; killing it", flush=True)
            process.kill()
            process.wait(timeout=timeout)


def run(poll_interval: float, debounce: float, include_extension: bool) -> int:
    state = snapshot(include_extension)
    child: Optional[subprocess.Popen] = None
    pending_since: Optional[float] = None
    pending_paths: set[str] = set()
    try:
        child = launch_companion()
        while True:
            time.sleep(poll_interval)
            current = snapshot(include_extension)
            changes = changed_paths(state, current)
            state = current
            if changes:
                pending_paths.update(changes)
                pending_since = time.monotonic()

            if pending_since is not None and time.monotonic() - pending_since >= debounce:
                shown = [str(Path(path).relative_to(WORKSPACE)) for path in sorted(pending_paths)]
                print(f"[reload] changed: {', '.join(shown)}", flush=True)
                stop_companion(child)
                child = launch_companion()
                pending_since = None
                pending_paths.clear()

            if child is not None and child.poll() is not None and pending_since is None:
                print(
                    f"[reload] companion exited with code {child.returncode}; "
                    "waiting for a source change",
                    flush=True,
                )
                child = None
    except KeyboardInterrupt:
        print("\n[reload] shutting down", flush=True)
        return 0
    finally:
        stop_companion(child)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the Lossless Scaling tray companion and reload it on source changes."
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=0.5,
        help="Seconds between source scans (default: 0.5).",
    )
    parser.add_argument(
        "--debounce",
        type=float,
        default=0.6,
        help="Quiet period before restarting after changes (default: 0.6).",
    )
    parser.add_argument(
        "--include-extension",
        action="store_true",
        help="Also restart the companion when Chrome extension JS/CSS/HTML changes.",
    )
    args = parser.parse_args()
    if args.poll_interval <= 0 or args.debounce < 0:
        parser.error("--poll-interval must be positive and --debounce cannot be negative")
    return args


def main() -> int:
    args = parse_args()
    return run(args.poll_interval, args.debounce, args.include_extension)


if __name__ == "__main__":
    raise SystemExit(main())
