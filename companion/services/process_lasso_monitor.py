"""Tail Process Lasso's CSV action log for Performance Mode transitions."""

from __future__ import annotations

import csv
import logging
import os
import re
import winreg
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

import psutil

logger = logging.getLogger("LSCompanion.ProcessLasso")

_PROCESS_RE = re.compile(r"(?i)([a-z0-9_.() -]+\.exe)\b")
_PID_RE = re.compile(r"(?i)\bpid\s*[:=#]?\s*(\d+)\b")
_START_RE = re.compile(
    r"(?i)(?:performance\s+mode.{0,80}\b(?:enter|engag|start|activat|induc|enabl|on)\w*\b|"
    r"\b(?:enter|engag|start|activat|induc|enabl)\w*\b.{0,80}performance\s+mode)"
)
_STOP_RE = re.compile(
    r"(?i)(?:performance\s+mode.{0,80}\b(?:exit|disengag|stop|deactivat|disabl|end|off)\w*\b|"
    r"\b(?:exit|disengag|stop|deactivat|disabl|end)\w*\b.{0,80}performance\s+mode)"
)
_IGNORED_PROCESSES = {"processlasso.exe", "processgovernor.exe", "bitsumsessionagent.exe"}
_LOG_FOLDER_ARGUMENT_RE = re.compile(
    r'(?i)(?:^|\s)[/-]logfolder\s*=\s*(?:"([^"]+)"|(\S+))'
)


@dataclass(frozen=True)
class PerformanceModeEvent:
    active: bool
    process_name: str
    pid: Optional[int]
    raw_line: str

    @property
    def key(self) -> str:
        return str(self.pid) if self.pid is not None else self.process_name.casefold()


def parse_performance_mode_line(line: str) -> Optional[PerformanceModeEvent]:
    """Parse one CSV record, tolerating Process Lasso version/localization layout changes."""
    try:
        fields = next(csv.reader([line]))
    except (csv.Error, StopIteration):
        fields = [line]
    text = " | ".join(field.strip() for field in fields)
    if "performance mode" not in text.casefold():
        return None

    # Test stop first because phrases such as "disabled/ended" can contain other
    # contextual verbs describing when the mode originally started.
    if _STOP_RE.search(text):
        active = False
    elif _START_RE.search(text):
        active = True
    else:
        return None

    candidates = []
    candidate_field_indexes = []
    for index, field in enumerate(fields):
        for match in _PROCESS_RE.finditer(field):
            name = Path(match.group(1).strip()).name
            if name.casefold() not in _IGNORED_PROCESSES:
                candidates.append(name)
                candidate_field_indexes.append(index)
    if not candidates:
        return None

    pid_match = _PID_RE.search(text)
    pid = int(pid_match.group(1)) if pid_match else None
    if pid is None:
        # Some CSV layouts place PID in a field adjacent to the executable.
        index = candidate_field_indexes[0]
        for adjacent in (index + 1, index - 1):
            if 0 <= adjacent < len(fields) and fields[adjacent].strip().isdigit():
                value = int(fields[adjacent].strip())
                if value > 0:
                    pid = value
                    break

    return PerformanceModeEvent(active, candidates[0], pid, line.rstrip())


class ProcessLassoLogTailer:
    """Non-blocking tailer that attaches at EOF and handles truncation/replacement."""

    def __init__(self, on_event: Callable[[PerformanceModeEvent], None]):
        self.on_event = on_event
        self.path: Optional[Path] = None
        self.offset = 0
        self.pending = ""
        self.encoding = "utf-8"
        self.file_identity = None

    @staticmethod
    def _paths_for_log_location(location: str) -> Iterable[Path]:
        expanded = Path(os.path.expandvars(location.strip().strip('"'))).expanduser()
        if expanded.name.casefold().startswith("prolasso.log"):
            yield expanded
            return
        yield expanded / "prolasso.log"
        yield expanded / "logs" / "prolasso.log"

    @classmethod
    def runtime_paths(cls) -> Iterable[Path]:
        """Read the effective /LogFolder override from running Process Lasso components."""
        for process in psutil.process_iter(["name", "cmdline", "exe"]):
            try:
                name = (process.info.get("name") or "").casefold()
                if name not in {"processlasso.exe", "processgovernor.exe"}:
                    continue
                arguments = process.info.get("cmdline") or []
                explicit_folder = next(
                    (
                        argument.split("=", 1)[1].strip('"')
                        for argument in arguments
                        if argument.casefold().startswith(("/logfolder=", "-logfolder="))
                    ),
                    None,
                )
                if explicit_folder:
                    yield from cls._paths_for_log_location(explicit_folder)
                command_line = " ".join(arguments)
                match = _LOG_FOLDER_ARGUMENT_RE.search(command_line)
                if match and not explicit_folder:
                    yield from cls._paths_for_log_location(match.group(1) or match.group(2))
                executable = process.info.get("exe")
                if executable:
                    yield from cls._paths_for_log_location(str(Path(executable).parent))
            except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
                continue

    @classmethod
    def registry_paths(cls) -> Iterable[Path]:
        """Read Process Lasso's configured LogFolder from per-machine/user registry views."""
        views = [0]
        for view in (getattr(winreg, "KEY_WOW64_64KEY", 0), getattr(winreg, "KEY_WOW64_32KEY", 0)):
            if view and view not in views:
                views.append(view)
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for view in views:
                try:
                    with winreg.OpenKey(
                        hive,
                        r"Software\ProcessLasso",
                        0,
                        winreg.KEY_READ | view,
                    ) as key:
                        value, _kind = winreg.QueryValueEx(key, "LogFolder")
                except OSError:
                    continue
                if isinstance(value, str) and value.strip():
                    yield from cls._paths_for_log_location(value)

    @classmethod
    def candidate_paths(cls) -> Iterable[Path]:
        seen = set()
        discovered = list(cls.runtime_paths()) + list(cls.registry_paths())
        for root in (
            os.environ.get("APPDATA"),
            os.environ.get("LOCALAPPDATA"),
            os.environ.get("PROGRAMDATA"),
        ):
            if not root:
                continue
            for folder in ("ProcessLasso", "Process Lasso"):
                for child in ("prolasso.log", str(Path("logs") / "prolasso.log")):
                    discovered.append(Path(root) / folder / child)
        for path in discovered:
            key = str(path).casefold()
            if key not in seen:
                seen.add(key)
                yield path

    @classmethod
    def resolve_path(cls, configured_path: Optional[str]) -> Optional[Path]:
        if configured_path and configured_path.strip():
            candidates = list(cls._paths_for_log_location(configured_path))
            return next((path for path in candidates if path.is_file()), candidates[0])
        return next((path for path in cls.candidate_paths() if path.is_file()), None)

    def reset(self) -> None:
        self.path = None
        self.offset = 0
        self.pending = ""
        self.encoding = "utf-8"
        self.file_identity = None

    def poll(self, configured_path: Optional[str]) -> int:
        selected = self.resolve_path(configured_path)
        if selected is None or not selected.is_file():
            self.reset()
            return 0

        if selected != self.path:
            self.path = selected
            self.pending = ""
            with selected.open("rb") as stream:
                prefix = stream.read(4)
                if prefix.startswith(b"\xff\xfe"):
                    self.encoding = "utf-16-le"
                elif prefix.startswith(b"\xfe\xff"):
                    self.encoding = "utf-16-be"
                else:
                    self.encoding = "utf-8"
                stream.seek(0, os.SEEK_END)
                self.offset = stream.tell()
            stat = selected.stat()
            self.file_identity = (stat.st_dev, stat.st_ino)
            logger.info("Tailing Process Lasso log from new entries: %s", selected)
            return 0

        stat = selected.stat()
        identity = (stat.st_dev, stat.st_ino)
        replaced = self.file_identity is not None and identity != self.file_identity
        self.file_identity = identity
        size = stat.st_size
        if replaced or size < self.offset:
            logger.info("Process Lasso log was replaced or truncated: %s", selected)
            self.offset = 0
            self.pending = ""
        if size == self.offset:
            return 0

        with selected.open("rb") as stream:
            stream.seek(self.offset)
            chunk = stream.read()
        self.offset += len(chunk)
        data = self.pending + chunk.decode(self.encoding, errors="replace").lstrip("\ufeff")
        lines = data.splitlines(keepends=True)
        self.pending = ""
        if lines and not lines[-1].endswith(("\n", "\r")):
            self.pending = lines.pop()

        count = 0
        for raw in lines:
            line = raw.rstrip("\r\n")
            event = parse_performance_mode_line(line)
            if event:
                self.on_event(event)
                count += 1
        return count


class ProcessLassoScalingMonitor:
    """Resolve log events to existing profiles and confirmed visible windows."""

    TRIGGER_PREFIX = "process_lasso_performance_mode:"

    def __init__(self, profile_manager, state, process_watcher, automation):
        self.profile_manager = profile_manager
        self.state = state
        self.process_watcher = process_watcher
        self.automation = automation
        self.tailer = ProcessLassoLogTailer(self._handle_event)
        self._owned_triggers = {}

    def poll(self) -> int:
        config = self.profile_manager.config
        if not config.process_lasso_performance_mode_scaling:
            self.tailer.reset()
            self._owned_triggers.clear()
            return 0
        return self.tailer.poll(config.process_lasso_log_path)

    def _handle_event(self, event: PerformanceModeEvent) -> None:
        trigger = f"{self.TRIGGER_PREFIX}{event.key}"
        process_key = event.process_name.casefold()
        if not event.active:
            self._owned_triggers.pop(process_key, None)
            logger.info(
                "Process Lasso Performance Mode ended for %s; scaling remains active",
                event.process_name,
            )
            return
        if not self.state.auto_scale_enabled:
            logger.info("Ignored Process Lasso event because global auto-scaling is disabled")
            return

        window = self.process_watcher.find_visible_window_for_process(
            process_name=event.process_name,
            pid=event.pid,
        )
        if not window:
            logger.info(
                "Ignored Process Lasso Performance Mode event for %s: no visible window",
                event.process_name,
            )
            return
        profile = self.profile_manager.match_target_profile(
            process_name=window.name,
            executable_path=window.exe_path,
        )
        if not profile:
            logger.info(
                "Ignored Process Lasso Performance Mode event for %s: no explicit helper profile",
                event.process_name,
            )
            return
        self.automation.activate_profile(
            profile,
            window.exe_path,
            target_pid=window.pid,
            target_hwnd=window.hwnd,
            suppress_auto_scale=True,
        )
        if not self.state.current_active_profile or self.state.current_active_profile.id != profile.id:
            logger.warning("Process Lasso profile activation did not complete for %s", profile.name)
            return
        # Profile activation may restart Lossless Scaling and steal focus, so focus
        # the intended source window only after the profile and DLL host are ready.
        if not self.process_watcher.focus_window(window):
            logger.warning(
                "Ignored Process Lasso Performance Mode event for %s: target window could not be focused",
                event.process_name,
            )
            return
        self.automation.set_scaling(
            True,
            profile,
            reason=trigger,
            target_pid=window.pid,
            target_hwnd=window.hwnd,
        )
        if self.state.is_scaling_active and self.state.scaling_trigger == trigger:
            self._owned_triggers[process_key] = trigger
