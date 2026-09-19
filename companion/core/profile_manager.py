"""
Profile and configuration management for LS Companion.
"""

import json
import os
import hashlib
import threading
import shutil
from pathlib import Path
from typing import Optional, List
from .models import AppConfig, Profile, HotkeyConfig, ReshadeConfig, DllOverrideConfig


DEFAULT_BROWSER_EXECUTABLES = [
    "chrome.exe",
    "msedge.exe",
    "firefox.exe",
    "brave.exe",
    "vivaldi.exe",
    "opera.exe",
    "opera_gx.exe",
    "arc.exe",
    "chromium.exe",
    "duckduckgo.exe",
    "waterfox.exe",
    "librewolf.exe",
    "floorp.exe",
    "zen.exe",
    "thorium.exe",
    "iexplore.exe",
]

GAME_DEFAULT_PROFILE_ID = "default-game"
BROWSER_DEFAULT_PROFILE_ID = "default-browser"
GAME_DEFAULT_PROFILE_NAME = "Game Default"
BROWSER_DEFAULT_PROFILE_NAME = "Browser Default"


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
        default_game_profile = Profile(
            id=GAME_DEFAULT_PROFILE_ID,
            name=GAME_DEFAULT_PROFILE_NAME,
            is_default=True,
            auto_scale=False,
            custom_notes="Base profile for games and new application profiles.",
        )

        default_browser_profile = Profile(
            id=BROWSER_DEFAULT_PROFILE_ID,
            name=BROWSER_DEFAULT_PROFILE_NAME,
            target_process="chrome.exe",
            target_processes=DEFAULT_BROWSER_EXECUTABLES,
            auto_scale=False,
            hotkey=HotkeyConfig(modifiers=["ctrl", "alt"], key="s", activation_delay_ms=300),
            custom_notes="Default profile for browser fullscreen video scaling."
        )

        return AppConfig(
            host="127.0.0.1",
            port=24892,
            active_profile_id=default_game_profile.id,
            profiles=[default_game_profile, default_browser_profile]
        )

    @staticmethod
    def _migrate_legacy_seed_profiles(config: AppConfig) -> bool:
        """Replace only the two untouched profiles seeded by older releases."""
        legacy = {
            "default-chrome": Profile(
                id="default-chrome",
                name="Chrome Video Auto-Scaler",
                target_process="chrome.exe",
                auto_scale=False,
                custom_notes="Default profile for browser fullscreen video scaling.",
            ),
            "youtube-profile": Profile(
                id="youtube-profile",
                name="YouTube 2x Enhancer",
                target_process="chrome.exe",
                target_domain="youtube.com",
                auto_scale=False,
                custom_notes="Optimized profile for YouTube videos.",
            ),
        }
        removable_ids = {
            profile.id
            for profile in config.profiles
            if profile.id in legacy
            and profile.model_dump(exclude={"id"})
            == legacy[profile.id].model_dump(exclude={"id"})
        }
        changed = False
        if removable_ids:
            changed = True
            config.profiles = [
                profile for profile in config.profiles if profile.id not in removable_ids
            ]
            game = next((profile for profile in config.profiles if profile.is_default), None)
            if game is None:
                game = Profile(
                    id=GAME_DEFAULT_PROFILE_ID,
                    name=GAME_DEFAULT_PROFILE_NAME,
                    is_default=True,
                    auto_scale=False,
                    custom_notes="Base profile for games and new application profiles.",
                )
                config.profiles.insert(0, game)
            elif game.id == "lossless-default":
                game.name = GAME_DEFAULT_PROFILE_NAME

            if not any(profile.id == BROWSER_DEFAULT_PROFILE_ID for profile in config.profiles):
                config.profiles.append(Profile(
                    id=BROWSER_DEFAULT_PROFILE_ID,
                    name=BROWSER_DEFAULT_PROFILE_NAME,
                    target_process="chrome.exe",
                    target_processes=DEFAULT_BROWSER_EXECUTABLES,
                    auto_scale=False,
                    custom_notes="Default profile for browser fullscreen video scaling.",
                ))
            if not config.active_profile_id or config.active_profile_id in removable_ids:
                config.active_profile_id = game.id

        browser = next(
            (profile for profile in config.profiles if profile.id == BROWSER_DEFAULT_PROFILE_ID),
            None,
        )
        game = next(
            (
                profile for profile in config.profiles
                if profile.is_default
                and (profile.lossless_profile_title or profile.id == "lossless-default")
            ),
            None,
        )
        if game is None:
            game = next(
                (profile for profile in config.profiles if profile.id == GAME_DEFAULT_PROFILE_ID),
                None,
            )
        if game is None:
            game = next(
                (
                    profile for profile in config.profiles
                    if profile.is_default and profile.id != BROWSER_DEFAULT_PROFILE_ID
                ),
                None,
            )
        if game:
            if not game.is_default:
                game.is_default = True
                changed = True
            if game.name != GAME_DEFAULT_PROFILE_NAME:
                game.name = GAME_DEFAULT_PROFILE_NAME
                changed = True
            if game.auto_scale:
                game.auto_scale = False
                changed = True
            for other in config.profiles:
                if other.id != game.id and other.is_default:
                    other.is_default = False
                    changed = True
        if browser:
            if browser.name != BROWSER_DEFAULT_PROFILE_NAME:
                browser.name = BROWSER_DEFAULT_PROFILE_NAME
                changed = True
            extras = [
                value for value in browser.target_processes
                if value.casefold() not in {item.casefold() for item in DEFAULT_BROWSER_EXECUTABLES}
            ]
            expected_targets = [*DEFAULT_BROWSER_EXECUTABLES, *extras]
            if browser.target_processes != expected_targets:
                browser.target_processes = expected_targets
                changed = True
        # The GPU dropdown always represents exactly one routing mode. Resolve
        # legacy ambiguous states while retaining an explicitly selected GPU.
        if config.auto_route_gpu_to_display and config.preferred_scaling_gpu_device_id:
            config.preferred_scaling_gpu_device_id = None
            changed = True
        elif not config.auto_route_gpu_to_display and not config.preferred_scaling_gpu_device_id:
            config.auto_route_gpu_to_display = True
            changed = True
        if config.default_profile_auto_scale != config.disable_native_auto_scale:
            config.default_profile_auto_scale = config.disable_native_auto_scale
            changed = True
        return changed

    def load_config(self) -> AppConfig:
        if not self.config_file.exists():
            default_cfg = self._get_default_config()
            self.save_config(default_cfg)
            return default_cfg

        try:
            with open(self.config_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            config = AppConfig.model_validate(data)
            if self._migrate_legacy_seed_profiles(config):
                self.save_config(config)
            return config
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
                if proc_clean in self._profile_process_names(p):
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
                if normalized_path in self._profile_executable_paths(profile):
                    return profile
        if process_name:
            process_clean = process_name.casefold().strip()
            for profile in self.config.profiles:
                if process_clean in self._profile_process_names(profile):
                    return profile
        return None

    def add_or_update_profile(self, profile: Profile) -> None:
        existing_index = next((i for i, p in enumerate(self.config.profiles) if p.id == profile.id), None)
        if existing_index is not None:
            existing = self.config.profiles[existing_index]
            if existing.is_default or existing.id == GAME_DEFAULT_PROFILE_ID:
                profile.is_default = True
                profile.name = GAME_DEFAULT_PROFILE_NAME
                profile.auto_scale = False
                profile.target_process = None
                profile.target_executable_path = None
                profile.target_processes = []
                profile.target_executable_paths = []
                profile.target_domain = None
                profile.lossless_profile_title = existing.lossless_profile_title
                profile.lossless_profile_path = existing.lossless_profile_path
            self.config.profiles[existing_index] = profile
        else:
            self.config.profiles.append(profile)
        self.save_config()

    def ensure_lossless_default_profile(self, native_profile: Optional[dict]) -> Optional[Profile]:
        """Expose Lossless Scaling's first/native default as an editable helper profile."""
        if not native_profile:
            return None
        before = self.config.model_dump()
        title = str(native_profile.get("Title") or "Default").strip() or "Default"
        path = str(native_profile.get("Path") or "").strip() or None
        profile = next((item for item in self.config.profiles if item.is_default), None)
        if profile is None:
            profile = self.get_profile_by_id(GAME_DEFAULT_PROFILE_ID)
        if profile is None:
            profile = next(
                (
                    item for item in self.config.profiles
                    if item.lossless_profile_title
                    and item.lossless_profile_title.casefold() == title.casefold()
                ),
                None,
            )
        if profile is None:
            profile_id = (
                "lossless-default"
                if self.get_profile_by_id("lossless-default") is None
                else Profile(name="temporary").id
            )
            profile = Profile(
                id=profile_id,
                name=GAME_DEFAULT_PROFILE_NAME,
                is_default=True,
                auto_scale=False,
                lossless_profile_title=title,
                lossless_profile_path=path,
            )
            self.config.profiles.insert(0, profile)
        else:
            profile.is_default = True
            profile.name = GAME_DEFAULT_PROFILE_NAME
            profile.auto_scale = False
            profile.target_process = None
            profile.target_executable_path = None
            profile.target_processes = []
            profile.target_executable_paths = []
            profile.target_domain = None
            profile.lossless_profile_title = title
            profile.lossless_profile_path = path
        profile.native_scaling_settings = {
            key: value for key, value in native_profile.items() if key not in {"Title", "Path"}
        }
        profile.last_imported_hash = self.native_profile_hash(native_profile)
        for other in self.config.profiles:
            if other.id != profile.id:
                other.is_default = False
        if self.config.model_dump() != before:
            self.save_config()
        return profile

    def delete_profile(self, profile_id: str) -> bool:
        selected = self.get_profile_by_id(profile_id)
        if selected and (selected.is_default or selected.id == GAME_DEFAULT_PROFILE_ID):
            return False
        initial_len = len(self.config.profiles)
        self.config.profiles = [p for p in self.config.profiles if p.id != profile_id]
        if len(self.config.profiles) < initial_len:
            if self.config.active_profile_id == profile_id:
                self.config.active_profile_id = self.config.profiles[0].id if self.config.profiles else None
            self.save_config()
            return True
        return False

    def new_profile_from_default(
        self,
        name: str,
        *,
        target_process: Optional[str] = None,
        target_executable_path: Optional[str] = None,
        target_domain: Optional[str] = None,
        target_processes: Optional[List[str]] = None,
        target_executable_paths: Optional[List[str]] = None,
    ) -> Profile:
        """Clone configurable defaults while clearing identity and runtime bookkeeping."""
        template = next((profile for profile in self.config.profiles if profile.is_default), None)
        profile = (
            template.model_copy(deep=True)
            if template
            else Profile(name=name)
        )
        profile.id = Profile(name="temporary").id
        profile.name = name
        profile.is_default = False
        profile.target_process = target_process
        profile.target_executable_path = target_executable_path
        profile.target_domain = target_domain
        profile.target_processes = list(target_processes or [])
        profile.target_executable_paths = list(target_executable_paths or [])
        profile.auto_scale = self.config.disable_native_auto_scale
        profile.lossless_profile_title = None
        profile.lossless_profile_path = None
        profile.last_imported_hash = None
        profile.rtss.framerate_limit = self.config.rtss_default_static_framerate_limit
        profile.rtss.learned_framerate_limit = None
        profile.rtss.game_gpu_baseline_percent = None
        profile.rtss.game_gpu_high_water_percent = None
        profile.rtss.automatic_calibration_disabled = False
        profile.rtss.managed_profile_created = False
        profile.rtss.managed_target_process = None
        profile.rtss.managed_install_path = None
        profile.rtss.original_values = {}
        return profile

    def create_profile_from_process(self, process_name: str, display_name: Optional[str] = None) -> Profile:
        name = display_name or f"Profile for {process_name}"
        new_prof = self.new_profile_from_default(
            name,
            target_process=process_name,
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

    @classmethod
    def _profile_executable_paths(cls, profile: Profile) -> set[str]:
        values = [profile.target_executable_path, *profile.target_executable_paths]
        return {cls._normalized_executable(value) for value in values if value}

    @staticmethod
    def _profile_process_names(profile: Profile) -> set[str]:
        values = [profile.target_process, *profile.target_processes]
        if profile.target_executable_path:
            values.append(Path(profile.target_executable_path).name)
        values.extend(Path(path).name for path in profile.target_executable_paths)
        return {value.casefold().strip() for value in values if value and value.strip()}

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
                    and normalized_path in (
                        self._profile_executable_paths(profile)
                        | {self._normalized_executable(profile.lossless_profile_path)}
                    )
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
                        if native_filename in (
                            self._profile_process_names(profile)
                            | {
                                Path(profile.lossless_profile_path).name.casefold()
                                if profile.lossless_profile_path
                                else ""
                            }
                        )
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
        profile = (
            existing.model_copy(deep=True)
            if existing
            else Profile(name=title, auto_scale=False)
        )
        profile.name = profile.name or title
        profile.lossless_profile_title = title
        profile.lossless_profile_path = path
        if path:
            profile.target_executable_path = profile.target_executable_path or path
            profile.target_process = profile.target_process or Path(path).name
        profile.native_scaling_settings = {
            key: value for key, value in native_profile.items() if key not in {"Title", "Path"}
        }
        profile.last_imported_hash = self.native_profile_hash(native_profile)
        self.add_or_update_profile(profile)
        return profile
