"""
Profile and configuration management for Lossless Companion.
"""

import json
import os
from pathlib import Path
from typing import Optional, List
from .models import AppConfig, Profile, HotkeyConfig, ReshadeConfig, DllOverrideConfig


class ProfileManager:
    def __init__(self, config_dir: Optional[Path] = None):
        if config_dir is None:
            self.config_dir = Path(os.path.dirname(os.path.dirname(__file__))) / "config"
        else:
            self.config_dir = config_dir

        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.config_file = self.config_dir / "settings.json"
        self.config: AppConfig = self.load_config()

    def _get_default_config(self) -> AppConfig:
        default_chrome_profile = Profile(
            id="default-chrome",
            name="Chrome Video Auto-Scaler",
            target_process="chrome.exe",
            auto_scale_on_fullscreen=True,
            auto_scale_on_demaximize=True,
            hotkey=HotkeyConfig(modifiers=["ctrl", "alt"], key="s", activation_delay_ms=300),
            custom_notes="Default profile for browser fullscreen video scaling."
        )

        youtube_profile = Profile(
            id="youtube-profile",
            name="YouTube 2x Enhancer",
            target_process="chrome.exe",
            target_domain="youtube.com",
            auto_scale_on_fullscreen=True,
            auto_scale_on_demaximize=True,
            hotkey=HotkeyConfig(modifiers=["ctrl", "alt"], key="s", activation_delay_ms=250),
            custom_notes="Optimized profile for YouTube videos."
        )

        return AppConfig(
            host="127.0.0.1",
            port=24892,
            active_profile_id=default_chrome_profile.id,
            profiles=[default_chrome_profile, youtube_profile]
        )

    def load_config(self) -> AppConfig:
        if not self.config_file.exists():
            default_cfg = self._get_default_config()
            self.save_config(default_cfg)
            return default_cfg

        try:
            with open(self.config_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return AppConfig.model_validate(data)
        except Exception as e:
            print(f"[ProfileManager] Error loading config: {e}. Generating defaults.")
            default_cfg = self._get_default_config()
            self.save_config(default_cfg)
            return default_cfg

    def save_config(self, config: Optional[AppConfig] = None) -> None:
        if config is not None:
            self.config = config
        with open(self.config_file, "w", encoding="utf-8") as f:
            json.dump(self.config.model_dump(), f, indent=2)

    def get_profile_by_id(self, profile_id: str) -> Optional[Profile]:
        for p in self.config.profiles:
            if p.id == profile_id:
                return p
        return None

    def match_profile(self, process_name: Optional[str] = None, domain: Optional[str] = None) -> Optional[Profile]:
        """
        Finds the most specific matching profile.
        Matches domain first if available, then process name, then active profile fallback.
        """
        # 1. Exact domain match
        if domain:
            for p in self.config.profiles:
                if p.target_domain and p.target_domain.lower() in domain.lower():
                    return p

        # 2. Process name match
        if process_name:
            proc_clean = process_name.lower().strip()
            for p in self.config.profiles:
                if p.target_process and p.target_process.lower() == proc_clean:
                    return p

        # 3. Active profile or first profile fallback
        if self.config.active_profile_id:
            active = self.get_profile_by_id(self.config.active_profile_id)
            if active:
                return active

        return self.config.profiles[0] if self.config.profiles else None

    def add_or_update_profile(self, profile: Profile) -> None:
        existing_index = next((i for i, p in enumerate(self.config.profiles) if p.id == profile.id), None)
        if existing_index is not None:
            self.config.profiles[existing_index] = profile
        else:
            self.config.profiles.append(profile)
        self.save_config()

    def delete_profile(self, profile_id: str) -> bool:
        initial_len = len(self.config.profiles)
        self.config.profiles = [p for p in self.config.profiles if p.id != profile_id]
        if len(self.config.profiles) < initial_len:
            if self.config.active_profile_id == profile_id:
                self.config.active_profile_id = self.config.profiles[0].id if self.config.profiles else None
            self.save_config()
            return True
        return False

    def create_profile_from_process(self, process_name: str, display_name: Optional[str] = None) -> Profile:
        name = display_name or f"Profile for {process_name}"
        new_prof = Profile(
            name=name,
            target_process=process_name,
            auto_scale_on_fullscreen=False,
            auto_scale_on_demaximize=False,
            hotkey=HotkeyConfig(modifiers=["ctrl", "alt"], key="s")
        )
        self.add_or_update_profile(new_prof)
        return new_prof
