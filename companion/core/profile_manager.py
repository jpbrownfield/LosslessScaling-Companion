"""
Profile and configuration management for Lossless Companion.
"""

import json
import os
import hashlib
import threading
import shutil
from pathlib import Path
from typing import Optional, List
from .models import AppConfig, Profile, HotkeyConfig, ReshadeConfig, DllOverrideConfig


class ProfileManager:
    def __init__(self, config_dir: Optional[Path] = None):
        self._lock = threading.RLock()
        if config_dir is None:
            legacy_dir = Path(os.path.dirname(os.path.dirname(__file__))) / "config"
            local_root = os.environ.get("LOCALAPPDATA")
            self.config_dir = (
                Path(local_root) / "LosslessScalingHelper"
                if local_root
                else legacy_dir
            )
            self.config_dir.mkdir(parents=True, exist_ok=True)
            legacy_settings = legacy_dir / "settings.json"
            new_settings = self.config_dir / "settings.json"
            if self.config_dir != legacy_dir and not new_settings.exists() and legacy_settings.is_file():
                shutil.copy2(legacy_settings, new_settings)
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
        with self._lock:
            if config is not None:
                self.config = config
            temp_file = self.config_file.with_suffix(".json.tmp")
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(self.config.model_dump(), f, indent=2)
            os.replace(temp_file, self.config_file)

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
            domain_clean = domain.casefold().strip().rstrip(".")
            for p in self.config.profiles:
                if p.target_domain:
                    target = p.target_domain.casefold().strip().lstrip(".").rstrip(".")
                    if domain_clean == target or domain_clean.endswith(f".{target}"):
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

    def match_target_profile(
        self,
        process_name: Optional[str] = None,
        domain: Optional[str] = None,
        executable_path: Optional[str] = None,
    ) -> Optional[Profile]:
        """Return only an explicit process/domain match, without a fallback."""
        if domain:
            domain_clean = domain.casefold().strip().rstrip(".")
            for profile in self.config.profiles:
                if profile.target_domain:
                    target = profile.target_domain.casefold().strip().lstrip(".").rstrip(".")
                    if domain_clean == target or domain_clean.endswith(f".{target}"):
                        return profile
        normalized_path = self._normalized_executable(executable_path)
        if normalized_path:
            for profile in self.config.profiles:
                if normalized_path == self._normalized_executable(profile.target_executable_path):
                    return profile
        if process_name:
            process_clean = process_name.casefold().strip()
            for profile in self.config.profiles:
                if profile.target_process and profile.target_process.casefold() == process_clean:
                    return profile
        return None

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
            target_executable_path=None,
            auto_scale_on_fullscreen=False,
            auto_scale_on_demaximize=False,
            hotkey=HotkeyConfig(modifiers=["ctrl", "alt"], key="s")
        )
        self.add_or_update_profile(new_prof)
        return new_prof

    @staticmethod
    def native_profile_hash(native_profile: dict) -> str:
        canonical = json.dumps(native_profile, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _normalized_executable(value: Optional[str]) -> str:
        return os.path.normcase(os.path.normpath(value.strip())) if value and value.strip() else ""

    def preview_native_imports(self, native_profiles: List[dict]) -> List[dict]:
        """Return non-mutating match suggestions for native Lossless Scaling profiles."""
        preview = []
        for native in native_profiles:
            title = str(native.get("Title") or "").strip()
            path = str(native.get("Path") or "").strip()
            normalized_path = self._normalized_executable(path)
            exact = next(
                (
                    profile
                    for profile in self.config.profiles
                    if normalized_path
                    and normalized_path
                    in {
                        self._normalized_executable(profile.lossless_profile_path),
                        self._normalized_executable(profile.target_executable_path),
                    }
                ),
                None,
            )
            match_basis = "path" if exact else None
            native_filename = Path(path).name.casefold() if path else ""
            if exact is None and native_filename:
                exact = next(
                    (
                        profile
                        for profile in self.config.profiles
                        if native_filename
                        in {
                            (profile.target_process or "").casefold(),
                            Path(profile.target_executable_path).name.casefold()
                            if profile.target_executable_path
                            else "",
                            Path(profile.lossless_profile_path).name.casefold()
                            if profile.lossless_profile_path
                            else "",
                        }
                    ),
                    None,
                )
                if exact:
                    match_basis = "filename"
            if exact is None and title:
                exact = next(
                    (
                        profile
                        for profile in self.config.profiles
                        if (profile.lossless_profile_title or profile.name).casefold() == title.casefold()
                    ),
                    None,
                )
                if exact:
                    match_basis = "title"
            digest = self.native_profile_hash(native)
            preview.append(
                {
                    "title": title,
                    "path": path,
                    "hash": digest,
                    "suggestedProfileId": exact.id if exact else None,
                    "suggestedProfileName": exact.name if exact else None,
                    "matchBasis": match_basis,
                    "status": (
                        "unchanged"
                        if exact and exact.last_imported_hash == digest
                        else "changed"
                        if exact and exact.last_imported_hash
                        else "new"
                    ),
                }
            )
        return preview

    def import_native_profile(
        self,
        native_profile: dict,
        *,
        existing_profile_id: Optional[str] = None,
    ) -> Profile:
        """Create or refresh one helper profile without deleting helper-only data."""
        title = str(native_profile.get("Title") or "").strip()
        if not title:
            raise ValueError("Native profile is missing Title")
        path = str(native_profile.get("Path") or "").strip() or None
        existing = self.get_profile_by_id(existing_profile_id) if existing_profile_id else None
        profile = existing.model_copy(deep=True) if existing else Profile(name=title)
        profile.name = profile.name or title
        profile.lossless_profile_title = title
        profile.lossless_profile_path = path
        if path:
            profile.target_executable_path = profile.target_executable_path or path
            profile.target_process = profile.target_process or Path(path).name
        profile.native_scaling_settings = {
            key: value for key, value in native_profile.items() if key not in {"Title", "Path"}
        }
        if self.config.disable_native_auto_scale:
            profile.native_scaling_settings["AutoScale"] = "false"
        profile.last_imported_hash = self.native_profile_hash(native_profile)
        self.add_or_update_profile(profile)
        return profile
