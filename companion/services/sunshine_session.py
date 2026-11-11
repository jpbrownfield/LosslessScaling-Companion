"""Read Sunshine/Apollo session and hardware-encoder identity from their live logs.

No server credentials or configuration changes are needed. Only logs belonging
to a running host process are accepted; startup encoder probes alone are idle.
Upstream signals: src/stream.cpp, src/rtsp.cpp, and
src/platform/windows/display_base.cpp in LizardByte/Sunshine and ClassicOldSong/Apollo.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

import psutil

logger = logging.getLogger("LSCompanion.Sunshine")
_TIMESTAMP = re.compile(r"^\[(\d{4}[-:]\d{2}[-:]\d{2}[ T:]\d{2}:\d{2}:\d{2}(?:\.\d+)?)\]")
_DEVICE = re.compile(r"Device Description\s*:\s*(.+)")
_ENCODER = re.compile(r"Creating encoder\s*\[([^]]+)\]", re.I)
_MAX_READ = 4 * 1024 * 1024


@dataclass
class SessionLog:
    created_after: float
    clients: int = 0
    starting: bool = False
    gpu_name: str | None = None
    hardware_encoder: bool = False
    timestamp: float = 0.0
    offset: int = 0
    identity: tuple | None = None
    modified: int = 0
    pending: bytes = b""

    def feed(self, line: str) -> None:
        stamp = _TIMESTAMP.match(line)
        if stamp:
            value = stamp[1]
            if value[4] == ":":
                value = value[:10].replace(":", "-") + " " + value[11:]
            try:
                self.timestamp = datetime.fromisoformat(value).timestamp()
            except ValueError:
                self.timestamp = 0.0
        # Device details are multiline records and inherit the preceding stamp.
        if self.timestamp < self.created_after:
            return
        if "New streaming session started" in line:
            if not self.clients:
                self.gpu_name = None
                self.hardware_encoder = False
            self.starting = True
        elif "CLIENT CONNECTED" in line:
            if not self.clients and not self.starting:
                self.gpu_name = None
                self.hardware_encoder = False
            self.clients += 1
            self.starting = False
        elif "CLIENT DISCONNECTED" in line:
            self.clients = max(0, self.clients - 1)
            if not self.clients:
                self.starting = False
                self.gpu_name = None
                self.hardware_encoder = False
        device = _DEVICE.search(line)
        if device and (self.clients or self.starting):
            self.gpu_name = device[1].strip()
            self.hardware_encoder = False
        encoder = _ENCODER.search(line)
        if encoder and (self.clients or self.starting):
            self.hardware_encoder = encoder[1].casefold().endswith(("_nvenc", "_amf", "_qsv"))

    def read(self, path: Path) -> bool:
        try:
            with path.open("rb") as stream:
                stat = path.stat()
                identity = (stat.st_dev, stat.st_ino)
                reset = (self.identity != identity or stat.st_size < self.offset
                         or (stat.st_size == self.offset and self.modified != stat.st_mtime_ns)
                         or stat.st_size - self.offset > _MAX_READ)
                if reset:
                    birth = self.created_after
                    self.__dict__.update(SessionLog(birth).__dict__)
                    self.offset = max(0, stat.st_size - _MAX_READ)
                    stream.seek(self.offset)
                    if self.offset:
                        stream.readline()  # Discard a partial first line.
                else:
                    stream.seek(self.offset)
                data = self.pending + stream.read(_MAX_READ)
                self.offset = stream.tell()
                self.identity, self.modified = identity, stat.st_mtime_ns
            lines = data.split(b"\n")
            self.pending = lines.pop()
            for line in lines:
                self.feed(line.decode("utf-8", errors="replace").rstrip("\r"))
            return True
        except OSError:
            return False


def _config_values(path: Path) -> dict[str, str]:
    try:
        with path.open(encoding="utf-8-sig", errors="replace") as stream:
            text = stream.read(65536)
        return {key.strip(): value.split("#", 1)[0].strip().strip('"')
                for line in text.splitlines() if "=" in line and not line.lstrip().startswith("#")
                for key, value in [line.split("=", 1)]}
    except OSError:
        return {}


def host_processes() -> Iterable[tuple[int, float, Path]]:
    for process in psutil.process_iter(["pid", "name", "exe", "create_time"]):
        if str(process.info.get("name") or "").casefold() not in {"sunshine.exe", "apollo.exe"}:
            continue
        try:
            executable = process.info.get("exe")
            birth = process.info.get("create_time")
            if not executable or not birth:
                continue
            root = Path(executable).parent / "config"
            try:
                args = process.cmdline()[1:]
            except psutil.AccessDenied:
                args = []
            config = root / "sunshine.conf"
            for argument in args:
                if argument.casefold().endswith(".conf") and "=" not in argument:
                    candidate = Path(argument)
                    config = candidate if candidate.is_absolute() else Path(process.cwd()) / candidate
                    break
            values = _config_values(config)
            for argument in args:
                if argument.startswith("log_path="):
                    values["log_path"] = argument.split("=", 1)[1].strip('"')
            log = Path(values.get("log_path") or "sunshine.log")
            # Both hosts resolve relative log_path against exe/config, even
            # when a custom .conf lives elsewhere.
            yield process.pid, float(birth), log if log.is_absolute() else root / log
        except (psutil.Error, OSError, ValueError):
            continue


@dataclass
class SunshineSessionMonitor:
    processes: Callable = host_processes
    clock: Callable = time.monotonic
    _logs: dict = field(default_factory=dict)
    _next_poll: float = 0.0
    _gpu_name: str | None = None
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def gpu_name(self) -> str | None:
        with self._lock:
            now = self.clock()
            if now < self._next_poll:
                return self._gpu_name
            self._next_poll = now + 1.0
            live = {}
            active = []
            try:
                for pid, birth, path in self.processes():
                    key = (pid, birth, str(path))
                    log = self._logs.get(key) or SessionLog(birth - 1.0)
                    live[key] = log
                    if log.read(path) and log.clients:
                        active.append(log.gpu_name if log.hardware_encoder else None)
            except (psutil.Error, OSError):
                logger.debug("Streaming host discovery unavailable", exc_info=True)
            self._logs = live
            # Unknown or conflicting concurrent sessions must never pick an
            # arbitrary GPU. Identical names are disambiguated by the router.
            names = {name.casefold() if name else None for name in active}
            selected = active[0] if len(names) == 1 and None not in names else None
            if selected != self._gpu_name:
                logger.info("Sunshine/Apollo encoder GPU: %s", selected or "no confirmed hardware session")
            self._gpu_name = selected
            return selected
