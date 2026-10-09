"""Per-profile Echo LS Add-on Manager configuration snapshots."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from ..core.models import Profile
from ..core.profile_manager import ProfileManager


class AddonConfigService:
    """Capture Echo's live config and materialize one config.json per profile."""

    def __init__(self, profile_manager: ProfileManager):
        self.profile_manager = profile_manager
        self.root = Path(profile_manager.config_dir) / "addon-configs"

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
        if profile is None or live is None or not live.is_file():
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
        """Return user state plus the small set of Companion-owned invariants."""
        configured = profile.addon_config or self.profile_manager.config.default_addon_config
        data = self._clean(configured)
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

        neural = profile.graphics.neural_render
        neural_addon = data["addons"].setdefault("DLSS5NR01", {})
        if not isinstance(neural_addon, dict):
            neural_addon = data["addons"]["DLSS5NR01"] = {}
        if neural.implementation == "lsp_neural_render":
            styles = {"standard": 0, "natural": 1, "cinematic": 2}
            neural_addon.update({
                "_enabled": True,
                "style": str(styles[neural.style]),
                "intensity": f"{neural.intensity:g}",
                "localStructure": f"{neural.local_structure:g}",
                "localTone": f"{neural.local_tone:g}",
                "skinStructure": f"{neural.skin_structure:g}",
                "useFlow": "1" if neural.use_lsfg_optical_flow else "0",
                "lsFirst": "1" if neural.prioritize_ls_gpu_work else "0",
                "composeIntensity": f"{neural.apply_strength:g}",
                "maxDelta": f"{neural.max_delta:g}",
                "hiProtect": f"{neural.protect_highlights_above:g}",
                "watchdogMs": str(neural.watchdog_ms),
            })
            if neural.sampling_resolution_preset != "custom":
                scales = {
                    "performance": 0.25, "balanced": 0.35,
                    "quality": 0.5, "full": 1.0,
                }
                neural_addon["workingScale"] = f"{scales[neural.sampling_resolution_preset]:g}"
            else:
                neural_addon.setdefault("workingScale", f"{neural.working_scale:g}")
        return data

    def materialize(self, profile: Profile, *, reshade_enabled: bool, hotkey_vk: int = 35) -> Path:
        data = self.effective(profile, reshade_enabled=reshade_enabled, hotkey_vk=hotkey_vk)
        profile.addon_config = copy.deepcopy(data)
        if profile.is_default:
            self.profile_manager.config.default_addon_config = copy.deepcopy(data)
        return self._write(self._snapshot_path(profile), data)
