"""Bounded persistent logs for installed (console-free) Companion builds."""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_runtime_logging(config_dir: Path) -> Path:
    path = (Path(config_dir) / "logs" / "companion.log").resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if not any(isinstance(handler, RotatingFileHandler) and Path(handler.baseFilename).resolve() == path
               for handler in root.handlers):
        handler = RotatingFileHandler(path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] (%(name)s) %(message)s"))
        root.addHandler(handler)
    return path
