"""Discover user-obtained graphics add-on sources without scanning game folders."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Dict, Iterable, Optional


class GraphicsSourceDetector:
    RESHADE_PATTERN = "ReShade_Setup_*_Addon*.exe"
    RESHADE_NAME = re.compile(
        r"^ReShade_Setup_(?P<version>\d+(?:\.\d+)+)_Addon(?: \(\d+\))?\.exe$", re.IGNORECASE
    )

    @staticmethod
    def _file_version(path: Path) -> Optional[str]:
        try:
            import win32api

            info = win32api.GetFileVersionInfo(str(path), "\\")
            parts = (
                info["FileVersionMS"] >> 16,
                info["FileVersionMS"] & 0xFFFF,
                info["FileVersionLS"] >> 16,
                info["FileVersionLS"] & 0xFFFF,
            )
            while len(parts) > 2 and parts[-1] == 0:
                parts = parts[:-1]
            return ".".join(str(part) for part in parts)
        except Exception:
            return None

    @staticmethod
    def _registry_value(key_path: str, value_name: str) -> Optional[str]:
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                value, _ = winreg.QueryValueEx(key, value_name)
            return os.path.expandvars(str(value)) if value else None
        except (ImportError, OSError):
            return None

    @classmethod
    def _download_directories(cls) -> Iterable[Path]:
        candidates = [Path.home() / "Downloads"]
        known_downloads = cls._registry_value(
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
            "{374DE290-123F-4565-9164-39C4925E467B}",
        )
        if known_downloads:
            candidates.append(Path(known_downloads))

        local_app_data = Path(os.environ.get("LOCALAPPDATA", ""))
        for browser in ("Google/Chrome", "Microsoft/Edge"):
            user_data = local_app_data / browser / "User Data"
            if not user_data.is_dir():
                continue
            for preferences in user_data.glob("*/Preferences"):
                try:
                    data = json.loads(preferences.read_text(encoding="utf-8"))
                    configured = data.get("download", {}).get("default_directory")
                    if configured:
                        candidates.append(Path(os.path.expandvars(str(configured))))
                except (OSError, json.JSONDecodeError, AttributeError):
                    continue

        seen = set()
        for candidate in candidates:
            try:
                resolved = candidate.expanduser().resolve()
            except OSError:
                continue
            key = str(resolved).casefold()
            if key not in seen and resolved.is_dir():
                seen.add(key)
                yield resolved

    @classmethod
    def reshade(cls) -> Dict:
        matches = []
        for directory in cls._download_directories():
            try:
                matches.extend(
                    path for path in directory.glob(cls.RESHADE_PATTERN)
                    if path.is_file() and cls.RESHADE_NAME.fullmatch(path.name)
                )
            except OSError:
                continue
        if not matches:
            return {
                "detected": False,
                "kind": "download",
                "message": "ReShade Full Add-On installer not found in a configured Downloads folder.",
            }
        newest = max(matches, key=lambda path: path.stat().st_mtime)
        name_match = cls.RESHADE_NAME.fullmatch(newest.name)
        return {
            "detected": True,
            "kind": "download",
            "path": str(newest.resolve()),
            "version": name_match.group("version") if name_match else None,
            "message": f"ReShade Full Add-On installer downloaded: {newest.name}",
        }

    @classmethod
    def special_k(cls) -> Dict:
        candidates = []
        configured = cls._registry_value(r"SOFTWARE\Kaldaien\Special K", "Path")
        if configured:
            candidates.append(Path(configured))
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            candidates.append(Path(local_app_data) / "Programs" / "Special K")

        for directory in candidates:
            payload = directory / "SpecialK64.dll"
            frontend = directory / "SKIF.exe"
            if payload.is_file():
                return {
                    "detected": True,
                    "kind": "installation",
                    "path": str(payload.resolve()),
                    "frontendPath": str(frontend.resolve()) if frontend.is_file() else None,
                    "version": cls._file_version(payload),
                    "message": "Special K 64-bit payload is installed and ready to import.",
                }
        return {
            "detected": False,
            "kind": "installation",
            "message": "Special K installation not found in its registry or default install location.",
        }

    @classmethod
    def status(cls) -> Dict[str, Dict]:
        return {"reshade": cls.reshade(), "special-k": cls.special_k()}
