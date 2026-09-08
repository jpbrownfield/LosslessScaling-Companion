"""Coordinates scaling and per-profile side effects."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, TYPE_CHECKING

from ..core.models import Profile
from ..core.profile_manager import ProfileManager
from ..core.state import AppState
from .asset_store import AssetStore
from .deployment_manager import DeploymentManager
from .graphics_resolver import GraphicsResolutionError, GraphicsResolver
from .gpu_router import GpuRouter
from .input_simulator import InputSimulator
from .ls_settings import LosslessSettingsXml
from .nvidia_profile import NvidiaProfileManager
from .reshade_manager import ReshadeManager
from .reshade_profiles import ReshadeProfileService
from .window_manager import MonitorWindowManager

if TYPE_CHECKING:
    from .process_watcher import ProcessWatcher

logger = logging.getLogger("LSCompanion.Automation")


class AutomationController:
    def __init__(
        self,
        profile_manager: ProfileManager,
        state: AppState,
        process_watcher: Optional["ProcessWatcher"] = None,
        asset_store: Optional[AssetStore] = None,
        deployment_manager: Optional[DeploymentManager] = None,
        graphics_resolver: Optional[GraphicsResolver] = None,
        window_manager: Optional[MonitorWindowManager] = None,
        scaling_state_probe: Optional[Callable[[], Optional[bool]]] = None,
        nvidia_profile_manager: Optional[NvidiaProfileManager] = None,
        gpu_router: Optional[GpuRouter] = None,
        lossless_settings: Optional[LosslessSettingsXml] = None,
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
        self.window_manager = window_manager or MonitorWindowManager()
        self.scaling_state_probe = scaling_state_probe
        self.nvidia_profile_manager = nvidia_profile_manager or NvidiaProfileManager(
            receipt_path=Path(profile_manager.config_dir) / "nvidia-profile-rollback.json"
        )
        self.gpu_router = gpu_router or GpuRouter()
        self.lossless_settings = lossless_settings or LosslessSettingsXml(
            profile_manager.config.lossless_settings_xml_path
        )
        self.reshade_profiles = ReshadeProfileService(profile_manager)

    def _lossless_scaling_dir(self) -> Optional[str]:
        """Return the sole directory in which managed graphics files may be deployed."""
        configured = self.profile_manager.config.lossless_scaling_exe_path
        if not configured:
            return None
        return str(Path(configured).parent)

    def _minimize_other_windows(self) -> None:
        try:
            self.window_manager.minimize_others_on_target_monitor(
                self.state.current_foreground_hwnd
            )
        except Exception:
            logger.exception("Scaling continued, but peer windows could not be minimized")

    def _restore_managed_windows(self) -> None:
        try:
            self.window_manager.restore_managed_windows()
        except Exception:
            logger.exception("Managed minimized windows could not be restored")

    def set_scaling(
        self,
        active: bool,
        profile: Optional[Profile] = None,
        *,
        reason: str,
        force: bool = False,
        target_pid: Optional[int] = None,
        target_hwnd: Optional[int] = None,
        park_runtime_on_stop: bool = True,
    ) -> bool:
        """Set scaling idempotently; return whether a hotkey was emitted."""
        with self._lock:
            if not force and self.state.is_scaling_active == active:
                logger.info("Scaling already %s; ignored %s request", "active" if active else "idle", reason)
                return False
            selected = profile or self.state.current_active_profile
            runtime_providers = self._runtime_providers(selected)
            configured_exe = self.profile_manager.config.lossless_scaling_exe_path
            if (
                active
                and runtime_providers
                and configured_exe
                and self.deployment_manager.runtime_packages_need_state(
                    runtime_providers, active=True
                )
            ):
                was_running = bool(
                    self.process_watcher
                    and self.process_watcher.check_is_lossless_scaling_running()
                )
                if was_running and not self.process_watcher.stop_lossless_scaling():
                    self._set_control_status("error", "Could not stop Lossless Scaling to activate graphics add-ons")
                    return False
                try:
                    changed = self.deployment_manager.set_runtime_packages_active(
                        configured_exe, runtime_providers, active=True
                    )
                except Exception as error:
                    logger.error("Could not activate runtime graphics add-ons: %s", error)
                    if was_running:
                        self.process_watcher.launch_lossless_scaling(force=True)
                    self._set_control_status("error", str(error))
                    return False
                if self.process_watcher and changed and not self.process_watcher.launch_lossless_scaling(force=True):
                    self._set_control_status("error", "Could not restart Lossless Scaling with graphics add-ons")
                    return False
            if active and (target_pid is not None or target_hwnd is not None):
                if (
                    target_pid is None
                    or target_hwnd is None
                    or self.process_watcher is None
                    or not self.process_watcher.focus_window_identity(target_pid, target_hwnd)
                ):
                    self._set_control_status(
                        "error",
                        f"Target PID {target_pid} / HWND {target_hwnd} could not be verified",
                    )
                    logger.error(
                        "Scaling activation refused because target PID %s / HWND %s could not be verified",
                        target_pid,
                        target_hwnd,
                    )
                    return False
            # Lossless Scaling exposes one global activation hotkey. Profile hotkeys
            # are retained only for migration/delay compatibility.
            hotkey = self.profile_manager.config.global_hotkey
            self._set_control_status(
                "pending",
                f"Waiting for Lossless Scaling to confirm {'activation' if active else 'deactivation'}",
            )
            emitted = InputSimulator.trigger_hotkey(
                modifiers=hotkey.modifiers,
                key=hotkey.key,
                hold_ms=hotkey.hold_delay_ms,
            )
            if not emitted:
                self._set_control_status("error", f"Hotkey injection failed for {reason}")
                logger.error("Hotkey injection failed for %s", reason)
                return False
            if not self._confirm_scaling_state(active):
                self._set_control_status(
                    "error",
                    f"Lossless Scaling did not confirm {'activation' if active else 'deactivation'}",
                )
                logger.error(
                    "Lossless Scaling did not confirm the requested %s state after the %s hotkey",
                    "active" if active else "idle",
                    reason,
                )
                return False
            self.state.mark_scaling_toggled(active)
            self._set_control_status(
                "confirmed",
                f"Lossless Scaling confirmed {'active' if active else 'idle'}",
            )
            self.state.scaling_owner_profile_id = selected.id if active and selected else None
            self.state.scaling_trigger = reason if active else None
            if active:
                self.state.scaling_target_pid = target_pid
                self.state.scaling_target_hwnd = target_hwnd
            else:
                self.state.scaling_target_pid = None
                self.state.scaling_target_hwnd = None
            if active and self.profile_manager.config.minimize_other_windows_on_scale:
                self._minimize_other_windows()
            elif not active:
                self._restore_managed_windows()
                if self.process_watcher:
                    self.process_watcher.invalidate_foreground_cache()
                if (
                    park_runtime_on_stop
                    and runtime_providers
                    and configured_exe
                    and self.process_watcher
                    and self.deployment_manager.runtime_packages_need_state(
                        runtime_providers, active=False
                    )
                ):
                    was_running = self.process_watcher.check_is_lossless_scaling_running()
                    if was_running and not self.process_watcher.stop_lossless_scaling():
                        self._set_control_status("error", "Scaling stopped, but LS could not close to park graphics add-ons")
                        return emitted
                    try:
                        changed = self.deployment_manager.set_runtime_packages_active(
                            configured_exe, runtime_providers, active=False
                        )
                    except Exception as error:
                        logger.error("Scaling stopped, but graphics add-ons could not be parked: %s", error)
                        self._set_control_status("error", str(error))
                        if was_running:
                            self.process_watcher.launch_lossless_scaling(force=True)
                        return emitted
                    if was_running and changed and not self.process_watcher.launch_lossless_scaling(force=True):
                        self._set_control_status("error", "Graphics add-ons were parked, but LS did not restart")
            return True

    @staticmethod
    def _runtime_providers(profile: Optional[Profile]) -> set[str]:
        if not profile:
            return set()
        providers = set()
        if profile.graphics.reshade.enabled or (
            profile.reshade and profile.reshade.enabled
        ):
            providers.add("reshade")
        if profile.graphics.special_k.enabled:
            providers.add("special-k")
        return providers

    def _confirm_scaling_state(self, expected: bool, timeout: float = 3.0) -> bool:
        """Wait for an authoritative LS overlay/log observation when available."""
        if self.scaling_state_probe is None:
            return True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                observed = self.scaling_state_probe()
            except Exception:
                logger.exception("Lossless Scaling confirmation probe failed")
                return False
            if observed is expected:
                return True
            time.sleep(0.1)
        return False

    def _set_control_status(self, state: str, message: str) -> None:
        self.state.scaling_control_status = {"state": state, "message": message}

    def toggle_scaling(self, profile: Optional[Profile] = None, *, reason: str) -> bool:
        return self.set_scaling(
            not self.state.is_scaling_active, profile, reason=reason, force=True
        )

    def reconcile_observed_scaling_state(self, active: bool) -> None:
        """Apply window side effects when LS changes state outside our hotkey path."""
        with self._lock:
            previous = self.state.is_scaling_active
            self.state.is_scaling_active = active
            self._set_control_status(
                "confirmed",
                f"Observed Lossless Scaling {'active' if active else 'idle'}",
            )
            if previous == active:
                return
            if active and self.profile_manager.config.minimize_other_windows_on_scale:
                self._minimize_other_windows()
            elif not active:
                self._restore_managed_windows()

    def reconcile_window_management_setting(self) -> None:
        """Apply a changed minimize setting immediately to the current scaling state."""
        with self._lock:
            if (
                self.state.is_scaling_active
                and self.profile_manager.config.minimize_other_windows_on_scale
            ):
                self._minimize_other_windows()
            else:
                self._restore_managed_windows()

    def _desired_gpu_route(self, target_hwnd: Optional[int]) -> Dict:
        config = self.profile_manager.config
        route = self.gpu_router.route_for_gpu(config.preferred_scaling_gpu_device_id)
        if config.auto_route_gpu_to_display and target_hwnd:
            detected = self.gpu_router.route_for_window(target_hwnd)
            if detected:
                route = detected
        return route

    @staticmethod
    def _gpu_route_key(route: Dict) -> str:
        return f"{int(route.get('lsGpuId', 0))}:{int(route.get('lsDisplayId', 0))}:{route.get('deviceName', '')}"

    def reconcile_gpu_route(self) -> bool:
        """Handoff active scaling when its target moves onto a differently routed display."""
        with self._lock:
            config = self.profile_manager.config
            profile = self.state.current_active_profile
            target_hwnd = self.state.scaling_target_hwnd
            target_pid = self.state.scaling_target_pid
            if not (
                config.auto_route_gpu_to_display
                and self.state.is_scaling_active
                and profile
                and target_hwnd
            ):
                return False
            route = self._desired_gpu_route(target_hwnd)
            route_key = self._gpu_route_key(route)
            if route_key == self.state.current_gpu_route_key:
                return False
            route_change = self.lossless_settings.gpu_route_changes_required(
                profile.lossless_profile_title,
                int(route.get("lsGpuId", 0)),
                int(route.get("lsDisplayId", 0)),
            )
            if route_change is None:
                self._set_control_status("error", "Lossless Scaling GPU routing settings are unavailable")
                return False
            if not route_change:
                self.state.current_gpu_route_key = route_key
                return False
            if not self.set_scaling(
                False,
                profile,
                reason="display_gpu_handoff",
                force=True,
                park_runtime_on_stop=False,
            ):
                return False
            was_running = bool(
                self.process_watcher
                and self.process_watcher.check_is_lossless_scaling_running()
            )
            if was_running and not self.process_watcher.stop_lossless_scaling():
                self._set_control_status("error", "Could not stop Lossless Scaling for GPU handoff")
                return False
            try:
                changed = self.lossless_settings.update_gpu_route(
                    profile.lossless_profile_title,
                    int(route.get("lsGpuId", 0)),
                    int(route.get("lsDisplayId", 0)),
                )
            except (OSError, ValueError) as error:
                logger.error("GPU handoff settings update failed: %s", error)
                if was_running:
                    self.process_watcher.launch_lossless_scaling(force=True)
                self.set_scaling(
                    True, profile, reason="display_gpu_handoff_recovery", force=True,
                    target_pid=target_pid, target_hwnd=target_hwnd,
                )
                self._set_control_status("error", f"GPU handoff failed: {error}")
                return False
            if not changed:
                if was_running:
                    self.process_watcher.launch_lossless_scaling(force=True)
                self.set_scaling(
                    True, profile, reason="display_gpu_handoff_recovery", force=True,
                    target_pid=target_pid, target_hwnd=target_hwnd,
                )
                self._set_control_status("error", "Lossless Scaling rejected the GPU route update")
                return False
            if was_running and not self.process_watcher.launch_lossless_scaling(force=True):
                self._set_control_status("error", "Could not restart Lossless Scaling after GPU handoff")
                return False
            self.state.current_gpu_route_key = route_key
            self.asset_store.audit(
                "gpu_route_handoff",
                profile_id=profile.id,
                gpu_id=int(route.get("lsGpuId", 0)),
                display_id=int(route.get("lsDisplayId", 0)),
            )
            return self.set_scaling(
                True,
                profile,
                reason="display_gpu_handoff",
                force=True,
                target_pid=target_pid,
                target_hwnd=target_hwnd,
            )

    def activate_profile(
        self,
        profile: Optional[Profile],
        exe_path: Optional[str] = None,
        *,
        force: bool = False,
        target_pid: Optional[int] = None,
        target_hwnd: Optional[int] = None,
        suppress_auto_scale: bool = False,
    ) -> None:
        with self._lock:
            previous = self.state.current_active_profile
            same_profile = bool(previous and profile and previous.id == profile.id)
            if not force and previous and profile and previous.id == profile.id:
                return

            if not force and self.state.is_scaling_active and previous and not same_profile:
                previous_is_browser = self._is_browser_profile(previous)
                incoming_is_game = bool(profile and not self._is_browser_profile(profile))
                if target_pid is not None and previous_is_browser and incoming_is_game:
                    if not self.set_scaling(False, previous, reason="browser_to_game_handoff"):
                        logger.error("Game profile handoff aborted because browser scaling did not stop")
                        return
                else:
                    logger.info(
                        "Keeping scaled profile '%s' active while foreground changed to '%s'",
                        previous.name,
                        profile.name if profile else "an unprofiled application",
                    )
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
                runtime_providers = self._runtime_providers(profile)
                will_auto_scale = bool(
                    profile
                    and profile.auto_scale
                    and self.profile_manager.config.disable_native_auto_scale
                    and self.state.auto_scale_enabled
                    and not suppress_auto_scale
                )
                park_runtime = bool(
                    profile and runtime_providers and not self.state.is_scaling_active and not will_auto_scale
                )
                runtime_state_change = bool(
                    park_runtime
                    and self.deployment_manager.runtime_packages_need_state(
                        runtime_providers, active=False
                    )
                )
                deployment_change = deployment_change or runtime_state_change
                gpu_route = self._desired_gpu_route(target_hwnd) if profile else None
                gpu_route_change = self.lossless_settings.gpu_route_changes_required(
                    profile.lossless_profile_title,
                    int(gpu_route.get("lsGpuId", 0)),
                    int(gpu_route.get("lsDisplayId", 0)),
                ) if profile and gpu_route else False
                deployment_change = deployment_change or bool(gpu_route_change)
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
                    park_runtime=park_runtime,
                    trigger_runtime_actions=not restart_lossless_scaling,
                    target_pid=target_pid,
                    target_hwnd=target_hwnd,
                    suppress_auto_scale=suppress_auto_scale,
                    gpu_route=gpu_route,
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
                self._trigger_profile_runtime_actions(
                    profile,
                    reshade_swapped=reshade_swapped,
                    target_pid=target_pid,
                    target_hwnd=target_hwnd,
                    suppress_auto_scale=suppress_auto_scale,
                )

    def _apply_profile_transition(
        self,
        previous: Optional[Profile],
        profile: Optional[Profile],
        same_profile: bool,
        deployment_files: List[Dict],
        *,
        park_runtime: bool,
        trigger_runtime_actions: bool,
        target_pid: Optional[int],
        target_hwnd: Optional[int],
        suppress_auto_scale: bool,
        gpu_route: Optional[Dict] = None,
    ) -> bool:
        """Clean the old profile and apply the new one while its DLL host is stopped."""
        reshade_swapped = False
        if previous:
            self._cleanup_profile(previous)

        configured_exe = self.profile_manager.config.lossless_scaling_exe_path
        if configured_exe:
            if gpu_route is not None:
                route_change = self.lossless_settings.gpu_route_changes_required(
                    profile.lossless_profile_title if profile else None,
                    int(gpu_route.get("lsGpuId", 0)),
                    int(gpu_route.get("lsDisplayId", 0)),
                )
                route_updated = self.lossless_settings.update_gpu_route(
                    profile.lossless_profile_title if profile else None,
                    int(gpu_route.get("lsGpuId", 0)),
                    int(gpu_route.get("lsDisplayId", 0)),
                ) if route_change else False
                if route_change is False or route_updated:
                    self.state.current_gpu_route_key = self._gpu_route_key(gpu_route)
            smooth_motion = bool(
                profile
                and profile.graphics.special_k.enabled
                and profile.graphics.special_k.experimental_smooth_motion
            )
            previous_smooth_motion = bool(
                previous
                and previous.graphics.special_k.enabled
                and previous.graphics.special_k.experimental_smooth_motion
            )
            if smooth_motion or previous_smooth_motion:
                changed = self.nvidia_profile_manager.set_lossless_scaling_smooth_motion(
                    configured_exe, smooth_motion
                )
                if changed:
                    self.asset_store.audit(
                        "nvidia_application_profile_updated",
                        executable=str(Path(configured_exe).resolve()),
                        setting="smooth_motion",
                        enabled=smooth_motion,
                    )
            manifest = self.deployment_manager.apply(
                profile_id=profile.id if profile else None,
                lossless_scaling_exe=configured_exe,
                files=deployment_files,
            )
            if profile:
                self.asset_store.write_profile_manifest(profile.id, manifest)
                if park_runtime:
                    self.deployment_manager.set_runtime_packages_active(
                        configured_exe,
                        self._runtime_providers(profile),
                        active=False,
                    )

        self.state.current_active_profile = profile
        if not profile:
            return False

        if profile.reshade and profile.reshade.enabled:
            reshade = profile.reshade
            lossless_dir = self._lossless_scaling_dir()
            reshade_ini_path = str(Path(lossless_dir) / "ReShade.ini") if lossless_dir else None
            managed_config = (
                self.reshade_profiles.config_path(reshade.managed_profile_id)
                if reshade.managed_profile_id
                else None
            )
            if reshade_ini_path and managed_config:
                backup_path = Path(reshade_ini_path).with_suffix(".ini.bak")
                owned_backup = not backup_path.exists()
                if ReshadeManager.apply_managed_config(reshade_ini_path, str(managed_config)):
                    reshade_swapped = True
                    if owned_backup:
                        self._reshade_inis[profile.id] = reshade_ini_path
            elif reshade_ini_path and reshade.preset_path:
                backup_path = Path(reshade_ini_path).with_suffix(".ini.bak")
                owned_backup = not backup_path.exists()
                if ReshadeManager.swap_preset(reshade_ini_path, reshade.preset_path):
                    reshade_swapped = True
                    if owned_backup:
                        self._reshade_inis[profile.id] = reshade_ini_path

        if trigger_runtime_actions:
            self._trigger_profile_runtime_actions(
                profile,
                reshade_swapped=reshade_swapped,
                target_pid=target_pid,
                target_hwnd=target_hwnd,
                suppress_auto_scale=suppress_auto_scale,
            )
        return reshade_swapped

    def _trigger_profile_runtime_actions(
        self,
        profile: Optional[Profile],
        *,
        reshade_swapped: bool,
        target_pid: Optional[int] = None,
        target_hwnd: Optional[int] = None,
        suppress_auto_scale: bool = False,
    ) -> None:
        if not profile:
            return
        if reshade_swapped and profile.reshade and profile.reshade.reload_hotkey:
            ReshadeManager.trigger_reshade_reload(profile.reshade.reload_hotkey)
        if (
            profile.auto_scale
            and self.profile_manager.config.disable_native_auto_scale
            and self.state.auto_scale_enabled
            and not suppress_auto_scale
        ):
            if target_pid is not None and profile.hotkey.activation_delay_ms:
                time.sleep(profile.hotkey.activation_delay_ms / 1000.0)
            self.set_scaling(
                True,
                profile,
                reason="focus",
                target_pid=target_pid,
                target_hwnd=target_hwnd,
            )

    @staticmethod
    def _is_browser_profile(profile: Profile) -> bool:
        if profile.target_domain:
            return True
        return (profile.target_process or "").casefold() in {
            "chrome.exe", "msedge.exe", "brave.exe", "firefox.exe", "opera.exe", "vivaldi.exe"
        }

    def _cleanup_profile(self, profile: Profile) -> None:
        reshade_ini = self._reshade_inis.pop(profile.id, None)
        if reshade_ini:
            if not ReshadeManager.restore_backup(reshade_ini):
                ini_path = Path(reshade_ini)
                if ReshadeManager._is_lossless_scaling_ini(ini_path):
                    ini_path.unlink(missing_ok=True)

    def revert_managed_addons(self) -> Dict:
        """Restore/remove only add-on files recorded as owned by the helper."""
        with self._lock:
            nvidia_reverted = self.nvidia_profile_manager.restore_managed_changes()
            for profile_id, reshade_ini in list(self._reshade_inis.items()):
                if not ReshadeManager.restore_backup(reshade_ini):
                    raise RuntimeError(
                        f"Could not restore the managed ReShade configuration for {profile_id}"
                    )
                self._reshade_inis.pop(profile_id, None)

            configured_exe = self.profile_manager.config.lossless_scaling_exe_path or ""
            manifest = self.deployment_manager.apply(
                profile_id=None,
                lossless_scaling_exe=configured_exe,
                files=[],
            )
            self.state.current_active_profile = None
            self.state.scaling_owner_profile_id = None
            self.state.scaling_trigger = None
            self._restore_managed_windows()
            manifest["nvidiaProfileReverted"] = nvidia_reverted
            return manifest

    def shutdown(self) -> None:
        with self._lock:
            for reshade_ini in list(self._reshade_inis.values()):
                ReshadeManager.restore_backup(reshade_ini)
            self._reshade_inis.clear()
            self._restore_managed_windows()
