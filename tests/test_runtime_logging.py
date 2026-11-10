import logging
import tempfile
import unittest
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from logging.handlers import RotatingFileHandler
from unittest.mock import patch

from companion.services.runtime_logging import (
    LOG_MAX_BYTES, LOG_MAX_AGE_SECONDS, RetainedLogFileHandler, RuntimeLogBuffer,
    RuntimeLogFormatter, configure_runtime_logging, runtime_log_buffer,
)


class RuntimeLoggingTests(unittest.TestCase):
    @staticmethod
    def record(message, timestamp):
        record = logging.LogRecord("LSCompanion.RetentionTest", logging.INFO, "", 0, message, (), None)
        record.created = timestamp
        return record

    def test_file_expires_old_entries_and_keeps_recent_multiline_records_while_idle(self):
        clock = [time.time()]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "companion.log"
            handler = RetainedLogFileHandler(path, clock=lambda: clock[0])
            handler.setFormatter(RuntimeLogFormatter("%(asctime)s [%(levelname)s] (%(name)s) %(message)s"))
            try:
                handler.handle(self.record("old entry\nold traceback", clock[0]))
                clock[0] += 3600
                handler.handle(self.record("recent entry\nrecent traceback", clock[0]))
                clock[0] += 23 * 3600 + 1
                handler.maintain(force=True)
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("old entry", text)
                self.assertNotIn("old traceback", text)
                self.assertIn("recent entry\nrecent traceback", text)
                clock[0] += LOG_MAX_AGE_SECONDS
                handler.maintain(force=True)
                self.assertEqual(path.read_bytes(), b"")
            finally:
                handler.close()

    def test_file_trims_oldest_complete_entries_with_exact_utf8_byte_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "companion.log"
            handler = RetainedLogFileHandler(path, max_bytes=512)
            formatter = RuntimeLogFormatter("%(asctime)s [%(levelname)s] (%(name)s) %(message)s")
            handler.setFormatter(formatter)
            try:
                for number in range(20):
                    record = self.record(f"record {number}: " + "é🔥" * 10 + "\ntraceback line", time.time())
                    handler.handle(record)
                    self.assertLessEqual(path.stat().st_size, 512)
                text = path.read_text(encoding="utf-8")
                self.assertIn("record 19:", text)
                self.assertNotIn("record 0:", text)
                self.assertTrue(text.startswith("20"))
                self.assertTrue(text.endswith("traceback line\n"))
                self.assertFalse(path.with_name("companion.log.1").exists())
                handler.handle(self.record("oversized " + "🔥" * 1000, time.time()))
                self.assertLessEqual(path.stat().st_size, 512)
                self.assertIn("oversized log entry truncated", path.read_text(encoding="utf-8"))
            finally:
                handler.close()

    def test_startup_merges_legacy_backups_and_discards_expired_entries(self):
        now = time.time()
        formatter = RuntimeLogFormatter("%(asctime)s [%(levelname)s] (%(name)s) %(message)s")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "companion.log"
            path.write_text(formatter.format(self.record("current", now)) + "\n", encoding="utf-8")
            path.with_name("companion.log.1").write_text(formatter.format(self.record("recent backup", now - 60)) + "\n", encoding="utf-8")
            path.with_name("companion.log.2").write_text(formatter.format(self.record("expired backup", now - LOG_MAX_AGE_SECONDS - 1)) + "\n", encoding="utf-8")
            handler = RetainedLogFileHandler(path, clock=lambda: now)
            try:
                text = path.read_text(encoding="utf-8")
                self.assertIn("recent backup", text)
                self.assertIn("current", text)
                self.assertNotIn("expired backup", text)
                self.assertLess(text.index("recent backup"), text.index("current"))
                self.assertFalse(path.with_name("companion.log.1").exists())
                self.assertFalse(path.with_name("companion.log.2").exists())
            finally:
                handler.close()

    def test_failed_trim_preserves_original_file_and_reopens_the_stream(self):
        now = time.time()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "companion.log"
            handler = RetainedLogFileHandler(path, clock=lambda: now)
            handler.setFormatter(RuntimeLogFormatter("%(asctime)s [%(levelname)s] %(message)s"))
            try:
                handler.handle(self.record("expired", now - LOG_MAX_AGE_SECONDS - 1))
                original = path.read_bytes()
                with patch("companion.services.runtime_logging.os.replace", side_effect=PermissionError("locked")):
                    with self.assertRaises(PermissionError):
                        handler.maintain(force=True)
                self.assertEqual(path.read_bytes(), original)
                self.assertFalse(handler.stream.closed)
                self.assertFalse(path.with_name("companion.log.tmp").exists())
            finally:
                handler.close()

    def test_live_history_expires_after_one_day_even_without_new_messages(self):
        now = time.time()
        clock = [now]
        buffer = RuntimeLogBuffer(clock=lambda: clock[0])
        buffer.handle(self.record("old", now))
        buffer.handle(self.record("recent", now + 3600))
        initial = buffer.snapshot()
        clock[0] = now + LOG_MAX_AGE_SECONDS + 1
        result = buffer.snapshot(initial["cursor"], initial["session"])
        self.assertEqual(result["oldestId"], 2)
        self.assertEqual([entry["message"] for entry in buffer.snapshot()["entries"]], ["recent"])
        clock[0] += LOG_MAX_AGE_SECONDS
        self.assertEqual(buffer.snapshot()["entries"], [])

    def test_live_history_has_levels_timestamps_tracebacks_and_bounded_cursors(self):
        buffer = RuntimeLogBuffer(capacity=3)
        log = logging.getLogger("LSCompanion.LogBufferTest")
        log.addHandler(buffer)
        log.setLevel(logging.DEBUG)
        log.propagate = False
        try:
            log.debug("not included")
            log.info("starting scaling")
            initial = buffer.snapshot()
            self.assertTrue(initial["reset"])
            self.assertEqual(len(initial["entries"]), 1)
            self.assertTrue(initial["entries"][0]["timestamp"].endswith("+00:00"))
            log.warning("target lost focus")
            try:
                raise RuntimeError("confirmation timed out")
            except RuntimeError:
                log.exception("scaling failed")
            delta = buffer.snapshot(initial["cursor"], initial["session"])
            self.assertFalse(delta["reset"])
            self.assertEqual([row["level"] for row in delta["entries"]], ["WARNING", "ERROR"])
            self.assertIn("RuntimeError: confirmation timed out", delta["entries"][-1]["message"])
            log.critical("background thread stopped")
            log.info("next attempt")
            expired = buffer.snapshot(initial["cursor"], initial["session"])
            self.assertTrue(expired["gap"])
            self.assertEqual(len(expired["entries"]), 3)
            self.assertEqual(expired["entries"][-2]["level"], "ERROR")
            self.assertEqual(buffer.snapshot(expired["cursor"], expired["session"])["entries"], [])
            self.assertTrue(buffer.snapshot(99999, expired["session"])["reset"])
            self.assertTrue(buffer.snapshot(0, "old process")["reset"])
        finally:
            log.removeHandler(buffer)
            log.propagate = True
            log.setLevel(logging.NOTSET)

    def test_concurrent_logging_is_ordered_bounded_and_large_entries_are_truncated(self):
        buffer = RuntimeLogBuffer(capacity=50)
        def emit(number):
            buffer.handle(logging.LogRecord("LSCompanion.Worker", logging.INFO, "", 0, "message %s", (number,), None))
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(emit, range(200)))
        result = buffer.snapshot()
        self.assertEqual(result["cursor"], 200)
        self.assertEqual([entry["id"] for entry in result["entries"]], list(range(151, 201)))
        buffer.handle(logging.LogRecord("LSCompanion.Worker", logging.ERROR, "", 0, "x" * 20000, (), None))
        self.assertIn("entry truncated", buffer.snapshot()["entries"][-1]["message"])

    def test_live_handler_is_installed_once_and_captures_info_without_basic_config(self):
        first = runtime_log_buffer()
        self.assertIs(runtime_log_buffer(), first)
        self.assertEqual(logging.getLogger().handlers.count(first), 1)
        before = first.snapshot()
        logging.getLogger("LSCompanion.LiveInfoTest").info("info is visible")
        self.assertEqual(first.snapshot(before["cursor"], before["session"])["entries"][-1]["message"], "info is visible")

    def test_persistent_log_records_errors_without_duplicating_handlers(self):
        root = logging.getLogger()
        previous = list(root.handlers)
        with tempfile.TemporaryDirectory() as directory:
            expected = (Path(directory) / "logs" / "companion.log").resolve()

            def owned(handler):
                return (isinstance(handler, RotatingFileHandler)
                        and Path(handler.baseFilename).resolve() == expected)

            try:
                path = configure_runtime_logging(Path(directory))
                first = [handler for handler in root.handlers if owned(handler)]
                self.assertEqual(len(first), 1)
                configure_runtime_logging(Path(directory))
                logging.getLogger("LSCompanion.Test").error("test scaling confirmation failure")
                added = [handler for handler in root.handlers if owned(handler)]
                self.assertEqual(added, first)
                self.assertEqual(path.read_text(encoding="utf-8").count("test scaling confirmation failure"), 1)
                self.assertEqual(added[0].backupCount, 0)
                self.assertEqual(added[0].maxBytes, LOG_MAX_BYTES)
            finally:
                # Do not remove or close pytest's handlers (or any other
                # unrelated handler installed while this test was running).
                for handler in list(root.handlers):
                    if handler not in previous and owned(handler):
                        root.removeHandler(handler)
                        handler.close()

    def test_existing_handler_path_alias_is_reused(self):
        root = logging.getLogger()
        previous = list(root.handlers)
        resolve = Path.resolve
        with tempfile.TemporaryDirectory() as directory:
            canonical = (Path(directory) / "logs" / "companion.log").resolve()
            alias = Path(directory) / "path-alias" / "logs" / "companion.log"
            canonical.parent.mkdir()
            existing = RotatingFileHandler(canonical, encoding="utf-8")
            root.addHandler(existing)
            # Simulate Windows short names/junctions without requiring symlink
            # privileges on CI. The open stream still points at the real file.
            existing.baseFilename = str(alias)

            def resolve_alias(candidate, *args, **kwargs):
                if candidate == alias:
                    return canonical
                return resolve(candidate, *args, **kwargs)

            try:
                with patch.object(Path, "resolve", autospec=True, side_effect=resolve_alias):
                    result = configure_runtime_logging(Path(directory))
                    handlers = [handler for handler in root.handlers
                                if isinstance(handler, RotatingFileHandler)
                                and Path(handler.baseFilename).resolve() == canonical]
                    self.assertEqual(handlers, [existing])
                    self.assertEqual(result, canonical)
            finally:
                for handler in list(root.handlers):
                    if (handler not in previous and isinstance(handler, RotatingFileHandler)
                            and (handler is existing or Path(handler.baseFilename).resolve() == canonical)):
                        root.removeHandler(handler)
                        handler.close()
