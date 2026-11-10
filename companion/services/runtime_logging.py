"""Bounded persistent logs for installed (console-free) Companion builds."""

import logging
import os
import re
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path


LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_MAX_AGE_SECONDS = 24 * 60 * 60
_ENTRY_START = re.compile(r"^(\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d(?:[.,]\d+)?(?:[+-]\d\d:\d\d)?) \[[A-Z]+\] ")


class RuntimeLogFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        return datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="milliseconds")


class RetainedLogFileHandler(RotatingFileHandler):
    """Retain complete text entries for 24 hours within one 10 MiB file."""

    def __init__(self, path, *, max_bytes=LOG_MAX_BYTES, max_age=LOG_MAX_AGE_SECONDS, clock=time.time):
        super().__init__(path, maxBytes=max_bytes, backupCount=0, encoding="utf-8", errors="backslashreplace")
        self.max_age = max_age
        self.clock = clock
        self._next_cleanup = 0
        self.maintain(force=True, migrate=True)

    def _open(self):
        # Keep byte accounting exact on Windows as well as other platforms.
        return open(self.baseFilename, self.mode, encoding=self.encoding, errors=self.errors, newline="\n")

    def maintain(self, *, force=False, budget=None, migrate=False):
        with self.lock:
            if not force and time.monotonic() < self._next_cleanup:
                return
            self._next_cleanup = time.monotonic() + 60
            self.flush()
            path = Path(self.baseFilename)
            legacy = [path.with_name(path.name + f".{index}") for index in (3, 2, 1)] if migrate else []
            sources = [item for item in legacy if item.is_file()] + [path]
            content = "".join(item.read_bytes().decode("utf-8", errors="replace") for item in sources)
            cutoff = self.clock() - self.max_age
            entries = deque()
            size = 0
            keep = False
            for line in content.splitlines(keepends=True):
                match = _ENTRY_START.match(line)
                if match:
                    stamp = match[1].replace(",", ".")
                    try:
                        keep = datetime.fromisoformat(stamp).timestamp() > cutoff
                    except ValueError:
                        keep = False
                    if keep:
                        entries.append(bytearray())
                if keep:
                    encoded = line.encode("utf-8")
                    entries[-1].extend(encoded)
                    size += len(encoded)
            limit = self.maxBytes if budget is None else budget
            while entries and size > limit:
                size -= len(entries.popleft())
            retained = b"".join(entries)
            if retained != content.encode("utf-8") or len(sources) > 1:
                temporary = path.with_name(path.name + ".tmp")
                try:
                    temporary.write_bytes(retained)
                    self.stream.close()
                    try:
                        os.replace(temporary, path)
                    finally:
                        self.stream = self._open()
                    for backup in sources[:-1]:
                        backup.unlink(missing_ok=True)
                finally:
                    temporary.unlink(missing_ok=True)

    def emit(self, record):
        try:
            encoded = (self.format(record) + self.terminator).encode("utf-8", errors="backslashreplace")
            if len(encoded) > self.maxBytes:
                marker = b"\n[oversized log entry truncated]\n"
                encoded = encoded[:max(0, self.maxBytes - len(marker))].decode("utf-8", errors="ignore").encode("utf-8") + marker
            self.flush()
            oversized = os.path.getsize(self.baseFilename) + len(encoded) > self.maxBytes
            # Leave headroom after size trimming instead of rewriting 10 MiB on
            # every subsequent line when the file is close to the cap.
            budget = min(self.maxBytes - len(encoded), self.maxBytes * 4 // 5) if oversized else self.maxBytes
            self.maintain(force=oversized, budget=budget)
            self.stream.write(encoded.decode("utf-8"))
            self.flush()
        except Exception:
            self.handleError(record)


def maintain_runtime_logs():
    """Called by the monitor even while scaling and other work are paused."""
    for handler in tuple(logging.getLogger().handlers):
        if isinstance(handler, RetainedLogFileHandler):
            try:
                handler.maintain()
            except OSError:
                logging.getLogger("LSCompanion.Logging").warning("Could not trim the companion log", exc_info=True)


class RuntimeLogBuffer(logging.Handler):
    """Thread-safe, bounded diagnostic history; never performs I/O in emit."""

    def __init__(self, capacity: int = 1000, *, clock=time.time):
        super().__init__(logging.INFO)
        self.entries = deque(maxlen=capacity)
        self.session = uuid.uuid4().hex
        self.sequence = 0
        self.clock = clock
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            if len(message) > 16000:
                message = message[:16000] + "\n[entry truncated; see companion.log]"
            # Handler.handle() holds this same reentrant lock; also protect direct emit.
            with self.lock:
                self.sequence += 1
                self.entries.append({
                    "id": self.sequence,
                    "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
                    "level": "ERROR" if record.levelno >= logging.ERROR else record.levelname,
                    "source": record.name,
                    "message": message,
                })
        except Exception:
            self.handleError(record)

    def snapshot(self, since: int = 0, session: str = "") -> dict:
        with self.lock:
            cutoff = datetime.fromtimestamp(self.clock() - LOG_MAX_AGE_SECONDS, timezone.utc).isoformat(timespec="milliseconds")
            while self.entries and self.entries[0]["timestamp"] <= cutoff:
                self.entries.popleft()
            reset = session != self.session or since > self.sequence
            oldest = self.entries[0]["id"] if self.entries else self.sequence + 1
            gap = not reset and since < oldest - 1
            return {"session": self.session, "cursor": self.sequence, "oldestId": oldest,
                    "reset": reset, "gap": gap,
                    "entries": [dict(entry) for entry in self.entries if reset or entry["id"] > since]}


_buffer_lock = threading.Lock()
_buffer = None


def runtime_log_buffer() -> RuntimeLogBuffer:
    """Install once, including before the persistent log can be opened."""
    global _buffer
    with _buffer_lock:
        if _buffer is None:
            _buffer = RuntimeLogBuffer()
        root = logging.getLogger()
        if _buffer not in root.handlers:
            root.addHandler(_buffer)
        logging.getLogger("LSCompanion").setLevel(logging.INFO)
        return _buffer


def configure_runtime_logging(config_dir: Path) -> Path:
    runtime_log_buffer()
    path = (Path(config_dir) / "logs" / "companion.log").resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if not any(isinstance(handler, RotatingFileHandler) and Path(handler.baseFilename).resolve() == path
               for handler in root.handlers):
        handler = RetainedLogFileHandler(path)
        handler.setFormatter(RuntimeLogFormatter("%(asctime)s [%(levelname)s] (%(name)s) %(message)s"))
        root.addHandler(handler)
    return path
