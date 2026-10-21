import logging
import tempfile
import unittest
from pathlib import Path
from logging.handlers import RotatingFileHandler

from companion.services.runtime_logging import configure_runtime_logging


class RuntimeLoggingTests(unittest.TestCase):
    def test_persistent_log_records_errors_without_duplicating_handlers(self):
        root = logging.getLogger()
        previous = list(root.handlers)
        try:
            with tempfile.TemporaryDirectory() as directory:
                try:
                    path = configure_runtime_logging(Path(directory))
                    configure_runtime_logging(Path(directory))
                    logging.getLogger("LSCompanion.Test").error("test scaling confirmation failure")
                    added = [handler for handler in root.handlers if handler not in previous]
                    self.assertEqual(len(added), 1)
                    self.assertIsInstance(added[0], RotatingFileHandler)
                    self.assertEqual(path.read_text(encoding="utf-8").count("test scaling confirmation failure"), 1)
                    self.assertEqual(added[0].backupCount, 3)
                finally:
                    for handler in list(root.handlers):
                        if handler not in previous:
                            root.removeHandler(handler)
                            handler.close()
        finally:
            root.handlers[:] = previous
