"""
ReShade preset manager and configuration swapper.
Allows programmatic switching of ReShade presets per profile or game process.
"""

import os
import re
import shutil
import tempfile
import logging
import hashlib
import stat
from pathlib import Path
from typing import Optional, List
from .input_simulator import InputSimulator
from .overlay_shortcuts import reshade_key_data

logger = logging.getLogger("LSCompanion.ReshadeManager")


class ReshadeManager:
    @staticmethod
    def _editable_preset(installation: Path, source: Path) -> Path:
        """Edit a private runtime copy, retaining edits across profile switches."""
        key = hashlib.sha256(os.path.normcase(str(source.resolve())).encode("utf-8")).hexdigest()[:16]
        folder = installation / ".companion" / "reshade-presets" / key
        if not folder.resolve().is_relative_to(installation.resolve()):
            raise ValueError("ReShade preset cache must remain inside the private LS runtime")
        folder.mkdir(parents=True, exist_ok=True)
        destination, receipt = folder / "preset.ini", folder / "source.sha256"
        content = source.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        previous = receipt.read_text(encoding="ascii") if receipt.is_file() else ""
        if not destination.is_file() or previous != digest:
            handle, temporary = tempfile.mkstemp(dir=folder, suffix=".tmp")
            try:
                with os.fdopen(handle, "wb") as output:
                    output.write(content)
                if destination.exists():
                    destination.chmod(destination.stat().st_mode | stat.S_IWUSR)
                os.replace(temporary, destination)
                receipt.write_text(digest, encoding="ascii")
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        destination.chmod(destination.stat().st_mode | stat.S_IWUSR)
        return destination

    @staticmethod
    def _is_lossless_scaling_ini(path: Path) -> bool:
        valid = (
            path.name.casefold() in {"reshade.ini", "reshadegui.ini"}
            and (path.parent / "LosslessScaling.exe").is_file()
        )
        if valid:
            from .managed_runtime import assert_managed_executable
            try:
                assert_managed_executable(path.parent / "LosslessScaling.exe")
            except (OSError, ValueError, RuntimeError):
                logger.error("ReShade configuration requires the managed LS clone: %s", path)
                return False
        return valid

    @staticmethod
    def find_reshade_ini(app_dir: str) -> Optional[Path]:
        """Searches for ReShade.ini in the application directory."""
        dir_path = Path(app_dir)
        if not dir_path.is_dir() or not (dir_path / "LosslessScaling.exe").is_file():
            return None

        candidates = ["ReShade.ini", "reshade.ini", "ReShadeGUI.ini"]
        for cand in candidates:
            p = dir_path / cand
            if p.exists() and p.is_file():
                return p
        return None

    @staticmethod
    def swap_preset(reshade_ini_path: str, new_preset_path: str, backup: bool = True,
                    *, overlay_hotkey: Optional[str] = None) -> bool:
        """
        Updates the CurrentPresetPath in the given ReShade.ini file.
        """
        ini_file = Path(reshade_ini_path)
        if not ini_file.exists() or not ReshadeManager._is_lossless_scaling_ini(ini_file):
            logger.error(f"ReShade.ini must be beside LosslessScaling.exe: {reshade_ini_path}")
            return False

        preset_file = Path(new_preset_path)
        if not preset_file.exists():
            logger.warning(f"Preset file does not exist at: {new_preset_path} (swapping anyway)")

        try:
            if preset_file.is_file():
                preset_file = ReshadeManager._editable_preset(ini_file.parent, preset_file)
            if backup:
                bak_path = ini_file.with_suffix(".ini.bak")
                if not bak_path.exists():
                    shutil.copy2(ini_file, bak_path)

            with open(ini_file, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()

            abs_preset_path = str(preset_file.resolve())
            general = re.compile(r"(?ims)(^\[GENERAL\][^\r\n]*\r?\n)(.*?)(?=^\[|\Z)")
            match = general.search(content)
            body = match.group(2) if match else ""
            # PresetPath is consumed by current ReShade. Keep the legacy key
            # synchronized and override any earlier startup selection too.
            for key in ("PresetPath", "CurrentPresetPath", "StartupPresetPath"):
                pattern = re.compile(rf"(?im)^{key}\s*=.*$")
                value = f"{key}={abs_preset_path}"
                body = pattern.sub(lambda _: value, body) if pattern.search(body) else body.rstrip() + "\n" + value + "\n"
            new_content = (content[:match.start()] + match.group(1) + body + content[match.end():]
                           if match else "[GENERAL]\n" + body.lstrip() + "\n" + content)
            install = re.compile(r"(?ims)(^\[INSTALL\][^\r\n]*\r?\n)(.*?)(?=^\[|\Z)")
            section = install.search(new_content)
            base_path = f"BasePath={ini_file.parent.resolve()}\n"
            if section:
                settings = re.sub(r"(?im)^BasePath\s*=.*(?:\r?\n|$)", "", section.group(2))
                new_content = (new_content[:section.start()] + section.group(1) + base_path + settings
                               + new_content[section.end():])
            else:
                new_content += "\n[INSTALL]\n" + base_path

            if overlay_hotkey is not None:
                # External presets used to keep an imported Home binding or
                # KeyOverlay=0 even though the host watched Companion's End key.
                key = "KeyOverlay=" + ",".join(map(str, reshade_key_data(overlay_hotkey))) + "\n"
                input_section = re.compile(r"(?ims)(^\[INPUT\][^\r\n]*\r?\n)(.*?)(?=^\[|\Z)")
                section = input_section.search(new_content)
                if section:
                    settings = re.sub(r"(?im)^KeyOverlay\s*=.*(?:\r?\n|$)", "", section.group(2))
                    new_content = (new_content[:section.start()] + section.group(1) + key + settings
                                   + new_content[section.end():])
                else:
                    new_content += "\n[INPUT]\n" + key

            handle, temp_name = tempfile.mkstemp(
                prefix=f".{ini_file.name}.", suffix=".tmp", dir=ini_file.parent
            )
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    stream.write(new_content)
                    stream.flush()
                    os.fsync(stream.fileno())
                if ini_file.exists():
                    ini_file.chmod(ini_file.stat().st_mode | stat.S_IWUSR)
                os.replace(temp_name, ini_file)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)

            logger.info(f"Successfully swapped ReShade preset to: {new_preset_path}")
            return True

        except Exception as e:
            logger.error(f"Failed to swap ReShade preset: {e}")
            return False

    @staticmethod
    def trigger_reshade_reload(reload_key: str = "Home") -> None:
        """Sends key event to force ReShade to reload shaders/preset."""
        logger.info(f"Triggering ReShade reload key: {reload_key}")
        InputSimulator.trigger_hotkey(modifiers=[], key=reload_key, hold_ms=50)

    @staticmethod
    def restore_backup(reshade_ini_path: str) -> bool:
        ini_file = Path(reshade_ini_path)
        if not ReshadeManager._is_lossless_scaling_ini(ini_file):
            logger.error("Refusing to restore ReShade config outside Lossless Scaling: %s", ini_file)
            return False
        backup = ini_file.with_suffix(".ini.bak")
        if not backup.is_file():
            return False
        try:
            shutil.copy2(backup, ini_file)
            backup.unlink()
            return True
        except Exception as e:
            logger.error(f"Failed to restore ReShade backup: {e}")
            return False

    @staticmethod
    def apply_managed_config(reshade_ini_path: str, managed_config_path: str, backup: bool = True) -> bool:
        """Atomically apply a companion-generated config only to Lossless Scaling."""
        ini_file = Path(reshade_ini_path)
        managed_file = Path(managed_config_path)
        if not ReshadeManager._is_lossless_scaling_ini(ini_file):
            logger.error("ReShade.ini must be beside LosslessScaling.exe: %s", ini_file)
            return False
        if (
            not managed_file.is_file()
            or managed_file.name.casefold() not in {"reshade.ini", "reshade.interactive.ini"}
        ):
            logger.error("Managed ReShade config does not exist: %s", managed_file)
            return False
        try:
            if backup and ini_file.exists():
                backup_path = ini_file.with_suffix(".ini.bak")
                if not backup_path.exists():
                    shutil.copy2(ini_file, backup_path)
            handle, temp_name = tempfile.mkstemp(prefix=f".{ini_file.name}.", suffix=".tmp", dir=ini_file.parent)
            try:
                with os.fdopen(handle, "wb") as stream, open(managed_file, "rb") as source:
                    shutil.copyfileobj(source, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                if ini_file.exists():
                    ini_file.chmod(ini_file.stat().st_mode | stat.S_IWUSR)
                os.replace(temp_name, ini_file)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
            logger.info("Applied managed ReShade config: %s", managed_file)
            return True
        except OSError as error:
            logger.error("Failed to apply managed ReShade config: %s", error)
            return False
