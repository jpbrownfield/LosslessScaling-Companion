"""
ReShade preset manager and configuration swapper.
Allows programmatic switching of ReShade presets per profile or game process.
"""

import os
import re
import shutil
import logging
from pathlib import Path
from typing import Optional, List
from .input_simulator import InputSimulator

logger = logging.getLogger("LosslessCompanion.ReshadeManager")


class ReshadeManager:
    @staticmethod
    def find_reshade_ini(app_dir: str) -> Optional[Path]:
        """Searches for ReShade.ini in the application directory."""
        dir_path = Path(app_dir)
        if not dir_path.is_dir():
            return None

        candidates = ["ReShade.ini", "reshade.ini", "ReShadeGUI.ini"]
        for cand in candidates:
            p = dir_path / cand
            if p.exists() and p.is_file():
                return p
        return None

    @staticmethod
    def swap_preset(reshade_ini_path: str, new_preset_path: str, backup: bool = True) -> bool:
        """
        Updates the CurrentPresetPath in the given ReShade.ini file.
        """
        ini_file = Path(reshade_ini_path)
        if not ini_file.exists():
            logger.error(f"ReShade.ini not found at: {reshade_ini_path}")
            return False

        preset_file = Path(new_preset_path)
        if not preset_file.exists():
            logger.warning(f"Preset file does not exist at: {new_preset_path} (swapping anyway)")

        try:
            if backup:
                bak_path = ini_file.with_suffix(".ini.bak")
                if not bak_path.exists():
                    shutil.copy2(ini_file, bak_path)

            with open(ini_file, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()

            # Pattern matches CurrentPresetPath=... or PresetPath=...
            preset_pattern = re.compile(r"^(CurrentPresetPath\s*=\s*)(.*)$", re.MULTILINE | re.IGNORECASE)
            preset_path_pattern = re.compile(r"^(PresetPath\s*=\s*)(.*)$", re.MULTILINE | re.IGNORECASE)

            abs_preset_path = str(preset_file.resolve()).replace("\\", "\\\\")

            if preset_pattern.search(content):
                new_content = preset_pattern.sub(rf"\g<1>{abs_preset_path}", content)
            elif preset_path_pattern.search(content):
                new_content = preset_path_pattern.sub(rf"\g<1>{abs_preset_path}", content)
            else:
                # Append under [GENERAL] section or at top
                if "[GENERAL]" in content.upper():
                    new_content = re.sub(r"(\[GENERAL\])", rf"\1\nCurrentPresetPath={abs_preset_path}", content, flags=re.IGNORECASE)
                else:
                    new_content = f"[GENERAL]\nCurrentPresetPath={abs_preset_path}\n\n" + content

            with open(ini_file, "w", encoding="utf-8") as f:
                f.write(new_content)

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
