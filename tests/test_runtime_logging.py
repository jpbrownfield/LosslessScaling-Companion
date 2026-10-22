import logging
import tempfile
import unittest
from pathlib import Path
from logging.handlers import RotatingFileHandler
from unittest.mock import patch

from companion.services.runtime_logging import configure_runtime_logging


class RuntimeLoggingTests(unittest.TestCase):
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
                self.assertEqual(added[0].backupCount, 3)
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
