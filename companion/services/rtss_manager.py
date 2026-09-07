"""Safe per-application RTSS frame-limiter profile management."""

from __future__ import annotations

import ctypes
import os
import re
from pathlib import Path
from typing import Dict, Optional

import psutil

from ..core.models import Profile


OFFICIAL_DOWNLOAD_URL = (
    "https://www.guru3d.com/download/rtss-rivatuner-statistics-server-download/"
)
LIMIT_METHOD_VALUES = {
    "async": 0,
    "front_edge_sync": 1,
    "back_edge_sync": 2,
    "nvidia_reflex": 3,
}
MANAGED_KEYS = ("Limit", "LimitDenominator", "SyncLimiter")


class RtssProfileManager:
    """Manage only limiter keys in RTSS profiles, never application files."""

    def __init__(self, configured_path: Optional[str] = None):
        self.configured_path = configured_path

    def status(self, configured_path: Optional[str] = None) -> dict:
        root = self.detect_install(configured_path)
        return {
            "installed": root is not None,
            "path": str(root) if root else "",
            "running": self._running_root() is not None,
            "downloadUrl": OFFICIAL_DOWNLOAD_URL,
        }

    def detect_install(self, configured_path: Optional[str] = None) -> Optional[Path]:
        candidates = []
        for value in (configured_path, self.configured_path):
            if value:
                candidates.append(Path(value).expanduser())
        running = self._running_root()
        if running:
            candidates.append(running)
        candidates.extend(self._registry_roots())
        for env_name in ("ProgramFiles(x86)", "ProgramFiles"):
            root = os.environ.get(env_name)
            if root:
                candidates.append(Path(root) / "RivaTuner Statistics Server")
        candidates.append(Path(r"C:\Program Files (x86)\RivaTuner Statistics Server"))
        candidates.append(Path(r"C:\Program Files\RivaTuner Statistics Server"))
        seen = set()
        for candidate in candidates:
            try:
                candidate = candidate.resolve()
            except OSError:
                continue
            key = os.path.normcase(str(candidate))
            if key in seen:
                continue
            seen.add(key)
            if self._is_install_root(candidate):
                return candidate
        return None

    def apply_profile(
        self,
        profile: Profile,
        previous: Optional[Profile] = None,
        configured_path: Optional[str] = None,
        framerate_limit: Optional[int] = None,
    ) -> Path:
        root = self.detect_install(configured_path)
        if root is None:
            raise RuntimeError("RTSS is not installed or its install directory could not be detected")
        process_name = self._target_process(profile)
        if previous and previous.rtss.enabled:
            previous_name = previous.rtss.managed_target_process or self._target_process(previous)
            if previous_name.casefold() != process_name.casefold():
                self.remove_profile(previous, configured_path)
                previous = None
            elif previous.rtss.managed_install_path and Path(
                previous.rtss.managed_install_path
            ).resolve() != root.resolve():
                previous = None

        profiles_dir = self._profiles_dir(root)
        profiles_dir.mkdir(parents=True, exist_ok=True)
        profile_path = profiles_dir / f"{process_name}.cfg"
        exists = profile_path.exists()
        created = not exists
        text = self._read_text(profile_path) if exists else ""
        marker = f"; Managed by Lossless Scaling Helper profile={profile.id}"

        if previous and previous.rtss.managed_target_process and exists:
            if previous.rtss.managed_profile_created and marker.casefold() not in text.casefold():
                raise RuntimeError(
                    "The managed RTSS profile was replaced externally; no changes were made"
                )
            created = previous.rtss.managed_profile_created
            original = dict(previous.rtss.original_values)
        else:
            original = {key: self._get_value(text, "Framerate", key) for key in MANAGED_KEYS}

        if created and not text:
            text = marker + "\n"
        values = {
            "Limit": framerate_limit if framerate_limit is not None else profile.rtss.framerate_limit,
            "LimitDenominator": 1,
            "SyncLimiter": LIMIT_METHOD_VALUES[profile.rtss.limit_method],
        }
        for key, value in values.items():
            text = self._set_value(text, "Framerate", key, value)
        self._atomic_write(profile_path, text)

        profile.rtss.managed_profile_created = created
        profile.rtss.managed_target_process = process_name
        profile.rtss.managed_install_path = str(root)
        profile.rtss.original_values = original
        self._notify_rtss(root)
        return profile_path

    def remove_profile(self, profile: Profile, configured_path: Optional[str] = None) -> bool:
        root = self.detect_install(profile.rtss.managed_install_path or configured_path)
        if root is None:
            raise RuntimeError("RTSS is not installed; the RTSS profile could not be removed")
        process_name = profile.rtss.managed_target_process or self._target_process(profile)
        profile_path = self._profiles_dir(root) / f"{process_name}.cfg"
        if not profile_path.exists():
            return False
        text = self._read_text(profile_path)
        marker = f"; Managed by Lossless Scaling Helper profile={profile.id}"
        if profile.rtss.managed_profile_created and marker.casefold() in text.casefold():
            profile_path.unlink()
        elif profile.rtss.managed_profile_created:
            raise RuntimeError(
                "The managed RTSS profile was replaced externally; it was left unchanged"
            )
        else:
            for key in MANAGED_KEYS:
                original = profile.rtss.original_values.get(key)
                text = (
                    self._remove_value(text, "Framerate", key)
                    if original is None
                    else self._set_value(text, "Framerate", key, original)
                )
            self._atomic_write(profile_path, text)
        self._notify_rtss(root)
        return True

    @staticmethod
    def clear_metadata(profile: Profile) -> None:
        profile.rtss.managed_profile_created = False
        profile.rtss.managed_target_process = None
        profile.rtss.managed_install_path = None
        profile.rtss.original_values = {}

    @staticmethod
    def _target_process(profile: Profile) -> str:
        value = (profile.target_process or "").strip()
        if not value and profile.target_executable_path:
            value = Path(profile.target_executable_path).name
        if (
            not value
            or value != Path(value).name
            or not value.casefold().endswith(".exe")
            or any(char in value for char in '<>:"/\\|?*')
        ):
            raise ValueError("RTSS limiting requires a valid target executable filename ending in .exe")
        return value

    @staticmethod
    def _is_install_root(root: Path) -> bool:
        return (
            root.is_dir()
            and any((root / name).is_file() for name in ("RTSS.exe", "RTSS64.exe"))
            and any((root / name).is_file() for name in ("RTSSHooks.dll", "RTSSHooks64.dll"))
        )

    @staticmethod
    def _profiles_dir(root: Path) -> Path:
        resolved_root = root.resolve()
        profiles = (resolved_root / "Profiles").resolve()
        try:
            profiles.relative_to(resolved_root)
        except ValueError as exc:
            raise RuntimeError("RTSS Profiles directory resolves outside the RTSS installation") from exc
        return profiles

    def _running_root(self) -> Optional[Path]:
        for process in psutil.process_iter(["name", "exe"]):
            try:
                if (process.info.get("name") or "").casefold() in {"rtss.exe", "rtss64.exe"}:
                    executable = process.info.get("exe")
                    if executable:
                        return Path(executable).parent
            except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
                continue
        return None

    @staticmethod
    def _registry_roots() -> list[Path]:
        if os.name != "nt":
            return []
        try:
            import winreg
        except ImportError:
            return []
        roots = []
        subkey = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
                try:
                    with winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ | view) as uninstall:
                        for index in range(winreg.QueryInfoKey(uninstall)[0]):
                            try:
                                with winreg.OpenKey(uninstall, winreg.EnumKey(uninstall, index)) as entry:
                                    name = str(winreg.QueryValueEx(entry, "DisplayName")[0])
                                    if "rivatuner statistics server" not in name.casefold():
                                        continue
                                    location = str(winreg.QueryValueEx(entry, "InstallLocation")[0]).strip()
                                    if location:
                                        roots.append(Path(location))
                            except OSError:
                                continue
                except OSError:
                    continue
        return roots

    @staticmethod
    def _section_bounds(lines: list[str], section: str) -> tuple[Optional[int], int]:
        wanted = section.casefold()
        start = None
        for index, line in enumerate(lines):
            match = re.match(r"^\s*\[([^]]+)]\s*$", line)
            if not match:
                continue
            if start is not None:
                return start, index
            if match.group(1).strip().casefold() == wanted:
                start = index
        return start, len(lines)

    @classmethod
    def _get_value(cls, text: str, section: str, key: str) -> Optional[int]:
        lines = text.splitlines()
        start, end = cls._section_bounds(lines, section)
        if start is None:
            return None
        pattern = re.compile(rf"^\s*{re.escape(key)}\s*=\s*(-?\d+)\s*$", re.I)
        for line in lines[start + 1:end]:
            match = pattern.match(line)
            if match:
                return int(match.group(1))
        return None

    @classmethod
    def _set_value(cls, text: str, section: str, key: str, value: int) -> str:
        lines = text.splitlines()
        start, end = cls._section_bounds(lines, section)
        replacement = f"{key}={int(value)}"
        if start is None:
            if lines and lines[-1].strip():
                lines.append("")
            lines.extend([f"[{section}]", replacement])
        else:
            pattern = re.compile(rf"^\s*{re.escape(key)}\s*=", re.I)
            for index in range(start + 1, end):
                if pattern.match(lines[index]):
                    lines[index] = replacement
                    break
            else:
                lines.insert(end, replacement)
        return "\n".join(lines).rstrip() + "\n"

    @classmethod
    def _remove_value(cls, text: str, section: str, key: str) -> str:
        lines = text.splitlines()
        start, end = cls._section_bounds(lines, section)
        if start is None:
            return text
        pattern = re.compile(rf"^\s*{re.escape(key)}\s*=", re.I)
        lines = [line for index, line in enumerate(lines) if not (start < index < end and pattern.match(line))]
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)

    @staticmethod
    def _read_text(path: Path) -> str:
        raw = path.read_bytes()
        try:
            return raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            return raw.decode("mbcs" if os.name == "nt" else "latin-1")

    @staticmethod
    def _notify_rtss(root: Path) -> None:
        if os.name != "nt":
            return
        for dll_name in ("RTSSHooks64.dll", "RTSSHooks.dll"):
            dll_path = root / dll_name
            if not dll_path.is_file():
                continue
            try:
                update = ctypes.CDLL(str(dll_path)).UpdateProfiles
                update.argtypes = []
                update.restype = None
                update()
                return
            except (OSError, AttributeError):
                continue
