"""Coordinates scaling and per-profile side effects."""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, TYPE_CHECKING

from ..core.models import Profile
from ..core.profile_manager import DEFAULT_BROWSER_EXECUTABLES, ProfileManager
from ..core.state import AppState
from .asset_store import AssetStore
from .addon_config import AddonConfigService
from .deployment_manager import DeploymentManager
from .graphics_resolver import GraphicsResolutionError, GraphicsResolver
from .gpu_router import GpuRouter
from .gpu_selections import GpuSelectionService
from .input_simulator import InputSimulator
from .ls_settings import LosslessSettingsXml
from .nvidia_profile import NvidiaProfileManager
from .reshade_manager import ReshadeManager
from .reshade_profiles import ReshadeProfileService
from .window_manager import MonitorWindowManager
from .overlay_indicator import OverlayIndicator

if TYPE_CHECKING:
    from .process_watcher import ProcessWatcher

logger = logging.getLogger("LSCompanion.Automation")


class AutomationController:
    AUTO_SCALE_MAX_ATTEMPTS = 6
    AUTO_SCALE_CONFIRMATION_SECONDS = 5.0
    ROUTE_HEALTHY_FPS = 10.0
    ROUTE_REPAIR_BUDGET = 6
    # Minimum gap between Lossless Scaling relaunches that are caused only by
    # a GPU-route change. LS reads its GPU route at startup, so every route
    # change costs a full stop/relaunch; without a cooldown, foreground
    # flicker between windows on different displays restarts LS in a loop.
    GPU_ROUTE_RELAUNCH_COOLDOWN_SECONDS = 60.0

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
        presentmon_provider: Optional[Callable[[], Optional[object]]] = None,
        route_verify_seconds: float = 5.0,
        overlay_indicator: Optional[OverlayIndicator] = None,
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
        self.gpu_router = gpu_router or GpuRouter(
            ls_gpu_id_map=lambda: dict(
                getattr(profile_manager.config, "ls_gpu_id_map", None) or {}
            ),
            ls_display_id_map=lambda: dict(
                getattr(profile_manager.config, "ls_display_id_map", None) or {}
            ),
            adapter_report_dir=self._adapter_report_dir(profile_manager),
        )
        self.gpu_selections = GpuSelectionService(profile_manager)
        self.lossless_settings = lossless_settings or LosslessSettingsXml(
            profile_manager.config.lossless_settings_xml_path
        )
        # Keep the production default resolved at call time so tests and host
        # integrations can patch the Win32 sender after construction.
        self.hotkey_trigger = hotkey_trigger
        self.presentmon_provider = presentmon_provider
        self.route_verify_seconds = route_verify_seconds
        self.overlay_indicator = overlay_indicator or OverlayIndicator(
            enabled=lambda: bool(
                self.profile_manager.config.overlay_indicator_enabled),
        )
        self._verifying_route = False
        self._last_verified_route_key: Optional[str] = None
        self._last_verified_ghost_signature = ""
        self.reshade_profiles = ReshadeProfileService(profile_manager)
        self.addon_configs = AddonConfigService(profile_manager)
        self._last_gpu_route_relaunch_at: float = 0.0
        self.profile_changes_blocked: Callable[[], bool] = lambda: False
        self._pending_profile_restart: Optional[Dict] = None
        self._pending_native_profiles: Dict[str, tuple] = {}
        self._pending_native_failed = False

    def resolve_deployment_files(self, profile: Optional[Profile]) -> List[Dict]:
        """Resolve runtime binaries plus any selected managed ReShade assets."""
        packages = self.asset_store.list_packages()
        addon_manager_available = any(
            item.get("provider") == "ls-addon-manager"
            and any(
                Path(str(file.get("relative_path") or "")).name
                == "lossless-scaling-deployment.json"
                for file in item.get("files", [])
            )
            for item in packages
        )
        use_addon_manager = bool(
            self.profile_manager.config.addon_manager_enabled
            and addon_manager_available
        )
        config_path = None
        if use_addon_manager and profile:
            self.addon_configs.catalog(packages)
            config_path = self.addon_configs.materialize(
                profile,
                reshade_enabled=bool(profile.graphics.reshade.enabled or (profile.reshade and profile.reshade.enabled)),
                hotkey_vk=self._echo_hotkey_vk(self.profile_manager.config.reshade_overlay_hotkey),
            )
        files = self.graphics_resolver.resolve(
            profile,
            lossless_scaling_exe=self.profile_manager.config.lossless_scaling_exe_path,
            reshade_overlay_hotkey=self.profile_manager.config.reshade_overlay_hotkey,
            # Automation never deploys the retired host. Before Echo has been
            # installed this is an ordinary host-disabled plan; direct resolver
            # callers may still use ``None`` solely for legacy receipt tests.
            addon_manager_enabled=True if use_addon_manager else False,
            addon_manager_config=self.profile_manager.config.addon_manager,
        )
        if profile and config_path:
            files.append({
                "relative_path": "addons/config.json",
                "source_path": str(config_path),
                "role": "addon_manager_config",
                "source_package": f"generated/echo-addon-config/{profile.id}",
            })
        if (
            profile
            and profile.reshade
            and profile.reshade.enabled
            and profile.reshade.managed_profile_id
        ):
            files.extend(
                self.reshade_profiles.deployment_files(profile.reshade.managed_profile_id)
            )
        return files

    @staticmethod
    def _echo_hotkey_vk(value: str) -> int:
        keys = {
            "insert": 45, "delete": 46, "home": 36, "end": 35,
            "pageup": 33, "page up": 33, "pagedown": 34, "page down": 34,
        }
        text = str(value or "end").strip().casefold()
        if text in keys:
            return keys[text]
        if len(text) == 1 and text.isalpha():
            return ord(text.upper())
        if text.startswith("f") and text[1:].isdigit() and 1 <= int(text[1:]) <= 12:
            return 111 + int(text[1:])
        return 35

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
        if not self.state.scaling_target_verified_for_minimization:
            logger.warning("Skipping peer minimization because the scaling target identity is unverified")
            return
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

    #: User-toggled stops skip the deactivation hotkey + confirmation wait and
    #: kill Lossless Scaling for an instant unscale. Automatic stops keep the
    #: graceful hotkey path so background transitions never close LS.
    INSTANT_STOP_REASONS = frozenset({"override_hotkey", "manual"})

    def begin_update(self) -> bool:
        """Quiesce automation after in-flight profile/scaling work finishes."""
        with self._lock:
            if (self.state.is_scaling_active or self.state.benchmark_mode_active
                    or self.profile_manager.repair_session
                    or self.state.scaling_control_status.get("state") == "pending"):
                return False
            self._cancel_delayed_scale()
            self.state.update_in_progress = True
            return True

    def end_update(self) -> None:
        with self._lock:
            self.state.update_in_progress = False

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
        if self.profile_manager.repair_session:
            self._set_control_status("error", "Repair saved settings and restart Companion before scaling")
            return False
        with self._lock:
            if self.state.update_in_progress:
                return False
            if (active and self.state.benchmark_mode_active
                    and reason != "performance_benchmark"
                    and not reason.startswith("gpu_calibration")):
                # Benchmark and GPU-calibration runs own Lossless Scaling
                # exclusively: automatic (focus/hotkey/lasso) starts stay out
                # until the run releases the flag. Stops always stay allowed.
                logger.info(
                    "Scaling start refused while benchmarking/calibrating (%s)",
                    reason,
                )
                self._set_control_status(
                    "idle", "Scaling paused while benchmarking or GPU calibration runs"
                )
                return False
            if not active:
                self._pending_profile_restart = None
            if not force and self.state.is_scaling_active == active:
                logger.info("Scaling already %s; ignored %s request", "active" if active else "idle", reason)
                return False
            selected = profile or self.state.current_active_profile
            # A direct scale/stop supersedes any pending Scale Now timer.
            self._cancel_delayed_scale()
            if active and not self.state.is_scaling_active and self._pending_native_profiles:
                try:
                    self._flush_pending_native_profiles()
                    self._pending_native_failed = False
                except Exception as error:
                    self._pending_native_failed = True
                    self._set_control_status("error", str(error))
                    return False
            if active and selected and self.addon_configs.has_pending(selected):
                self.activate_profile(
                    selected, force=True, suppress_auto_scale=True,
                    target_pid=target_pid, target_hwnd=target_hwnd,
                )
                if self.addon_configs.has_pending(selected):
                    self._set_control_status("error", "Could not apply saved add-on settings before scaling")
                    return False
            if active and selected and self._live_deployment_stale(selected):
                # An idle foreground switch deferred the DLL swap; refresh
                # Lossless Scaling now that scaling is actually starting.
                self.activate_profile(
                    selected, force=True, suppress_auto_scale=True,
                    target_pid=target_pid, target_hwnd=target_hwnd,
                    for_scaling=True,
                )
                if self._live_deployment_stale(selected):
                    self._set_control_status("error", "Could not refresh Lossless Scaling deployment before scaling")
                    return False
            runtime_providers = self._runtime_providers(selected)
            configured_exe = self.profile_manager.config.lossless_scaling_exe_path
            if (
                active
                and runtime_providers
                and configured_exe
                and self.deployment_manager.runtime_packages_need_state(
                    runtime_providers, active=True, lossless_scaling_exe=configured_exe
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
            if not active and self.state.is_scaling_active and reason in self.INSTANT_STOP_REASONS:
                # User-toggled stop with confirmed scaling: kill LS now instead
                # of sending the toggle hotkey and waiting up to 8s for the
                # deactivation confirmation. Falls through to the graceful
                # path when scaling never confirmed or the kill fails.
                instant = self._instant_stop_scaling(selected, reason)
                if instant is not None:
                    if instant:
                        try:
                            self.overlay_indicator.log("Scaling stopped",
                                                       anchor_hwnd=target_hwnd)
                            self.overlay_indicator.set_border(None)
                        except Exception:
                            pass
                    return instant
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
                            else:
                                self.overlay_indicator.log(
                                    "Moved game window to configured monitor",
                                    anchor_hwnd=target_hwnd)
                                self.overlay_indicator.set_border(
                                    "pending", anchor_hwnd=target_hwnd)
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
                    reason in {"focus", "profile_edit"}
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
                verified = False
                for _ in range(3):
                    try:
                        # A deferred edit must never focus the game over a
                        # dashboard/editor the user returned to during reload.
                        if reason == "profile_edit":
                            verified = self.process_watcher.maintain_scaling_target_foreground(
                                target_pid, target_hwnd
                            )
                            break
                        if self.process_watcher.focus_window_identity(target_pid, target_hwnd):
                            verified = True
                            break
                    except Exception:
                        logger.exception("PID/HWND verification attempt failed")
                    try:
                        self.process_watcher.invalidate_foreground_cache()
                    except Exception:
                        pass
                    time.sleep(0.5)
                if not verified:
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
            if active and automatic_activation:
                # Cold LS starts need longer to confirm the first toggle.
                confirmation_timeout = 15.0
            else:
                confirmation_timeout = (
                    self.AUTO_SCALE_CONFIRMATION_SECONDS if active else 8.0
                )
            confirmed = False
            for attempt in range(1, max_attempts + 1):
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
                if active and reason == "profile_edit" and (
                    self.profile_changes_blocked()
                    or not self.process_watcher.maintain_scaling_target_foreground(target_pid, target_hwnd)
                ):
                    if self.scaling_confirmation_end is not None:
                        self.scaling_confirmation_end()
                    self._set_control_status("idle", "Profile applied; scaling waits for a manual restart after focus changed")
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
                    try:
                        self.overlay_indicator.log(
                            "Hotkey injection failed", kind="error",
                            anchor_hwnd=target_hwnd)
                        self.overlay_indicator.set_border(None)
                    except Exception:
                        pass
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
                if wrong_target_pid is None:
                    # The activation hotkey is a toggle. If add-on injection
                    # prevents the observer from confirming the new state, a
                    # second press may turn a successfully scaled session off.
                    # Retry only after positively observing and stopping the
                    # wrong target below.
                    logger.warning(
                        "Lossless Scaling activation remained ambiguous after %s; "
                        "the toggle will not be resent",
                        reason,
                    )
                    break
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
                        "Retrying %s only because PID %s was positively identified "
                        "and stopped",
                        reason,
                        wrong_target_pid,
                    )
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
                try:
                    self.overlay_indicator.log(
                        "Scaling did not confirm", kind="error",
                        anchor_hwnd=target_hwnd)
                    self.overlay_indicator.set_border(None)
                except Exception:
                    pass
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
                self.state.scaling_target_verified_for_minimization = bool(target_pid and target_hwnd)
            else:
                if self.profile_manager.config.addon_manager_enabled:
                    self.addon_configs.capture(selected, configured_exe)
                self.state.scaling_target_pid = None
                self.state.scaling_target_hwnd = None
                self.state.scaling_target_verified_for_minimization = False
            if active and self.profile_manager.config.minimize_other_windows_on_scale:
                self._minimize_other_windows(target_hwnd, target_pid)
            elif not active:                # The latest profile stays hotloaded on stop: no undeploy, no
                # park, no restart. A later scale with the same profile needs
                # no file changes; a different profile redeploys (and restarts
                # only if the deployment actually changed) via activate_profile.
                if self.process_watcher:
                    self.process_watcher.invalidate_foreground_cache()
            if active and not self._verifying_route:
                adopted = None
                try:
                    adopted = self._adopt_adapter_report(selected)
                except Exception:
                    logger.debug("Adapter report adoption skipped", exc_info=True)
                if adopted == "corrected":
                    self._enforce_intended_route(
                        selected, reason, target_pid, target_hwnd)
                if self._route_verification_due():
                    self._verify_and_repair_route(
                        selected, reason, target_pid, target_hwnd)
            if active:
                label = (selected.name if selected
                         else f"PID {target_pid}" if target_pid else "game")
                try:
                    self.overlay_indicator.log(
                        f"Scaling started: {label}", kind="ok",
                        anchor_hwnd=target_hwnd)
                    self.overlay_indicator.set_border(
                        "active", anchor_hwnd=target_hwnd)
                except Exception:
                    pass
            else:
                try:
                    self.overlay_indicator.log("Scaling stopped",
                                               anchor_hwnd=target_hwnd)
                    self.overlay_indicator.set_border(None)
                except Exception:
                    pass
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

    def _instant_stop_scaling(
        self,
        selected: Optional[Profile],
        reason: str,
    ) -> Optional[bool]:
        """Stop confirmed scaling by killing LS. None = use the graceful path."""
        if self.process_watcher is None:
            return None
        try:
            stopped = self.process_watcher.stop_lossless_scaling()
            still_running = self.process_watcher.check_is_lossless_scaling_running()
        except Exception:
            logger.exception("Instant stop kill failed; falling back to hotkey toggle")
            return None
        if not stopped and still_running:
            logger.warning("Instant stop could not close LS; falling back to hotkey toggle")
            return None
        configured_exe = self.profile_manager.config.lossless_scaling_exe_path
        try:
            if self.profile_manager.config.addon_manager_enabled:
                self.addon_configs.capture(selected, configured_exe)
        except Exception:
            logger.exception("Instant stop continued without capturing live add-on settings")
        self.state.mark_scaling_toggled(False)
        self._set_control_status("confirmed", "Scaling stopped instantly (Lossless Scaling closed)")
        self.state.scaling_owner_profile_id = None
        self.state.scaling_trigger = None
        self.state.scaling_target_pid = None
        self.state.scaling_target_hwnd = None
        self.state.scaling_target_verified_for_minimization = False
        self.process_watcher.invalidate_foreground_cache()
        # Keep the deployment hotloaded: no parking, no undeploy. A later
        # scale with the same profile needs no restart; a different profile
        # redeploys via activate_profile.
        # Relaunch hidden so the next scale does not pay a cold start.
        def relaunch() -> None:
            try:
                self.process_watcher.launch_lossless_scaling(force=True)
            except Exception:
                logger.exception("Instant-stop relaunch of Lossless Scaling failed")

        threading.Thread(target=relaunch, daemon=True, name="LSInstantStopRelaunch").start()
        logger.info("Scaling stopped instantly for %s (Lossless Scaling closed)", reason)
        return True

    def scale_focused_after_delay(self, delay_seconds: float = 5.0) -> float:
        """Scale whatever window is focused when the delay elapses. Restarts."""
        if self.state.update_in_progress:
            raise RuntimeError("Companion is installing updates")
        try:
            delay = float(delay_seconds)
        except (TypeError, ValueError):
            delay = 5.0
        delay = min(max(delay, 1.0), 30.0)
        previous = getattr(self, "_delayed_scale_timer", None)
        if previous is not None:
            try:
                previous.cancel()
            except Exception:
                pass
        self._set_control_status(
            "pending",
            f"Scale Now: focus the target window — scaling in {delay:.0f}s",
        )
        logger.info("Scale Now scheduled in %.0fs", delay)

        def fire() -> None:
            self._delayed_scale_timer = None
            if self.state.is_scaling_active:
                logger.info("Scale Now skipped; scaling is already active")
                self._set_control_status("confirmed", "Scale Now skipped; scaling is already active")
                return
            self.handle_override_hotkey()

        timer = threading.Timer(delay, fire)
        timer.daemon = True
        timer.name = "LSScaleNow"
        self._delayed_scale_timer = timer
        timer.start()
        return delay

    def _cancel_delayed_scale(self) -> None:
        timer = getattr(self, "_delayed_scale_timer", None)
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass
            self._delayed_scale_timer = None

    def toggle_scaling(self, profile: Optional[Profile] = None, *, reason: str) -> bool:
        return self.set_scaling(
            not self.state.is_scaling_active, profile, reason=reason, force=True
        )

    def handle_override_hotkey(self) -> bool:
        """Route the user's proxy hotkey through the profile for the foreground window."""
        if self.state.update_in_progress:
            return False
        if not self.profile_manager.config.override_lossless_hotkey:
            return False
        action = ("stopping scaling"
                  if self.state.is_scaling_active else "starting scaling")
        try:
            self.overlay_indicator.log(f"Hotkey received: {action}",
                                       kind="hotkey")
            self.overlay_indicator.set_border("pending")
        except Exception:
            pass
        if not self.process_watcher:
            return False
        target = self.process_watcher.get_foreground_window_info()
        if self.state.is_scaling_active:
            session_pid = int(self.state.scaling_target_pid or 0)
            session_hwnd = int(self.state.scaling_target_hwnd or 0)
            if (target is not None and session_pid and session_hwnd
                    and (int(target.pid), int(target.hwnd)) != (session_pid, session_hwnd)):
                # Hotkey on a different window: switch sessions instead of
                # merely toggling the current one off.
                logger.info(
                    "Hotkey switching scaling from PID %s to foreground PID %s",
                    session_pid, target.pid,
                )
                if not self.set_scaling(
                    False,
                    self.state.current_active_profile,
                    reason="override_hotkey_switch",
                    force=True,
                ):
                    return False
            else:
                return self.set_scaling(
                    False,
                    self.state.current_active_profile,
                    reason="override_hotkey",
                    force=True,
                )
        if not target:
            self._set_control_status("error", "No foreground window was available for scaling")
            return False
        try:
            self.overlay_indicator.set_border(
                "pending", anchor_hwnd=target.hwnd, immediate=True)
        except Exception:
            pass
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

    def handle_scaling_target_closed(self) -> None:
        """End a scaling session whose target process/window is gone.

        Lossless Scaling itself is left running (idle): tearing it down
        would only force a cold start when the user scales again.
        """
        with self._lock:
            if not self.state.is_scaling_active:
                return
            try:
                owner = self.state.current_active_profile
                label = owner.name if owner else "game"
            except Exception:
                label = "game"
            logger.info("Scaling target closed; ending scaling session for '%s'", label)
            self.state.mark_scaling_toggled(False)
            self._set_control_status("confirmed", "Scaling target closed")
            self.state.scaling_owner_profile_id = None
            self.state.scaling_trigger = None
            self.state.scaling_target_pid = None
            self.state.scaling_target_hwnd = None
            self.state.scaling_target_verified_for_minimization = False
            try:
                self.overlay_indicator.log(
                    f"Scaling stopped (target closed): {label}")
                self.overlay_indicator.set_border(None)
            except Exception:
                pass
            if self.process_watcher:
                try:
                    self.process_watcher.invalidate_foreground_cache()
                except Exception:
                    pass

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
                verified_target = bool(
                    observed_pid and target.get("detectionSource") == "log_file"
                )
                if not verified_target and self.process_watcher:
                    expected_name = target.get("processName")
                    if str(expected_name or "").casefold() == "losslessscaling.exe":
                        expected_name = None
                    recent = self.process_watcher.recent_unscaled_foreground_target(
                        expected_name
                    )
                    if recent:
                        observed_pid = recent.pid
                        observed_hwnd = recent.hwnd
                        verified_target = True
                        logger.info(
                            "Using recent pre-scaling foreground HWND %s / PID %s for peer minimization",
                            observed_hwnd,
                            observed_pid,
                        )
                self.state.scaling_target_pid = observed_pid
                self.state.scaling_target_hwnd = observed_hwnd
                self.state.scaling_target_verified_for_minimization = verified_target
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
                self.state.scaling_target_verified_for_minimization = False
            self._set_control_status(
                "confirmed",
                f"Observed Lossless Scaling {'active' if active else 'idle'}",
            )
            if previous == active:
                return
            if active and self.profile_manager.config.minimize_other_windows_on_scale:
                self._minimize_other_windows()
            if active:
                label = str((target or {}).get("processName") or "game")
                try:
                    anchor = int((target or {}).get("hwnd") or 0) or None
                except (TypeError, ValueError):
                    anchor = None
                try:
                    self.overlay_indicator.log(
                        f"Scaling started (native hotkey): {label}",
                        kind="ok", anchor_hwnd=anchor)
                    self.overlay_indicator.set_border("active",
                                                      anchor_hwnd=anchor)
                except Exception:
                    pass
            else:
                try:
                    self.overlay_indicator.log("Scaling stopped")
                    self.overlay_indicator.set_border(None)
                except Exception:
                    pass

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

    def _queue_profile_restart(self, profile: Profile) -> None:
        self._pending_profile_restart = {
            "profile_id": profile.id,
            "pid": self.state.scaling_target_pid,
            "hwnd": self.state.scaling_target_hwnd,
            "session": self.state.last_scale_toggle_time,
            "ready_at": time.monotonic() + 0.75,
        }
        self._set_control_status(
            "pending", "Profile saved; close the editor and return to the scaled application to reload scaling"
        )

    def profile_reload_pending(self, profile_id: str) -> bool:
        pending = self._pending_profile_restart
        return bool(pending and pending["profile_id"] == profile_id)

    def capture_active_addon_config(self) -> bool:
        """Capture edits from Echo's native panel and queue the same safe reload."""
        with self._lock:
            active = self.state.current_active_profile
            if active is None:
                return False
            profile = self.profile_manager.get_profile_by_id(active.id) or active
            previous = profile.model_copy(deep=True)
            captured = self.addon_configs.capture(
                profile, self.profile_manager.config.lossless_scaling_exe_path
            )
            if captured and profile.addon_config != previous.addon_config:
                self.addon_configs.mark_edited(profile)
                self.state.current_active_profile = profile
                if self.state.is_scaling_active:
                    self._queue_profile_restart(profile)
            return captured

    def apply_saved_profile(
        self, profile: Profile, *,
        native_settings: Optional[LosslessSettingsXml] = None,
        template_title: Optional[str] = None,
    ) -> None:
        """Apply an idle edit, or queue one reload of the existing scaling target."""
        with self._lock:
            self._pending_native_failed = False
            if native_settings is not None and native_settings.path.is_file():
                # Keep the original template across successive renames/edits.
                self._pending_native_profiles.setdefault(
                    profile.id, (native_settings, template_title)
                )
            active = self.state.current_active_profile
            if self.state.is_scaling_active:
                if active and active.id == profile.id:
                    self._queue_profile_restart(profile)
                return
            self._flush_pending_native_profiles()
            if active and active.id == profile.id:
                self.activate_profile(profile, force=True, suppress_auto_scale=True)

    def _write_pending_native_profiles(self) -> None:
        """Write saved XML values only while their host is stopped."""
        for profile_id, (settings, template_title) in list(self._pending_native_profiles.items()):
            profile = self.profile_manager.get_profile_by_id(profile_id)
            if profile and settings.path.is_file():
                values = dict(profile.native_scaling_settings)
                config = self.profile_manager.config
                if config.lossless_control_configured and config.disable_native_auto_scale:
                    values["AutoScale"] = False
                if profile.is_default:
                    if not settings.update_profile(profile.lossless_profile_title, values):
                        raise RuntimeError("The native Lossless Scaling default profile could not be found")
                else:
                    settings.upsert_profile(
                        profile.lossless_profile_title, values, template_title=template_title
                    )
            self._pending_native_profiles.pop(profile_id, None)

    def _flush_pending_native_profiles(self) -> None:
        if not self._pending_native_profiles:
            return
        was_running = bool(
            self.process_watcher and self.process_watcher.check_is_lossless_scaling_running()
        )
        if was_running and not self.process_watcher.stop_lossless_scaling():
            raise RuntimeError("Lossless Scaling could not be stopped for the profile settings update")
        try:
            self._write_pending_native_profiles()
        finally:
            if was_running and not self.process_watcher.launch_lossless_scaling(force=True):
                raise RuntimeError("Lossless Scaling could not be restarted after updating profile settings")

    def reconcile_pending_addon_settings(self) -> None:
        """Reload saved edits after editing ends and the original game regains focus."""
        with self._lock:
            if (self.profile_changes_blocked() or self.profile_manager.repair_session
                    or self.state.update_in_progress or self.state.benchmark_mode_active):
                return
            pending = self._pending_profile_restart
            active = self.state.current_active_profile
            if pending and (
                not self.state.is_scaling_active or not active
                or active.id != pending["profile_id"]
                or self.state.scaling_target_pid != pending["pid"]
                or self.state.scaling_target_hwnd != pending["hwnd"]
                or self.state.last_scale_toggle_time != pending["session"]
            ):
                self._pending_profile_restart = pending = None
            if pending:
                pid, hwnd = pending["pid"], pending["hwnd"]
                if not pid or not hwnd or not self.process_watcher:
                    self._set_control_status("pending", "Profile saved; stop and restart scaling to apply it (target unavailable)")
                    return
                if (time.monotonic() < pending["ready_at"]
                        or not self.process_watcher.maintain_scaling_target_foreground(pid, hwnd)):
                    return
                profile = self.profile_manager.get_profile_by_id(pending["profile_id"])
                if profile is None:
                    self._pending_profile_restart = None
                    return
                # Consume before executing: failures never create a restart loop.
                self._pending_profile_restart = None
                try:
                    self.resolve_deployment_files(profile)
                    self.overlay_indicator.log(
                        f"Profile updated; restarting scaling: {profile.name}", anchor_hwnd=hwnd
                    )
                    self.overlay_indicator.set_border("pending", anchor_hwnd=hwnd)
                    if not self.process_watcher.stop_lossless_scaling():
                        raise RuntimeError("Could not stop Lossless Scaling to apply profile changes")
                    self.state.mark_scaling_toggled(False)
                    self.state.scaling_owner_profile_id = None
                    self.state.scaling_trigger = None
                    self.state.scaling_target_pid = None
                    self.state.scaling_target_hwnd = None
                    self.state.scaling_target_verified_for_minimization = False
                    self._write_pending_native_profiles()
                    self.activate_profile(
                        profile, force=True, suppress_auto_scale=True,
                        target_pid=pid, target_hwnd=hwnd, for_scaling=True,
                    )
                    if not self.process_watcher.launch_lossless_scaling(force=True):
                        raise RuntimeError("Could not restart Lossless Scaling after profile changes")
                    if self.profile_changes_blocked() or not self.process_watcher.maintain_scaling_target_foreground(pid, hwnd):
                        raise RuntimeError("Profile applied; return to the application and start scaling again")
                    if not self.set_scaling(
                        True, profile, reason="profile_edit", target_pid=pid, target_hwnd=hwnd
                    ):
                        raise RuntimeError("Profile applied, but scaling could not be resumed")
                except Exception as error:
                    logger.exception("Could not reload the edited scaling profile")
                    self._pending_native_failed = bool(self._pending_native_profiles)
                    if not self.state.is_scaling_active:
                        # Leave the host available for a deliberate retry even
                        # when applying XML/deployment changes failed.
                        try:
                            if not self.process_watcher.check_is_lossless_scaling_running():
                                self.process_watcher.launch_lossless_scaling(force=True)
                        except Exception:
                            logger.exception("Could not restore the idle Lossless Scaling host")
                    self._set_control_status("error", str(error))
                    self.overlay_indicator.log(str(error), kind="error", anchor_hwnd=hwnd)
                    self.overlay_indicator.set_border(None)
                return
            if not self.state.is_scaling_active:
                if self._pending_native_failed:
                    return
                try:
                    self._flush_pending_native_profiles()
                except Exception as error:
                    self._pending_native_failed = True
                    self._set_control_status("error", str(error))
                    logger.exception("Could not apply saved native profile settings")
                    return
                if active:
                    profile = self.profile_manager.get_profile_by_id(active.id) or active
                    if self.addon_configs.has_pending(profile):
                        self.activate_profile(profile, force=True, suppress_auto_scale=True)

    def save_addon_config(self, profile_id: str, data: Dict) -> Profile:
        """Serialize profile edits with deployment and scaling transitions."""
        with self._lock:
            existing = self.profile_manager.get_profile_by_id(profile_id)
            if existing is None:
                raise ValueError("Save the profile before editing its add-ons")
            profile = existing.model_copy(deep=True)
            profile.addon_config = data
            self.addon_configs.catalog(self.asset_store.list_packages())
            self.addon_configs.materialize(
                profile,
                reshade_enabled=bool(profile.graphics.reshade.enabled or (profile.reshade and profile.reshade.enabled)),
                hotkey_vk=self._echo_hotkey_vk(self.profile_manager.config.reshade_overlay_hotkey),
            )
            if profile == existing:
                return existing
            self.addon_configs.mark_edited(profile)
            self.profile_manager.add_or_update_profile(profile)
            active = self.state.current_active_profile
            if active and active.id == profile.id:
                if self.state.is_scaling_active:
                    self.state.current_active_profile = profile
                    self._queue_profile_restart(profile)
                else:
                    self.activate_profile(profile, force=True, suppress_auto_scale=True)
            return profile

    def _desired_gpu_route(
        self, profile: Optional[Profile], target_hwnd: Optional[int]
    ) -> Dict:
        self.refresh_gpu_selections()
        profile_gpu_id, profile_display_id = self._profile_gpu_route_ids(profile)
        config = self.profile_manager.config
        # An explicit preferred-GPU pin is user intent: honor it instead of
        # chasing the foreground window across displays. Previously the pin
        # was silently discarded whenever auto-routing was on, so Browser and
        # Game profiles flapped between GPUs on every foreground switch.
        pinned = bool(
            config.preferred_scaling_gpu_device_id
            or config.preferred_scaling_gpu_name
        )
        follow_window = bool(config.auto_route_gpu_to_display and not pinned)
        route = self.gpu_router.route_for_gpu(
            None if profile_gpu_id or follow_window else config.preferred_scaling_gpu_device_id,
            None if profile_gpu_id or follow_window else config.preferred_scaling_gpu_name,
        )
        if not follow_window and not profile_gpu_id and route.get("gpuDeviceId"):
            # A pinned GPU without a window context still needs a concrete
            # output: resolve the pin GPU's own (primary-first) display so the
            # Settings rewrite carries a validated LS display ID, never 0.
            try:
                inventory = self.gpu_router.detect()
                connected = [
                    item for item in inventory["displays"]
                    if str(item.get("gpuDeviceId") or "").casefold()
                    == str(route.get("gpuDeviceId") or "").casefold()
                ]
                if connected:
                    connected.sort(key=lambda item: (
                        not bool(item.get("primary")),
                        int(item.get("lsDisplayId") or 0),
                    ))
                    route["lsDisplayId"] = int(
                        connected[0].get("lsDisplayId") or 0)
            except Exception:
                logger.debug("Pinned GPU display resolution failed",
                             exc_info=True)
        if follow_window and target_hwnd:
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
                route["gpuName"] = selected.get("gpuName")
            if profile_display_id:
                route["lsDisplayId"] = selected["lsDisplayId"]
                route["deviceName"] = selected["deviceName"]
        return route

    def refresh_gpu_selections(self, inventory=None):
        with self._lock:
            if inventory is None:
                inventory = self.gpu_router.detect()
            self.gpu_selections.refresh(inventory)
            return inventory

    @staticmethod
    def _adapter_report_dir(profile_manager) -> Optional[str]:
        try:
            from .adapter_report import report_directory

            return str(report_directory(Path(profile_manager.config_dir)))
        except Exception:
            return None

    @staticmethod
    def _gpu_route_key(route: Dict) -> str:
        return f"{int(route.get('lsGpuId', 0))}:{int(route.get('lsDisplayId', 0))}:{route.get('deviceName', '')}"

    def _note_route_verified(self, route_key: Optional[str],
                             ghost_signature: str = "") -> None:
        """Record a proven route plus the ghost population it was proven under."""
        if route_key is not None:
            self._last_verified_route_key = route_key
        self._last_verified_ghost_signature = ghost_signature or ""

    def _ghost_signature_now(self) -> str:
        """Current DXGI ghost population; "" when unknown or ghost-free."""
        try:
            from .gpu_router import ghost_signature

            return ghost_signature(
                self.gpu_router.detect().get("dxgiAdapterCounts"))
        except Exception:
            return ""

    def _gpu_route_relaunch_due(self) -> bool:
        return (
            time.monotonic() - self._last_gpu_route_relaunch_at
        ) >= self.GPU_ROUTE_RELAUNCH_COOLDOWN_SECONDS

    def gpu_map_status(self) -> Dict:
        """Report whether the calibrated LS GPU map matches this machine.

        Returns one of ok (map present and fingerprint matches), stale
        (topology changed since calibration), missing (multi-GPU box with no
        calibration yet), or unnecessary (zero/one GPU: enumeration order is
        unambiguous). Callers should fail loudly on stale and warn on missing.
        """
        from .gpu_router import inventory_fingerprint

        inventory = self.gpu_router.detect()
        live = inventory_fingerprint(inventory)
        gpu_count = len(inventory.get("gpus", []))
        config = self.profile_manager.config
        stored = dict(getattr(config, "ls_gpu_id_map", None) or {})
        recorded = getattr(config, "ls_gpu_map_fingerprint", None)
        if gpu_count < 2:
            state = "unnecessary"
        elif not stored:
            state = "missing"
        elif recorded and recorded != live:
            state = "stale"
        else:
            state = "ok"
        from .gpu_router import ghost_signature, GpuRouter

        counts = inventory.get("dxgiAdapterCounts") or {}
        ghosts = GpuRouter._dxgi_ghosts(inventory.get("gpus", []), counts)
        return {
            "state": state,
            "gpuCount": gpu_count,
            "liveFingerprint": live,
            "recordedFingerprint": recorded,
            "mappedGpus": len(stored),
            "ghostSignature": ghost_signature(counts),
            "ghosts": ghosts,
        }

    def rollback_gpu_map(self) -> bool:
        """Restore the pre-calibration maps; returns False when none exists."""
        config = self.profile_manager.config
        previous_gpu = dict(getattr(config, "ls_gpu_id_map_previous", None) or {})
        if not previous_gpu:
            return False
        config.ls_gpu_id_map_previous = dict(config.ls_gpu_id_map or {})
        config.ls_gpu_id_map = previous_gpu
        previous_display = dict(
            getattr(config, "ls_display_id_map_previous", None) or {})
        config.ls_display_id_map_previous = dict(
            getattr(config, "ls_display_id_map", None) or {})
        config.ls_display_id_map = previous_display
        self.profile_manager.save_config(config)
        return True

    def _route_verification_due(self) -> bool:
        """Whether a fresh PresentMon proof of the active route is warranted."""
        if self._verifying_route or self.presentmon_provider is None:
            return False
        try:
            executable = self.presentmon_provider()
        except Exception:
            return False
        if not executable:
            return False
        try:
            status = self.gpu_map_status()
        except Exception:
            return False
        if status["state"] in ("stale", "missing"):
            return True
        # Ghost adapters reshuffle LS's own GPU numbering while the physical
        # topology (and its fingerprint) is unchanged, so a previously proven
        # LS id can silently point at a different card. Re-prove once per
        # ghost population instead of invalidating the calibration.
        if str(status.get("ghostSignature") or "") != self._last_verified_ghost_signature:
            return True
        key = self.state.current_gpu_route_key
        return bool(key) and key != self._last_verified_route_key

    def _verify_active_route(self) -> Optional[bool]:
        """Capture a few seconds of LS output; True=flowing, False=starved."""
        try:
            executable = self.presentmon_provider()
        except Exception:
            return None
        if not executable:
            return None
        try:
            from .benchmark_metrics import summarize_presentmon_files
            from .performance_benchmark import PresentMonCapture
        except Exception as error:
            logger.debug("Route verification unavailable (%s)", error)
            return None
        import tempfile

        try:
            with tempfile.TemporaryDirectory(prefix="ls-route-verify-") as tmp:
                run_dir = Path(tmp)
                capture = PresentMonCapture(Path(str(executable)), run_dir)
                try:
                    capture.start(["LosslessScaling.exe"],
                                  max(1.0, float(self.route_verify_seconds)))
                    capture.wait(timeout=float(self.route_verify_seconds) + 30)
                finally:
                    try:
                        capture.stop()
                    except Exception:
                        pass
                summaries = summarize_presentmon_files(
                    sorted(run_dir.glob("presentmon*.csv")), warmup_seconds=1.0)
        except Exception as error:
            logger.debug("Route verification capture failed (%s)", error)
            return None
        target = next(
            (item for item in summaries
             if str(item.get("application") or "").casefold()
             == "losslessscaling.exe"),
            None,
        )
        if target is None:
            return False
        fps = (target.get("metrics") or {}).get("displayed_fps") or {}
        median = fps.get("median")
        if not isinstance(median, (int, float)):
            return False
        return bool(median >= self.ROUTE_HEALTHY_FPS)

    def _verify_and_repair_route(self, profile: Optional[Profile],
                                 reason: str, target_pid: Optional[int],
                                 target_hwnd: Optional[int]) -> bool:
        """Prove the active route flows; auto-repair a starved one in production."""
        try:
            from . import adapter_report as adapter_report_module

            layers = adapter_report_module.detect_reshaping_layers(
                pid=target_pid)
        except Exception:
            layers = []
        if layers:
            logger.info(
                "Graphics-shaping layers present in LS (%s); host-side GPU "
                "order cannot be trusted, verification decides.",
                ", ".join(layers),
            )
        health = self._verify_active_route()
        if health is True:
            self._note_route_verified(self.state.current_gpu_route_key,
                                      self._ghost_signature_now())
            return True
        if health is None:
            return True
        logger.error(
            "Scaling is active but Lossless Scaling emits no frames; "
            "the LS GPU route is likely wrong for this machine.")
        if self.state.benchmark_mode_active:
            return False
        self._verifying_route = True
        try:
            return self._repair_route(profile, reason, target_pid, target_hwnd)
        finally:
            self._verifying_route = False

    def _repair_route(self, profile: Optional[Profile], reason: str,
                      target_pid: Optional[int],
                      target_hwnd: Optional[int]) -> bool:
        """Try every (GPU, display) route until LS emits frames; persist winners."""
        if not self.process_watcher:
            return False
        try:
            inventory = self.gpu_router.detect()
        except Exception as error:
            logger.error("Route repair cannot enumerate displays (%s)", error)
            return False
        gpu_count = max(1, len(inventory.get("gpus", [])))
        gdi_order = [str(d.get("deviceName") or "")
                     for d in inventory.get("displays", [])]
        try:
            from . import adapter_report as adapter_report_module

            adapter_report = adapter_report_module.load_latest(
                adapter_report_module.report_directory(
                    self.profile_manager.config_dir))
        except Exception:
            adapter_report = None
        display_ids = list(range(1, len(inventory.get("displays", [])) + 2))
        window_row = None
        if target_hwnd:
            try:
                window_row = self.gpu_router.route_for_window(target_hwnd)
            except Exception:
                window_row = None
        window_device = str((window_row or {}).get("deviceName") or "")
        window_gpu = str((window_row or {}).get("gpuDeviceId") or "")
        # Static wiring first: the window's own GDI display, then the mapped
        # recipe, then the remaining IDs. Displays are physically wired and
        # GDI-enumerated, so the static candidate is tried before any probe
        # of LS's private numbering.
        ordered_displays: list[int] = []
        for candidate in ([gdi_order.index(window_device) + 1]
                          if window_device in gdi_order else []):
            ordered_displays.append(candidate)
        for candidate in ([int((window_row or {}).get("lsDisplayId") or 0)]
                          + display_ids):
            if candidate and candidate not in ordered_displays:
                ordered_displays.append(candidate)
        title = (getattr(profile, "lossless_profile_title", None)
                 or getattr(profile, "name", None) or "Default")
        try:
            snapshot = self.lossless_settings.read()
            original: Optional[tuple[int, int]] = None
            for item in snapshot.get("profiles", []):
                if str(item.get("Title") or "").casefold() == title.casefold():
                    original = (int(item.get("PreferredGpuId") or 0),
                                int(item.get("OutputDisplayId") or 0))
                    break
        except Exception:
            original = None
        if original is None:
            logger.error("Route repair cannot read the current LS route")
            return False
        tried = 0
        for ls_display in ordered_displays:
            if tried >= self.ROUTE_REPAIR_BUDGET:
                break
            ordered_gpus = self._repair_gpu_order(
                adapter_report, gpu_count, title)
            for ls_gpu in ordered_gpus:
                if (ls_gpu, ls_display) == original:
                    continue
                if tried >= self.ROUTE_REPAIR_BUDGET:
                    break
                tried += 1
                logger.warning(
                    "Route repair trying LS GPU %s display %s (%d/%d)",
                    ls_gpu, ls_display, tried, self.ROUTE_REPAIR_BUDGET)
                try:
                    if self.process_watcher.check_is_lossless_scaling_running():
                        if not self.process_watcher.stop_lossless_scaling():
                            continue
                        self._last_gpu_route_relaunch_at = time.monotonic()
                    if not self.lossless_settings.update_gpu_route(
                            title, ls_gpu, ls_display):
                        continue
                    if not self.process_watcher.launch_lossless_scaling(force=True):
                        continue
                    if not self.set_scaling(
                            True, profile,
                            reason=f"{reason}_route_repair",
                            force=True, target_pid=target_pid,
                            target_hwnd=target_hwnd,
                            park_runtime_on_stop=False):
                        continue
                except Exception:
                    logger.exception("Route repair attempt failed")
                    continue
                if self._verify_active_route() is True:
                    device_pci = None
                    try:
                        from . import adapter_report as adapter_report_module

                        device_pci = adapter_report_module.device_pci(
                            adapter_report) if adapter_report else None
                    except Exception:
                        device_pci = None
                    self._persist_verified_route(
                        device_pci or window_gpu, window_device,
                        ls_gpu, ls_display)
                    return True
        logger.error("Route repair exhausted %d candidates without LS output",
                     tried)
        try:
            if self.process_watcher.check_is_lossless_scaling_running():
                self.process_watcher.stop_lossless_scaling()
            self.lossless_settings.update_gpu_route(title, *original)
            if self.process_watcher.launch_lossless_scaling(force=True):
                self.set_scaling(
                    True, profile, reason=f"{reason}_route_restore",
                    force=True, target_pid=target_pid, target_hwnd=target_hwnd,
                    park_runtime_on_stop=False)
        except Exception:
            logger.exception("Route repair restoration failed")
        return False

    def _persist_verified_route(self, window_gpu: str, window_device: str,
                                ls_gpu: int, ls_display: int) -> None:
        from .gpu_router import inventory_fingerprint

        config = self.profile_manager.config
        if window_gpu:
            config.ls_gpu_id_map_previous = dict(config.ls_gpu_id_map or {})
            config.ls_gpu_id_map = dict(config.ls_gpu_id_map or {})
            config.ls_gpu_id_map[window_gpu] = ls_gpu
        if window_device:
            config.ls_display_id_map_previous = dict(
                config.ls_display_id_map or {})
            config.ls_display_id_map = dict(config.ls_display_id_map or {})
            config.ls_display_id_map[window_device] = ls_display
        try:
            config.ls_gpu_map_fingerprint = inventory_fingerprint(
                self.gpu_router.detect())
        except Exception:
            pass
        self.profile_manager.save_config(config)
        self.state.current_gpu_route_key = self._gpu_route_key(
            {"lsGpuId": ls_gpu, "lsDisplayId": ls_display,
             "deviceName": window_device})
        self._note_route_verified(self.state.current_gpu_route_key,
                                  self._ghost_signature_now())
        logger.info("Route repair verified LS GPU %s display %s for %s",
                    ls_gpu, ls_display, window_device or "the target")

    def calibrate_gpu_map(self, workload_exe, *, progress=None,
                          window_timeout: float = 15.0) -> Dict:
        """Identify the correct LS GPU/display indices via the benchmark workload.

        Launches the benchmark workload as a scaling target, places it on each
        physical GPU's display in turn, and sweeps LS GPU ids until PresentMon
        proves Lossless Scaling emits frames. Winners persist through
        _persist_verified_route, so later scaling resolves stable PCI
        identities instead of today's volatile numbers. The Default profile's
        original route is always restored and Lossless Scaling is left as
        found. Returns a summary dict with state ok/partial/failed.
        """
        def notify(stage: str, detail: str = "") -> None:
            try:
                logger.info("GPU calibration [%s] %s", stage, detail)
                if progress is not None:
                    progress(stage, detail)
            except Exception:
                logger.debug("GPU calibration progress callback failed",
                             exc_info=True)

        from .performance_benchmark import (
            find_largest_window,
            focus_window,
        )

        if self.process_watcher is None or self.lossless_settings is None:
            raise RuntimeError("GPU calibration requires the desktop process watcher")
        if self.state.is_scaling_active:
            raise RuntimeError("Stop the current scaling session before calibrating GPUs")
        try:
            inventory = self.gpu_router.detect()
        except Exception as error:
            raise RuntimeError(f"GPU calibration cannot enumerate displays ({error})")
        gpus = list(inventory.get("gpus", []) or [])
        displays = list(inventory.get("displays", []) or [])
        if len(gpus) < 2:
            return {"state": "unnecessary", "gpuCount": len(gpus),
                    "mapped": {}, "tried": 0,
                    "detail": "Single-GPU system: enumeration order is unambiguous"}
        default_profile = next(
            (item for item in self.profile_manager.config.profiles
             if item.is_default),
            None,
        )
        if default_profile is None:
            raise RuntimeError("The Lossless Scaling default profile is unavailable")
        try:
            presentmon = self.presentmon_provider() if self.presentmon_provider else None
        except Exception:
            presentmon = None
        if not presentmon:
            raise RuntimeError("PresentMon is unavailable; stage or detect it before calibrating")
        workload_path = Path(str(workload_exe or ""))
        if not workload_path.is_file():
            raise RuntimeError("The benchmark workload executable is unavailable")
        title = (getattr(default_profile, "lossless_profile_title", None)
                 or getattr(default_profile, "name", None) or "Default")
        try:
            snapshot = self.lossless_settings.read()
            original: Optional[tuple[int, int]] = None
            for item in snapshot.get("profiles", []):
                if str(item.get("Title") or "").casefold() == title.casefold():
                    original = (int(item.get("PreferredGpuId") or 0),
                                int(item.get("OutputDisplayId") or 0))
                    break
        except Exception:
            original = None
        if original is None:
            raise RuntimeError("GPU calibration cannot read the current LS route")
        try:
            from . import adapter_report as adapter_report_module

            adapter_report = adapter_report_module.load_latest(
                adapter_report_module.report_directory(
                    self.profile_manager.config_dir))
        except Exception:
            adapter_report = None

        was_running = bool(
            self.process_watcher.check_is_lossless_scaling_running())
        workload = None
        mapped: Dict[str, int] = {}
        tried = 0
        budget = self.ROUTE_REPAIR_BUDGET * max(1, len(gpus))
        self._verifying_route = True
        try:
            notify("starting",
                   f"Probing {len(gpus)} GPUs with {workload_path.name}")
            workload = subprocess.Popen(
                [str(workload_path)], cwd=str(workload_path.parent))
            hwnd = find_largest_window(workload.pid, timeout=window_timeout)
            if not hwnd or not focus_window(hwnd):
                raise RuntimeError("The benchmark window could not take foreground")
            self.activate_profile(
                default_profile, str(workload_path), force=True,
                target_pid=workload.pid, target_hwnd=hwnd,
                suppress_auto_scale=True,
            )
            ordered_ids = self._repair_gpu_order(
                adapter_report, len(gpus), title)
            for gpu in gpus:
                pci = str(gpu.get("deviceId") or "")
                gpu_name = str(gpu.get("name") or "GPU")
                connected = [
                    item for item in displays
                    if str(item.get("gpuDeviceId") or "").casefold() == pci.casefold()
                ]
                connected.sort(key=lambda item: (
                    not bool(item.get("primary")),
                    int(item.get("lsDisplayId") or 0),
                ))
                display_device = str((connected[0].get("deviceName") or "")
                                     if connected else "")
                if display_device:
                    try:
                        if not self.window_manager.move_window_to_display(
                                hwnd, display_device):
                            logger.warning(
                                "Calibration could not move the workload to %s",
                                display_device)
                    except Exception:
                        logger.exception(
                            "Calibration continued without moving to %s",
                            display_device)
                    try:
                        focus_window(hwnd)
                    except Exception:
                        pass
                try:
                    window_row = self.gpu_router.route_for_window(hwnd)
                except Exception:
                    window_row = None
                window_device = str(
                    (window_row or {}).get("deviceName") or display_device)
                try:
                    ls_display = int((window_row or {}).get("lsDisplayId") or 0)
                except (TypeError, ValueError):
                    ls_display = 0
                if not ls_display:
                    ls_display = int((connected[0].get("lsDisplayId") or 1)
                                     if connected else 1)
                notify("probing", f"{gpu_name}: sweeping LS GPU ids")
                for ls_gpu in ordered_ids:
                    if tried >= budget:
                        break
                    tried += 1
                    notify("attempt",
                           f"{gpu_name}: trying LS GPU {ls_gpu} display "
                           f"{ls_display} ({tried}/{budget})")
                    try:
                        if self.process_watcher.check_is_lossless_scaling_running():
                            if not self.process_watcher.stop_lossless_scaling():
                                continue
                            self._last_gpu_route_relaunch_at = time.monotonic()
                        if not self.lossless_settings.update_gpu_route(
                                title, ls_gpu, ls_display):
                            continue
                        if not self.process_watcher.launch_lossless_scaling(force=True):
                            continue
                        if not self.set_scaling(
                                True, default_profile,
                                reason="gpu_calibration",
                                force=True, target_pid=workload.pid,
                                target_hwnd=hwnd,
                                park_runtime_on_stop=False):
                            continue
                    except Exception:
                        logger.exception("GPU calibration attempt failed")
                        continue
                    try:
                        healthy = self._verify_active_route() is True
                    finally:
                        try:
                            if self.state.is_scaling_active:
                                self.set_scaling(
                                    False, default_profile,
                                    reason="gpu_calibration_probe",
                                    force=True, park_runtime_on_stop=False)
                        except Exception:
                            logger.debug("Calibration probe stop failed",
                                         exc_info=True)
                    if healthy:
                        self._persist_verified_route(
                            pci, window_device, ls_gpu, ls_display)
                        mapped[pci] = ls_gpu
                        notify("verified",
                               f"{gpu_name} renders as LS GPU {ls_gpu}")
                        break
                if tried >= budget:
                    break
            if mapped:
                self.refresh_gpu_selections()
            state = ("ok" if len(mapped) == len(gpus)
                     else "partial" if mapped else "failed")
            notify("done",
                   f"{len(mapped)}/{len(gpus)} GPUs identified after "
                   f"{tried} attempt(s)")
            return {"state": state, "gpuCount": len(gpus), "mapped": mapped,
                    "tried": tried,
                    "fingerprint": getattr(
                        self.profile_manager.config,
                        "ls_gpu_map_fingerprint", None)}
        finally:
            self._verifying_route = False
            try:
                if self.state.is_scaling_active:
                    self.set_scaling(False, default_profile,
                                     reason="gpu_calibration_done",
                                     force=True, park_runtime_on_stop=False)
            except Exception:
                logger.debug("Calibration final stop failed", exc_info=True)
            try:
                if original is not None:
                    self.lossless_settings.update_gpu_route(title, *original)
            except Exception:
                logger.exception("Calibration could not restore the LS route")
            try:
                if self.process_watcher.check_is_lossless_scaling_running():
                    self.process_watcher.stop_lossless_scaling()
                if was_running:
                    self.process_watcher.launch_lossless_scaling(force=True)
            except Exception:
                logger.debug("Calibration LS state restore failed", exc_info=True)
            if workload is not None:
                try:
                    workload.terminate()
                    try:
                        workload.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        workload.kill()
                except Exception:
                    logger.debug("Calibration workload cleanup failed",
                                 exc_info=True)

    def _invert_map(self, mapping: Dict[str, int],
                    slot: int) -> Optional[str]:
        """Find the single PCI key claiming a numeric ID, else None."""
        if not isinstance(slot, int) or slot < 1:
            return None
        keys = [key for key, value in (mapping or {}).items()
                if value == slot]
        return keys[0] if len(keys) == 1 else None

    def _migrate_stored_routes(self) -> Dict[str, list]:
        """Translate explicit companion per-profile GPU numbers across a remap.

        Settings.json is ours, so this runs live at any time. Only entries
        that unambiguously match the previous map are rewritten; anything
        else (hand edits, unknown provenance) is reported untouched.
        """
        config = self.profile_manager.config
        old_gpu = dict(getattr(config, "ls_gpu_id_map_previous", None) or {})
        old_disp = dict(getattr(config, "ls_display_id_map_previous", None) or {})
        if not old_gpu and not old_disp:
            return {"migrated": [], "skipped": []}
        summary: Dict[str, list] = {"migrated": [], "skipped": []}
        try:
            inventory = self.gpu_router.detect()
        except Exception:
            return summary
        gpus = inventory.get("gpus", [])
        displays = inventory.get("displays", [])

        def new_gpu_id(pci: str) -> Optional[int]:
            for gpu in gpus:
                if str(gpu.get("deviceId") or "").casefold() == pci.casefold():
                    return int(gpu.get("lsGpuId") or 0) or None
            return None

        def new_disp_id(device: str) -> Optional[int]:
            for item in displays:
                if str(item.get("deviceName") or "").casefold() == device.casefold():
                    return int(item.get("lsDisplayId") or 0) or None
            return None

        def old_display_device(disp: int) -> Optional[str]:
            match = self._invert_map(old_disp, disp)
            return match

        changed = False
        for profile in config.profiles:
            values = profile.native_scaling_settings or {}
            try:
                gpu = int(values.get("PreferredGpuId") or 0)
                disp = int(values.get("OutputDisplayId") or 0)
            except (TypeError, ValueError):
                continue
            if gpu < 1 and disp < 1:
                continue
            pci = self._invert_map(old_gpu, gpu) if gpu >= 1 else None
            device = old_display_device(disp) if disp >= 1 else None
            if gpu >= 1 and pci is None:
                summary["skipped"].append(f"{profile.id}:gpu")
                continue
            if disp >= 1 and device is None:
                summary["skipped"].append(f"{profile.id}:display")
                continue
            target_gpu = new_gpu_id(pci) if pci else gpu
            target_disp = new_disp_id(device) if device else disp
            if target_gpu is None or target_disp is None:
                summary["skipped"].append(profile.id)
                continue
            if target_gpu != gpu or target_disp != disp:
                updated = dict(values)
                updated["PreferredGpuId"] = str(target_gpu)
                updated["OutputDisplayId"] = str(target_disp)
                profile.native_scaling_settings = updated
                summary["migrated"].append(profile.id)
                changed = True
        if changed:
            try:
                self.profile_manager.save_config(config)
            except Exception:
                logger.debug("Migrated profile routes could not be saved",
                             exc_info=True)
        return summary

    def _migrate_ls_profile_routes(self,
                                   skip_titles: Iterable[str] = ()) -> Dict[str, list]:
        """Translate stored LS Settings.xml routes across a remap.

        Only call while Lossless Scaling is stopped: Settings.xml is its live
        state file. Skips titles whose numbers don't unambiguously match the
        previous map (hand edits keep working untouched).
        """
        summary: Dict[str, list] = {"migrated": [], "skipped": []}
        config = self.profile_manager.config
        old_gpu = dict(getattr(config, "ls_gpu_id_map_previous", None) or {})
        old_disp = dict(getattr(config, "ls_display_id_map_previous", None) or {})
        if not old_gpu and not old_disp:
            return summary
        skip = {str(item).casefold() for item in (skip_titles or ())}
        try:
            inventory = self.gpu_router.detect()
        except Exception:
            return summary
        by_pci = {str(gpu.get("deviceId") or "").casefold(): gpu
                  for gpu in inventory.get("gpus", [])}
        by_device = {str(item.get("deviceName") or "").casefold(): item
                     for item in inventory.get("displays", [])}
        try:
            snapshot = self.lossless_settings.read()
        except Exception:
            return summary
        for item in snapshot.get("profiles", []):
            title = str(item.get("Title") or "")
            if not title or title.casefold() in skip:
                continue
            try:
                gpu = int(item.get("PreferredGpuId") or 0)
                disp = int(item.get("OutputDisplayId") or 0)
            except (TypeError, ValueError):
                continue
            if gpu < 1 and disp < 1:
                continue
            pci = self._invert_map(old_gpu, gpu) if gpu >= 1 else None
            device = self._invert_map(old_disp, disp) if disp >= 1 else None
            if gpu >= 1 and pci is None:
                summary["skipped"].append(f"{title}:gpu")
                continue
            if disp >= 1 and device is None:
                summary["skipped"].append(f"{title}:display")
                continue
            target_gpu = None
            target_disp = None
            if pci is not None:
                match = by_pci.get(pci.casefold())
                target_gpu = int(match.get("lsGpuId") or 0) if match else None
            else:
                target_gpu = gpu
            if device is not None:
                match = by_device.get(device.casefold())
                target_disp = int(match.get("lsDisplayId") or 0) if match else None
            else:
                target_disp = disp
            if not target_gpu or not target_disp:
                summary["skipped"].append(title)
                continue
            if (target_gpu, target_disp) == (gpu, disp):
                continue
            try:
                changed = self.lossless_settings.gpu_route_changes_required(
                    title, target_gpu, target_disp)
            except (OSError, ValueError):
                summary["skipped"].append(title)
                continue
            if changed:
                try:
                    if self.lossless_settings.update_gpu_route(
                            title, target_gpu, target_disp):
                        summary["migrated"].append(title)
                    else:
                        summary["skipped"].append(title)
                except (OSError, ValueError):
                    summary["skipped"].append(title)
        return summary

    def _repair_gpu_order(self, report: Optional[Dict], gpu_count: int,
                          title: Optional[str] = None) -> List[int]:
        """GPU IDs to try, report-derived candidates first.

        The in-LS report names the device adapter LS renders with plus its
        index in each enumeration LS could number from, so those IDs are
        tried before the blind 1..N sweep. The title-matched Settings route
        comes first when the report carries per-title numbers.
        """
        ordered: List[int] = []
        if report:
            try:
                from . import adapter_report as adapter_report_module

                ordered.extend(
                    adapter_report_module.candidate_gpu_ids(report, title))
            except Exception:
                pass
        for slot in range(1, max(1, gpu_count) + 1):
            if slot not in ordered:
                ordered.append(slot)
        return ordered

    def _settings_route(self, title: str) -> Optional[tuple[int, int]]:
        """Read the active (GPU, display) numbers of a Settings.xml profile."""
        try:
            snapshot = self.lossless_settings.read()
        except Exception:
            return None
        for item in snapshot.get("profiles", []):
            if str(item.get("Title") or "").casefold() != str(title).casefold():
                continue
            try:
                gpu = int(item.get("PreferredGpuId") or 0)
                disp = int(item.get("OutputDisplayId") or 0)
            except (TypeError, ValueError):
                return None
            return (gpu, disp) if gpu > 0 else None
        return None

    def _adopt_adapter_report(self, profile: Optional[Profile]) -> Optional[str]:
        """Silent per-scale check: adopt the in-LS adapter report.

        The report names the physical GPU LS actually renders with, so the
        active Settings numbers anchor to a PCI identity with zero probing.
        Only the mapping is corrected; the numbers stay untouched, so no
        restart is ever needed here. Returns "corrected", "confirmed", or
        None when no fresh report applied.
        """
        try:
            from . import adapter_report as adapter_report_module
            from .performance_benchmark import find_process_id
        except Exception:
            return None
        try:
            report = adapter_report_module.load_latest(
                adapter_report_module.report_directory(
                    self.profile_manager.config_dir))
        except Exception:
            return None
        if not report:
            return None
        try:
            ls_pid = find_process_id("LosslessScaling.exe")
        except Exception:
            ls_pid = None
        try:
            report_pid = int(report.get("pid") or 0)
        except (TypeError, ValueError):
            report_pid = 0
        if ls_pid and report_pid and int(ls_pid) != report_pid:
            return None
        device_pci = adapter_report_module.device_pci(report)
        if not device_pci:
            return None
        title = (getattr(profile, "lossless_profile_title", None)
                 or getattr(profile, "name", None) or "Default")
        active = self._settings_route(title)
        if active is None:
            return None
        settings_gpu, _ = active
        config = self.profile_manager.config
        mapping = dict(getattr(config, "ls_gpu_id_map", None) or {})
        result = "confirmed"
        changed = False
        if not any(adapter_report_module.same_device(key, device_pci)
                   and slot == settings_gpu
                   for key, slot in mapping.items()):
            known = {key: slot for key, slot in mapping.items()
                     if adapter_report_module.same_device(key, device_pci)}
            if known and all(slot == settings_gpu for slot in known.values()):
                logger.debug("Adapter report confirms LS GPU %s is %s",
                             settings_gpu, device_pci)
            else:
                logger.info("Adapter report remaps LS GPU %s to %s",
                            settings_gpu, device_pci)
            config.ls_gpu_id_map_previous = dict(mapping)
            mapping[device_pci] = settings_gpu
            config.ls_gpu_id_map = mapping
            changed = True
            result = "corrected"
        try:
            from .gpu_router import inventory_fingerprint

            live = inventory_fingerprint(self.gpu_router.detect())
            if getattr(config, "ls_gpu_map_fingerprint", None) != live:
                config.ls_gpu_map_fingerprint = live
                changed = True
        except Exception:
            pass
        if changed:
            try:
                self.profile_manager.save_config(config)
            except Exception:
                logger.debug("Adapter report map could not be saved",
                             exc_info=True)
            try:
                migrated = self._migrate_stored_routes()
                if migrated["migrated"] or migrated["skipped"]:
                    logger.info("Migrated stored profile routes: %s", migrated)
            except Exception:
                logger.debug("Stored profile route migration skipped",
                             exc_info=True)
        return result

    def _enforce_intended_route(self, profile: Optional[Profile],
                                reason: str, target_pid: Optional[int],
                                target_hwnd: Optional[int]) -> bool:
        """One clean unscale/rewrite/rescale when the map correction changed intent.

        A corrected map can re-resolve the pin or explicit profile numbers to
        different LS numbers than the live session uses (e.g. GPUs 1 and 2
        turn out reversed). Scaling would otherwise continue on the wrong
        card indefinitely. This performs a single handoff; the updated route
        key plus the relaunch cooldown prevent any loop.
        """
        if (self.state.benchmark_mode_active or self.process_watcher is None
                or not self._gpu_route_relaunch_due()):
            return True
        title = (getattr(profile, "lossless_profile_title", None)
                 or getattr(profile, "name", None) or "Default")
        active = self._settings_route(title)
        if active is None:
            return False
        try:
            desired = self._desired_gpu_route(profile, target_hwnd)
            wanted = (int(desired.get("lsGpuId") or 0),
                      int(desired.get("lsDisplayId") or 0))
        except Exception as error:
            logger.error("Route correction target is unavailable (%s)", error)
            return False
        if wanted == active or wanted[0] < 1:
            return True
        logger.warning(
            "Correcting live route %s to %s for '%s' after the GPU map changed",
            active, wanted, getattr(profile, "name", None) or "Default")
        if not self.set_scaling(
                False, profile, reason=f"{reason}_route_correction",
                force=True, park_runtime_on_stop=False):
            logger.error("Could not stop scaling for the route correction")
            return False
        if not self.process_watcher.stop_lossless_scaling():
            logger.error("Could not stop Lossless Scaling for the route correction")
            return False
        self._last_gpu_route_relaunch_at = time.monotonic()
        try:
            migrated = self._migrate_ls_profile_routes()
            if migrated["migrated"] or migrated["skipped"]:
                logger.info("Migrated stored LS profile routes: %s", migrated)
        except Exception:
            logger.debug("Stored LS profile route migration skipped", exc_info=True)
        try:
            updated = self.lossless_settings.update_gpu_route(title, *wanted)
        except (OSError, ValueError) as error:
            logger.error("Route correction write failed (%s)", error)
            updated = False
        if not updated:
            if self.process_watcher:
                self.process_watcher.launch_lossless_scaling(force=True)
            return False
        if not self.process_watcher.launch_lossless_scaling(force=True):
            logger.error("Could not restart Lossless Scaling after route correction")
            return False
        return bool(self.set_scaling(
            True, profile, reason=f"{reason}_route_corrected",
            force=True, target_pid=target_pid, target_hwnd=target_hwnd,
            park_runtime_on_stop=False))

    def _live_deployment_stale(self, profile: Optional[Profile]) -> bool:
        """Whether the running Lossless Scaling host needs a DLL refresh.

        Idle foreground switches defer deployment swaps (see
        activate_profile); this reports when the live host is behind the
        desired deployment so scaling can refresh it first.
        """
        try:
            if profile is None or self.process_watcher is None:
                return False
            if not self.process_watcher.check_is_lossless_scaling_running():
                return False
            configured_exe = self.profile_manager.config.lossless_scaling_exe_path
            if not configured_exe:
                return False
            return bool(self.deployment_manager.needs_change(
                profile.id,
                self.resolve_deployment_files(profile),
                lossless_scaling_exe=configured_exe,
            ))
        except Exception:
            return False

    def _defer_gpu_route_relaunch(
        self, profile: Optional[Profile], gpu_route: Optional[Dict]
    ) -> Optional[Dict]:
        """Return gpu_route, or None when a route-only relaunch must wait.

        Lossless Scaling reads its GPU route only at startup, so applying a
        route costs a full stop/relaunch that drops scaling and forces WGC
        re-capture. While scaling is active, or when LS was relaunched moments
        ago (foreground flicker between displays), keep the current route
        instead of restarting in a loop; the pending route applies on the next
        natural launch. Returns None when the caller must skip both the
        Settings.xml rewrite and the relaunch.
        """
        if gpu_route is None:
            return None
        if not (
            self.process_watcher
            and self.process_watcher.check_is_lossless_scaling_running()
        ):
            return gpu_route
        if self.state.is_scaling_active or not self._gpu_route_relaunch_due():
            logger.info(
                "Keeping Lossless Scaling on its current GPU route while %s; "
                "route %s for '%s' will apply on the next launch",
                "scaling is active"
                if self.state.is_scaling_active
                else "a recent relaunch is still settling",
                self._gpu_route_key(gpu_route),
                profile.name if profile else "Default",
            )
            return None
        return gpu_route

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
            if not self._gpu_route_relaunch_due():
                logger.info(
                    "Skipping display GPU handoff to route %s; a recent relaunch is still settling",
                    route_key,
                )
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
            self._last_gpu_route_relaunch_at = time.monotonic()
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

    def _announce_profile_activation(
        self, previous: Optional[Profile], profile: Optional[Profile],
        target_hwnd: Optional[int], suppress_auto_scale: bool,
        source: Optional[str] = None) -> None:
        """Narrate detection + routing on the overlay; never interrupts."""
        try:
            if profile is None or (previous is not None
                                   and previous.id == profile.id):
                return
            config = self.profile_manager.config
            automatic = bool(
                profile and not profile.is_default and profile.auto_scale
                and config.disable_native_auto_scale
                and self.state.auto_scale_enabled
                and not suppress_auto_scale)
            detected = f"Game window detected: {profile.name}"
            if source:
                detected += f" ({source})"
            self.overlay_indicator.log(detected, kind="armed",
                                       anchor_hwnd=target_hwnd)
            self.overlay_indicator.log(
                f"Configuring for profile: {profile.name}"
                + (" (auto-scale on focus)" if automatic
                   else " (manual hotkey)"),
                kind="armed", anchor_hwnd=target_hwnd)
            if automatic:
                self.overlay_indicator.set_border("pending",
                                                  anchor_hwnd=target_hwnd)
        except Exception:
            pass

    def activate_profile(
        self,
        profile: Optional[Profile],
        exe_path: Optional[str] = None,
        *,
        force: bool = False,
        target_pid: Optional[int] = None,
        target_hwnd: Optional[int] = None,
        suppress_auto_scale: bool = False,
        source: Optional[str] = None,
        for_scaling: bool = False,
    ) -> None:
        if self.profile_manager.repair_session:
            return
        with self._lock:
            if self.state.update_in_progress and not suppress_auto_scale:
                return
            previous = self.state.current_active_profile
            same_profile = bool(previous and profile and previous.id == profile.id)
            if (
                previous
                and not same_profile
                and self.profile_manager.config.addon_manager_enabled
            ):
                self.addon_configs.capture(
                    previous,
                    self.profile_manager.config.lossless_scaling_exe_path,
                )
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
                gpu_route = self._defer_gpu_route_relaunch(profile, gpu_route)
                if gpu_route is None:
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
                        self._last_gpu_route_relaunch_at = time.monotonic()
                        self.process_watcher.launch_lossless_scaling(force=True)
                return

            if not force and self.state.is_scaling_active and previous and not same_profile:
                # A live scaling session owns Lossless Scaling until its
                # target closes or the user scales something else explicitly.
                # Foreground changes never steal or disturb it.
                logger.info(
                    "Keeping scaled profile '%s' active while foreground changed to '%s'",
                    previous.name,
                    profile.name if profile else "an unprofiled application",
                )
                return

            try:
                configured_exe = self.profile_manager.config.lossless_scaling_exe_path
                deployment_files = self.resolve_deployment_files(profile)
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
                    profile and runtime_providers and not self.state.is_scaling_active
                    and not will_auto_scale and not for_scaling
                )
                runtime_state_change = bool(
                    park_runtime
                    and self.deployment_manager.runtime_packages_need_state(
                        runtime_providers, active=False,
                        lossless_scaling_exe=self.profile_manager.config.lossless_scaling_exe_path,
                    )
                )
                deployment_change = deployment_change or runtime_state_change
                # DLL/addon/runtime file state (but not the GPU route, which
                # still needs a live restart to take effect).
                dll_deployment_change = deployment_change
                gpu_route = self._desired_gpu_route(profile, target_hwnd) if profile else None
                gpu_route_change = self.lossless_settings.gpu_route_changes_required(
                    profile.lossless_profile_title,
                    int(gpu_route.get("lsGpuId", 0)),
                    int(gpu_route.get("lsDisplayId", 0)),
                ) if profile and gpu_route else False
                if bool(gpu_route_change) and not deployment_change:
                    # Route-only change: no DLL/manifest difference. Skip the
                    # stop/relaunch while a session is live or LS just
                    # relaunched; the route applies on the next natural launch.
                    gpu_route = self._defer_gpu_route_relaunch(profile, gpu_route)
                    gpu_route_change = gpu_route is not None
                deployment_change = deployment_change or bool(gpu_route_change)
            except (GraphicsResolutionError, OSError, ValueError, RuntimeError) as error:
                logger.error("Profile '%s' graphics plan is invalid: %s", profile.name if profile else "none", error)
                return
            restart_lossless_scaling = False
            restart_succeeded = True
            reshade_swapped = False
            transition_succeeded = False
            restart_lossless_scaling = False
            defer_live_files = False
            if deployment_change and self.process_watcher:
                ls_running = self.process_watcher.check_is_lossless_scaling_running()
                live_needed = bool(
                    force or for_scaling
                    or self.state.is_scaling_active or will_auto_scale
                )
                if (ls_running and not live_needed
                        and dll_deployment_change):
                    # Idle handoff (e.g. alt-tab to a browser): Lossless
                    # Scaling is running but capturing nothing, so tearing it
                    # down and relaunching buys nothing. Update bookkeeping
                    # and the GPU route now; the DLL set is applied lazily
                    # when scaling actually starts (see set_scaling).
                    # Route-only changes are NOT deferred: the route is read
                    # at startup, so a live restart is the only way to apply
                    # it.
                    logger.info(
                        "Deferring DLL profile restart for '%s' until scaling starts (Lossless Scaling is idle)",
                        profile.name if profile else "none",
                    )
                    try:
                        self.overlay_indicator.log(
                            f"Profile ready: {profile.name if profile else 'none'} "
                            "(restart deferred)",
                            anchor_hwnd=target_hwnd)
                    except Exception:
                        pass
                    defer_live_files = True
                else:
                    restart_lossless_scaling = ls_running
                    if restart_lossless_scaling:
                        self._last_gpu_route_relaunch_at = time.monotonic()
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
                    trigger_runtime_actions=(
                        not restart_lossless_scaling and not defer_live_files),
                    target_pid=target_pid,
                    target_hwnd=target_hwnd,
                    suppress_auto_scale=suppress_auto_scale,
                    gpu_route=gpu_route,
                    defer_live_files=defer_live_files,
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
            if transition_succeeded:
                self._announce_profile_activation(
                    previous, profile, target_hwnd, suppress_auto_scale,
                    source=source)

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
        defer_live_files: bool = False,
    ) -> bool:
        """Clean the old profile and apply the new one while its DLL host is stopped.

        With ``defer_live_files`` only bookkeeping, the GPU route, and log
        lines are updated; DLL/addon/ReShade files are left untouched because
        the host is idle, and applied lazily when scaling starts.
        """
        reshade_swapped = False
        if previous and not defer_live_files:
            self._cleanup_profile(previous)

        configured_exe = self.profile_manager.config.lossless_scaling_exe_path
        if configured_exe and not defer_live_files:
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
                if route_updated:
                    logger.info("Applied GPU route for '%s': GPU %s (%s), display %s", profile.name if profile else "Default", gpu_route.get("lsGpuId"), gpu_route.get("gpuName") or gpu_route.get("gpuDeviceId") or "Auto", gpu_route.get("lsDisplayId"))
                    self.overlay_indicator.log(
                        f"Routing GPU {gpu_route.get('lsGpuId')} / "
                        f"display {gpu_route.get('lsDisplayId')} for "
                        f"'{profile.name if profile else 'Default'}'",
                        kind="info", anchor_hwnd=target_hwnd)
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
                self.addon_configs.mark_applied(profile)
                if getattr(profile, "addon_config", None):
                    self.overlay_indicator.log(
                        "Applying addon configuration",
                        anchor_hwnd=target_hwnd)
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

        if not defer_live_files and profile.reshade and profile.reshade.enabled:
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
                    self.overlay_indicator.log(
                        f"Copying ReShade preset: {reshade.managed_profile_id}",
                        anchor_hwnd=target_hwnd)
                    if owned_backup:
                        self._reshade_inis[profile.id] = reshade_ini_path
            elif reshade_ini_path and reshade.preset_path:
                backup_path = Path(reshade_ini_path).with_suffix(".ini.bak")
                owned_backup = not backup_path.exists()
                if ReshadeManager.swap_preset(reshade_ini_path, reshade.preset_path):
                    reshade_swapped = True
                    self.overlay_indicator.log(
                        "Copying ReShade preset: "
                        f"{Path(reshade.preset_path).name}",
                        anchor_hwnd=target_hwnd)
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

    def retry_deployment_recovery(self) -> None:
        with self._lock:
            if self.state.is_scaling_active:
                raise ValueError("Stop scaling before retrying deployment recovery")
            was_running = bool(self.process_watcher and self.process_watcher.check_is_lossless_scaling_running())
            if was_running and not self.process_watcher.stop_lossless_scaling():
                raise RuntimeError("Lossless Scaling could not be stopped for recovery")
            self.deployment_manager.retry_recovery(self.profile_manager.config.lossless_scaling_exe_path)
            if was_running and not self.process_watcher.launch_lossless_scaling(force=True):
                raise RuntimeError("Recovery succeeded, but Lossless Scaling could not be restarted")

    def shutdown(self) -> None:
        with self._lock:
            try:
                self.overlay_indicator.stop()
            except Exception:
                pass
            for reshade_ini in list(self._reshade_inis.values()):
                ReshadeManager.restore_backup(reshade_ini)
            self._reshade_inis.clear()
