"""Locate a real Steam installation of Lossless Scaling."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable, Optional


_RELATIVE_EXECUTABLE = Path("steamapps/common/Lossless Scaling/LosslessScaling.exe")


def _valid_executable(value: Optional[str | Path]) -> Optional[Path]:
    if not value:
        return None
    try:
        path = Path(value).expanduser().resolve()
    except OSError:
        return None
    if path.name.casefold() != "losslessscaling.exe" or not path.is_file():
        return None
    # Development simulation fixtures use this folder name and must never be
    # promoted into production configuration by process/path discovery.
    if any(part.casefold() in {"simulation", ".tmp"} for part in path.parts):
        return None
    return path


def _registry_steam_roots() -> list[Path]:
    if os.name != "nt":
        return []
    try:
        import winreg
    except ImportError:
        return []
    locations = (
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", ("SteamPath", "InstallPath")),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", ("InstallPath", "SteamPath")),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", ("InstallPath", "SteamPath")),
    )
    roots: list[Path] = []
    for hive, key_name, value_names in locations:
        try:
            with winreg.OpenKey(hive, key_name) as key:
                for value_name in value_names:
                    try:
                        value, _kind = winreg.QueryValueEx(key, value_name)
                    except OSError:
                        continue
                    if value:
                        roots.append(Path(os.path.expandvars(str(value))))
        except OSError:
            continue
    return roots


def _default_steam_roots() -> list[Path]:
    roots = _registry_steam_roots()
    for variable in ("PROGRAMFILES(X86)", "PROGRAMFILES"):
        value = os.environ.get(variable)
        if value:
            roots.append(Path(value) / "Steam")
    # Environment variables can be absent in constrained/elevated sessions.
    roots.extend((Path(r"C:\Program Files (x86)\Steam"), Path(r"C:\Program Files\Steam")))
    # Portable Steam installs do not always register themselves. Check only
    # conventional roots on mounted drive letters; this avoids an expensive
    # recursive disk scan while still covering D:\SteamLibrary-style installs.
    if os.name == "nt":
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            drive = Path(f"{letter}:\\")
            try:
                if drive.exists():
                    roots.extend((drive / "Steam", drive / "SteamLibrary"))
            except OSError:
                continue
    return roots


def _library_roots(steam_roots: Iterable[Path]) -> list[Path]:
    libraries: list[Path] = []
    for root in steam_roots:
        libraries.append(root)
        vdf = root / "steamapps" / "libraryfolders.vdf"
        try:
            text = vdf.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        values = re.findall(r'"path"\s*"([^"]+)"', text, flags=re.IGNORECASE)
        # Steam's older VDF format stored library paths directly under numeric keys.
        values.extend(re.findall(r'"\d+"\s*"([A-Za-z]:\\[^"]+)"', text))
        for value in values:
            libraries.append(Path(value.replace(r"\\", "\\")))
    return libraries


def find_lossless_scaling_executable(
    configured_path: Optional[str] = None,
    *,
    steam_roots: Optional[Iterable[Path]] = None,
) -> Optional[Path]:
    """Return the configured executable or discover it from Steam libraries."""
    configured = _valid_executable(configured_path)
    if configured:
        return configured

    roots = list(steam_roots) if steam_roots is not None else _default_steam_roots()
    seen: set[str] = set()
    for library in _library_roots(roots):
        try:
            candidate = (library / _RELATIVE_EXECUTABLE).resolve()
        except OSError:
            continue
        key = os.path.normcase(str(candidate))
        if key in seen:
            continue
        seen.add(key)
        valid = _valid_executable(candidate)
        if valid:
            return valid
    return None
