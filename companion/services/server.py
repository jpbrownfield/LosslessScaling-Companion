"""
Async WebSocket server for communication with Chrome Extension and UI clients.
"""

import asyncio
import json
import logging
import secrets
import zipfile
from http import HTTPStatus
from pathlib import Path
from typing import Dict, Set, Optional
from urllib.parse import parse_qs, urlsplit
from websockets.legacy.server import WebSocketServerProtocol, serve
from websockets.exceptions import ConnectionClosed

from ..core.models import (
    AppConfig,
    BrowserFullscreenEvent,
    GraphicsStackConfig,
    HotkeyConfig,
    Profile,
    RtssLimiterConfig,
)
from ..core.profile_manager import ProfileManager
from ..core.state import AppState
from .process_watcher import ProcessWatcher
from .automation import AutomationController
from .ls_settings import LosslessSettingsXml
from .asset_store import AssetStore
from .release_providers import ReleaseManager
from .startup_manager import StartupTaskManager
from .process_lasso_monitor import ProcessLassoLogTailer
from .rtss_manager import RtssProfileManager
from .dynamic_limiter import DynamicLimiterController

logger = logging.getLogger("LosslessCompanion.Server")


class CompanionWebSocketServer:
    def __init__(
        self,
        profile_manager: ProfileManager,
        state: AppState,
        process_watcher: Optional[ProcessWatcher] = None,
        automation: Optional[AutomationController] = None,
        ls_settings: Optional[LosslessSettingsXml] = None,
        asset_store: Optional[AssetStore] = None,
        release_manager: Optional[ReleaseManager] = None,
        startup_manager: Optional[StartupTaskManager] = None,
        rtss_manager: Optional[RtssProfileManager] = None,
        dynamic_limiter: Optional[DynamicLimiterController] = None,
    ):
        self.profile_manager = profile_manager
        self.state = state
        self.process_watcher = process_watcher
        self.automation = automation or AutomationController(profile_manager, state)
        self.ls_settings = ls_settings or LosslessSettingsXml(
            profile_manager.config.lossless_settings_xml_path
        )
        self.asset_store = asset_store or self.automation.asset_store
        self.release_manager = release_manager or ReleaseManager(self.asset_store)
        self.startup_manager = startup_manager or StartupTaskManager()
        self.rtss_manager = rtss_manager or RtssProfileManager(
            profile_manager.config.rtss_install_path
        )
        self.dynamic_limiter = dynamic_limiter
        self.config: AppConfig = profile_manager.config
        self.clients: Set[WebSocketServerProtocol] = set()
        self.fullscreen_tasks: Dict[str, asyncio.Task] = {}
        self.server = None
        self.dashboard_token = secrets.token_urlsafe(32)
        self.client_authority: Dict[WebSocketServerProtocol, str] = {}

    async def start(self) -> None:
        logger.info(f"Starting WebSocket server on {self.config.host}:{self.config.port}...")
        self.server = await serve(
            self.handle_client,
            self.config.host,
            self.config.port,
            process_request=self.process_http_request,
        )
        logger.info(f"WebSocket server listening on ws://{self.config.host}:{self.config.port}/ws")

    async def stop(self) -> None:
        for task in self.fullscreen_tasks.values():
            task.cancel()
        self.fullscreen_tasks.clear()
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            logger.info("WebSocket server stopped.")

    async def handle_client(self, websocket: WebSocketServerProtocol, path: str = "") -> None:
        parsed_path = urlsplit(path)
        if parsed_path.path != "/ws":
            await websocket.close(code=1008, reason="Unknown endpoint")
            return
        origin = websocket.request_headers.get("Origin")
        if not self.is_origin_allowed(origin):
            logger.warning("Rejected WebSocket origin: %r", origin)
            await websocket.close(code=1008, reason="Origin not allowed")
            return
        query_token = parse_qs(parsed_path.query).get("token", [None])[0]
        if self._is_dashboard_origin(origin) and secrets.compare_digest(
            query_token or "", self.dashboard_token
        ):
            authority = "dashboard"
        elif origin and origin.startswith("chrome-extension://"):
            authority = "extension"
        else:
            authority = "readonly"
        self.clients.add(websocket)
        self.client_authority[websocket] = authority
        self.state.connected_clients = len(self.clients)
        logger.info(f"Client connected. Total clients: {self.state.connected_clients}")

        # Send initial handshake / status
        await websocket.send(json.dumps({
            "type": "INITIAL_STATE",
            "isScalingActive": self.state.is_scaling_active,
            "activeProfile": self.state.current_active_profile.model_dump() if self.state.current_active_profile else None,
            "scalingTarget": self.state.current_scaled_target,
            "dynamicLimiter": self.state.dynamic_limiter_status,
            "scalingControl": self.state.scaling_control_status,
        }))

        try:
            async for message in websocket:
                await self.process_message(websocket, message)
        except ConnectionClosed:
            pass
        finally:
            self.clients.remove(websocket)
            self.client_authority.pop(websocket, None)
            self.state.connected_clients = len(self.clients)
            logger.info(f"Client disconnected. Total clients: {self.state.connected_clients}")

    async def process_message(self, websocket: WebSocketServerProtocol, raw_message: str) -> None:
        try:
            msg = json.loads(raw_message)
            msg_type = msg.get("type")

            dashboard_messages = {
                "UPDATE_LOSSLESS_PROFILE",
                "IMPORT_LOSSLESS_PROFILE",
                "SET_ACTIVE_PROFILE",
                "SAVE_PROFILE",
                "DELETE_PROFILE",
                "CHECK_GRAPHICS_RELEASES",
                "STAGE_GRAPHICS_RELEASE",
                "IMPORT_GRAPHICS_ASSET",
                "IMPORT_GRAPHICS_PACKAGE",
                "SAVE_CONTROL_SETTINGS",
                "SAVE_GENERAL_SETTINGS",
                "RESET_RTSS_CALIBRATION",
                "START_RTSS_MANUAL_CALIBRATION",
                "REVERT_ALL_CHANGES",
            }
            extension_messages = {"BROWSER_FULLSCREEN_EVENT", "MANUAL_TRIGGER"}
            authority = self.client_authority.get(websocket, "readonly")
            if msg_type in dashboard_messages and authority != "dashboard":
                raise PermissionError("Dashboard capability token required")
            if msg_type in extension_messages and authority not in {"dashboard", "extension"}:
                raise PermissionError("Automation messages require an authorized local client")

            if msg_type == "PING":
                await websocket.send(json.dumps({"type": "PONG", "timestamp": msg.get("timestamp")}))
                return

            if msg_type == "BROWSER_FULLSCREEN_EVENT":
                event = BrowserFullscreenEvent.model_validate(msg)
                self.schedule_fullscreen_event(event.model_dump())
                return

            if msg_type == "MANUAL_TRIGGER":
                await self.handle_manual_trigger(msg)
                return

            if msg_type == "GET_STATE_AND_PROCESSES":
                visible_only = msg.get("visibleOnly", True)
                await self.send_full_data_update(websocket, visible_windows_only=visible_only)
                return

            if msg_type == "GET_LOSSLESS_SETTINGS":
                await websocket.send(json.dumps({
                    "type": "LOSSLESS_SETTINGS",
                    "settings": self.ls_settings.read(),
                }))
                return

            if msg_type == "GET_NATIVE_IMPORT_PREVIEW":
                native = self.ls_settings.read().get("profiles", [])
                await websocket.send(json.dumps({
                    "type": "NATIVE_IMPORT_PREVIEW",
                    "profiles": self.profile_manager.preview_native_imports(native),
                }))
                return

            if msg_type == "IMPORT_LOSSLESS_PROFILE":
                title = str(msg.get("title") or "")
                native = self.ls_settings.get_profile(title)
                if native is None:
                    raise ValueError("Unknown native Lossless Scaling profile")
                profile = self.profile_manager.import_native_profile(
                    native,
                    existing_profile_id=msg.get("profileId"),
                )
                await websocket.send(json.dumps({"type": "NATIVE_PROFILE_IMPORTED", "profile": profile.model_dump()}))
                await self.broadcast_full_data_update()
                return

            if msg_type == "GET_GRAPHICS_STATUS":
                await websocket.send(json.dumps({
                    "type": "GRAPHICS_STATUS",
                    "providers": self.release_manager.registry.list_provider_ids(),
                    "packages": self.asset_store.list_packages(),
                    "assetStore": str(self.asset_store.root),
                }))
                return

            if msg_type == "GET_GENERAL_SETTINGS":
                await websocket.send(json.dumps({
                    "type": "GENERAL_SETTINGS",
                    "runAtStartup": await asyncio.to_thread(self.startup_manager.is_enabled),
                    "startupTaskName": self.startup_manager.TASK_NAME,
                    "trayOnly": True,
                    "controlSettings": self._control_settings_payload(),
                }))
                return

            if msg_type == "GET_RTSS_STATUS":
                await websocket.send(json.dumps({
                    "type": "RTSS_STATUS",
                    "status": self.rtss_manager.status(self.config.rtss_install_path),
                }))
                return

            if msg_type == "RESET_RTSS_CALIBRATION":
                if self.dynamic_limiter is None:
                    raise RuntimeError("Dynamic limiter service is unavailable")
                result = await asyncio.to_thread(
                    self.dynamic_limiter.reset, str(msg.get("profileId") or "")
                )
                await websocket.send(json.dumps({"type": "RTSS_CALIBRATION_STATUS", "status": result}))
                await self.broadcast_full_data_update()
                return

            if msg_type == "START_RTSS_MANUAL_CALIBRATION":
                if self.dynamic_limiter is None:
                    raise RuntimeError("Dynamic limiter service is unavailable")
                result = await asyncio.to_thread(
                    self.dynamic_limiter.start_manual, str(msg.get("profileId") or "")
                )
                await websocket.send(json.dumps({"type": "RTSS_CALIBRATION_STATUS", "status": result}))
                await self.broadcast_state()
                return

            if msg_type == "REVERT_ALL_CHANGES":
                if not bool(msg.get("confirmRevertAll")):
                    raise ValueError("Confirm revert of all helper-managed changes")
                result = await self._revert_all_changes()
                await websocket.send(json.dumps({"type": "ALL_CHANGES_REVERTED", **result}))
                await self.broadcast_full_data_update()
                return

            if msg_type == "SAVE_CONTROL_SETTINGS":
                hotkey = HotkeyConfig.model_validate(msg.get("hotkey") or {})
                mode = str(msg.get("mode") or "")
                if mode not in {"helper_controls_lossless", "follow_lossless", "warn_only"}:
                    raise ValueError("Unsupported hotkey synchronization mode")
                self.config.global_hotkey = hotkey
                self.config.hotkey_sync_mode = mode
                self.config.lossless_control_configured = True
                self.config.disable_native_auto_scale = bool(msg.get("disableNativeAutoScale", True))
                self.config.minimize_other_windows_on_scale = bool(
                    msg.get(
                        "minimizeOtherWindowsOnScale",
                        self.config.minimize_other_windows_on_scale,
                    )
                )
                self.config.process_lasso_performance_mode_scaling = bool(
                    msg.get(
                        "processLassoPerformanceModeScaling",
                        self.config.process_lasso_performance_mode_scaling,
                    )
                )
                self.config.process_lasso_log_path = str(
                    msg.get("processLassoLogPath") or ""
                ).strip() or None
                self.config.rtss_frame_limiting_enabled = bool(
                    msg.get(
                        "rtssFrameLimitingEnabled",
                        self.config.rtss_frame_limiting_enabled,
                    )
                )
                self.config.rtss_install_path = str(
                    msg.get("rtssInstallPath", self.config.rtss_install_path) or ""
                ).strip() or None
                self.rtss_manager.configured_path = self.config.rtss_install_path
                self.profile_manager.save_config()
                await asyncio.to_thread(
                    self.automation.reconcile_window_management_setting
                )
                synchronized = await asyncio.to_thread(
                    self.process_watcher.enforce_helper_scaling_control
                ) if self.process_watcher else False
                await websocket.send(json.dumps({
                    "type": "CONTROL_SETTINGS_SAVED",
                    "synchronized": synchronized,
                    "controlSettings": self._control_settings_payload(),
                }))
                return

            if msg_type == "SAVE_GENERAL_SETTINGS":
                hotkey = HotkeyConfig.model_validate(msg.get("hotkey") or {})
                mode = str(msg.get("mode") or "")
                if mode not in {"helper_controls_lossless", "follow_lossless", "warn_only"}:
                    raise ValueError("Unsupported hotkey synchronization mode")
                run_at_startup = bool(msg.get("runAtStartup", False))
                enable_rtss = bool(msg.get("rtssFrameLimitingEnabled", False))
                requested_default_mode = str(msg.get("rtssDefaultLimitMode") or "static")
                if requested_default_mode not in {"static", "dynamic"}:
                    raise ValueError("Unsupported default RTSS limiter mode")
                requested_gpu_target = float(msg.get("rtssDefaultGpuTargetPercent", 15.0))
                if not 1.0 <= requested_gpu_target <= 100.0:
                    raise ValueError("Default Lossless Scaling GPU target must be between 1 and 100")
                requested_rtss_path = str(msg.get("rtssInstallPath") or "").strip() or None
                requested_default_auto_scale = bool(
                    msg.get("defaultProfileAutoScale", True)
                )
                managed_rtss_profiles = [
                    profile for profile in self.config.profiles if profile.rtss.enabled
                ]
                disabling_rtss = self.config.rtss_frame_limiting_enabled and not enable_rtss
                relocating_rtss = (
                    self.config.rtss_frame_limiting_enabled
                    and enable_rtss
                    and (self.config.rtss_install_path or "").casefold()
                    != (requested_rtss_path or "").casefold()
                )
                strategy_changed = (
                    requested_default_mode != self.config.rtss_default_limit_mode
                    or requested_gpu_target != self.config.rtss_default_gpu_target_percent
                )
                if (
                    managed_rtss_profiles
                    and (disabling_rtss or relocating_rtss)
                    and not bool(msg.get("confirmRtssGlobalChange"))
                ):
                    raise ValueError(
                        "Confirm deactivation of all currently managed RTSS limiter profiles"
                    )
                startup = await asyncio.to_thread(
                    self.startup_manager.set_enabled, run_at_startup
                )

                if disabling_rtss or relocating_rtss:
                    for profile in managed_rtss_profiles:
                        if profile.rtss.managed_target_process:
                            await asyncio.to_thread(
                                self.rtss_manager.remove_profile,
                                profile,
                                self.config.rtss_install_path,
                            )

                if enable_rtss and (
                    not self.config.rtss_frame_limiting_enabled or relocating_rtss or strategy_changed
                ):
                    updated_profiles = []
                    for profile in managed_rtss_profiles:
                        updated = profile.model_copy(deep=True)
                        if strategy_changed and updated.rtss.limit_mode == "inherit":
                            updated.rtss.learned_framerate_limit = None
                            updated.rtss.game_gpu_baseline_percent = None
                            updated.rtss.game_gpu_high_water_percent = None
                            updated.rtss.automatic_calibration_disabled = False
                        previous = (
                            updated
                            if updated.rtss.managed_target_process and not relocating_rtss
                            else None
                        )
                        await asyncio.to_thread(
                            self.rtss_manager.apply_profile,
                            updated,
                            previous,
                            requested_rtss_path,
                            framerate_limit=self._rtss_limit_for_profile(
                                updated, requested_default_mode
                            ),
                        )
                        updated_profiles.append((profile, updated))
                    for profile, updated in updated_profiles:
                        profile.rtss = updated.rtss

                self.config.run_at_startup = run_at_startup
                self.config.default_profile_auto_scale = requested_default_auto_scale
                self.config.global_hotkey = hotkey
                self.config.hotkey_sync_mode = mode
                self.config.lossless_control_configured = True
                self.config.disable_native_auto_scale = bool(msg.get("disableNativeAutoScale", True))
                self.config.minimize_other_windows_on_scale = bool(
                    msg.get("minimizeOtherWindowsOnScale", False)
                )
                self.config.process_lasso_performance_mode_scaling = bool(
                    msg.get("processLassoPerformanceModeScaling", False)
                )
                self.config.process_lasso_log_path = str(
                    msg.get("processLassoLogPath") or ""
                ).strip() or None
                self.config.rtss_frame_limiting_enabled = enable_rtss
                self.config.rtss_install_path = requested_rtss_path
                self.config.rtss_default_limit_mode = requested_default_mode
                self.config.rtss_default_gpu_target_percent = requested_gpu_target
                self.rtss_manager.configured_path = self.config.rtss_install_path
                self.profile_manager.save_config()
                await asyncio.to_thread(
                    self.automation.reconcile_window_management_setting
                )
                synchronized = await asyncio.to_thread(
                    self.process_watcher.enforce_helper_scaling_control
                ) if self.process_watcher else False
                await websocket.send(json.dumps({
                    "type": "GENERAL_SETTINGS_SAVED",
                    "runAtStartup": startup["enabled"],
                    "startupTaskName": startup["taskName"],
                    "trayOnly": True,
                    "synchronized": synchronized,
                    "controlSettings": self._control_settings_payload(),
                }))
                return

            if msg_type == "CHECK_GRAPHICS_RELEASES":
                provider = str(msg.get("provider") or "")
                channel = str(msg.get("channel") or "stable")
                releases = await asyncio.to_thread(self.release_manager.check, provider, channel=channel)
                await websocket.send(json.dumps({
                    "type": "GRAPHICS_RELEASES",
                    "provider": provider,
                    "releases": releases,
                }))
                return

            if msg_type == "STAGE_GRAPHICS_RELEASE":
                result = await asyncio.to_thread(
                    self.release_manager.stage_release,
                    str(msg.get("provider") or ""),
                    str(msg.get("version") or ""),
                    str(msg.get("assetName") or ""),
                    str(msg.get("operationId") or ""),
                    channel=str(msg.get("channel") or "stable"),
                )
                await websocket.send(json.dumps({"type": "GRAPHICS_RELEASE_STAGED", "package": result}))
                return

            if msg_type == "IMPORT_GRAPHICS_ASSET":
                result = await asyncio.to_thread(
                    self.asset_store.import_file,
                    str(msg.get("path") or ""),
                    expected_sha256=msg.get("sha256"),
                    allowed_suffixes=(".dll",),
                )
                await websocket.send(json.dumps({"type": "GRAPHICS_ASSET_IMPORTED", "asset": result}))
                return

            if msg_type == "IMPORT_GRAPHICS_PACKAGE":
                provider = str(msg.get("provider") or "")
                self.release_manager.registry.get(provider)
                version = str(msg.get("version") or "")
                source = str(msg.get("path") or "")
                expected = msg.get("sha256")
                source_path = Path(source).resolve(strict=True)
                metadata = {"manual_import": True, "original_path": str(source_path)}
                if zipfile.is_zipfile(source_path):
                    result = await asyncio.to_thread(
                        self.asset_store.import_release_archive,
                        source,
                        provider=provider,
                        version=version,
                        source_metadata=metadata,
                        expected_sha256=expected,
                    )
                else:
                    if source_path.suffix.casefold() not in {".dll", ".exe", ".7z"}:
                        raise ValueError("Manual packages must be ZIP, DLL, EXE, or 7Z files")
                    result = await asyncio.to_thread(
                        self.asset_store.import_release_file,
                        source,
                        provider=provider,
                        version=version,
                        source_metadata=metadata,
                        expected_sha256=expected,
                    )
                await websocket.send(json.dumps({
                    "type": "GRAPHICS_PACKAGE_IMPORTED", "package": result
                }))
                return

            if msg_type == "UPDATE_LOSSLESS_PROFILE":
                title = msg.get("title")
                values = msg.get("values")
                if not isinstance(title, str) or not isinstance(values, dict):
                    raise ValueError("title and values are required")
                values = dict(values)
                if (
                    self.config.lossless_control_configured
                    and self.config.disable_native_auto_scale
                ):
                    values["AutoScale"] = False
                was_running = bool(
                    self.process_watcher
                    and await asyncio.to_thread(
                        self.process_watcher.check_is_lossless_scaling_running
                    )
                )
                if was_running and not await asyncio.to_thread(
                    self.process_watcher.stop_lossless_scaling
                ):
                    raise RuntimeError("Lossless Scaling could not be stopped for the settings update")
                try:
                    updated = self.ls_settings.update_profile(title, values)
                finally:
                    if was_running:
                        await asyncio.to_thread(
                            self.process_watcher.launch_lossless_scaling, force=True
                        )
                await websocket.send(json.dumps({"type": "LOSSLESS_SETTINGS_UPDATED", "updated": updated}))
                return

            if msg_type == "SET_ACTIVE_PROFILE":
                profile = self.profile_manager.get_profile_by_id(msg.get("profileId", ""))
                if not profile:
                    raise ValueError("Unknown profile")
                if (
                    self.state.is_scaling_active
                    and self.state.current_active_profile
                    and self.state.current_active_profile.id != profile.id
                ):
                    stopped = await asyncio.to_thread(
                        self.automation.set_scaling,
                        False,
                        self.state.current_active_profile,
                        reason="manual_profile_switch",
                    )
                    if not stopped:
                        raise RuntimeError("Stop scaling before switching profiles")
                self.profile_manager.config.active_profile_id = profile.id
                self.profile_manager.save_config()
                await asyncio.to_thread(
                    self.automation.activate_profile,
                    profile,
                    profile.target_executable_path,
                    force=True,
                )
                await self.broadcast_full_data_update()
                return

            if msg_type == "SAVE_PROFILE":
                prof_data = msg.get("profile")
                if prof_data:
                    prof = Profile.model_validate(prof_data)
                    previous = self.profile_manager.get_profile_by_id(prof.id)
                    if previous:
                        calibration_changed = (
                            previous.target_process != prof.target_process
                            or previous.target_executable_path != prof.target_executable_path
                            or previous.rtss.limit_mode != prof.rtss.limit_mode
                            or previous.rtss.gpu_target_percent != prof.rtss.gpu_target_percent
                            or previous.rtss.minimum_framerate_limit != prof.rtss.minimum_framerate_limit
                            or previous.rtss.maximum_framerate_limit != prof.rtss.maximum_framerate_limit
                        )
                        if not calibration_changed:
                            prof.rtss.learned_framerate_limit = previous.rtss.learned_framerate_limit
                            prof.rtss.game_gpu_baseline_percent = previous.rtss.game_gpu_baseline_percent
                            prof.rtss.game_gpu_high_water_percent = previous.rtss.game_gpu_high_water_percent
                            prof.rtss.automatic_calibration_disabled = previous.rtss.automatic_calibration_disabled
                    removing_rtss = bool(previous and previous.rtss.enabled and not prof.rtss.enabled)
                    changing_rtss_target = False
                    if previous and previous.rtss.enabled and prof.rtss.enabled:
                        changing_rtss_target = (
                            self.rtss_manager._target_process(previous).casefold()
                            != self.rtss_manager._target_process(prof).casefold()
                        )
                    if (removing_rtss or changing_rtss_target) and not bool(msg.get("confirmRtssRemoval")):
                        raise ValueError(
                            "Confirm removal of the existing RTSS limiter profile before saving"
                        )
                    if previous and previous.rtss.enabled and (removing_rtss or changing_rtss_target):
                        await asyncio.to_thread(
                            self.rtss_manager.remove_profile,
                            previous,
                            self.config.rtss_install_path,
                        )
                        self.rtss_manager.clear_metadata(prof)
                        previous = None
                    if prof.rtss.enabled:
                        if not self.config.rtss_frame_limiting_enabled:
                            raise ValueError("Turn on RTSS Frame Limiting in General Settings first")
                        target_name = self.rtss_manager._target_process(prof).casefold()
                        for other in self.config.profiles:
                            if other.id == prof.id or not other.rtss.enabled:
                                continue
                            if self.rtss_manager._target_process(other).casefold() == target_name:
                                raise ValueError(
                                    f"RTSS limiting is already managed by helper profile '{other.name}' for this executable"
                                )
                        await asyncio.to_thread(
                            self.rtss_manager.apply_profile,
                            prof,
                            previous,
                            self.config.rtss_install_path,
                            framerate_limit=self._rtss_limit_for_profile(prof),
                        )
                    else:
                        self.rtss_manager.clear_metadata(prof)
                    self.profile_manager.add_or_update_profile(prof)
                    logger.info(f"Saved profile: {prof.name}")
                    if self.state.current_active_profile and self.state.current_active_profile.id == prof.id:
                        await asyncio.to_thread(
                            self.automation.activate_profile,
                            prof,
                            prof.target_executable_path,
                            force=True,
                        )
                    await self.broadcast_full_data_update()
                return

            if msg_type == "DELETE_PROFILE":
                prof_id = msg.get("profileId")
                if prof_id:
                    profile = self.profile_manager.get_profile_by_id(prof_id)
                    if profile and profile.rtss.enabled:
                        if not bool(msg.get("confirmRtssRemoval")):
                            raise ValueError(
                                "Confirm deletion of the associated RTSS limiter profile"
                            )
                        await asyncio.to_thread(
                            self.rtss_manager.remove_profile,
                            profile,
                            self.config.rtss_install_path,
                        )
                    if self.state.current_active_profile and self.state.current_active_profile.id == prof_id:
                        if self.state.is_scaling_active:
                            stopped = await asyncio.to_thread(
                                self.automation.set_scaling,
                                False,
                                self.state.current_active_profile,
                                reason="profile_deleted",
                            )
                            if not stopped:
                                raise RuntimeError("Stop scaling before deleting the active profile")
                        await asyncio.to_thread(self.automation.activate_profile, None, force=True)
                    self.profile_manager.delete_profile(prof_id)
                    logger.info(f"Deleted profile ID: {prof_id}")
                    await self.broadcast_full_data_update()
                return

        except Exception as e:
            logger.error(f"Error processing message: {e}", exc_info=True)
            try:
                await websocket.send(json.dumps({"type": "ERROR", "message": str(e)}))
            except Exception:
                pass

    async def _revert_all_changes(self) -> Dict:
        """Return LS, RTSS, and deployed add-ons to their pre-helper state."""
        was_running = bool(
            self.process_watcher
            and await asyncio.to_thread(
                self.process_watcher.check_is_lossless_scaling_running
            )
        )
        if was_running and not await asyncio.to_thread(
            self.process_watcher.stop_lossless_scaling
        ):
            raise RuntimeError("Lossless Scaling could not be stopped; no cleanup was attempted")

        failures = []
        addons_reverted = False
        xml_restored = False
        rtss_reverted = 0
        startup_task_removed = False
        try:
            try:
                await asyncio.to_thread(self.automation.revert_managed_addons)
                addons_reverted = True
            except Exception as error:
                failures.append(f"Lossless Scaling add-ons: {error}")

            try:
                xml_restored = await asyncio.to_thread(
                    self.ls_settings.restore_initial_backup
                )
            except Exception as error:
                failures.append(f"Settings.xml: {error}")

            for profile in self.config.profiles:
                managed = bool(
                    profile.rtss.managed_target_process
                    or profile.rtss.managed_profile_created
                    or profile.rtss.original_values
                )
                if managed:
                    try:
                        await asyncio.to_thread(
                            self.rtss_manager.remove_profile,
                            profile,
                            self.config.rtss_install_path,
                        )
                        rtss_reverted += 1
                    except Exception as error:
                        failures.append(f"RTSS profile {profile.name}: {error}")
                        continue
                profile.rtss = RtssLimiterConfig()

            if addons_reverted:
                for profile in self.config.profiles:
                    profile.graphics = GraphicsStackConfig()
                    profile.dll_overrides = []
                    profile.reshade = None

            try:
                startup = await asyncio.to_thread(
                    self.startup_manager.set_enabled, False
                )
                startup_task_removed = not bool(startup.get("enabled"))
            except Exception as error:
                failures.append(f"Startup task: {error}")

            # Keep the restored XML and removed RTSS profiles from being recreated.
            self.config.lossless_control_configured = False
            self.config.rtss_frame_limiting_enabled = False
            self.config.run_at_startup = not startup_task_removed
            self.config.process_lasso_performance_mode_scaling = False
            self.config.active_profile_id = None
            self.rtss_manager.configured_path = self.config.rtss_install_path
            self.profile_manager.save_config()
            if self.dynamic_limiter is not None:
                self.state.dynamic_limiter_status = {
                    "state": "inactive",
                    "message": "RTSS integration was disabled by revert all",
                }
        finally:
            if was_running:
                restarted = await asyncio.to_thread(
                    self.process_watcher.launch_lossless_scaling, force=True
                )
                if not restarted:
                    failures.append("Lossless Scaling could not be restarted")

        return {
            "ok": not failures,
            "settingsRestored": xml_restored,
            "settingsBackupFound": self.ls_settings.initial_backup_path.is_file(),
            "addonsReverted": addons_reverted,
            "rtssProfilesReverted": rtss_reverted,
            "startupTaskRemoved": startup_task_removed,
            "failures": failures,
        }

    def is_origin_allowed(self, origin: Optional[str]) -> bool:
        if origin is None:
            return True  # Native/local clients don't send an Origin header.
        for allowed in self.config.allowed_websocket_origins:
            if origin == allowed or (allowed.endswith("://") and origin.startswith(allowed)):
                return True
        if self._is_dashboard_origin(origin):
            return True
        return False

    def _is_dashboard_origin(self, origin: Optional[str]) -> bool:
        return origin in {
            f"http://127.0.0.1:{self.config.port}",
            f"http://localhost:{self.config.port}",
            f"http://[::1]:{self.config.port}",
        }

    async def process_http_request(self, path, request_headers):
        """Serve the dashboard from the WebSocket server's trusted origin."""
        if path not in ("/", "/dashboard"):
            return None
        dashboard = Path(__file__).parents[1] / "ui" / "dashboard.html"
        if not dashboard.is_file():
            return HTTPStatus.NOT_FOUND, [("Content-Type", "text/plain")], b"Dashboard not found"
        body = dashboard.read_text(encoding="utf-8").replace("__COMPANION_TOKEN__", self.dashboard_token).encode("utf-8")
        return HTTPStatus.OK, [
            ("Content-Type", "text/html; charset=utf-8"),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"),
            (
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                f"connect-src ws://127.0.0.1:{self.config.port} "
                f"ws://localhost:{self.config.port} ws://[::1]:{self.config.port}",
            ),
        ], body

    async def handle_fullscreen_event(self, data: dict) -> None:
        if not self.state.auto_scale_enabled:
            logger.info("Auto-scaling is disabled globally in companion. Skipping event.")
            return

        event_type = data.get("event")  # FULLSCREEN_ENTER or FULLSCREEN_EXIT
        domain = data.get("domain", "")
        process_name = data.get("processName", "chrome.exe")

        profile = self.profile_manager.match_target_profile(
            process_name=process_name,
            domain=domain,
        )
        foreground = self.process_watcher.get_foreground_window_info() if self.process_watcher else None
        if (
            foreground is None
            or foreground.name.casefold() != process_name.casefold()
            or not foreground.hwnd
        ):
            logger.info("Ignored browser video event because its exact browser window is not foreground")
            return
        await asyncio.to_thread(
            self.automation.activate_profile,
            profile,
            foreground.exe_path,
            target_pid=foreground.pid,
            target_hwnd=foreground.hwnd,
            suppress_auto_scale=True,
        )

        logger.info(f"Received {event_type} from {domain} (matched profile: {profile.name if profile else 'Default'})")

        if not profile:
            return

        if (
            not self.state.current_active_profile
            or self.state.current_active_profile.id != profile.id
        ):
            logger.info("Browser video event yielded to the currently scaled game profile")
            return

        if event_type == "FULLSCREEN_ENTER":
            logger.info(f"Triggering Lossless Scaling for fullscreen enter in {profile.hotkey.activation_delay_ms}ms...")
            # Run delay asynchronously without blocking the event loop
            await asyncio.sleep(profile.hotkey.activation_delay_ms / 1000.0)
            
            # Fire hardware hotkey
            await asyncio.to_thread(
                self.automation.set_scaling,
                True,
                profile,
                reason="browser_video",
                target_pid=foreground.pid,
                target_hwnd=foreground.hwnd,
            )
            await self.broadcast_state()

        elif (
            event_type == "FULLSCREEN_EXIT"
            and self.state.scaling_owner_profile_id == profile.id
            and self.state.scaling_trigger == "browser_video"
        ):
            logger.info("Browser video fullscreen exit detected. Toggling Lossless Scaling off...")
            await asyncio.sleep(0.15)
            await asyncio.to_thread(self.automation.set_scaling, False, profile, reason="fullscreen_exit")
            await self.broadcast_state()

    def schedule_fullscreen_event(self, data: dict) -> None:
        """Debounce fullscreen transitions without blocking later exit events."""
        key = f"{data.get('processName', '')}|{data.get('domain', '')}".casefold()
        previous = self.fullscreen_tasks.get(key)
        if previous and not previous.done():
            previous.cancel()

        task = asyncio.create_task(self.handle_fullscreen_event(data))
        self.fullscreen_tasks[key] = task

        def discard(completed: asyncio.Task) -> None:
            if self.fullscreen_tasks.get(key) is completed:
                self.fullscreen_tasks.pop(key, None)

        task.add_done_callback(discard)

    async def handle_manual_trigger(self, data: dict) -> None:
        profile = self.state.current_active_profile
        if not profile and self.profile_manager.config.active_profile_id:
            profile = self.profile_manager.get_profile_by_id(self.profile_manager.config.active_profile_id)
        logger.info("Manual scale trigger invoked using profile: %s", profile.name if profile else "global")
        await asyncio.to_thread(self.automation.toggle_scaling, profile, reason="manual")
        await self.broadcast_state()

    async def send_full_data_update(self, websocket: WebSocketServerProtocol, visible_windows_only: bool = True) -> None:
        procs = self.process_watcher.list_running_executables(visible_windows_only=visible_windows_only) if self.process_watcher else []
        payload = json.dumps({
            "type": "FULL_DATA_UPDATE",
            "processes": procs,
            "profiles": [p.model_dump() for p in self.profile_manager.config.profiles],
            "activeProfileId": self.profile_manager.config.active_profile_id,
            "activeProfile": self.state.current_active_profile.model_dump() if self.state.current_active_profile else None,
            "isScalingActive": self.state.is_scaling_active,
            "scalingTarget": self.state.current_scaled_target,
            "controlSettings": self._control_settings_payload(),
            "dynamicLimiter": self.state.dynamic_limiter_status,
            "scalingControl": self.state.scaling_control_status,
        })
        await websocket.send(payload)

    async def broadcast_full_data_update(self, visible_windows_only: bool = True) -> None:
        if not self.clients:
            return
        procs = self.process_watcher.list_running_executables(visible_windows_only=visible_windows_only) if self.process_watcher else []
        payload = json.dumps({
            "type": "FULL_DATA_UPDATE",
            "processes": procs,
            "profiles": [p.model_dump() for p in self.profile_manager.config.profiles],
            "activeProfileId": self.profile_manager.config.active_profile_id,
            "activeProfile": self.state.current_active_profile.model_dump() if self.state.current_active_profile else None,
            "isScalingActive": self.state.is_scaling_active,
            "scalingTarget": self.state.current_scaled_target,
            "controlSettings": self._control_settings_payload(),
            "dynamicLimiter": self.state.dynamic_limiter_status,
            "scalingControl": self.state.scaling_control_status,
        })
        await asyncio.gather(*[client.send(payload) for client in self.clients], return_exceptions=True)

    def _control_settings_payload(self) -> Dict:
        detected_process_lasso_log = ProcessLassoLogTailer.resolve_path(None)
        return {
            "mode": self.config.hotkey_sync_mode,
            "disableNativeAutoScale": self.config.disable_native_auto_scale,
            "minimizeOtherWindowsOnScale": self.config.minimize_other_windows_on_scale,
            "processLassoPerformanceModeScaling": self.config.process_lasso_performance_mode_scaling,
            "processLassoLogPath": self.config.process_lasso_log_path or "",
            "processLassoDetectedLogPath": (
                str(detected_process_lasso_log) if detected_process_lasso_log else ""
            ),
            "rtssFrameLimitingEnabled": self.config.rtss_frame_limiting_enabled,
            "rtssInstallPath": self.config.rtss_install_path or "",
            "rtssStatus": self.rtss_manager.status(self.config.rtss_install_path),
            "rtssDefaultLimitMode": self.config.rtss_default_limit_mode,
            "rtssDefaultGpuTargetPercent": self.config.rtss_default_gpu_target_percent,
            "defaultProfileAutoScale": self.config.default_profile_auto_scale,
            "losslessControlConfigured": self.config.lossless_control_configured,
            "hotkey": self.config.global_hotkey.model_dump(),
        }

    def _rtss_limit_for_profile(
        self, profile: Profile, default_mode: Optional[str] = None
    ) -> int:
        mode = (
            default_mode or self.config.rtss_default_limit_mode
            if profile.rtss.limit_mode == "inherit"
            else profile.rtss.limit_mode
        )
        if mode == "static":
            return profile.rtss.framerate_limit
        return profile.rtss.learned_framerate_limit or profile.rtss.maximum_framerate_limit

    async def broadcast_state(self) -> None:
        if not self.clients:
            return
        payload = json.dumps({
            "type": "STATE_UPDATE",
            "isScalingActive": self.state.is_scaling_active,
            "activeProfile": self.state.current_active_profile.model_dump() if self.state.current_active_profile else None,
            "scalingTarget": self.state.current_scaled_target,
            "dynamicLimiter": self.state.dynamic_limiter_status,
            "scalingControl": self.state.scaling_control_status,
        })
        await asyncio.gather(*[client.send(payload) for client in self.clients], return_exceptions=True)
