"""Per-profile Echo LS Add-on Manager configuration snapshots."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from ..core.models import Profile
from ..core.profile_manager import ProfileManager
from .addon_schema import ADDONS, DEFAULT_ADDON_IDS


class AddonConfigService:
    """Capture Echo's live config and materialize one config.json per profile."""

    def __init__(self, profile_manager: ProfileManager):
        self.profile_manager = profile_manager
        self.root = Path(profile_manager.config_dir) / "addon-configs"
        self._pending_profiles: set[str] = set()
        self.available_addon_ids = set(DEFAULT_ADDON_IDS)

    def catalog(self, packages=()) -> list[Dict]:
        """Read names from installed manifests; never execute an add-on to edit it."""
        entries = {item["id"]: dict(item, installed=item["id"] in {"WindowedMode", "ReShadePassthrough", "GpuLimit", "Recorder", "SuperLog"}) for item in ADDONS}
        manifests = []
        for package in packages:
            if package.get("provider") != "ls-addon-manager":
                continue
            root = Path(package["payload_path"]).resolve()
            for item in package.get("files", []):
                manifests.append((root / item["relative_path"], root))
        live = self.live_path(getattr(self.profile_manager.config, "lossless_scaling_exe_path", None))
        if live and live.parent.is_dir():
            manifests.extend((path, live.parent.resolve()) for path in live.parent.glob("*/addon.json"))
        for path, root in manifests:
            try:
                path = path.resolve()
                if root not in path.parents or path.name.casefold() != "addon.json" or path.stat().st_size > 1024 * 1024:
                    continue
                metadata = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(metadata, dict):
                    continue
                addon_id = path.parent.name
                entry = entries.setdefault(addon_id, dict(id=addon_id, fields=[]))
                entry.update(name=metadata.get("name") or addon_id,
                             description=metadata.get("description") or "Installed Echo add-on.", installed=True)
                self.available_addon_ids.add(addon_id)
            except (OSError, ValueError, TypeError):
                continue
        return list(entries.values())

    def mark_edited(self, profile: Profile) -> None:
        self._pending_profiles.add(profile.id)

    def mark_applied(self, profile: Profile) -> None:
        self._pending_profiles.discard(profile.id)

    def has_pending(self, profile: Profile) -> bool:
        return profile.id in self._pending_profiles

    @staticmethod
    def _clean(value: object) -> Dict[str, Any]:
        if not isinstance(value, dict):
            return {"global": {}, "addons": {}}
        data = copy.deepcopy(value)
        if not isinstance(data.get("global"), dict):
            data["global"] = {}
        if not isinstance(data.get("addons"), dict):
            data["addons"] = {}
        return data

    def _snapshot_path(self, profile: Optional[Profile]) -> Path:
        name = profile.id if profile and not profile.is_default else "default"
        return self.root / name / "config.json"

    @staticmethod
    def live_path(lossless_scaling_exe: Optional[str]) -> Optional[Path]:
        if not lossless_scaling_exe:
            return None
        executable = Path(lossless_scaling_exe).resolve()
        if executable.name.casefold() != "losslessscaling.exe":
            return None
        return executable.parent / "addons" / "config.json"

    def _write(self, path: Path, data: Dict[str, Any]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
        return path

    def capture(self, profile: Optional[Profile], lossless_scaling_exe: Optional[str]) -> bool:
        """Capture an atomic Echo write into the active profile/default snapshot."""
        live = self.live_path(lossless_scaling_exe)
        if profile is None or self.has_pending(profile) or live is None or not live.is_file():
            return False
        try:
            data = self._clean(json.loads(live.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            return False
        if profile.is_default:
            self.profile_manager.config.default_addon_config = data
        profile.addon_config = copy.deepcopy(data)
        self._write(self._snapshot_path(profile), data)
        self.profile_manager.save_config()
        return True

    def effective(self, profile: Profile, *, reshade_enabled: bool, hotkey_vk: int = 35) -> Dict[str, Any]:
        """Return Echo-owned profile state plus Companion's host invariants.

        ``addon_config`` is authoritative.  The NeuralRender model remains in
        the profile schema only to read older Companion profiles and select a
        user-supplied runtime; it must not overwrite settings subsequently
        edited in Echo or in the complete profile editor.
        """
        configured = profile.addon_config or self.profile_manager.config.default_addon_config
        data = self._clean(configured)
        if not profile.addon_config and not profile.is_default:
            for section in data["addons"].values():
                if isinstance(section, dict):
                    section["_enabled"] = False
        global_settings = data["global"]
        ui = global_settings.setdefault("ui", {})
        updates = global_settings.setdefault("updates", {})
        if not isinstance(ui, dict):
            ui = global_settings["ui"] = {}
        if not isinstance(updates, dict):
            updates = global_settings["updates"] = {}
        ui["open_on_start"] = False
        updates["check"] = False
        global_settings["auto_load"] = True

        # Echo may load an add-on when its entry/flag is absent. Opt in only
        # through an explicit boolean, including for legacy NeuralRender profiles.
        for addon_id in self.available_addon_ids | set(data["addons"]):
            section = data["addons"].get(addon_id)
            if not isinstance(section, dict):
                section = data["addons"][addon_id] = {}
            if "_enabled" not in section:
                section["_enabled"] = False
            else:
                section["_enabled"] = section["_enabled"] is True

        if reshade_enabled:
            passthrough = data["addons"].setdefault("ReShadePassthrough", {})
            if not isinstance(passthrough, dict):
                passthrough = data["addons"]["ReShadePassthrough"] = {}
            passthrough["_enabled"] = True
            passthrough.setdefault("hotkey_vk", str(hotkey_vk))
            passthrough.setdefault("hotkey_ctrl", "0")
            passthrough.setdefault("hotkey_alt", "0")
            passthrough.setdefault("hotkey_shift", "0")
            passthrough.setdefault("auto_click_repress", "1")
        return data

    def materialize(self, profile: Profile, *, reshade_enabled: bool, hotkey_vk: int = 35) -> Path:
        data = self.effective(profile, reshade_enabled=reshade_enabled, hotkey_vk=hotkey_vk)
        profile.addon_config = copy.deepcopy(data)
        neural_enabled = data["addons"]["DLSS5NR01"]["_enabled"] is True
        profile.graphics.neural_render.implementation = "lsp_neural_render" if neural_enabled else "disabled"
        profile.graphics.neural_render.package.enabled = neural_enabled
        if profile.is_default:
            self.profile_manager.config.default_addon_config = copy.deepcopy(data)
        return self._write(self._snapshot_path(profile), data)
