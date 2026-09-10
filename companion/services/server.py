"""
Async WebSocket server for communication with Chrome Extension and UI clients.
"""

import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import sys
import time
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
    ManagedReshadeProfile,
    Profile,
    RtssLimiterConfig,
)
from ..core.profile_manager import ProfileManager
from ..core.state import AppState
from .process_watcher import ProcessWatcher
from .automation import AutomationController
from .ls_settings import LosslessSettingsXml
from .asset_store import AssetStore, UnsafeAssetError
from .graphics_source_detector import GraphicsSourceDetector
from .reshade_profiles import ReshadeProfileService
from .release_providers import ReleaseManager
from .startup_manager import StartupTaskManager
from .process_lasso_monitor import ProcessLassoLogTailer
from .rtss_manager import RtssProfileManager
from .dynamic_limiter import DynamicLimiterController
from ..ui.icons import lightning_icon_png
from ..ui.path_picker import choose_dlssnr_runtime, choose_general_setting_path
from scripts.benchmark_lossless_scaling import main as run_performance_benchmark

logger = logging.getLogger("LSCompanion.Server")


class CompanionWebSocketServer:
    PRESENTMON_STARTUP_DELAY_SECONDS = 1.0

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
        self.addon_update_task: Optional[asyncio.Task] = None
        self.presentmon_detection_task: Optional[asyncio.Task] = None
        self._background_thread_tasks: Set[asyncio.Task] = set()
        self.addon_update_lock = asyncio.Lock()
        self.addon_update_status: Optional[Dict] = None
        self.benchmark_task: Optional[asyncio.Task] = None
        self._benchmark_previous_profile: Optional[Profile] = None
        self._detected_presentmon_path: Optional[Path] = None
        self._detected_presentmon_sha256: Optional[str] = None
        self.reshade_profiles = ReshadeProfileService(profile_manager)

    async def start(self) -> None:
        logger.info(f"Starting WebSocket server on {self.config.host}:{self.config.port}...")
        self.server = await serve(
            self.handle_client,
            self.config.host,
            self.config.port,
            process_request=self.process_http_request,
        )
        self.addon_update_task = asyncio.create_task(self._addon_update_loop())
        self.presentmon_detection_task = asyncio.create_task(self._detect_presentmon_on_startup())
        logger.info(f"WebSocket server listening on ws://{self.config.host}:{self.config.port}/ws")

    async def stop(self) -> None:
        if self.addon_update_task:
            self.addon_update_task.cancel()
        if self.presentmon_detection_task:
            self.presentmon_detection_task.cancel()
        for task in self.fullscreen_tasks.values():
            task.cancel()
        self.fullscreen_tasks.clear()
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            logger.info("WebSocket server stopped.")
        if self.addon_update_task:
            with contextlib.suppress(asyncio.CancelledError):
                await self.addon_update_task
            self.addon_update_task = None
        if self.presentmon_detection_task:
            with contextlib.suppress(asyncio.CancelledError):
                await self.presentmon_detection_task
            self.presentmon_detection_task = None
        # Cancelling a coroutine that awaits asyncio.to_thread() does not stop
        # its executor worker. Drain server-owned workers before callers tear
        # down the asset store they may still be reading or writing.
        while self._background_thread_tasks:
            pending = tuple(self._background_thread_tasks)
            await asyncio.gather(*pending, return_exceptions=True)

    async def _run_background_thread(self, function, /, *args, **kwargs):
        """Run lifecycle-owned blocking work and keep it drainable on shutdown."""
        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        self._background_thread_tasks.add(task)
        task.add_done_callback(self._background_thread_tasks.discard)
        return await asyncio.shield(task)

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
                "INSTALL_LATEST_GRAPHICS_ADDON",
                "DOWNLOAD_BENCHMARK_TOOL",
                "START_PERFORMANCE_BENCHMARK",
                "SELECT_DLSSNR_RUNTIME",
                "SAVE_CONTROL_SETTINGS",
                "SAVE_GENERAL_SETTINGS",
                "RESET_RTSS_CALIBRATION",
                "START_RTSS_MANUAL_CALIBRATION",
                "REVERT_ALL_CHANGES",
                "OPEN_CONFIG_FOLDER",
                "BROWSE_GENERAL_PATH",
                "GET_RESHADE_PROFILES",
                "SAVE_RESHADE_PROFILE",
                "DELETE_RESHADE_PROFILE",
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

            if msg_type == "OPEN_CONFIG_FOLDER":
                config_path = str(self.profile_manager.config_dir.resolve())
                await asyncio.to_thread(os.startfile, config_path)
                await websocket.send(json.dumps({
                    "type": "CONFIG_FOLDER_OPENED",
                    "path": config_path,
                }))
                return

            if msg_type == "BROWSE_GENERAL_PATH":
                kind = str(msg.get("kind") or "")
                if kind == "process_lasso_log":
                    detected = ProcessLassoLogTailer.resolve_path(None)
                    current_path = self.config.process_lasso_log_path or (
                        str(detected) if detected else None
                    )
                elif kind == "rtss_directory":
                    status = self.rtss_manager.status(self.config.rtss_install_path)
                    current_path = self.config.rtss_install_path or status.get("path")
                else:
                    raise ValueError("Unsupported settings path picker")
                selected = await asyncio.to_thread(
                    choose_general_setting_path, kind, current_path
                )
                await websocket.send(json.dumps({
                    "type": "GENERAL_PATH_SELECTED",
                    "kind": kind,
                    "path": selected,
                }))
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
                dlssnr_runtimes = [
                    asset for asset in self.asset_store.list_imports()
                    if str(asset.get("filename") or "").casefold() == "nvngx_dlssnr.dll"
                    and asset.get("architecture") == "x64"
                ]
                external_sources = await asyncio.to_thread(GraphicsSourceDetector.status)
                await websocket.send(json.dumps({
                    "type": "GRAPHICS_STATUS",
                    "packages": self.asset_store.list_packages(),
                    "dlssnrRuntimes": dlssnr_runtimes,
                    "externalSources": external_sources,
                }))
                return

            if msg_type == "GET_BENCHMARK_STATUS":
                await websocket.send(json.dumps(self._benchmark_status_payload()))
                return

            if msg_type == "GET_RESHADE_PROFILES":
                payload = await asyncio.to_thread(self.reshade_profiles.payload)
                await websocket.send(json.dumps({"type": "RESHADE_PROFILES", **payload}))
                return

            if msg_type == "SAVE_RESHADE_PROFILE":
                profile = ManagedReshadeProfile.model_validate(msg.get("profile") or {})
                result = await asyncio.to_thread(self.reshade_profiles.save, profile)
                await websocket.send(json.dumps({"type": "RESHADE_PROFILE_SAVED", "profile": result}))
                return

            if msg_type == "DELETE_RESHADE_PROFILE":
                deleted = await asyncio.to_thread(
                    self.reshade_profiles.delete, str(msg.get("profileId") or "")
                )
                await websocket.send(json.dumps({
                    "type": "RESHADE_PROFILE_DELETED",
                    "profileId": str(msg.get("profileId") or ""),
                    "deleted": deleted,
                }))
                return

            if msg_type == "GET_ADDON_UPDATES":
                if self.addon_update_status is None:
                    await self._refresh_addon_updates(force=False)
                else:
                    await websocket.send(json.dumps(self.addon_update_status))
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
                smart_auto_scale_enabled = bool(
                    msg.get("smartAutoScaleEnabled", msg.get("disableNativeAutoScale", True))
                )
                self._apply_hotkey_override_settings(msg)
                self.config.lossless_control_configured = True
                self.config.disable_native_auto_scale = smart_auto_scale_enabled
                self.state.auto_scale_enabled = smart_auto_scale_enabled
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
                run_at_startup = bool(msg.get("runAtStartup", False))
                smart_auto_scale_enabled = bool(msg.get("smartAutoScaleEnabled", True))
                gpu_inventory = self.automation.gpu_router.detect()
                detected_gpu_ids = {
                    str(item.get("deviceId") or "").casefold()
                    for item in gpu_inventory.get("gpus", [])
                }
                requested_gpu_id = str(
                    msg.get("preferredScalingGpuDeviceId") or ""
                ).strip() or None
                if requested_gpu_id and requested_gpu_id.casefold() not in detected_gpu_ids:
                    raise ValueError("Select a currently detected GPU")
                requested_auto_route = bool(msg.get("autoRouteGpuToDisplay", False))
                requested_rtx_hdr = bool(msg.get("nvidiaRtxHdrEnabled", False))
                if requested_rtx_hdr and not gpu_inventory.get("hasNvidia"):
                    raise ValueError("NVIDIA RTX HDR requires a detected NVIDIA GPU")
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
                if run_at_startup != self.config.run_at_startup:
                    startup = await asyncio.to_thread(
                        self.startup_manager.set_enabled, run_at_startup
                    )
                else:
                    startup = {
                        "enabled": run_at_startup,
                        "taskName": self.startup_manager.TASK_NAME,
                    }

                if requested_rtx_hdr != self.config.nvidia_rtx_hdr_enabled:
                    configured_exe = self.config.lossless_scaling_exe_path
                    if not configured_exe:
                        raise ValueError("Configure LosslessScaling.exe before changing RTX HDR")
                    await asyncio.to_thread(
                        self.automation.nvidia_profile_manager.set_lossless_scaling_rtx_hdr,
                        configured_exe,
                        requested_rtx_hdr,
                    )
                    self.asset_store.audit(
                        "nvidia_application_profile_updated",
                        executable=str(Path(configured_exe).resolve()),
                        setting="rtx_hdr",
                        enabled=requested_rtx_hdr,
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

                self._apply_hotkey_override_settings(msg)
                self.config.run_at_startup = run_at_startup
                self.config.preferred_scaling_gpu_device_id = requested_gpu_id
                self.config.auto_route_gpu_to_display = requested_auto_route
                self.config.nvidia_rtx_hdr_enabled = requested_rtx_hdr
                self.config.default_profile_auto_scale = requested_default_auto_scale
                self.config.lossless_control_configured = True
                self.config.disable_native_auto_scale = smart_auto_scale_enabled
                self.state.auto_scale_enabled = smart_auto_scale_enabled
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

            if msg_type == "INSTALL_LATEST_GRAPHICS_ADDON":
                provider = str(msg.get("provider") or "")
                if provider not in {"lossless-proxy", "lsp-neural-render"}:
                    raise ValueError("This add-on cannot be installed automatically")
                releases = await asyncio.to_thread(
                    self.release_manager.check, provider, channel="stable", force=True
                )
                release = next((item for item in releases if item.get("assets")), None)
                if release is None:
                    raise ValueError(f"No downloadable stable release is available for {provider}")
                assets = list(release.get("assets") or [])

                def asset_priority(asset: Dict) -> tuple:
                    name = str(asset.get("name") or "").casefold()
                    unwanted = any(word in name for word in ("source", "debug", "symbol", "pdb"))
                    if provider == "lossless-proxy":
                        preferred = 0 if name.endswith(".zip") else 1
                    elif provider == "lsp-neural-render":
                        preferred = 0 if name.endswith(".zip") else 1
                    else:
                        preferred = 0 if "specialk" in name and name.endswith((".7z", ".zip")) else 1
                    return (unwanted, preferred, name)

                asset = min(assets, key=asset_priority)
                result = await asyncio.to_thread(
                    self.release_manager.stage_release,
                    provider,
                    str(release.get("version") or ""),
                    str(asset.get("name") or ""),
                    str(msg.get("operationId") or ""),
                    channel="stable",
                )
                await websocket.send(json.dumps({"type": "GRAPHICS_RELEASE_STAGED", "package": result}))
                return

            if msg_type == "DOWNLOAD_BENCHMARK_TOOL":
                provider = str(msg.get("provider") or "")
                if provider != "presentmon":
                    raise ValueError("Unsupported benchmark tool")
                releases = await asyncio.to_thread(
                    self.release_manager.check, provider, channel="stable", force=True
                )
                release = next((item for item in releases if item.get("assets")), None)
                if release is None:
                    raise ValueError(f"No downloadable stable release is available for {provider}")
                asset = release["assets"][0]
                if not str(asset.get("digest") or "").startswith("sha256:"):
                    raise UnsafeAssetError(
                        "GitHub has not published a SHA-256 digest for this release asset yet"
                    )
                await asyncio.to_thread(
                    self.release_manager.stage_release,
                    provider,
                    str(release.get("version") or ""),
                    str(asset.get("name") or ""),
                    str(msg.get("operationId") or ""),
                    channel="stable",
                )
                await websocket.send(json.dumps({
                    "type": "BENCHMARK_TOOL_STAGED",
                    "provider": provider,
                }))
                await websocket.send(json.dumps(self._benchmark_status_payload()))
                return

            if msg_type == "START_PERFORMANCE_BENCHMARK":
                status = self._benchmark_status_payload()
                if not status["ready"]:
                    raise RuntimeError("PresentMon or the embedded benchmark workload is unavailable")
                if self.benchmark_task and not self.benchmark_task.done():
                    raise RuntimeError("A performance benchmark is already running")
                if self.state.is_scaling_active:
                    raise RuntimeError("Stop the current scaling session before running the benchmark")
                default_profile = next(
                    (profile for profile in self.config.profiles if profile.is_default),
                    None,
                )
                if default_profile is None:
                    raise RuntimeError("The Lossless Scaling default profile is unavailable")
                workload = self._embedded_benchmark_executable()
                presentmon = self._presentmon_executable()
                command = self._benchmark_launch_command(workload, presentmon)
                self._benchmark_previous_profile = self.state.current_active_profile
                self.state.benchmark_mode_active = True
                try:
                    await asyncio.to_thread(
                        self.automation.activate_profile,
                        default_profile,
                        force=True,
                        suppress_auto_scale=True,
                    )
                    if self.state.current_active_profile is not default_profile:
                        raise RuntimeError("The default profile could not be activated")
                    self.benchmark_task = asyncio.create_task(
                        self._run_benchmark(command)
                    )
                except Exception:
                    await asyncio.to_thread(
                        self.automation.activate_profile,
                        self._benchmark_previous_profile,
                        force=True,
                        suppress_auto_scale=True,
                    )
                    self._benchmark_previous_profile = None
                    self.state.benchmark_mode_active = False
                    raise
                await websocket.send(json.dumps({
                    "type": "BENCHMARK_STARTED",
                }))
                return

            if msg_type == "SELECT_DLSSNR_RUNTIME":
                selected = await asyncio.to_thread(choose_dlssnr_runtime)
                if not selected:
                    await websocket.send(json.dumps({"type": "DLSSNR_RUNTIME_SELECTION_CANCELLED"}))
                    return
                source = Path(selected).resolve(strict=True)
                if source.name.casefold() != "nvngx_dlssnr.dll":
                    raise UnsafeAssetError("Select a file named nvngx_dlssnr.dll")
                if self.asset_store.pe_architecture(source) != "x64":
                    raise UnsafeAssetError("The DLSS neural-rendering runtime must be a valid x64 DLL")
                result = await asyncio.to_thread(
                    self.asset_store.import_file, str(source), allowed_suffixes=(".dll",)
                )
                await websocket.send(json.dumps({"type": "DLSSNR_RUNTIME_SELECTED", "asset": result}))
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
                    prof_data = dict(prof_data)
                    requested_id = str(prof_data.get("id") or "")
                    existing_requested = (
                        self.profile_manager.get_profile_by_id(requested_id)
                        if requested_id else None
                    )
                    if existing_requested is None:
                        draft = self.profile_manager.new_profile_from_default(
                            str(prof_data.get("name") or "New Profile"),
                            target_process=prof_data.get("target_process"),
                            target_executable_path=prof_data.get("target_executable_path"),
                            target_domain=prof_data.get("target_domain"),
                        ).model_dump()

                        def merge_template(base, override):
                            merged = dict(base)
                            for key, value in override.items():
                                if isinstance(value, dict) and isinstance(merged.get(key), dict):
                                    merged[key] = merge_template(merged[key], value)
                                else:
                                    merged[key] = value
                            return merged

                        if not requested_id:
                            prof_data.pop("id", None)
                        prof_data = merge_template(draft, prof_data)
                        prof_data["is_default"] = False
                    prof = Profile.model_validate(prof_data)
                    if (
                        prof.graphics.special_k.experimental_smooth_motion
                        and not self.automation.gpu_router.detect().get("hasNvidia")
                    ):
                        raise ValueError("NVIDIA Smooth Motion requires a detected NVIDIA GPU")
                    if prof.reshade and prof.reshade.enabled and prof.reshade.managed_profile_id:
                        if not any(
                            item.id == prof.reshade.managed_profile_id
                            for item in self.config.reshade_profiles
                        ):
                            raise ValueError("Select an existing managed ReShade profile")
                    previous = self.profile_manager.get_profile_by_id(prof.id)
                    if previous and previous.is_default:
                        prof.is_default = True
                    elif prof.is_default:
                        raise ValueError("Only the native Lossless Scaling profile may be the default")
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
                    selected_profile = self.profile_manager.get_profile_by_id(prof_id)
                    if selected_profile and selected_profile.is_default:
                        raise ValueError("The Lossless Scaling default profile cannot be deleted")
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
                self.config.nvidia_rtx_hdr_enabled = False
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
        request_path = urlsplit(path).path
        if request_path == "/favicon.png":
            body = lightning_icon_png()
            return HTTPStatus.OK, [
                ("Content-Type", "image/png"),
                ("Content-Length", str(len(body))),
                ("Cache-Control", "public, max-age=86400"),
                ("X-Content-Type-Options", "nosniff"),
            ], body
        if request_path not in ("/", "/dashboard"):
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
                "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
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

    @staticmethod
    def _same_addon_version(left: str, right: str) -> bool:
        def normalize(value: str):
            text = str(value or "").strip().casefold()
            numbers = tuple(int(part) for part in re.findall(r"\d+", text))
            return numbers or text.removeprefix("v")

        return bool(normalize(left)) and normalize(left) == normalize(right)

    async def _refresh_addon_updates(self, *, force: bool) -> Dict:
        async with self.addon_update_lock:
            providers = ("lossless-proxy", "lsp-neural-render", "reshade", "special-k")

            async def check(provider: str):
                try:
                    releases = await self._run_background_thread(
                        self.release_manager.check,
                        provider,
                        channel="stable",
                        force=force,
                    )
                    return provider, (releases[0] if releases else None)
                except Exception:
                    logger.warning("Daily add-on update check failed for %s", provider, exc_info=True)
                    return provider, None

            checked, sources = await asyncio.gather(
                asyncio.gather(*(check(provider) for provider in providers)),
                self._run_background_thread(GraphicsSourceDetector.status),
            )
            latest = {provider: release for provider, release in checked if release}
            packages = self.asset_store.list_packages()
            installed = {
                provider: [str(item.get("version") or "") for item in packages if item.get("provider") == provider]
                for provider in providers
            }
            source_versions = {
                "reshade": str(sources.get("reshade", {}).get("version") or ""),
                "special-k": str(sources.get("special-k", {}).get("version") or ""),
            }
            updates = []
            labels = {
                "lossless-proxy": "LosslessProxy",
                "lsp-neural-render": "LSP-NeuralRender",
                "reshade": "ReShade Full Add-On",
                "special-k": "Special K",
            }
            for provider in providers:
                release = latest.get(provider)
                if not release:
                    continue
                latest_version = str(release.get("version") or "")
                current_versions = installed[provider]
                if provider in source_versions and source_versions[provider]:
                    current_versions.append(source_versions[provider])
                if not current_versions or any(
                    self._same_addon_version(current, latest_version) for current in current_versions
                ):
                    continue
                updates.append({
                    "provider": provider,
                    "name": labels[provider],
                    "currentVersion": current_versions[-1],
                    "latestVersion": latest_version,
                })

            self.addon_update_status = {
                "type": "ADDON_UPDATES",
                "checkedAt": int(time.time()),
                "updates": updates,
                "latestVersions": {
                    provider: str(release.get("version") or "")
                    for provider, release in latest.items()
                },
            }
            if self.clients:
                payload = json.dumps(self.addon_update_status)
                await asyncio.gather(
                    *(client.send(payload) for client in list(self.clients)),
                    return_exceptions=True,
                )
            return self.addon_update_status

    async def _addon_update_loop(self) -> None:
        await asyncio.sleep(30)
        force = False
        while True:
            try:
                await self._refresh_addon_updates(force=force)
            except Exception:
                logger.warning("Daily add-on update check failed", exc_info=True)
            force = True
            await asyncio.sleep(24 * 60 * 60)

    def _verified_benchmark_executable(self, provider: str) -> Optional[Path]:
        """Return a staged x64 tool only while its immutable package still verifies."""
        expected_names = {
            "presentmon": lambda name: (
                name.casefold().startswith("presentmon-")
                and name.casefold().endswith("-x64.exe")
            ),
        }
        matches_name = expected_names.get(provider)
        if matches_name is None:
            return None
        for package in reversed(self.asset_store.list_packages()):
            if package.get("provider") != provider:
                continue
            verification = (package.get("source") or {}).get("verification") or {}
            if not verification.get("publisher_digest_verified"):
                continue
            try:
                payload = Path(str(package.get("payload_path") or "")).resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            for item in package.get("files") or []:
                relative = str(item.get("relative_path") or "")
                try:
                    candidate = (payload / relative).resolve(strict=True)
                except (OSError, RuntimeError):
                    continue
                if candidate != payload and payload not in candidate.parents:
                    continue
                digest = self.asset_store.sha256(candidate) if candidate.is_file() else ""
                expected_hashes = {
                    str(item.get("sha256") or "").casefold(),
                    str(package.get("archive_sha256") or "").casefold(),
                    str(verification.get("calculated_sha256") or "").casefold(),
                    str(verification.get("publisher_sha256") or "").casefold(),
                }
                if (
                    matches_name(candidate.name)
                    and item.get("architecture") == "x64"
                    and digest
                    and expected_hashes == {digest.casefold()}
                ):
                    return candidate
        return None

    def _presentmon_executable(self) -> Optional[Path]:
        managed = self._verified_benchmark_executable("presentmon")
        if managed:
            return managed
        candidate = self._detected_presentmon_path
        expected = self._detected_presentmon_sha256
        if (
            candidate
            and expected
            and candidate.is_file()
            and self.asset_store.pe_architecture(candidate) == "x64"
            and self.asset_store.sha256(candidate).casefold() == expected.casefold()
        ):
            return candidate
        return None

    def _discover_official_presentmon(self) -> Optional[tuple[Path, str]]:
        if self._verified_benchmark_executable("presentmon"):
            return None
        releases = self.release_manager.check("presentmon", channel="stable")
        official_hashes = {
            str(asset.get("name") or "").casefold(): str(asset.get("digest") or "").removeprefix("sha256:")
            for release in releases
            for asset in release.get("assets") or []
            if str(asset.get("digest") or "").startswith("sha256:")
        }
        roots = [
            Path.home() / "Downloads",
            Path(sys.executable).resolve().parent,
        ]
        for variable in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
            value = os.environ.get(variable)
            if value:
                roots.extend((Path(value) / "PresentMon", Path(value) / "Intel" / "PresentMon"))
        roots.extend(Path(entry) for entry in os.environ.get("PATH", "").split(os.pathsep) if entry)
        seen = set()
        for root in roots:
            try:
                resolved_root = root.resolve()
            except OSError:
                continue
            root_key = os.path.normcase(str(resolved_root))
            if root_key in seen or not resolved_root.is_dir():
                continue
            seen.add(root_key)
            try:
                candidates = resolved_root.glob("PresentMon-*-x64.exe")
                for candidate in candidates:
                    expected = official_hashes.get(candidate.name.casefold())
                    if (
                        expected
                        and candidate.is_file()
                        and self.asset_store.pe_architecture(candidate) == "x64"
                        and self.asset_store.sha256(candidate).casefold() == expected.casefold()
                    ):
                        return candidate.resolve(), expected
            except OSError:
                continue
        return None

    async def _detect_presentmon_on_startup(self) -> None:
        try:
            # Let short-lived server instances shut down without starting a
            # network/cache worker that they will immediately need to drain.
            await asyncio.sleep(self.PRESENTMON_STARTUP_DELAY_SECONDS)
            detected = await self._run_background_thread(self._discover_official_presentmon)
            if detected:
                self._detected_presentmon_path, self._detected_presentmon_sha256 = detected
                logger.info("Detected verified PresentMon at %s", self._detected_presentmon_path)
            if self.clients:
                encoded = json.dumps(self._benchmark_status_payload())
                await asyncio.gather(
                    *(client.send(encoded) for client in self.clients),
                    return_exceptions=True,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("Startup PresentMon detection did not find a verified binary", exc_info=True)

    def _embedded_benchmark_executable(self) -> Optional[Path]:
        candidates = []
        if getattr(sys, "frozen", False):
            candidates.append(Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "tools" / "LSBenchmark.exe")
        candidates.append(Path(__file__).resolve().parents[2] / "build" / "native" / "LSBenchmark.exe")
        for candidate in candidates:
            if candidate.is_file() and self.asset_store.pe_architecture(candidate) == "x64":
                return candidate.resolve()
        return None

    async def _run_benchmark(self, arguments: list[str]) -> None:
        return_code = 2
        error_message = None
        try:
            return_code = await asyncio.to_thread(run_performance_benchmark, arguments)
        except Exception as error:
            error_message = str(error)
            logger.exception("Performance benchmark runner failed")
        finally:
            try:
                await asyncio.to_thread(
                    self.automation.activate_profile,
                    self._benchmark_previous_profile,
                    force=True,
                    suppress_auto_scale=True,
                )
            finally:
                self._benchmark_previous_profile = None
                self.state.benchmark_mode_active = False
        if self.process_watcher:
            self.process_watcher.invalidate_foreground_cache()
        self.benchmark_task = None
        payload = self._benchmark_status_payload()
        payload["lastExitCode"] = return_code
        payload["lastError"] = error_message
        if self.clients:
            encoded = json.dumps(payload)
            await asyncio.gather(
                *(client.send(encoded) for client in self.clients),
                return_exceptions=True,
            )

    def _benchmark_launch_command(self, workload: Path, presentmon: Path) -> list[str]:
        return [
            "--benchmark-exe", str(workload),
            "--presentmon-exe", str(presentmon),
            "--use-default-profile",
            "--yes",
        ]

    def _benchmark_status_payload(self) -> Dict:
        workload = self._embedded_benchmark_executable()
        managed_presentmon = self._verified_benchmark_executable("presentmon")
        presentmon = managed_presentmon or self._presentmon_executable()
        running = bool(self.benchmark_task and not self.benchmark_task.done())
        return {
            "type": "BENCHMARK_STATUS",
            "ready": bool(workload and presentmon),
            "running": running,
            "tools": {
                "workload": {"ready": bool(workload)},
                "presentmon": {
                    "ready": bool(presentmon),
                    "source": "managed" if managed_presentmon else "detected" if presentmon else None,
                },
            },
        }

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
        selected_process_lasso_log = ProcessLassoLogTailer.resolve_path(
            self.config.process_lasso_log_path
        )
        process_lasso_detected = bool(
            selected_process_lasso_log and selected_process_lasso_log.is_file()
        )
        gpu_inventory = self.automation.gpu_router.detect()
        displayed_override_hotkey = self.config.override_hotkey
        if not self.config.override_lossless_hotkey:
            displayed_override_hotkey = (
                self.ls_settings.read_hotkey() or self.config.global_hotkey
            )
        return {
            "smartAutoScaleEnabled": self.config.disable_native_auto_scale,
            "minimizeOtherWindowsOnScale": self.config.minimize_other_windows_on_scale,
            "processLassoPerformanceModeScaling": self.config.process_lasso_performance_mode_scaling,
            "processLassoLogPath": self.config.process_lasso_log_path or "",
            "processLassoDetectedLogPath": (
                str(selected_process_lasso_log) if process_lasso_detected else ""
            ),
            "processLassoDetected": process_lasso_detected,
            "rtssFrameLimitingEnabled": self.config.rtss_frame_limiting_enabled,
            "rtssInstallPath": self.config.rtss_install_path or "",
            "rtssStatus": self.rtss_manager.status(self.config.rtss_install_path),
            "rtssDefaultLimitMode": self.config.rtss_default_limit_mode,
            "rtssDefaultGpuTargetPercent": self.config.rtss_default_gpu_target_percent,
            "defaultProfileAutoScale": self.config.default_profile_auto_scale,
            "gpuInventory": gpu_inventory,
            "hasNvidiaGpu": bool(gpu_inventory.get("hasNvidia")),
            "preferredScalingGpuDeviceId": self.config.preferred_scaling_gpu_device_id or "",
            "autoRouteGpuToDisplay": self.config.auto_route_gpu_to_display,
            "nvidiaRtxHdrEnabled": self.config.nvidia_rtx_hdr_enabled,
            "losslessControlConfigured": self.config.lossless_control_configured,
            "hotkey": self.config.global_hotkey.model_dump(),
            "overrideLosslessHotkey": self.config.override_lossless_hotkey,
            "overrideHotkey": (
                displayed_override_hotkey.model_dump()
            ),
        }

    def _apply_hotkey_override_settings(self, message: Dict) -> None:
        """Apply proxy-hotkey state while preserving enough state to restore LS."""
        was_enabled = self.config.override_lossless_hotkey
        enabled = bool(message.get("overrideLosslessHotkey", was_enabled))
        current = self.config.override_hotkey
        payload = message.get("overrideHotkey") or {}
        requested = HotkeyConfig(
            modifiers=payload.get("modifiers", current.modifiers),
            key=payload.get("key", current.key),
            hold_delay_ms=current.hold_delay_ms,
            activation_delay_ms=current.activation_delay_ms,
        )
        if not requested.modifiers and requested.key == "f24":
            raise ValueError("F24 without modifiers is reserved as the hidden Lossless Scaling hotkey")
        self.config.override_hotkey = requested
        self.config.override_lossless_hotkey = enabled
        if enabled:
            self.config.global_hotkey = HotkeyConfig(
                modifiers=[],
                key="f24",
                hold_delay_ms=self.config.global_hotkey.hold_delay_ms,
                activation_delay_ms=self.config.global_hotkey.activation_delay_ms,
            )
            self.config.hotkey_sync_mode = "helper_controls_lossless"
        elif was_enabled:
            self.config.global_hotkey = requested.model_copy()
            self.config.hotkey_sync_mode = "helper_controls_lossless"

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
