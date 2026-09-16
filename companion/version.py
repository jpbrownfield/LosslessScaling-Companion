"""Build-version metadata embedded by PyInstaller."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


DEVELOPMENT_VERSION = "0.0.0-dev"
_STABLE_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def _metadata_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "version.json"
    return Path(__file__).with_name("version.json")


def current_version() -> str:
    override = os.environ.get("LS_COMPANION_BUILD_VERSION") if not getattr(sys, "frozen", False) else None
    if override:
        return override.strip()
    try:
        value = json.loads(_metadata_path().read_text(encoding="utf-8")).get("version")
        return str(value).strip() or DEVELOPMENT_VERSION
    except (OSError, ValueError, TypeError, AttributeError):
        return DEVELOPMENT_VERSION


def stable_version_tuple(value: str):
    match = _STABLE_VERSION.fullmatch(str(value or "").strip())
    return tuple(int(part) for part in match.groups()) if match else None


__version__ = current_version()

