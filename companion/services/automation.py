"""Coordinates scaling and per-profile side effects."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Dict, List, Optional, TYPE_CHECKING

from ..core.models import Profile
from ..core.profile_manager import ProfileManager
from ..core.state import AppState
from .asset_store import AssetStore
from .deployment_manager import DeploymentManager
from .graphics_resolver import GraphicsResolutionError, GraphicsResolver
from .input_simulator import InputSimulator
from .reshade_manager import ReshadeManager

if TYPE_CHECKING:
    from .process_watcher import ProcessWatcher

logger = logging.getLogger("LosslessCompanion.Automation")


class AutomationController:
    def __init__(
        self,
        profile_manager: ProfileManager,
        state: AppState,
        process_watcher: Optional["ProcessWatcher"] = None,
        asset_store: Optional[AssetStore] = None,
        deployment_manager: Optional[DeploymentManager] = None,
        graphics_resolver: Optional[GraphicsResolver] = None,
    ):
        self.profile_manager = profile_manager
        self.state = state
        self._lock = threading.RLock()
        self._reshade_inis: Dict[str, str] = {}
        self.process_watcher = process_watcher
        fallback_store = Path(profile_manager.config_dir) / "managed-assets"
        self.asset_store = asset_store or AssetStore(
            profile_manager.config.asset_store_path or str(fallback_store)
        )
        self.deployment_manager = deployment_manager or DeploymentManager(self.asset_store)
        self.graphics_resolver = graphics_resolver or GraphicsResolver(self.asset_store)

    def _lossless_scaling_dir(self) -> Optional[str]:
        """Return the sole directory in which managed graphics files may be deployed."""
        configured = self.profile_manager.config.lossless_scaling_exe_path
        if not configured:
            return None
        return str(Path(configured).parent)

    def set_scaling(
        self,
        active: bool,
        profile: Optional[Profile] = None,
        *,
        reason: str,
        force: bool = False,
    ) -> bool:
        """Set scaling idempotently; return whether a hotkey was emitted."""
        with self._lock:
            if not force and self.state.is_scaling_active == active:
                logger.info("Scaling already %s; ignored %s request", "active" if active else "idle", reason)
                return False
            selected = profile or self.state.current_active_profile
            # Lossless Scaling exposes one global activation hotkey. Profile hotkeys
            # are retained only for migration/delay compatibility.
            hotkey = self.profile_manager.config.global_hotkey
            emitted = InputSimulator.trigger_hotkey(
                modifiers=hotkey.modifiers,
                key=hotkey.key,
                hold_ms=hotkey.hold_delay_ms,
            )
            if not emitted:
                logger.error("Hotkey injection failed for %s", reason)
                return False
            self.state.mark_scaling_toggled(active)
            self.state.scaling_owner_profile_id = selected.id if active and selected else None
            self.state.scaling_trigger = reason if active else None
            return True

    def toggle_scaling(self, profile: Optional[Profile] = None, *, reason: str) -> bool:
        return self.set_scaling(
            not self.state.is_scaling_active, profile, reason=reason, force=True
        )

    def activate_profile(
        self,
        profile: Optional[Profile],
        exe_path: Optional[str] = None,
        *,
        force: bool = False,
    ) -> None:
        with self._lock:
            previous = self.state.current_active_profile
            same_profile = bool(previous and profile and previous.id == profile.id)
            if not force and previous and profile and previous.id == profile.id:
                return

            try:
                configured_exe = self.profile_manager.config.lossless_scaling_exe_path
                deployment_files = self.graphics_resolver.resolve(
                    profile, lossless_scaling_exe=configured_exe
                )
                deployment_change = bool(
                    configured_exe
                    and self.deployment_manager.needs_change(
                        profile.id if profile else None,
                        deployment_files,
                        lossless_scaling_exe=configured_exe,
                    )
                )
            except (GraphicsResolutionError, OSError, ValueError, RuntimeError) as error:
                logger.error("Profile '%s' graphics plan is invalid: %s", profile.name if profile else "none", error)
                return
            restart_lossless_scaling = False
            restart_succeeded = True
            reshade_swapped = False
            transition_succeeded = False
            if deployment_change and self.process_watcher:
                restart_lossless_scaling = self.process_watcher.check_is_lossless_scaling_running()
                if restart_lossless_scaling and not self.process_watcher.stop_lossless_scaling():
                    logger.error(
                        "Profile '%s' was not activated because Lossless Scaling could not be stopped",
                        profile.name if profile else "none",
                    )
                    return

            try:
                reshade_swapped = self._apply_profile_transition(
                    previous,
                    profile,
                    same_profile,
                    deployment_files,
                    trigger_runtime_actions=not restart_lossless_scaling,
                )
                transition_succeeded = True
            except Exception as error:
                logger.error("Profile '%s' activation failed: %s", profile.name if profile else "none", error)
            finally:
                if restart_lossless_scaling and self.process_watcher:
                    restart_succeeded = self.process_watcher.launch_lossless_scaling(force=True)
                    if not restart_succeeded:
                        logger.error("DLL profile was applied, but Lossless Scaling could not be restarted")

            if transition_succeeded and restart_lossless_scaling and restart_succeeded:
                self._trigger_profile_runtime_actions(profile, reshade_swapped=reshade_swapped)

    def _apply_profile_transition(
        self,
        previous: Optional[Profile],
        profile: Optional[Profile],
        same_profile: bool,
        deployment_files: List[Dict],
        *,
        trigger_runtime_actions: bool,
    ) -> bool:
        """Clean the old profile and apply the new one while its DLL host is stopped."""
        reshade_swapped = False
        if previous:
            if (
                not same_profile
                and previous.auto_scale_on_blur
                and self.state.scaling_trigger == "focus"
            ):
                self.set_scaling(False, previous, reason="blur")
            self._cleanup_profile(previous)

        configured_exe = self.profile_manager.config.lossless_scaling_exe_path
        if configured_exe:
            manifest = self.deployment_manager.apply(
                profile_id=profile.id if profile else None,
                lossless_scaling_exe=configured_exe,
                files=deployment_files,
            )
            if profile:
                self.asset_store.write_profile_manifest(profile.id, manifest)

        self.state.current_active_profile = profile
        if not profile:
            return False

        if profile.reshade and profile.reshade.enabled:
            reshade = profile.reshade
            lossless_dir = self._lossless_scaling_dir()
            reshade_ini_path = str(Path(lossless_dir) / "ReShade.ini") if lossless_dir else None
            if reshade_ini_path and reshade.preset_path:
                backup_path = Path(reshade_ini_path).with_suffix(".ini.bak")
                owned_backup = not backup_path.exists()
                if ReshadeManager.swap_preset(reshade_ini_path, reshade.preset_path):
                    reshade_swapped = True
                    if owned_backup:
                        self._reshade_inis[profile.id] = reshade_ini_path

        if trigger_runtime_actions:
            self._trigger_profile_runtime_actions(profile, reshade_swapped=reshade_swapped)
        return reshade_swapped

    def _trigger_profile_runtime_actions(
        self, profile: Optional[Profile], *, reshade_swapped: bool
    ) -> None:
        if not profile:
            return
        if reshade_swapped and profile.reshade and profile.reshade.reload_hotkey:
            ReshadeManager.trigger_reshade_reload(profile.reshade.reload_hotkey)
        if profile.auto_scale_on_focus and self.state.auto_scale_enabled:
            self.set_scaling(True, profile, reason="focus")

    def _cleanup_profile(self, profile: Profile) -> None:
        reshade_ini = self._reshade_inis.pop(profile.id, None)
        if reshade_ini:
            ReshadeManager.restore_backup(reshade_ini)

    def shutdown(self) -> None:
        with self._lock:
            for reshade_ini in list(self._reshade_inis.values()):
                ReshadeManager.restore_backup(reshade_ini)
            self._reshade_inis.clear()
