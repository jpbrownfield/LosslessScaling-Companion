"""Tolerant reader/writer for Lossless Scaling's Settings.xml."""

from __future__ import annotations

import os
import logging
import shutil
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..core.models import HotkeyConfig


logger = logging.getLogger("LosslessCompanion.LosslessSettings")


class LosslessSettingsXml:
    """Reads known fields while preserving unknown XML elements on writes."""

    def __init__(self, path: Optional[str] = None):
        configured = os.path.expandvars(
            path or r"%LOCALAPPDATA%\Lossless Scaling\Settings.xml"
        )
        self.path = Path(configured)

    @staticmethod
    def _text(element: ET.Element, name: str) -> Optional[str]:
        child = element.find(name)
        return child.text if child is not None else None

    def read(self) -> Dict:
        if not self.path.is_file():
            return {"path": str(self.path), "exists": False, "profiles": []}

        try:
            root = ET.parse(self.path).getroot()
        except (ET.ParseError, OSError) as error:
            return {
                "path": str(self.path),
                "exists": True,
                "profiles": [],
                "error": str(error),
            }
        profiles: List[Dict[str, Optional[str]]] = []
        for profile in root.findall("./GameProfiles/Profile"):
            profiles.append({child.tag: child.text for child in profile})

        return {
            "path": str(self.path),
            "exists": True,
            "hotkey": self._text(root, "Hotkey"),
            "hotkeyModifierKeys": self._text(root, "HotkeyModifierKeys"),
            "profiles": profiles,
        }

    def get_profile(self, title: Optional[str] = None) -> Optional[Dict[str, Optional[str]]]:
        profiles = self.read()["profiles"]
        if title:
            for profile in profiles:
                if (profile.get("Title") or "").casefold() == title.casefold():
                    return profile
        return profiles[0] if profiles else None

    @staticmethod
    def _parse_modifiers(value: Optional[str]) -> List[str]:
        aliases = {
            "control": "ctrl",
            "ctrl": "ctrl",
            "alt": "alt",
            "shift": "shift",
            "windows": "win",
            "win": "win",
        }
        found = []
        for token in (value or "").replace("+", " ").replace(",", " ").split():
            normalized = aliases.get(token.casefold())
            if normalized and normalized not in found:
                found.append(normalized)
        order = ("ctrl", "alt", "shift", "win")
        return [item for item in order if item in found]

    @staticmethod
    def _serialize_modifiers(values: List[str]) -> str:
        native = {"ctrl": "Control", "alt": "Alt", "shift": "Shift", "win": "Windows"}
        return " ".join(native[value] for value in ("ctrl", "alt", "shift", "win") if value in values)

    def read_hotkey(self) -> Optional[HotkeyConfig]:
        """Read and canonicalize Lossless Scaling's global activation hotkey."""
        data = self.read()
        key = data.get("hotkey")
        if not data.get("exists") or not key:
            return None
        try:
            return HotkeyConfig(
                key=str(key),
                modifiers=self._parse_modifiers(data.get("hotkeyModifierKeys")),
            )
        except ValueError as error:
            logger.warning("Unsupported native Lossless Scaling hotkey: %s", error)
            return None

    def control_changes_required(
        self,
        *,
        hotkey: Optional[HotkeyConfig] = None,
        disable_auto_scale: bool = False,
    ) -> Optional[Dict[str, bool]]:
        """Inspect global helper-owned settings without changing Settings.xml."""
        if not self.path.is_file():
            return None
        try:
            root = ET.parse(self.path).getroot()
        except (ET.ParseError, OSError) as error:
            logger.error("Could not inspect helper-owned Lossless Scaling settings: %s", error)
            return None

        hotkey_changed = False
        if hotkey is not None:
            native_key = (self._text(root, "Hotkey") or "").strip().casefold()
            native_modifiers = self._parse_modifiers(self._text(root, "HotkeyModifierKeys"))
            hotkey_changed = native_key != hotkey.key.casefold() or native_modifiers != hotkey.modifiers
        auto_scale_changed = disable_auto_scale and any(
            (self._text(profile, "AutoScale") or "").strip().casefold() != "false"
            for profile in root.findall("./GameProfiles/Profile")
        )
        return {"hotkey": hotkey_changed, "auto_scale": auto_scale_changed}

    def update_control_settings(
        self,
        *,
        hotkey: Optional[HotkeyConfig] = None,
        disable_auto_scale: bool = False,
        backup: bool = True,
    ) -> Optional[Dict[str, bool]]:
        """Atomically update the helper-owned global settings in one XML write."""
        if not self.path.is_file():
            return None
        try:
            tree = ET.parse(self.path)
        except (ET.ParseError, OSError) as error:
            logger.error("Could not update helper-owned Lossless Scaling settings: %s", error)
            return None
        root = tree.getroot()
        changed = {"hotkey": False, "auto_scale": False}

        if hotkey is not None:
            key_node = root.find("Hotkey")
            if key_node is None:
                key_node = ET.SubElement(root, "Hotkey")
            modifier_node = root.find("HotkeyModifierKeys")
            if modifier_node is None:
                modifier_node = ET.SubElement(root, "HotkeyModifierKeys")
            serialized_key = hotkey.key.upper()
            serialized_modifiers = self._serialize_modifiers(hotkey.modifiers)
            if (key_node.text or "") != serialized_key or (modifier_node.text or "") != serialized_modifiers:
                key_node.text = serialized_key
                modifier_node.text = serialized_modifiers
                changed["hotkey"] = True

        if disable_auto_scale:
            for profile in root.findall("./GameProfiles/Profile"):
                node = profile.find("AutoScale")
                if node is None:
                    node = ET.SubElement(profile, "AutoScale")
                if (node.text or "").strip().casefold() != "false":
                    node.text = "false"
                    changed["auto_scale"] = True

        if any(changed.values()):
            self._write_tree(tree, backup=backup)
        return changed

    def native_auto_scale_enabled(self) -> Optional[bool]:
        """Return whether any Lossless Scaling profile has native Auto Scale enabled."""
        if not self.path.is_file():
            return None
        try:
            root = ET.parse(self.path).getroot()
        except (ET.ParseError, OSError) as error:
            logger.error("Could not inspect native Auto Scale settings: %s", error)
            return None
        return any(
            (self._text(profile, "AutoScale") or "").strip().casefold() == "true"
            for profile in root.findall("./GameProfiles/Profile")
        )

    def disable_native_auto_scale(self, *, backup: bool = True) -> bool:
        """Atomically disable native Auto Scale for every Lossless Scaling profile."""
        result = self.update_control_settings(disable_auto_scale=True, backup=backup)
        if result is None:
            logger.warning("Lossless Scaling settings not found: %s", self.path)
            return False
        if result["auto_scale"]:
            logger.info("Disabled native Auto Scale in all Lossless Scaling profiles")
        return True

    def _write_tree(self, tree: ET.ElementTree, *, backup: bool) -> None:
        """Write Settings.xml atomically, preserving the first original backup."""
        if backup:
            backup_path = self.path.with_suffix(".xml.bak")
            if not backup_path.exists():
                shutil.copy2(self.path, backup_path)

        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        os.close(fd)
        try:
            tree.write(temp_name, encoding="utf-8", xml_declaration=True)
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def update_profile(
        self,
        title: str,
        values: Dict[str, object],
        *,
        backup: bool = True,
    ) -> bool:
        """Atomically update an existing profile without discarding unknown fields."""
        if not self.path.is_file():
            return False

        tree = ET.parse(self.path)
        root = tree.getroot()
        target = None
        for profile in root.findall("./GameProfiles/Profile"):
            if (self._text(profile, "Title") or "").casefold() == title.casefold():
                target = profile
                break
        if target is None:
            return False

        for key, value in values.items():
            if not key or not key.replace("_", "").isalnum():
                raise ValueError(f"Invalid XML setting name: {key!r}")
            node = target.find(key)
            if node is None:
                node = ET.SubElement(target, key)
            if isinstance(value, bool):
                node.text = str(value).lower()
            elif value is None:
                node.text = ""
            elif not isinstance(value, (str, int, float)):
                raise ValueError(f"XML setting {key!r} must be a scalar value")
            else:
                node.text = str(value)

        self._write_tree(tree, backup=backup)
        return True
