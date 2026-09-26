"""Coordinates scaling and per-profile side effects."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, TYPE_CHECKING

from ..core.models import Profile
from ..core.profile_manager import DEFAULT_BROWSER_EXECUTABLES, ProfileManager
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
    AUTO_SCALE_MAX_ATTEMPTS = 6
    AUTO_SCALE_CONFIRMATION_SECONDS = 5.0

    def __init__(
        self,
        profile_manager: ProfileManager,
        state: AppState,
        process_watcher: Optional["ProcessWatcher"] = None,
        asset_store: Optional[AssetStore] = None,
        deployment_manager: Optional[DeploymentManager] = None,
        graphics_resolver: Optional[GraphicsResolver] = None,
        window_manager: Optional[MonitorWindowManager] = None,
        scaling_state_probe: Optional[Callable[[], Optional[object]]] = None,
        nvidia_profile_manager: Optional[NvidiaProfileManager] = None,
        gpu_router: Optional[GpuRouter] = None,
        lossless_settings: Optional[LosslessSettingsXml] = None,
        hotkey_trigger: Optional[Callable[..., bool]] = None,
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
        self._last_confirmation_wrong_target_pid: Optional[int] = None
        self.scaling_confirmation_begin: Optional[Callable[[bool], None]] = None
        self.scaling_confirmation_end: Optional[Callable[[], None]] = None
        self.nvidia_profile_manager = nvidia_profile_manager or NvidiaProfileManager(
            receipt_path=Path(profile_manager.config_dir) / "nvidia-profile-rollback.json"
        )
        self.gpu_router = gpu_router or GpuRouter()
        self.lossless_settings = lossless_settings or LosslessSettingsXml(
            profile_manager.config.lossless_settings_xml_path
        )
        # Keep the production default resolved at call time so tests and host
        # integrations can patch the Win32 sender after construction.
        self.hotkey_trigger = hotkey_trigger
        self.reshade_profiles = ReshadeProfileService(profile_manager)

    def _lossless_scaling_dir(self) -> Optional[str]:
        """Return the sole directory in which managed graphics files may be deployed."""
        configured = self.profile_manager.config.lossless_scaling_exe_path
        if not configured:
            return None
        return str(Path(configured).parent)

    def _minimize_other_windows(
        self,
        target_hwnd: Optional[int] = None,
        target_pid: Optional[int] = None,
    ) -> None:
        verified_hwnd = target_hwnd or self.state.scaling_target_hwnd
        verified_pid = target_pid or self.state.scaling_target_pid
        if not verified_hwnd and not verified_pid:
            logger.warning(
                "Skipping peer minimization because no verified scaling target is available"
            )
            return
        try:
            self.window_manager.minimize_others_on_target_monitor(
                verified_hwnd,
                target_pid=verified_pid,
            )
        except Exception:
            logger.exception("Scaling continued, but peer windows could not be minimized")

    @staticmethod
    def _profile_allows_monitor_edge_snap(profile: Optional[Profile]) -> bool:
        """Snap only frame-generation/no-spatial-scale fullscreen profiles."""
        if not profile:
            return False
        settings = profile.native_scaling_settings
        scaling_type = str(settings.get("ScalingType") or "").strip().casefold()
        windowed = str(settings.get("WindowedMode") or "false").strip().casefold()
        return scaling_type == "off" and windowed not in {"1", "true", "yes", "on"}

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
            if (
                active
                and target_hwnd
            ):
                profile_gpu_id, profile_display_id = self._profile_gpu_route_ids(selected)
                if profile_gpu_id or profile_display_id:
                    explicit_route = self.gpu_router.route_for_ls_ids(
                        profile_gpu_id, profile_display_id
                    )
                    device_name = str(explicit_route.get("deviceName") or "")
                    if device_name:
                        try:
                            if not self.window_manager.move_window_to_display(
                                target_hwnd, device_name
                            ):
                                logger.warning(
                                    "Could not move HWND %s to explicit profile display %s before scaling",
                                    target_hwnd, device_name,
                                )
                        except Exception:
                            logger.exception(
                                "Scaling continued without moving HWND %s to explicit profile display %s",
                                target_hwnd, device_name,
                            )
            if (
                active
                and target_hwnd
                and self.profile_manager.config.snap_near_fullscreen_windows_to_monitor
                and self._profile_allows_monitor_edge_snap(selected)
            ):
                try:
                    self.window_manager.snap_borderless_window_to_monitor(
                        target_hwnd, tolerance_px=8
                    )
                except Exception:
                    logger.exception(
                        "Scaling continued without near-monitor boundary correction for HWND %s",
                        target_hwnd,
                    )
            if active and (target_pid is not None or target_hwnd is not None):
                if (
                    reason == "focus"
                    and target_pid is not None
                    and target_hwnd is not None
                    and self.process_watcher is not None
                    and not self.process_watcher.maintain_scaling_target_foreground(
                        target_pid, target_hwnd
                    )
                ):
                    self._set_control_status(
                        "idle", "Cancelled stale automatic scaling target"
                    )
                    self.process_watcher.invalidate_foreground_cache()
                    return False
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
            trigger_hotkey = self.hotkey_trigger or InputSimulator.trigger_hotkey
            automatic_activation = bool(
                active
                and (
                    reason in {"focus", "browser_video"}
                    or reason.startswith("process_lasso_performance_mode:")
                )
            )
            max_attempts = (
                self.AUTO_SCALE_MAX_ATTEMPTS if automatic_activation else 1
            )
            confirmation_timeout = (
                self.AUTO_SCALE_CONFIRMATION_SECONDS if active else 8.0
            )
            confirmed = False
            for attempt in range(1, max_attempts + 1):
                attempt_started = time.monotonic()
                if attempt > 1 and target_pid and target_hwnd and self.process_watcher:
                    if not self.process_watcher.target_is_foreground(
                        target_pid, target_hwnd
                    ):
                        self._set_control_status(
                            "idle", "Cancelled automatic scaling retries after the target lost focus"
                        )
                        self.process_watcher.invalidate_foreground_cache()
                        logger.info(
                            "Cancelled %s retry %d/%d after PID %s / HWND %s lost focus",
                            reason,
                            attempt,
                            max_attempts,
                            target_pid,
                            target_hwnd,
                        )
                        return False
                attempt_suffix = (
                    f" (attempt {attempt}/{max_attempts})" if max_attempts > 1 else ""
                )
                self._set_control_status(
                    "pending",
                    f"Waiting for Lossless Scaling to confirm "
                    f"{'activation' if active else 'deactivation'}{attempt_suffix}",
                )
                if self.scaling_confirmation_begin is not None:
                    try:
                        self.scaling_confirmation_begin(active)
                    except Exception:
                        logger.exception("Could not initialize Lossless Scaling confirmation")
                        self._set_control_status("error", "Could not initialize Lossless Scaling confirmation")
                        return False
                emitted = trigger_hotkey(
                    modifiers=hotkey.modifiers,
                    key=hotkey.key,
                    hold_ms=hotkey.hold_delay_ms,
                )
                if not emitted:
                    if self.scaling_confirmation_end is not None:
                        self.scaling_confirmation_end()
                    self._set_control_status("error", f"Hotkey injection failed for {reason}")
                    logger.error("Hotkey injection failed for %s", reason)
                    return False
                try:
                    confirmed = self._confirm_scaling_state(
                        active,
                        timeout=confirmation_timeout,
                        target_pid=target_pid if active else None,
                        target_hwnd=target_hwnd if active else None,
                    )
                finally:
                    if self.scaling_confirmation_end is not None:
                        self.scaling_confirmation_end()
                if confirmed:
                    break
                wrong_target_pid = self._last_confirmation_wrong_target_pid
                if wrong_target_pid is not None:
                    logger.warning(
                        "Lossless Scaling activated PID %s instead of requested PID %s; "
                        "stopping the incorrect session before retrying",
                        wrong_target_pid,
                        target_pid,
                    )
                    recovered = self.set_scaling(
                        False,
                        selected,
                        reason=f"{reason}_wrong_target_recovery",
                        force=True,
                        park_runtime_on_stop=False,
                    )
                    if not recovered:
                        self._set_control_status(
                            "error",
                            "Wrong scaling target was detected but could not be stopped safely",
                        )
                        return False
                if attempt < max_attempts:
                    if target_pid and target_hwnd and self.process_watcher:
                        if not self.process_watcher.target_is_foreground(
                            target_pid, target_hwnd
                        ):
                            self._set_control_status(
                                "idle",
                                "Cancelled automatic scaling retries after the target lost focus",
                            )
                            self.process_watcher.invalidate_foreground_cache()
                            return False
                    logger.warning(
                        "Lossless Scaling did not confirm %s after %s attempt %d/%d; retrying",
                        "activation" if active else "deactivation",
                        reason,
                        attempt,
                        max_attempts,
                    )
                    # Confirmation normally consumes this entire interval. If
                    # a probe errors early, preserve the requested five-second
                    # cadence instead of firing toggle hotkeys back-to-back.
                    retry_at = attempt_started + self.AUTO_SCALE_CONFIRMATION_SECONDS
                    remaining = retry_at - time.monotonic()
                    if remaining > 0:
                        time.sleep(remaining)
            if not confirmed:
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
                self._minimize_other_windows(target_hwnd, target_pid)
            elif not active:
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

    def _confirm_scaling_state(
        self,
        expected: bool,
        timeout: float = 3.0,
        *,
        target_pid: Optional[int] = None,
        target_hwnd: Optional[int] = None,
    ) -> bool:
        """Wait for an authoritative LS overlay/log observation when available."""
        self._last_confirmation_wrong_target_pid = None
        if self.scaling_state_probe is None:
            return True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if expected and target_pid and target_hwnd and self.process_watcher:
                if not self.process_watcher.maintain_scaling_target_foreground(
                    target_pid, target_hwnd
                ):
                    logger.info(
                        "Cancelled stale scaling activation after foreground moved away from PID %s / HWND %s",
                        target_pid,
                        target_hwnd,
                    )
                    return False
            try:
                observation = self.scaling_state_probe()
            except Exception:
                logger.exception("Lossless Scaling confirmation probe failed")
                return False
            observed_pid = None
            if isinstance(observation, dict):
                observed = observation.get("isActive", observation.get("is_active"))
                try:
                    observed_pid = int(observation.get("pid")) if observation.get("pid") else None
                except (TypeError, ValueError):
                    observed_pid = None
            else:
                observed = observation
            if observed is expected:
                if (
                    expected
                    and target_pid is not None
                    and observed_pid is not None
                    and observed_pid != target_pid
                ):
                    self._last_confirmation_wrong_target_pid = observed_pid
                    return False
                return True
            time.sleep(0.075)
        return False

    def _set_control_status(self, state: str, message: str) -> None:
        self.state.scaling_control_status = {"state": state, "message": message}

    def toggle_scaling(self, profile: Optional[Profile] = None, *, reason: str) -> bool:
        return self.set_scaling(
            not self.state.is_scaling_active, profile, reason=reason, force=True
        )

    def handle_override_hotkey(self) -> bool:
        """Route the user's proxy hotkey through the profile for the foreground window."""
        if not self.profile_manager.config.override_lossless_hotkey:
            return False
        if self.state.is_scaling_active:
            return self.set_scaling(
                False,
                self.state.current_active_profile,
                reason="override_hotkey",
                force=True,
            )
        if not self.process_watcher:
            return False
        target = self.process_watcher.get_foreground_window_info()
        if not target:
            self._set_control_status("error", "No foreground window was available for scaling")
            return False
        profile = self.profile_manager.match_target_profile(
            process_name=target.name,
            executable_path=target.exe_path,
        )
        if profile is None:
            profile = next(
                (item for item in self.profile_manager.config.profiles if item.is_default),
                None,
            )
        if profile is None:
            self._set_control_status("error", "No default Lossless Scaling profile is available")
            return False
        self.activate_profile(
            profile,
            target.exe_path,
            force=True,
            target_pid=target.pid,
            target_hwnd=target.hwnd,
            suppress_auto_scale=True,
        )
        return self.set_scaling(
            True,
            profile,
            reason="override_hotkey",
            force=True,
            target_pid=target.pid,
            target_hwnd=target.hwnd,
        )

    def reconcile_observed_scaling_state(self, active: bool, target: Optional[Dict] = None) -> None:
        """Apply window side effects when LS changes state outside our hotkey path."""
        with self._lock:
            previous = self.state.is_scaling_active
            self.state.is_scaling_active = active
            if active and target:
                try:
                    observed_pid = int(target.get("pid") or 0) or None
                except (TypeError, ValueError):
                    observed_pid = None
                try:
                    observed_hwnd = int(target.get("hwnd") or 0) or None
                except (TypeError, ValueError):
                    observed_hwnd = None
                self.state.scaling_target_pid = observed_pid
                self.state.scaling_target_hwnd = observed_hwnd
                matched = self.profile_manager.match_target_profile(
                    process_name=target.get("processName"),
                    executable_path=target.get("exePath"),
                )
                if matched is not None and (
                    self.state.current_active_profile is None
                    or self.state.current_active_profile.id != matched.id
                ):
                    logger.info(
                        "Detected scaled window '%s'; matched profile '%s'",
                        target.get("processName"),
                        matched.name,
                    )
                    self.state.current_active_profile = matched
            elif not active:
                self.state.scaling_target_pid = None
                self.state.scaling_target_hwnd = None
            self._set_control_status(
                "confirmed",
                f"Observed Lossless Scaling {'active' if active else 'idle'}",
            )
            if previous == active:
                return
            if active and self.profile_manager.config.minimize_other_windows_on_scale:
                self._minimize_other_windows()

    def reconcile_window_management_setting(self) -> None:
        """Apply a changed minimize setting immediately to the current scaling state."""
        with self._lock:
            if (
                self.state.is_scaling_active
                and self.profile_manager.config.minimize_other_windows_on_scale
            ):
                self._minimize_other_windows()

    @staticmethod
    def _profile_gpu_route_ids(profile: Optional[Profile]) -> tuple[int, int]:
        values = profile.native_scaling_settings if profile else {}
        try:
            gpu_id = int(values.get("PreferredGpuId") or 0)
        except (TypeError, ValueError):
            gpu_id = 0
        try:
            display_id = int(values.get("OutputDisplayId") or 0)
        except (TypeError, ValueError):
            display_id = 0
        return max(0, gpu_id), max(0, display_id)

    def _desired_gpu_route(
        self, profile: Optional[Profile], target_hwnd: Optional[int]
    ) -> Dict:
        profile_gpu_id, profile_display_id = self._profile_gpu_route_ids(profile)
        config = self.profile_manager.config
        route = self.gpu_router.route_for_gpu(config.preferred_scaling_gpu_device_id)
        if config.auto_route_gpu_to_display and target_hwnd:
            detected = self.gpu_router.route_for_window(target_hwnd)
            if detected:
                route = detected
        if profile_gpu_id or profile_display_id:
            selected = self.gpu_router.route_for_ls_ids(
                profile_gpu_id, profile_display_id
            )
            if profile_gpu_id:
                route["lsGpuId"] = selected["lsGpuId"]
                route["gpuDeviceId"] = selected["gpuDeviceId"]
            if profile_display_id:
                route["lsDisplayId"] = selected["lsDisplayId"]
                route["deviceName"] = selected["deviceName"]
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
            if any(self._profile_gpu_route_ids(profile)):
                # A profile-specific numeric route is fixed; only profiles set
                # to Auto follow the global per-display handoff behavior.
                return False
            route = self._desired_gpu_route(profile, target_hwnd)
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
            if not force and same_profile:
                # A different window can match the profile that is already
                # active. Re-check its runtime monitor route without
                # redeploying DLLs/manifests or re-running profile actions.
                try:
                    gpu_route = self._desired_gpu_route(profile, target_hwnd)
                    route_change = self.lossless_settings.gpu_route_changes_required(
                        profile.lossless_profile_title,
                        int(gpu_route.get("lsGpuId", 0)),
                        int(gpu_route.get("lsDisplayId", 0)),
                    )
                except (OSError, ValueError, RuntimeError) as error:
                    logger.error("Could not refresh profile '%s' GPU route: %s", profile.name, error)
                    return
                if not route_change:
                    if route_change is False:
                        self.state.current_gpu_route_key = self._gpu_route_key(gpu_route)
                    return
                restart_lossless_scaling = bool(
                    self.process_watcher
                    and self.process_watcher.check_is_lossless_scaling_running()
                )
                if restart_lossless_scaling and not self.process_watcher.stop_lossless_scaling():
                    logger.error(
                        "Profile '%s' GPU route was not updated because Lossless Scaling could not be stopped",
                        profile.name,
                    )
                    return
                try:
                    route_updated = self.lossless_settings.update_gpu_route(
                        profile.lossless_profile_title,
                        int(gpu_route.get("lsGpuId", 0)),
                        int(gpu_route.get("lsDisplayId", 0)),
                    )
                    if route_updated:
                        self.state.current_gpu_route_key = self._gpu_route_key(gpu_route)
                except (OSError, ValueError) as error:
                    logger.error("Could not update profile '%s' GPU route: %s", profile.name, error)
                finally:
                    if restart_lossless_scaling:
                        self.process_watcher.launch_lossless_scaling(force=True)
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
                neural_sampling_preset_before = (
                    profile.graphics.neural_render.sampling_resolution_preset
                    if profile else None
                )
                deployment_files = self.graphics_resolver.resolve(
                    profile,
                    lossless_scaling_exe=configured_exe,
                    reshade_overlay_hotkey=(
                        self.profile_manager.config.reshade_overlay_hotkey
                    ),
                )
                if (
                    profile
                    and neural_sampling_preset_before !=
                    profile.graphics.neural_render.sampling_resolution_preset
                ):
                    # The resolver detected that workingScale was changed later
                    # in the add-on menu and relinquished ownership as Custom.
                    self.profile_manager.save_config()
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
                    and not profile.is_default
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
                gpu_route = self._desired_gpu_route(profile, target_hwnd) if profile else None
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
                self.reshade_profiles.config_path(
                    reshade.managed_profile_id,
                    interactive=bool(
                        reshade.menu_proxy_enabled
                    ),
                )
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
            not profile.is_default
            and profile.auto_scale
            and self.profile_manager.config.disable_native_auto_scale
            and self.state.auto_scale_enabled
            and not suppress_auto_scale
        ):
            # Browser profiles scale only on an explicit extension event
            # (reason="browser_video"). Focusing chrome.exe — including our
            # own dashboard --app window — must never auto-scale it, or the
            # Chrome profile latches on with nothing actually scaling.
            if self._is_browser_profile(profile):
                logger.debug(
                    "Skipped focus auto-scale for browser profile '%s'; waiting for extension event",
                    profile.name,
                )
                return
            delay_ms = self.profile_manager.config.global_hotkey.activation_delay_ms
            if target_pid is not None and target_hwnd is not None and self.process_watcher:
                deadline = time.monotonic() + (delay_ms / 1000.0)
                while True:
                    if not self.process_watcher.target_is_foreground(
                        target_pid, target_hwnd
                    ):
                        logger.info(
                            "Cancelled stale auto-scale for '%s' because foreground moved away",
                            profile.name,
                        )
                        self.process_watcher.invalidate_foreground_cache()
                        return
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    time.sleep(min(0.05, remaining))
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
        browser_processes = {value.casefold() for value in DEFAULT_BROWSER_EXECUTABLES}
        configured = {value.casefold() for value in profile.target_processes}
        if profile.target_process:
            configured.add(profile.target_process.casefold())
        return bool(configured & browser_processes)

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
            manifest["nvidiaProfileReverted"] = nvidia_reverted
            return manifest

    def shutdown(self) -> None:
        with self._lock:
            for reshade_ini in list(self._reshade_inis.values()):
                ReshadeManager.restore_backup(reshade_ini)
            self._reshade_inis.clear()
