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
import threading
import time
from http import HTTPStatus
from pathlib import Path
from typing import Callable, Dict, Set, Optional, TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit, quote
from websockets.legacy.server import WebSocketServerProtocol, serve
from websockets.exceptions import ConnectionClosed
from pydantic import ValidationError

from ..core.models import (
    AppConfig,
    ReshadeSdrWhiteSettings,
    BrowserFullscreenEvent,
    GraphicsStackConfig,
    HotkeyConfig,
    ManagedReshadeProfile,
    Profile,
    RtssLimiterConfig,
)
from ..core.profile_manager import GAME_DEFAULT_PROFILE_NAME, ProfileManager
from ..core.state import AppState
from ..core.config_validation import validation_issues
from .process_watcher import ProcessWatcher
from .automation import AutomationController
from .overlay_indicator import production_indicator
from .ls_settings import LosslessSettingsXml
from .asset_store import AssetStore, UnsafeAssetError
from .graphics_source_detector import GraphicsSourceDetector
from .graphics_source_importer import GraphicsSourceImporter
from .reshade_profiles import ReshadeProfileService
from .release_providers import ReleaseManager
from .startup_manager import StartupTaskManager
from .process_lasso_monitor import ProcessLassoLogTailer
from .rtss_manager import RtssProfileManager
from .gpu_selections import match_device
from .dynamic_limiter import DynamicLimiterController
from .diagnostic_runner import DiagnosticRunner
from .input_simulator import InputSimulator
from .runtime_logging import runtime_log_buffer
from .self_update import CompanionUpdateService
from .automatic_updates import AutomaticUpdates, newer_addon_version
from .addon_host import runtime_status as addon_runtime_status, addon_version_key
from .gpu_index_refresh import IdleGpuIndexRefresh
from .profile_icons import ProfileIconService
from .reshade_overlay import ReShadeOverlayController
from .overlay_shortcuts import validate_overlay_hotkeys, parse_shortcut, reshade_key_data
from .live_performance import LivePerformanceGraph
from ..ui.icons import lightning_icon_png
from ..ui.path_picker import choose_dlssnr_runtime, choose_general_setting_path
from ..ui.dashboard_window import dashboard_css_rect_to_client_pixels, open_dashboard_window
from ..version import current_version
from scripts.benchmark_lossless_scaling import main as run_performance_benchmark

logger = logging.getLogger("LSCompanion.Server")

if TYPE_CHECKING:
    from .hotkey_listener import GlobalHotkeyListener


class CompanionWebSocketServer:
    PRESENTMON_STARTUP_DELAY_SECONDS = 1.0
    COMPONENT_ACTIONS = {
        "INSTALL_LATEST_GRAPHICS_ADDON", "STAGE_GRAPHICS_RELEASE", "IMPORT_GRAPHICS_SOURCE",
        "DOWNLOAD_COMPANION_UPDATE", "APPLY_COMPANION_UPDATE", "INSTALL_COMPANION_UPDATE",
        "RETRY_DEPLOYMENT_RECOVERY",
    }

    def _lossless_scaling_found(self) -> bool:
        value = self.config.lossless_scaling_exe_path
        path = Path(value) if value else None
        return bool(path and path.is_file() and path.name.casefold() == "losslessscaling.exe")

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
        hotkey_listener: Optional["GlobalHotkeyListener"] = None,
        on_installer_launched: Optional[Callable[[], None]] = None,
        proxy_menu=None,
    ):
        self.profile_manager = profile_manager
        self.runtime_logs = runtime_log_buffer()
        self.state = state
        self.process_watcher = process_watcher
        self.automation = automation or AutomationController(
            profile_manager, state,
            overlay_indicator=production_indicator(profile_manager))
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
        self.hotkey_listener = hotkey_listener
        self.on_installer_launched = on_installer_launched
        self.proxy_menu = proxy_menu or getattr(process_watcher, "proxy_menu", None)
        self.config: AppConfig = profile_manager.config
        self.live_performance = LivePerformanceGraph(state, self.automation.overlay_indicator,
                                                     self._presentmon_executable)
        self.reshade_overlay = ReShadeOverlayController(
            profile_manager, state, self.automation.overlay_indicator, hotkey_listener,
            open_profile=self._open_overlay_profile, performance_graph=self.live_performance.toggle,
            open_addon_panel=self._open_overlay_addon_panel)
        self._reshade_overlay_task = None
        self.clients: Set[WebSocketServerProtocol] = set()
        self.fullscreen_tasks: Dict[str, asyncio.Task] = {}
        self.server = None
        self.dashboard_token = secrets.token_urlsafe(32)
        self.profile_icons = ProfileIconService(Path(profile_manager.config_dir) / "icon-cache")
        self.client_authority: Dict[WebSocketServerProtocol, str] = {}
        self.addon_update_task: Optional[asyncio.Task] = None
        self.presentmon_detection_task: Optional[asyncio.Task] = None
        self._background_thread_tasks: Set[asyncio.Task] = set()
        self.addon_update_lock = asyncio.Lock()
        self.component_install_lock = asyncio.Lock()
        self.component_operation_status = None
        self._component_progress_callback = None
        self.addon_update_status: Optional[Dict] = None
        self.benchmark_task: Optional[asyncio.Task] = None
        self._benchmark_previous_profile: Optional[Profile] = None
        self._gpu_calibration_task: Optional[asyncio.Task] = None
        self._last_benchmark_result: Optional[Dict] = None
        self._last_benchmark_report_path: Optional[Path] = None
        self._detected_presentmon_path: Optional[Path] = None
        self._detected_presentmon_sha256: Optional[str] = None
        self.reshade_profiles = ReshadeProfileService(profile_manager)
        self.graphics_source_importer = GraphicsSourceImporter(self.asset_store)
        self.diagnostic_runner = DiagnosticRunner(self)
        self.diagnostics_task: Optional[asyncio.Task] = None
        self.clone_rebuild_task: Optional[asyncio.Task] = None
        self.clone_rebuild_status = {"busy": False, "message": "Rebuild the managed program files while preserving settings."}
        self._diagnostics_selected_test: Optional[str] = None
        self.last_diagnostics_result: Optional[Dict] = None
        self._last_diagnostics_archive: Optional[Path] = None
        self.companion_updater = CompanionUpdateService(
            self.release_manager, self.profile_manager.config_dir
        )
        self.companion_update_status: Optional[Dict] = None
        self.companion_update_lock = asyncio.Lock()
        self._automatic_update_client = None
        self._update_handoff = False
        self.automatic_updates = AutomaticUpdates(self)
        self._next_update_check = 0.0
        self._active_mutations = 0
        self._gpu_activity_lock = threading.Lock()
        self._active_requests = 0
        self._editing_clients = set()
        self.gpu_index_refresh = IdleGpuIndexRefresh(self)
        self.automation.gpu_index_refresh_requested = self.gpu_index_refresh.request_refresh
        self.gpu_index_task: Optional[asyncio.Task] = None
        self.automation.profile_changes_blocked = lambda: bool(
            self._editing_clients or self._active_mutations
        )
        self.on_runtime_changed = None
        self._publication_task = None
        self._publication_unsubscribers = []
        self._config_dirty = False

    def _start_state_publication(self):
        loop = asyncio.get_running_loop()
        self._state_changed = asyncio.Event()

        def enqueue():
            if not loop.is_closed():
                loop.call_soon_threadsafe(self._state_changed.set)

        def config_saved():
            # Marshal configuration publication too: tray quick-add and startup
            # changes must not wait for process detection or another UI request.
            if not loop.is_closed():
                loop.call_soon_threadsafe(mark_config_dirty)

        def mark_config_dirty():
            self._config_dirty = True
            self.state.publish_change()

        self._publication_unsubscribers = [self.state.subscribe(enqueue),
                                           self.profile_manager.subscribe(config_saved)]
        self._publication_task = asyncio.create_task(self._publish_state_changes())

    async def _publish_state_changes(self):
        while True:
            await self._state_changed.wait()
            # Coalesce a worker's related assignments into one update. This is
            # independent of the slower process/overlay inspection loop.
            await asyncio.sleep(0.025)
            self._state_changed.clear()
            config_dirty, self._config_dirty = self._config_dirty, False
            try:
                if self.on_runtime_changed:
                    self.on_runtime_changed()
                await self.broadcast_state()
                if config_dirty and self.clients:
                    payload = json.dumps({"type": "CONFIG_UPDATE",
                                          "profiles": self._profile_summaries(),
                                          "activeProfileId": self.config.active_profile_id,
                                          "runAtStartup": self.config.run_at_startup})
                    await asyncio.gather(*(client.send(payload) for client in tuple(self.clients)),
                                         return_exceptions=True)
            except Exception:
                logger.exception("Could not publish Companion state")

    async def start(self) -> None:
        logger.info(f"Starting WebSocket server on {self.config.host}:{self.config.port}...")
        self.server = await serve(
            self.handle_client,
            self.config.host,
            self.config.port,
            process_request=self.process_http_request,
            # Mute the per-connection open/close lines; with dashboard +
            # extension reconnects they drown the log and look like a flicker
            # loop. Real rejects/errors are still logged explicitly.
            logger=logging.getLogger("LSCompanion.Server.WS"),
        )
        logging.getLogger("LSCompanion.Server.WS").setLevel(logging.WARNING)
        self._start_state_publication()
        self.addon_update_task = asyncio.create_task(self._addon_update_loop())
        self.presentmon_detection_task = asyncio.create_task(self._detect_presentmon_on_startup())
        self.gpu_index_task = asyncio.create_task(self._gpu_index_loop())
        self._reshade_overlay_task = asyncio.create_task(self._reshade_overlay_loop())
        logger.info(f"WebSocket server listening on ws://{self.config.host}:{self.config.port}/ws")

    async def stop(self) -> None:
        await self._run_background_thread(self.automation.capture_reshade_settings)
        if self._reshade_overlay_task:
            self._reshade_overlay_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reshade_overlay_task
            self._reshade_overlay_task = None
        self.reshade_overlay.clear()
        await self._run_background_thread(self.live_performance.stop, wait=True)
        for unsubscribe in self._publication_unsubscribers:
            unsubscribe()
        self._publication_unsubscribers.clear()
        if self._publication_task:
            self._publication_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._publication_task
            self._publication_task = None
        self.gpu_index_refresh.stop()
        if self.gpu_index_task:
            self.gpu_index_task.cancel()
        if self.addon_update_task:
            self.addon_update_task.cancel()
        if self.presentmon_detection_task:
            self.presentmon_detection_task.cancel()
        for task in self.fullscreen_tasks.values():
            task.cancel()
        self.fullscreen_tasks.clear()
        if self.diagnostics_task:
            with contextlib.suppress(asyncio.CancelledError):
                await self.diagnostics_task
        if self.clone_rebuild_task:
            await self.clone_rebuild_task
            self.clone_rebuild_task = None
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
        if self.gpu_index_task:
            with contextlib.suppress(asyncio.CancelledError):
                await self.gpu_index_task
            self.gpu_index_task = None
        # Cancelling a coroutine that awaits asyncio.to_thread() does not stop
        # its executor worker. Drain server-owned workers before callers tear
        # down the asset store they may still be reading or writing.
        while self._background_thread_tasks:
            pending = tuple(self._background_thread_tasks)
            await asyncio.gather(*pending, return_exceptions=True)
            # gather() may complete synchronously for already-finished tasks.
            # Remove these directly instead of starving their queued callbacks
            # in a loop after a fast cancellation/completion during shutdown.
            self._background_thread_tasks.difference_update(pending)

    async def _run_background_thread(self, function, /, *args, **kwargs):
        """Run lifecycle-owned blocking work and keep it drainable on shutdown."""
        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        self._background_thread_tasks.add(task)
        task.add_done_callback(self._background_thread_tasks.discard)
        return await asyncio.shield(task)

    def _open_overlay_profile(self, profile_id: str, section: str | None = None) -> bool:
        if self.profile_manager.get_profile_by_id(profile_id) is None:
            return False
        url = f"http://{self.config.host}:{self.config.port}/dashboard?editProfile={quote(profile_id, safe='')}"
        if section == "addons":
            url += "&profileSection=addons"
        return open_dashboard_window(url)

    async def _reshade_overlay_loop(self):
        next_capture = 0.0
        try:
            while True:
                try:
                    status = (await self._run_background_thread(addon_runtime_status, self.config.lossless_scaling_exe_path)
                              if self.reshade_overlay.eligible() else {"available": False})
                    was_open = self.reshade_overlay.active
                    self.reshade_overlay.update(status)
                    if time.monotonic() >= next_capture or (was_open and not self.reshade_overlay.active):
                        await self._run_background_thread(self.automation.capture_reshade_settings)
                        next_capture = time.monotonic() + 1.0
                    self.live_performance.refresh()
                except Exception:
                    logger.exception("Could not refresh ReShade overlay status")
                    self.reshade_overlay.clear()
                await asyncio.sleep(0.25)
        finally:
            self.reshade_overlay.clear()

    async def handle_client(self, websocket: WebSocketServerProtocol) -> None:
        # websockets>=11 calls the handler with only the protocol; the HTTP
        # request path (including ?token=...) lives on websocket.path and
        # websocket.request_headers. The old (websocket, path) signature
        # received "" here, so every dashboard/extension socket was closed
        # with 1008 "Unknown endpoint" and the UI sat on Disconnected /
        # LS Not Running forever.
        parsed_path = urlsplit(getattr(websocket, "path", "") or "")
        if parsed_path.path != "/ws":
            await websocket.close(code=1008, reason="Unknown endpoint")
            return
        origin = websocket.request_headers.get("Origin")
        if not self.is_origin_allowed(origin):
            logger.warning("Rejected WebSocket origin: %r", origin)
            await websocket.close(code=1008, reason="Origin not allowed")
            return
        query_token = parse_qs(parsed_path.query).get("token", [None])[0]
        if self._is_dashboard_origin(origin) and not secrets.compare_digest(query_token or '', self.dashboard_token):
            logger.info('Dashboard session expired; requesting session renewal')
            await websocket.close(code=4001, reason='Dashboard session expired')
            return
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
        await websocket.send(json.dumps({"type": "INITIAL_STATE", **self._runtime_payload(),
                                         "componentOperation": self.component_operation_status}))

        requests = asyncio.Queue(maxsize=64)

        async def consume_requests():
            while True:
                message = await requests.get()
                if message is None:
                    return
                await self.process_message(websocket, message)

        consumer = asyncio.create_task(consume_requests())
        try:
            async for message in websocket:
                try:
                    parsed = json.loads(message)
                except (ValueError, TypeError):
                    parsed = None
                if isinstance(parsed, dict) and parsed.get("type") == "PING" and parsed.get("healthcheck") is True:
                    # Read health checks independently of ordered save/install
                    # requests, including while they await worker threads.
                    await websocket.send(json.dumps({"type": "PONG", "timestamp": parsed.get("timestamp")}))
                elif isinstance(parsed, dict) and parsed.get("type") == "GET_RUNTIME_LOGS":
                    # Logs stay readable while diagnostics/install requests own the queue.
                    await self._process_message(websocket, message)
                else:
                    await requests.put(message)
        except ConnectionClosed:
            pass
        finally:
            # Finish the in-flight operation, but do not start queued work for
            # a window that is gone. In particular, startup status requests may
            # otherwise initiate downloads during teardown.
            while not requests.empty():
                requests.get_nowait()
            requests.put_nowait(None)
            await asyncio.gather(consumer, return_exceptions=True)
            if self.client_authority.get(websocket) == "dashboard" and self.proxy_menu:
                active = self.state.current_active_profile
                if active and self.config.addon_manager_enabled:
                    await asyncio.to_thread(
                        self.automation.capture_active_addon_config,
                    )
                await asyncio.to_thread(self.proxy_menu.detach, hide=True)
            self.clients.remove(websocket)
            self.client_authority.pop(websocket, None)
            self._editing_clients.discard(websocket)
            self.state.connected_clients = len(self.clients)
            logger.info(f"Client disconnected. Total clients: {self.state.connected_clients}")

    async def process_message(self, websocket: WebSocketServerProtocol, raw_message: str) -> None:
        try:
            parsed = json.loads(raw_message)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict) and parsed.get("type") == "GET_RUNTIME_LOGS":
            return await self._process_message(websocket, raw_message)
        # Keep incoming editor/save/install requests in order while the short
        # idle transaction owns LS. The UI remains responsive and drafts stay
        # in the browser; no request is rejected or discarded.
        while True:
            if getattr(websocket, "closed", False) is True:
                return
            # Claim request activity atomically against the worker's claim on
            # LS. Include reads: some inventory reads also refresh saved pins.
            with self._gpu_activity_lock:
                if not self.state.gpu_mapping_in_progress:
                    self._active_requests += 1
                    break
            await asyncio.sleep(0.05)
        try:
            return await self._process_ready_message(websocket, raw_message)
        finally:
            with self._gpu_activity_lock:
                self._active_requests -= 1

    async def _process_ready_message(self, websocket: WebSocketServerProtocol, raw_message: str) -> None:
        try:
            message = json.loads(raw_message)
        except (ValueError, TypeError):
            return await self._process_message(websocket, raw_message)
        action = message.get("type") if isinstance(message, dict) else None
        if (self.clone_rebuild_task and not self.clone_rebuild_task.done()
                and isinstance(action, str) and not (action.startswith("GET_") or action == "PING")):
            await websocket.send(json.dumps({"type": "ERROR", "action": action,
                                            "message": "The cloned installation is rebuilding. Wait for it to finish."}))
            return
        if (self.state.update_in_progress and websocket is not self._automatic_update_client
                and not (isinstance(action, str) and (action.startswith("GET_") or action == "PING"))):
            await websocket.send(json.dumps({"type": "ERROR", "action": action,
                                            "message": "Companion is installing updates. Try again when it finishes."}))
            return
        if action not in self.COMPONENT_ACTIONS or self.client_authority.get(websocket) != "dashboard":
            mutation = isinstance(action, str) and not (action.startswith("GET_") or action == "PING")
            if mutation:
                self._active_mutations += 1
            try:
                return await self._process_message(websocket, raw_message)
            finally:
                if mutation:
                    self._active_mutations -= 1
        if self.component_install_lock.locked():
            await websocket.send(json.dumps(self.component_operation_status))
            await websocket.send(json.dumps({"type": "ERROR", "message": "Another component download/install is in progress"}))
            return
        async with self.component_install_lock:
            provider = "companion" if "COMPANION_UPDATE" in action else str(message.get("provider") or "")
            if provider == 'companion' and self._editing_clients:
                await websocket.send(json.dumps({'type': 'ERROR', 'action': action, 'message': 'Close profile editors before updating Companion.'}))
                return
            owns_maintenance = provider == "companion" and not self.state.update_in_progress
            if owns_maintenance and not await self._run_background_thread(self.automation.begin_update):
                await websocket.send(json.dumps({"type": "ERROR", "action": action, "message": "Wait for scaling to stop before updating Companion."}))
                return
            self.component_operation_status = {
                "type": "COMPONENT_OPERATION", "provider": provider, "busy": True,
                "phase": "checking", "receivedBytes": 0, "totalBytes": 0,
                "version": message.get("version"), "automatic": websocket is self._automatic_update_client,
            }
            queue = asyncio.Queue()
            loop = asyncio.get_running_loop()
            self._component_progress_callback = lambda status: loop.call_soon_threadsafe(queue.put_nowait, status)
            await self._broadcast_component_operation(websocket)
            task = asyncio.create_task(self._process_message(websocket, raw_message))
            try:
                while not task.done():
                    try:
                        progress = await asyncio.wait_for(queue.get(), timeout=0.1)
                    except asyncio.TimeoutError:
                        continue
                    while not queue.empty():
                        progress = queue.get_nowait()
                    self.component_operation_status.update(progress)
                    await self._broadcast_component_operation(websocket)
                result = await task
                self.component_operation_status.update(
                    busy=False, phase="failed" if isinstance(result, Exception) else "complete",
                    error=str(result) if isinstance(result, Exception) else None,
                )
            finally:
                # A client disconnect must not unlock while a worker still writes.
                if not task.done():
                    await asyncio.shield(task)
                self._component_progress_callback = None
                self.component_operation_status["busy"] = self._update_handoff
                if self._update_handoff:
                    self.component_operation_status["phase"] = "restarting"
                elif owns_maintenance:
                    await self._run_background_thread(self.automation.end_update)
                await self._broadcast_component_operation(websocket)

    async def _broadcast_component_operation(self, requester=None):
        self.component_operation_status["locked"] = self.state.update_in_progress
        recipients = set(self.clients)
        if requester:
            recipients.add(requester)
        payload = json.dumps(self.component_operation_status)
        await asyncio.gather(*(client.send(payload) for client in recipients), return_exceptions=True)

    def _stage_release(self, *args, **kwargs):
        if self._component_progress_callback:
            kwargs["progress_callback"] = self._component_progress_callback
        result = self.release_manager.stage_release(*args, **kwargs)
        if self._component_progress_callback:
            self._component_progress_callback({"phase": "installing", "receivedBytes": 0, "totalBytes": 0})
        return result

    async def _process_message(self, websocket: WebSocketServerProtocol, raw_message: str) -> Optional[Exception]:
        msg = {}
        try:
            msg = json.loads(raw_message)
            msg_type = msg.get("type")

            dashboard_messages = {
                "GET_RUNTIME_LOGS",
                "REBUILD_CLONED_INSTALLATION",
                "UPDATE_LOSSLESS_PROFILE", "SET_EDITOR_ACTIVITY",
                "IMPORT_LOSSLESS_PROFILE",
                "SET_ACTIVE_PROFILE",
                "SAVE_PROFILE",
                "DELETE_PROFILE",
                "CHECK_GRAPHICS_RELEASES",
                "STAGE_GRAPHICS_RELEASE",
                "IMPORT_GRAPHICS_SOURCE",
                "INSTALL_LATEST_GRAPHICS_ADDON",
                "DOWNLOAD_BENCHMARK_TOOL",
                "START_PERFORMANCE_BENCHMARK",
                "CALIBRATE_GPUS",
                "SELECT_DLSSNR_RUNTIME",
                "SAVE_CONTROL_SETTINGS",
                "SAVE_GENERAL_SETTINGS",
                "SCALE_NOW",
                "RESET_RTSS_CALIBRATION",
                "START_RTSS_MANUAL_CALIBRATION",
                "REVERT_ALL_CHANGES",
                "OPEN_CONFIG_FOLDER",
                "OPEN_PROXY_MENU",
                "EMBED_ADDON_MENU",
                "DETACH_ADDON_MENU",
                "GET_ADDON_EDITOR_CATALOG",
                "SAVE_ADDON_CONFIG",
                "BROWSE_GENERAL_PATH",
                "GET_RESHADE_PROFILES",
                "SAVE_RESHADE_PROFILE",
                "IMPORT_RESHADE_PRESET",
                "DELETE_RESHADE_PROFILE",
                "RUN_DIAGNOSTICS",
                "OPEN_DIAGNOSTICS_FOLDER",
                "CHECK_COMPANION_UPDATE",
                "DOWNLOAD_COMPANION_UPDATE",
                "APPLY_COMPANION_UPDATE",
                "INSTALL_COMPANION_UPDATE",
                "REPAIR_SETTING", "RETRY_DEPLOYMENT_RECOVERY",
            }
            extension_messages = {"BROWSER_FULLSCREEN_EVENT", "MANUAL_TRIGGER"}
            authority = self.client_authority.get(websocket, "readonly")
            if msg_type in dashboard_messages and authority != "dashboard":
                raise PermissionError("Dashboard capability token required")
            if msg_type in extension_messages and authority not in {"dashboard", "extension"}:
                raise PermissionError("Automation messages require an authorized local client")

            if msg_type == "GET_RUNTIME_LOGS":
                since = msg.get("since", 0)
                if type(since) is not int or since < 0:
                    raise ValueError("Log cursor must be a non-negative integer")
                await websocket.send(json.dumps({"type": "RUNTIME_LOGS", **self.runtime_logs.snapshot(since, msg.get("session", "")),
                    "path": str(self.profile_manager.config_dir / "logs" / "companion.log"),
                    "scalingControl": dict(self.state.scaling_control_status),
                    "cloneRebuild": dict(self.clone_rebuild_status)}))
                return
            if (isinstance(msg_type, str) and not msg_type.startswith("GET_")
                    and msg_type not in {"PING", "SET_EDITOR_ACTIVITY", "EMBED_ADDON_MENU", "DETACH_ADDON_MENU"}):
                logger.info("Received %s request from %s", msg_type, authority)

            if msg_type == "GET_NOTIFICATIONS":
                await websocket.send(json.dumps({"type": "NOTIFICATIONS", **self._notifications_payload()}))
                return
            if msg_type == "SET_EDITOR_ACTIVITY":
                if msg.get("editing") is True:
                    self._editing_clients.add(websocket)
                else:
                    self._editing_clients.discard(websocket)
                return
            if msg_type == "REPAIR_SETTING":
                await asyncio.to_thread(self.profile_manager.repair_setting, msg.get("path"), msg.get("value"))
                await self.broadcast_full_data_update()
                await websocket.send(json.dumps({"type": "NOTIFICATIONS", **self._notifications_payload()}))
                return
            if self.profile_manager.repair_session and not (
                str(msg_type).startswith("GET_") or msg_type in {"PING", "OPEN_CONFIG_FOLDER"}
            ):
                raise ValueError("Repair saved settings and restart Companion before changing other settings or scaling")
            if msg_type == "REBUILD_CLONED_INSTALLATION":
                if ((self.diagnostics_task and not self.diagnostics_task.done())
                        or self.component_install_lock.locked()
                        or self.state.benchmark_mode_active or self.state.update_in_progress):
                    raise RuntimeError("Finish downloads, updates or live tests before rebuilding the clone")
                self.clone_rebuild_status = {"busy": True, "message": "Stopping LS and rebuilding the cloned program files…"}
                # Keep idle GPU work and automatic updates out until the
                # background worker finishes, while log reads stay available.
                with self._gpu_activity_lock:
                    self._active_requests += 1
                self._active_mutations += 1
                self.clone_rebuild_task = asyncio.create_task(self._run_clone_rebuild())
                await websocket.send(json.dumps({"type": "CLONE_REBUILD_STATUS", **self.clone_rebuild_status}))
                return
            if msg_type == "RETRY_DEPLOYMENT_RECOVERY":
                if self.state.is_scaling_active:
                    raise ValueError("Stop scaling before retrying deployment recovery")
                await self._run_background_thread(self.automation.retry_deployment_recovery)
                await websocket.send(json.dumps({"type": "NOTIFICATIONS", **self._notifications_payload()}))
                return

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
                elif kind == "program_executable":
                    current_path = str(msg.get("currentPath") or "") or None
                elif kind == "reshade_preset":
                    current_path = str(msg.get("currentPath") or "") or None
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

            if msg_type == "SCALE_NOW":
                try:
                    delay = float(msg.get("delaySeconds", 5.0))
                except (TypeError, ValueError):
                    delay = 5.0
                if self._gpu_calibration_task and not self._gpu_calibration_task.done():
                    raise RuntimeError("GPU calibration is running; wait for it to finish")
                scheduled = await asyncio.to_thread(
                    self.automation.scale_focused_after_delay, delay
                )
                await websocket.send(json.dumps({
                    "type": "SCALE_NOW_SCHEDULED",
                    "delaySeconds": scheduled,
                }))
                await self.broadcast_state()
                return

            if msg_type == "CALIBRATE_GPUS":
                if self.benchmark_task and not self.benchmark_task.done():
                    raise RuntimeError("A performance benchmark is already running")
                if self._gpu_calibration_task and not self._gpu_calibration_task.done():
                    raise RuntimeError("GPU calibration is already running")
                workload = self._embedded_benchmark_executable()
                presentmon = self._presentmon_executable()
                if not workload or not presentmon:
                    raise RuntimeError("PresentMon or the embedded benchmark workload is unavailable")
                # Calibration owns Lossless Scaling exclusively from here on:
                # flag first so racing automatic starts refuse, then stop any
                # live session before the workload takes foreground.
                self.state.benchmark_mode_active = True
                try:
                    if self.state.is_scaling_active:
                        stopped = await asyncio.to_thread(
                            self.automation.set_scaling,
                            False,
                            self.state.current_active_profile,
                            reason="gpu_calibration_takeover",
                            force=True,
                        )
                        if not stopped or self.state.is_scaling_active:
                            raise RuntimeError("The active scaling session could not be stopped")
                except Exception:
                    self.state.benchmark_mode_active = False
                    raise
                self._gpu_calibration_task = asyncio.create_task(
                    self._run_gpu_calibration(workload)
                )
                await websocket.send(json.dumps({
                    "type": "GPU_CALIBRATION_STATUS",
                    "running": True,
                    "stage": "starting",
                    "detail": "",
                }))
                return

            if msg_type == "GET_STATE_AND_PROCESSES":
                visible_only = msg.get("visibleOnly", True)
                await self.send_full_data_update(websocket, visible_windows_only=visible_only)
                return

            if msg_type == "GET_PROFILE_SUMMARIES":
                await websocket.send(json.dumps({
                    "type": "PROFILE_SUMMARIES",
                    "profiles": self._profile_summaries(),
                }))
                return

            if msg_type == "GET_PROFILE_DETAILS":
                await asyncio.to_thread(self.automation.refresh_gpu_selections)
                if self.config.addon_manager_enabled:
                    await asyncio.to_thread(self.automation.capture_active_addon_config)
                await websocket.send(json.dumps({
                    "type": "PROFILE_DETAILS",
                    "generation": msg.get("generation"),
                    "profiles": [self._profile_editor_data(profile) for profile in self.profile_manager.config.profiles],
                }))
                return

            if msg_type == "GET_PROFILE_DETAIL":
                await asyncio.to_thread(self.automation.refresh_gpu_selections)
                if self.config.addon_manager_enabled:
                    await asyncio.to_thread(self.automation.capture_active_addon_config)
                profile = self.profile_manager.get_profile_by_id(str(msg.get("profileId") or ""))
                if profile is None:
                    raise ValueError("Unknown profile")
                await websocket.send(json.dumps({
                    "type": "PROFILE_DETAIL",
                    "profile": self._profile_editor_data(profile),
                    "action": msg.get("action"),
                }))
                return

            if msg_type == "GET_ADDON_EDITOR_CATALOG":
                catalog = await asyncio.to_thread(
                    self.automation.addon_configs.catalog, self.asset_store.list_packages()
                )
                await websocket.send(json.dumps({"type": "ADDON_EDITOR_CATALOG", "addons": catalog}))
                return

            if msg_type == "GET_ADDON_RUNTIME_STATUS":
                status = await asyncio.to_thread(addon_runtime_status, self.config.lossless_scaling_exe_path)
                await websocket.send(json.dumps({"type": "ADDON_RUNTIME_STATUS", **status}))
                return

            if msg_type == "SAVE_ADDON_CONFIG":
                profile = self.profile_manager.get_profile_by_id(str(msg.get("profileId") or ""))
                if profile is None:
                    raise ValueError("Save the profile before editing its add-ons")
                data = msg.get("config")
                if not isinstance(data, dict) or not isinstance(data.get("addons"), dict) or not isinstance(data.get("global"), dict):
                    raise ValueError("Add-on configuration must contain global and addons objects")
                configs = self.automation.addon_configs
                profile = await asyncio.to_thread(self.automation.save_addon_config, profile.id, data)
                await websocket.send(json.dumps({
                    "type": "ADDON_CONFIG_SAVED", "profileId": profile.id,
                    "generation": msg.get("generation"), "config": profile.addon_config,
                    "neuralRender": profile.graphics.neural_render.model_dump(mode="json"),
                    "pending": configs.has_pending(profile),
                    "restartPending": self.automation.profile_reload_pending(profile.id),
                }))
                await self.broadcast_full_data_update()
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
                    "componentOperation": self.component_operation_status,
                }))
                return

            if msg_type == "GET_BENCHMARK_STATUS":
                await websocket.send(json.dumps(self._benchmark_status_payload()))
                return

            if msg_type == "OPEN_PROXY_MENU":
                if not self.proxy_menu:
                    raise RuntimeError("Add-on menu control is unavailable")
                if not self.config.addon_manager_enabled:
                    raise RuntimeError("Companion Addon Host is disabled in General Settings")
                if not self.state.is_scaling_active:
                    raise RuntimeError("The add-on menu is available only while scaling")
                status = await asyncio.to_thread(addon_runtime_status, self.config.lossless_scaling_exe_path)
                if not status.get("available") or status.get("headless") is not False:
                    raise RuntimeError("Install the current Companion Addon Host and restart Lossless Scaling to open the panel")
                result = await asyncio.to_thread(self.proxy_menu.open, status["pid"])
                if not result.get("opened"):
                    raise RuntimeError(str(result.get("reason") or "Could not open the add-on menu"))
                await websocket.send(json.dumps({"type": "PROXY_MENU_OPENED"}))
                return

            if msg_type == "EMBED_ADDON_MENU":
                if not self.proxy_menu:
                    raise RuntimeError("Add-on menu control is unavailable")
                if not self.config.addon_manager_enabled:
                    raise RuntimeError("Companion Addon Host is disabled in General Settings")
                profile_id = str(msg.get("profileId") or "")
                active = self.state.current_active_profile
                if not active or active.id != profile_id:
                    await websocket.send(json.dumps({
                        "type": "ADDON_MENU_EMBED_STATUS",
                        "embedded": False,
                        "reason": "Echo can edit only the active profile. Activate and scale this profile first.",
                    }))
                    return
                rect = msg.get("rect") or {}
                placement = dashboard_css_rect_to_client_pixels(rect)
                if not placement:
                    await websocket.send(json.dumps({
                        "type": "ADDON_MENU_EMBED_STATUS",
                        "embedded": False,
                        "reason": "Native embedding is available in Companion's app window only.",
                    }))
                    return
                parent, left, top, width, height = placement
                clip = rect.get("clip") or {}
                css_width = float(rect.get("width") or 0)
                css_height = float(rect.get("height") or 0)
                if css_width <= 0 or css_height <= 0:
                    raise ValueError("The add-on panel has no usable host area")
                clip_left = max(0, min(width, round(float(clip.get("left") or 0) * width / css_width)))
                clip_top = max(0, min(height, round(float(clip.get("top") or 0) * height / css_height)))
                clip_right = max(clip_left, min(width, round(
                    (float(clip.get("left") or 0) + float(clip.get("width") or css_width))
                    * width / css_width
                )))
                clip_bottom = max(clip_top, min(height, round(
                    (float(clip.get("top") or 0) + float(clip.get("height") or css_height))
                    * height / css_height
                )))
                result = await asyncio.to_thread(
                    self.proxy_menu.embed,
                    parent,
                    left=left,
                    top=top,
                    width=width,
                    height=height,
                    clip=(clip_left, clip_top, clip_right, clip_bottom),
                )
                await websocket.send(json.dumps({
                    "type": "ADDON_MENU_EMBED_STATUS",
                    **result,
                }))
                return

            if msg_type == "DETACH_ADDON_MENU":
                active = self.state.current_active_profile
                if active and self.config.addon_manager_enabled:
                    await asyncio.to_thread(
                        self.automation.capture_active_addon_config,
                    )
                if self.proxy_menu:
                    await asyncio.to_thread(self.proxy_menu.detach, hide=True)
                await websocket.send(json.dumps({
                    "type": "ADDON_MENU_EMBED_STATUS", "embedded": False,
                }))
                return

            if msg_type == "IMPORT_GRAPHICS_SOURCE":
                result = await asyncio.to_thread(
                    self.graphics_source_importer.stage,
                    str(msg.get("provider") or ""),
                    str(msg.get("operationId") or ""),
                )
                await websocket.send(json.dumps({
                    "type": "GRAPHICS_RELEASE_STAGED", "package": result,
                }))
                return

            if msg_type == "GET_DIAGNOSTICS_STATUS":
                payload = dict(self.last_diagnostics_result or {
                    "type": "DIAGNOSTICS_STATUS",
                    "running": False,
                    "results": [],
                })
                payload["running"] = bool(
                    self.diagnostics_task and not self.diagnostics_task.done()
                )
                if payload["running"]:
                    payload["selectedTest"] = self._diagnostics_selected_test
                payload["availableTests"] = self.diagnostic_runner.catalog()
                await websocket.send(json.dumps(payload))
                return

            if msg_type == "RUN_DIAGNOSTICS":
                if self.diagnostics_task and not self.diagnostics_task.done():
                    raise RuntimeError("Application diagnostics are already running")
                selected_test = str(msg.get("testId") or "").strip() or None
                available = {item["id"] for item in self.diagnostic_runner.catalog()}
                if selected_test is not None and selected_test not in available:
                    raise ValueError("Unknown application test")
                if (selected_test not in self.diagnostic_runner.READ_ONLY_TESTS
                        and self.state.benchmark_mode_active):
                    raise RuntimeError("Wait for the benchmark or GPU calibration to finish before running live diagnostics")
                self._diagnostics_selected_test = selected_test
                self.diagnostics_task = asyncio.create_task(self._run_diagnostics(selected_test))
                await websocket.send(json.dumps({
                    "type": "DIAGNOSTICS_STATUS", "running": True, "results": [],
                    "selectedTest": selected_test,
                    "availableTests": self.diagnostic_runner.catalog(),
                }))
                return

            if msg_type == "OPEN_DIAGNOSTICS_FOLDER":
                self.diagnostic_runner.root.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(os.startfile, str(self.diagnostic_runner.root.resolve()))
                return

            if msg_type == "GET_RESHADE_PROFILES":
                payload = await asyncio.to_thread(self.reshade_profiles.payload)
                await websocket.send(json.dumps({"type": "RESHADE_PROFILES", **payload}))
                return

            if msg_type == "SAVE_RESHADE_PROFILE":
                await asyncio.to_thread(self.automation.capture_reshade_settings)
                profile = ManagedReshadeProfile.model_validate(msg.get("profile") or {})
                previous = next((item for item in self.config.reshade_profiles if item.id == profile.id), None)
                result = await asyncio.to_thread(self.reshade_profiles.save, profile)
                active = self.state.current_active_profile
                if (previous != profile and active and active.reshade
                        and active.reshade.enabled and active.reshade.managed_profile_id == profile.id):
                    await asyncio.to_thread(self.automation.apply_saved_profile, active)
                await websocket.send(json.dumps({"type": "RESHADE_PROFILE_SAVED", "profile": result}))
                return

            if msg_type == "IMPORT_RESHADE_PRESET":
                selected = await asyncio.to_thread(
                    choose_general_setting_path, "reshade_preset", None
                )
                if not selected:
                    await websocket.send(json.dumps({"type": "RESHADE_PROFILE_IMPORT_CANCELLED"}))
                    return
                result = await asyncio.to_thread(self.reshade_profiles.import_preset, selected)
                await websocket.send(json.dumps({"type": "RESHADE_PROFILE_IMPORTED", "profile": result}))
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
                self.config.snap_near_fullscreen_windows_to_monitor = bool(
                    msg.get(
                        "snapNearFullscreenWindowsToMonitor",
                        self.config.snap_near_fullscreen_windows_to_monitor,
                    )
                )
                self.config.minimize_other_windows_on_scale = bool(
                    msg.get(
                        "minimizeOtherWindowsOnScale",
                        self.config.minimize_other_windows_on_scale,
                    )
                )
                self.config.overlay_indicator_enabled = bool(
                    msg.get(
                        "overlayIndicatorEnabled",
                        self.config.overlay_indicator_enabled,
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
                if self.hotkey_listener:
                    self.hotkey_listener.refresh()
                await websocket.send(json.dumps({
                    "type": "CONTROL_SETTINGS_SAVED",
                    "synchronized": synchronized,
                    "controlSettings": self._control_settings_payload(),
                }))
                return

            if msg_type == "SAVE_GENERAL_SETTINGS":
                overlay_hotkeys = validate_overlay_hotkeys(msg.get("overlayHotkeys", self.config.overlay_hotkeys))
                reshade_binding = parse_shortcut(msg.get("reshadeOverlayHotkey", self.config.reshade_overlay_hotkey))
                if reshade_binding and any(parse_shortcut(binding) and parse_shortcut(binding)[:2] == reshade_binding[:2]
                                           for binding in overlay_hotkeys.values()):
                    raise ValueError("Overlay shortcuts must differ from the ReShade menu hotkey")
                notify_updates = msg.get("notifyOnUpdates", self.config.notify_on_updates)
                automatic_updates = msg.get("automaticallyInstallUpdates", self.config.automatically_install_updates)
                if type(notify_updates) is not bool or type(automatic_updates) is not bool:
                    raise ValueError("Update preferences must be checkboxes")
                run_at_startup = bool(msg.get("runAtStartup", False))
                previous_addon_manager_enabled = self.config.addon_manager_enabled
                requested_addon_manager_enabled = bool(
                    msg.get("addonManagerEnabled", self.config.addon_manager_enabled)
                )
                match_encoder_gpu = msg.get("sunshineMatchEncoderGpu", self.config.sunshine_match_encoder_gpu)
                gloam_hdr = msg.get("gloamHdrEnabled", self.config.gloam_hdr_enabled)
                if type(gloam_hdr) is not bool:
                    raise ValueError("Gloam HDR integration must be a checkbox")
                if type(match_encoder_gpu) is not bool:
                    raise ValueError("Sunshine/Apollo GPU matching must be a checkbox")
                smart_auto_scale_enabled = bool(msg.get("smartAutoScaleEnabled", True))
                gpu_inventory = self.automation.gpu_router.detect()
                requested_auto_route = bool(msg.get("autoRouteGpuToDisplay", False))
                requested_gpu_id = str(
                    msg.get("preferredScalingGpuDeviceId") or ""
                ).strip() or None
                requested_gpu_name = str(
                    msg.get("preferredScalingGpuName") or ""
                ).strip() or None
                if requested_auto_route:
                    requested_gpu_id = None
                    requested_gpu_name = None
                elif requested_gpu_name or requested_gpu_id:
                    resolved = match_device(
                        gpu_inventory.get("gpus", []),
                        requested_gpu_id,
                        requested_gpu_name,
                        gpu=True,
                    )
                    if resolved is None:
                        raise ValueError("Select a currently detected GPU")
                    requested_gpu_id = str(resolved.get("deviceId") or "")
                    requested_gpu_name = str(resolved.get("name") or "")
                gpu_settings_changed = (
                    self.config.preferred_scaling_gpu_device_id,
                    self.config.preferred_scaling_gpu_name,
                    self.config.auto_route_gpu_to_display,
                ) != (requested_gpu_id, requested_gpu_name, requested_auto_route)
                requested_rtx_hdr = bool(msg.get("nvidiaRtxHdrEnabled", False))
                if requested_rtx_hdr and not gpu_inventory.get("hasNvidia"):
                    raise ValueError("NVIDIA RTX HDR requires a detected NVIDIA GPU")
                requested_reshade_peak = self.config.reshade_hdr_peak_nits
                requested_reshade_peak_raw = msg.get(
                    "reshadeHdrPeakNits", requested_reshade_peak
                )
                if requested_reshade_peak_raw is None or requested_reshade_peak_raw == "":
                    requested_reshade_peak = None
                else:
                    requested_reshade_peak_value = float(requested_reshade_peak_raw)
                    if not requested_reshade_peak_value.is_integer():
                        raise ValueError("ReShade HDR peak brightness must be a whole number")
                    requested_reshade_peak = int(requested_reshade_peak_value)
                    if not 80 <= requested_reshade_peak <= 10000:
                        raise ValueError("ReShade HDR peak brightness must be between 80 and 10000 nits")
                reshade_peak_changed = (
                    requested_reshade_peak != self.config.reshade_hdr_peak_nits
                )
                requested_sdr_white = ReshadeSdrWhiteSettings(
                    default_nits=msg.get("reshadeSdrWhiteNits", self.config.reshade_sdr_white.default_nits),
                    monitors=msg.get("reshadeMonitorSdrWhiteNits", self.config.reshade_sdr_white.monitors),
                )
                reshade_white_changed = requested_sdr_white != self.config.reshade_sdr_white
                requested_reshade_hotkey = str(
                    msg.get("reshadeOverlayHotkey", self.config.reshade_overlay_hotkey)
                ).strip().casefold()
                if not requested_reshade_hotkey:
                    raise ValueError("Enter a ReShade menu hotkey")
                reshade_key_data(requested_reshade_hotkey)
                reshade_hotkey_changed = (
                    requested_reshade_hotkey != self.config.reshade_overlay_hotkey
                )
                enable_rtss = bool(msg.get("rtssFrameLimitingEnabled", False))
                requested_default_mode = str(msg.get("rtssDefaultLimitMode") or "static")
                if requested_default_mode not in {"static", "dynamic"}:
                    raise ValueError("Unsupported default RTSS limiter mode")
                requested_static_limit_raw = float(
                    msg.get(
                        "rtssDefaultStaticFramerateLimit",
                        self.config.rtss_default_static_framerate_limit,
                    )
                )
                if not requested_static_limit_raw.is_integer():
                    raise ValueError("Default RTSS static frame limit must be a whole number")
                requested_static_limit = int(requested_static_limit_raw)
                if not 1 <= requested_static_limit <= 1000:
                    raise ValueError("Default RTSS static frame limit must be between 1 and 1000 FPS")
                requested_gpu_target = float(msg.get("rtssDefaultGpuTargetPercent", 15.0))
                if not 1.0 <= requested_gpu_target <= 100.0:
                    raise ValueError("Default Lossless Scaling GPU target must be between 1 and 100")
                requested_rtss_path = str(msg.get("rtssInstallPath") or "").strip() or None
                requested_default_auto_scale = smart_auto_scale_enabled
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
                self.config.notify_on_updates = notify_updates
                self.config.automatically_install_updates = automatic_updates
                self._next_update_check = 0.0
                self.config.addon_manager_enabled = requested_addon_manager_enabled
                self.config.sunshine_match_encoder_gpu = match_encoder_gpu
                self.config.gloam_hdr_enabled = gloam_hdr
                self.config.addon_manager.enabled = requested_addon_manager_enabled
                self.config.preferred_scaling_gpu_device_id = requested_gpu_id
                self.config.preferred_scaling_gpu_name = requested_gpu_name
                self.config.auto_route_gpu_to_display = requested_auto_route
                self.config.nvidia_rtx_hdr_enabled = requested_rtx_hdr
                self.config.reshade_hdr_peak_nits = requested_reshade_peak
                self.config.reshade_sdr_white = requested_sdr_white
                self.config.reshade_overlay_hotkey = requested_reshade_hotkey
                self.config.default_profile_auto_scale = requested_default_auto_scale
                self.config.lossless_control_configured = True
                self.config.disable_native_auto_scale = smart_auto_scale_enabled
                self.state.auto_scale_enabled = smart_auto_scale_enabled
                self.config.snap_near_fullscreen_windows_to_monitor = bool(
                    msg.get(
                        "snapNearFullscreenWindowsToMonitor",
                        self.config.snap_near_fullscreen_windows_to_monitor,
                    )
                )
                self.config.minimize_other_windows_on_scale = bool(
                    msg.get("minimizeOtherWindowsOnScale", False)
                )
                self.config.overlay_hotkeys = overlay_hotkeys
                self.config.overlay_indicator_enabled = bool(
                    msg.get("overlayIndicatorEnabled", self.config.overlay_indicator_enabled)
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
                self.config.rtss_default_static_framerate_limit = requested_static_limit
                self.config.rtss_default_gpu_target_percent = requested_gpu_target
                self.rtss_manager.configured_path = self.config.rtss_install_path
                self.profile_manager.save_config()
                self.automation.overlay_indicator.tick()
                await asyncio.to_thread(self.automation.gloam_integration.poll)
                if gpu_settings_changed:
                    await asyncio.to_thread(self.automation.apply_general_gpu_settings)
                if (
                    previous_addon_manager_enabled != requested_addon_manager_enabled
                    and self.state.current_active_profile
                    and self._lossless_scaling_found()
                ):
                    await asyncio.to_thread(
                        self.automation.activate_profile,
                        self.state.current_active_profile,
                        force=True,
                        suppress_auto_scale=True,
                    )
                if reshade_peak_changed or reshade_white_changed:
                    await asyncio.to_thread(self.reshade_profiles.refresh_hdr_peak_settings)
                    active = self.state.current_active_profile
                    if reshade_white_changed and active and active.reshade and active.reshade.enabled:
                        self.automation.addon_configs.mark_edited(active)
                if reshade_hotkey_changed:
                    await asyncio.to_thread(self.reshade_profiles.sync_all_configs)
                    await asyncio.to_thread(self.automation.queue_reshade_hotkey_reload)
                await asyncio.to_thread(
                    self.automation.reconcile_window_management_setting
                )
                synchronized = await asyncio.to_thread(
                    self.process_watcher.enforce_helper_scaling_control
                ) if self.process_watcher else False
                if self.hotkey_listener:
                    self.hotkey_listener.refresh()
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
                releases = await asyncio.to_thread(self.release_manager.check, provider, channel=channel, force=True)
                await websocket.send(json.dumps({
                    "type": "GRAPHICS_RELEASES",
                    "provider": provider,
                    "releases": releases,
                }))
                return

            if msg_type == "STAGE_GRAPHICS_RELEASE":
                result = await asyncio.to_thread(
                    self._stage_release,
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
                if provider not in {"ls-addon-manager", "sm86-sr", "amd-neural-render-runtime", "lsp-neural-render", "reshade", "special-k", "display-commander"}:
                    raise ValueError("This add-on cannot be installed automatically")
                if provider == "lsp-neural-render" and msg.get("acceptCommunityRuntime") is not True:
                    raise ValueError(
                        "Confirm the warning for the community-modified DLSS 5 runtime"
                    )
                releases = await asyncio.to_thread(
                    self.release_manager.check, provider, channel="stable", force=True
                )
                requested_version = str(msg.get("version") or "") if websocket is self._automatic_update_client else ""
                release = next((item for item in releases if item.get("assets")
                                and (not requested_version or str(item.get("version") or "") == requested_version)), None)
                if release is None:
                    raise ValueError(f"No downloadable stable release is available for {provider}")
                if self.component_operation_status:
                    self.component_operation_status["version"] = str(release.get("version") or "")
                automatic = websocket is self._automatic_update_client
                release_version = str(release.get("version") or "")
                if (automatic and provider == "ls-addon-manager" and self.config.addon_manager.version
                        and not newer_addon_version(self.config.addon_manager.version, release_version)):
                    raise ValueError("The Echo release is no longer newer than the selected version")
                assets = list(release.get("assets") or [])

                def asset_priority(asset: Dict) -> tuple:
                    name = str(asset.get("name") or "").casefold()
                    unwanted = any(word in name for word in ("source", "debug", "symbol", "pdb"))
                    if provider == "ls-addon-manager":
                        preferred = 0 if name.endswith(".zip") else 1
                    elif provider == "lsp-neural-render":
                        preferred = 0 if name.endswith(".zip") else 1
                    else:
                        preferred = 0 if "specialk" in name and name.endswith((".7z", ".zip")) else 1
                    return (unwanted, preferred, name)

                asset = min(assets, key=asset_priority)
                result = await asyncio.to_thread(
                    self._stage_release,
                    provider,
                    str(release.get("version") or ""),
                    str(asset.get("name") or ""),
                    str(msg.get("operationId") or ""),
                    channel="stable",
                )
                if provider in {"sm86-sr", "amd-neural-render-runtime", "display-commander"}:
                    # Staging is installation-wide; opt-in and stopped deployment
                    # remain per profile. Never change an active profile here.
                    await websocket.send(json.dumps({"type": "GRAPHICS_RELEASE_STAGED", "package": result}))
                    return
                if provider == "ls-addon-manager":
                    active_profile = self.state.current_active_profile
                    if active_profile:
                        await asyncio.to_thread(
                            self.automation.capture_active_addon_config,
                        )
                        active_profile = self.state.current_active_profile
                    if websocket is not self._automatic_update_client:
                        self.config.addon_manager.enabled = True
                    self.config.addon_manager.version = str(result.get("version") or "") or None
                    if websocket is not self._automatic_update_client:
                        self.config.addon_manager_enabled = True
                    await asyncio.to_thread(self.profile_manager.save_config)
                    active_redeployed = False
                    if active_profile and self.config.addon_manager_enabled and self._lossless_scaling_found():
                        await asyncio.to_thread(
                            self.automation.activate_profile,
                            active_profile,
                            force=True,
                            suppress_auto_scale=True,
                        )
                        desired = self.automation.resolve_deployment_files(active_profile)
                        if self.automation.deployment_manager.needs_change(
                            active_profile.id,
                            desired,
                            lossless_scaling_exe=self.config.lossless_scaling_exe_path,
                        ):
                            raise RuntimeError(
                                "Echo was downloaded but the active installation could not be updated"
                            )
                        active_redeployed = True
                    await websocket.send(json.dumps({
                        "type": "GRAPHICS_RELEASE_STAGED",
                        "package": result,
                        "activeProfileRedeployed": active_redeployed,
                    }))
                    return
                if provider == "lsp-neural-render":
                    # NeuralRender cannot run without LosslessProxy. Treat this
                    # button as a complete dependency bundle instead of leaving
                    # a successfully downloaded add-on undeployable until the
                    # user discovers and installs a second package.
                    proxy_releases = await asyncio.to_thread(
                        self.release_manager.check,
                        "lossless-proxy",
                        channel="stable",
                        force=True,
                    )
                    proxy_release = next(
                        (item for item in proxy_releases if item.get("assets")), None
                    )
                    if proxy_release is None:
                        raise ValueError("No downloadable stable LosslessProxy release is available")
                    proxy_assets = list(proxy_release.get("assets") or [])
                    proxy_asset = min(
                        proxy_assets,
                        key=lambda item: (
                            any(
                                word in str(item.get("name") or "").casefold()
                                for word in ("source", "debug", "symbol", "pdb")
                            ),
                            0
                            if str(item.get("name") or "").casefold().endswith(".zip")
                            else 1,
                            str(item.get("name") or "").casefold(),
                        ),
                    )
                    proxy_package = await asyncio.to_thread(
                        self._stage_release,
                        "lossless-proxy",
                        str(proxy_release.get("version") or ""),
                        str(proxy_asset.get("name") or ""),
                        f"{str(msg.get('operationId') or '')}proxy",
                        channel="stable",
                    )
                    runtime_provider = "dlssnr-community-runtime"
                    runtime_releases = await asyncio.to_thread(
                        self.release_manager.check,
                        runtime_provider,
                        channel="stable",
                        force=True,
                    )
                    runtime_release = next(
                        (item for item in runtime_releases if item.get("assets")), None
                    )
                    if runtime_release is None:
                        raise ValueError("No community DLSS 5 runtime is currently available")
                    runtime_asset = runtime_release["assets"][0]
                    if not str(runtime_asset.get("digest") or "").startswith("sha256:"):
                        raise UnsafeAssetError(
                            "The community runtime release has no publisher SHA-256 digest"
                        )
                    runtime_package = await asyncio.to_thread(
                        self._stage_release,
                        runtime_provider,
                        str(runtime_release.get("version") or ""),
                        str(runtime_asset.get("name") or ""),
                        f"{str(msg.get('operationId') or '')}nr",
                        channel="stable",
                    )
                    payload = Path(runtime_package["payload_path"]).resolve(strict=True)
                    runtime_matches = [
                        (payload / Path(str(item.get("relative_path") or ""))).resolve(strict=True)
                        for item in runtime_package.get("files", [])
                        if Path(str(item.get("relative_path") or "")).name.casefold()
                        == "nvngx_dlssnr.dll"
                    ]
                    if len(runtime_matches) != 1 or payload not in runtime_matches[0].parents:
                        raise UnsafeAssetError(
                            "The verified runtime archive must contain exactly one nvngx_dlssnr.dll"
                        )
                    runtime_source = runtime_matches[0]
                    if self.asset_store.pe_architecture(runtime_source) != "x64":
                        raise UnsafeAssetError(
                            "The downloaded DLSS neural-rendering runtime is not a valid x64 DLL"
                        )
                    runtime_file = next(
                        item for item in runtime_package["files"]
                        if Path(str(item.get("relative_path") or "")).name.casefold()
                        == "nvngx_dlssnr.dll"
                    )
                    runtime = await asyncio.to_thread(
                        self.asset_store.import_file,
                        str(runtime_source),
                        expected_sha256=str(runtime_file.get("sha256") or ""),
                        allowed_suffixes=(".dll",),
                        source_metadata={
                            "kind": "community-release",
                            "provider": runtime_provider,
                            "version": runtime_package.get("version"),
                            "archive_sha256": runtime_package.get("archive_sha256"),
                            "publisher_digest_verified": True,
                            "warning": "Community-modified, unsigned NVIDIA-derived runtime",
                        },
                    )
                    # Upgrade every existing NeuralRender profile away from a
                    # stale upstream pin. This makes the bundled fork and the
                    # just-verified dependencies authoritative immediately.
                    profiles_updated = False
                    for saved_profile in self.config.profiles:
                        saved_neural = saved_profile.graphics.neural_render
                        if saved_neural.implementation != "lsp_neural_render":
                            continue
                        saved_neural.package.enabled = True
                        saved_neural.package.version = str(result.get("version") or "") or None
                        saved_neural.runtime_asset_sha256 = runtime["sha256"]
                        saved_profile.graphics.lossless_proxy.enabled = True
                        saved_profile.graphics.lossless_proxy.version = (
                            str(proxy_package.get("version") or "") or None
                        )
                        profiles_updated = True
                    if profiles_updated:
                        await asyncio.to_thread(self.profile_manager.save_config)

                    # If NeuralRender is active, replace its deployed DLLs now
                    # while Automation can safely stop and restart LS. Verify
                    # the transaction rather than treating a caught activation
                    # error as success.
                    active_profile = self.state.current_active_profile
                    active_redeployed = False
                    if (
                        active_profile
                        and active_profile.graphics.neural_render.implementation
                        == "lsp_neural_render"
                        and self._lossless_scaling_found()
                    ):
                        await asyncio.to_thread(
                            self.automation.activate_profile,
                            active_profile,
                            force=True,
                            suppress_auto_scale=True,
                        )
                        desired = self.automation.resolve_deployment_files(active_profile)
                        if self.automation.deployment_manager.needs_change(
                            active_profile.id,
                            desired,
                            lossless_scaling_exe=self.config.lossless_scaling_exe_path,
                        ):
                            raise RuntimeError(
                                "NeuralRender was downloaded but the active installation could not be replaced"
                            )
                        active_redeployed = True
                    await websocket.send(json.dumps({
                        "type": "NEURAL_RENDER_BUNDLE_STAGED",
                        "package": result,
                        "proxyPackage": proxy_package,
                        "runtime": runtime,
                        "profilesUpdated": profiles_updated,
                        "activeProfileRedeployed": active_redeployed,
                    }))
                    return
                updated_profiles = set()
                for profile in self.config.profiles:
                    component = getattr(profile.graphics, provider.replace("-", "_"))
                    if component.enabled or (provider == "reshade" and profile.reshade and profile.reshade.enabled):
                        if automatic and component.version and not newer_addon_version(component.version, release_version):
                            continue
                        component.version = str(result.get("version") or "") or None
                        updated_profiles.add(profile.id)
                await asyncio.to_thread(self.profile_manager.save_config)
                active = self.state.current_active_profile
                if active and active.id in updated_profiles:
                    # Do not interrupt a running game just to refresh an injector.
                    # Activation after scaling stops will apply the new files.
                    getattr(active.graphics, provider.replace("-", "_")).version = str(result.get("version") or "") or None
                    self.automation.addon_configs.mark_edited(active)
                await websocket.send(json.dumps({"type": "GRAPHICS_RELEASE_STAGED", "package": result}))
                return

            if msg_type in {"GET_COMPANION_UPDATE", "CHECK_COMPANION_UPDATE"}:
                force = msg_type == "CHECK_COMPANION_UPDATE"
                if self.companion_update_status is None or force:
                    await self._refresh_companion_update(force=force)
                else:
                    await websocket.send(json.dumps(self.companion_update_status))
                return

            if msg_type == "DOWNLOAD_COMPANION_UPDATE":
                version = str(msg.get("version") or "")
                result = await self._download_companion_update(websocket, version)
                await websocket.send(json.dumps(result))
                await self._refresh_companion_update(force=False)
                return

            if msg_type == "APPLY_COMPANION_UPDATE":
                version = str(msg.get("version") or "")
                result = await self._download_companion_update(websocket, version)
                await websocket.send(json.dumps(result))
                await websocket.send(json.dumps({
                    "type": "COMPANION_INSTALLER_STARTING", "version": version,
                }))
                launched = await self._launch_companion_installer(version, automatic=websocket is self._automatic_update_client)
                await websocket.send(json.dumps(launched))
                return

            if msg_type == "INSTALL_COMPANION_UPDATE":
                result = await self._launch_companion_installer(str(msg.get("version") or ""))
                await websocket.send(json.dumps(result))
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
                if self._gpu_calibration_task and not self._gpu_calibration_task.done():
                    raise RuntimeError("GPU calibration is already running")
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
                    active_profile = self.state.current_active_profile
                    if (
                        requested_id
                        and active_profile
                        and active_profile.id == requested_id
                        and self.config.addon_manager_enabled
                        and "addon_config" not in prof_data
                    ):
                        # The embedded Echo panel writes the live config. Pull
                        # it before validating the browser's autosave payload
                        # so a stale page copy can never overwrite native edits.
                        await asyncio.to_thread(
                            self.automation.addon_configs.capture,
                            active_profile,
                            self.config.lossless_scaling_exe_path,
                        )
                        prof_data["addon_config"] = json.loads(
                            json.dumps(active_profile.addon_config)
                        )
                        addons = active_profile.addon_config.get("addons", {})
                        neural_enabled = bool(
                            isinstance(addons.get("DLSSNR02"), dict)
                            and addons["DLSSNR02"].get("_enabled")
                        )
                        graphics = dict(prof_data.get("graphics") or {})
                        neural = dict(graphics.get("neural_render") or {})
                        package = dict(neural.get("package") or {})
                        package["enabled"] = neural_enabled
                        neural["package"] = package
                        neural["implementation"] = (
                            "lsp_neural_render" if neural_enabled else "disabled"
                        )
                        graphics["neural_render"] = neural
                        prof_data["graphics"] = graphics
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
                    previous = self.profile_manager.get_profile_by_id(prof.id)
                    inventory = await asyncio.to_thread(self.automation.refresh_gpu_selections)
                    self.automation.gpu_selections.capture_profile(prof, inventory, previous)
                    await asyncio.to_thread(self.automation.graphics_resolver.select_neural_runtime, prof)
                    if previous and previous.is_default:
                        # This helper profile is an alias for LS's first/native
                        # profile. Its display name and native identity are not
                        # user-editable and must never become a new XML profile.
                        prof.is_default = True
                        prof.name = GAME_DEFAULT_PROFILE_NAME
                        prof.auto_scale = False
                        prof.target_process = None
                        prof.target_executable_path = None
                        prof.target_processes = []
                        prof.target_executable_paths = []
                        prof.target_domain = None
                        prof.lossless_profile_title = previous.lossless_profile_title
                        prof.lossless_profile_path = previous.lossless_profile_path
                    elif prof.is_default:
                        raise ValueError("Only the native Lossless Scaling profile may be the default")
                    has_target = bool(
                        prof.is_default
                        or prof.target_domain
                        or prof.target_process
                        or prof.target_executable_path
                        or prof.target_processes
                        or prof.target_executable_paths
                    )
                    if not prof.is_default:
                        prof.lossless_profile_title = prof.name if has_target else None
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
                    if previous:
                        calibration_changed = (
                            previous.target_process != prof.target_process
                            or previous.target_executable_path != prof.target_executable_path
                            or previous.target_processes != prof.target_processes
                            or previous.target_executable_paths != prof.target_executable_paths
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
                    if prof.rtss.enabled and has_target:
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
                    native_settings_changed = bool(
                        prof.lossless_profile_title
                        and (
                            previous is None
                            or previous.lossless_profile_title != prof.lossless_profile_title
                            or previous.native_scaling_settings != prof.native_scaling_settings
                        )
                    )
                    if native_settings_changed:
                        if (
                            self.config.lossless_control_configured
                            and self.config.disable_native_auto_scale
                        ):
                            prof.native_scaling_settings["AutoScale"] = "false"
                        prof.last_imported_hash = None
                    if prof.is_default:
                        self.config.default_addon_config = json.loads(
                            json.dumps(prof.addon_config)
                        )
                    self.profile_manager.add_or_update_profile(prof)
                    reshade_enabled = bool(
                        prof.graphics.reshade.enabled
                        or (prof.reshade and prof.reshade.enabled)
                    )
                    await asyncio.to_thread(
                        self.automation.addon_configs.materialize,
                        prof,
                        reshade_enabled=reshade_enabled,
                        hotkey=self.config.reshade_overlay_hotkey,
                    )
                    await asyncio.to_thread(self.profile_manager.save_config)
                    logger.info(f"Saved profile: {prof.name}")
                    if previous is None or previous != prof:
                        if previous is None or previous.addon_config != prof.addon_config:
                            self.automation.addon_configs.mark_edited(prof)
                        await asyncio.to_thread(
                            self.automation.apply_saved_profile,
                            prof,
                            native_settings=self.ls_settings if native_settings_changed else None,
                            template_title=(
                                previous.lossless_profile_title
                                if previous and previous.lossless_profile_title
                                else next((
                                    item.lossless_profile_title for item in self.config.profiles
                                    if item.is_default and item.lossless_profile_title
                                ), None)
                            ),
                        )
                    await websocket.send(json.dumps({
                        "type": "PROFILE_SAVED",
                        "profile": prof.model_dump(mode="json"),
                        "requestId": msg.get("requestId"),
                        "restartPending": self.automation.profile_reload_pending(prof.id),
                    }))
                    await self.broadcast_full_data_update()
                return

            if msg_type == "DELETE_PROFILE":
                prof_id = msg.get("profileId")
                if prof_id:
                    selected_profile = self.profile_manager.get_profile_by_id(prof_id)
                    if selected_profile is None:
                        raise ValueError("Unknown profile")
                    if selected_profile.is_default:
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

        except ConnectionClosed:
            return
        except Exception as e:
            logger.error("Request %s failed: %s", msg.get("type") if isinstance(msg, dict) else "invalid message", e, exc_info=True)
            try:
                context = msg if isinstance(msg, dict) else {}
                profile_data = context.get("profile") or {}
                details = validation_issues(e, profile_data, profile_id=profile_data.get("id")) if (
                    isinstance(e, ValidationError) and isinstance(profile_data, dict) and context.get("type") == "SAVE_PROFILE"
                ) else []
                await websocket.send(json.dumps({"type": "ERROR", "message": str(e),
                    "code": "DASHBOARD_CAPABILITY_REQUIRED" if isinstance(e, PermissionError) and str(e) == "Dashboard capability token required" else None,
                    "action": context.get("type"), "profileId": profile_data.get("id") if isinstance(profile_data, dict) else None,
                    "requestId": context.get("requestId"), "validationErrors": details}))
            except Exception:
                pass
            return e

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
        port = self.config.port
        if self.server and self.server.sockets:
            port = self.server.sockets[0].getsockname()[1]
        return origin in {
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
            f"http://[::1]:{port}",
        }

    async def process_http_request(self, path, request_headers):
        """Serve the dashboard from the WebSocket server's trusted origin."""
        parsed = urlsplit(path)
        request_path = parsed.path
        if request_path.startswith("/profile-icon/") and request_path.endswith(".png"):
            token = parse_qs(parsed.query).get("token", [""])[0]
            if not token.isascii() or not secrets.compare_digest(token, self.dashboard_token):
                return HTTPStatus.FORBIDDEN, [("Content-Type", "text/plain")], b"Forbidden"
            profile_id = request_path[len("/profile-icon/"):-4]
            profile = self.profile_manager.get_profile_by_id(profile_id)
            body = await asyncio.to_thread(
                self.profile_icons.png, profile.model_copy(), self.config.lossless_scaling_exe_path,
            ) if profile else None
            if not body:
                return HTTPStatus.NOT_FOUND, [("Content-Type", "text/plain"),
                                              ("Cache-Control", "private, max-age=30")], b"No application icon"
            return HTTPStatus.OK, [
                ("Content-Type", "image/png"),
                ("Content-Length", str(len(body))),
                ("Cache-Control", "private, max-age=300"),
                ("X-Content-Type-Options", "nosniff"),
            ], body
        if request_path == "/diagnostics/download":
            token = parse_qs(parsed.query).get("token", [""])[0]
            archive_path = self._last_diagnostics_archive
            if not secrets.compare_digest(token, self.dashboard_token):
                return HTTPStatus.FORBIDDEN, [("Content-Type", "text/plain")], b"Forbidden"
            diagnostics_root = self.diagnostic_runner.root.resolve()
            if (
                not archive_path
                or archive_path.parent != diagnostics_root
                or not archive_path.is_file()
            ):
                return HTTPStatus.NOT_FOUND, [("Content-Type", "text/plain")], b"No diagnostic archive"
            body = archive_path.read_bytes()
            return HTTPStatus.OK, [
                ("Content-Type", "application/zip"),
                ("Content-Length", str(len(body))),
                ("Content-Disposition", f'attachment; filename="{archive_path.name}"'),
                ("Cache-Control", "no-store"),
                ("X-Content-Type-Options", "nosniff"),
            ], body
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
                f"connect-src 'self' ws://127.0.0.1:{self.config.port} "
                f"ws://localhost:{self.config.port} ws://[::1]:{self.config.port}",
            ),
        ], body

    async def _run_clone_rebuild(self) -> None:
        try:
            result = await self._run_background_thread(self.automation.rebuild_cloned_installation)
            self.clone_rebuild_status = {"busy": False, "success": True, **result}
        except Exception as error:
            logger.exception("Requested clone rebuild failed")
            self.clone_rebuild_status = {"busy": False, "success": False, "message": str(error)}
        finally:
            with self._gpu_activity_lock:
                self._active_requests -= 1
            self._active_mutations -= 1
        payload = json.dumps({"type": "CLONE_REBUILD_STATUS", **self.clone_rebuild_status})
        await asyncio.gather(*(client.send(payload) for client in tuple(self.clients)), return_exceptions=True)
        self.state.publish_change()

    async def _run_diagnostics(self, selected_test: Optional[str] = None) -> None:
        try:
            result = await self._run_background_thread(
                self.diagnostic_runner.run, selected_test
            )
            archive = result.pop("archivePath", None)
            self._last_diagnostics_archive = Path(archive).resolve() if archive else None
            self.last_diagnostics_result = result
        except Exception as error:
            logger.exception("Application diagnostic run failed")
            self.last_diagnostics_result = {
                "type": "DIAGNOSTICS_RESULT",
                "running": False,
                "error": str(error),
                "results": [],
                "selectedTest": selected_test,
                "availableTests": self.diagnostic_runner.catalog(),
            }
        finally:
            self.diagnostics_task = None
            self._diagnostics_selected_test = None
        if self.clients:
            encoded = json.dumps(self.last_diagnostics_result)
            await asyncio.gather(
                *(client.send(encoded) for client in list(self.clients)),
                return_exceptions=True,
            )

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
            delay_ms = self.config.global_hotkey.activation_delay_ms
            logger.info(f"Triggering Lossless Scaling for fullscreen enter in {delay_ms}ms...")
            # Run delay asynchronously without blocking the event loop
            await asyncio.sleep(delay_ms / 1000.0)
            
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
        if str(left).startswith("build-") or str(right).startswith("build-"):
            return addon_version_key(left) == addon_version_key(right)
        def normalize(value: str):
            text = str(value or "").strip().casefold()
            text = re.sub(r"^sk[_-]", "", text).replace("_", ".").removeprefix("v")
            if re.fullmatch(r"\d+(?:\.\d+)*", text):
                numbers = [int(part) for part in text.split(".")]
                while len(numbers) > 1 and numbers[-1] == 0:
                    numbers.pop()
                return tuple(numbers)
            return text

        return bool(normalize(left)) and normalize(left) == normalize(right)

    async def _refresh_addon_updates(self, *, force: bool) -> Dict:
        async with self.addon_update_lock:
            providers = ("ls-addon-manager", "reshade", "special-k")
            # Cards need release metadata on first load, including backends
            # which are optional and are not part of automatic installation.
            check_providers = (*providers, "sm86-sr", "amd-neural-render-runtime", "display-commander")

            async def check(provider: str):
                try:
                    releases = await self._run_background_thread(
                        self.release_manager.check,
                        provider,
                        channel="stable",
                        force=force,
                    )
                    return provider, (releases[0] if releases else None)
                except Exception as error:
                    # A single provider's site being unreachable or changing
                    # markup must not spam a full traceback into the log on
                    # every daily check; one line is enough.
                    logger.warning("Daily add-on update check failed for %s: %s", provider, error)
                    return provider, None

            checked, sources = await asyncio.gather(
                asyncio.gather(*(check(provider) for provider in check_providers)),
                self._run_background_thread(GraphicsSourceDetector.status),
            )
            latest = {provider: release for provider, release in checked if release}
            packages = self.asset_store.list_packages()
            installed = {
                provider: [str(item.get("version") or "") for item in packages if item.get("provider") == provider]
                for provider in providers
            }
            # Staged packages and external installers are fallbacks for an
            # unselected integration, never evidence that a pinned profile was updated.
            selected = {provider: [] for provider in providers}
            if self.config.addon_manager.version:
                selected["ls-addon-manager"].append(self.config.addon_manager.version)
            for profile in self.config.profiles:
                for provider in ("reshade", "special-k"):
                    component = getattr(profile.graphics, provider.replace("-", "_"))
                    if component.enabled or (provider == "reshade" and profile.reshade and profile.reshade.enabled):
                        # Match the resolver's selection for an unpinned profile.
                        version = component.version or max(installed[provider], default="")
                        if version:
                            selected[provider].append(version)
            source_versions = {
                "reshade": str(sources.get("reshade", {}).get("version") or ""),
                "special-k": str(sources.get("special-k", {}).get("version") or ""),
            }
            updates = []
            labels = {
                "ls-addon-manager": "Companion Addon Host",
                "lsp-neural-render": "LSP-NeuralRender",
                "reshade": "ReShade Full Add-On",
                "special-k": "Special K",
            }
            for provider in providers:
                release = latest.get(provider)
                if not release:
                    continue
                latest_version = str(release.get("version") or "")
                current_versions = selected[provider] or (
                    [max(installed[provider])] if installed[provider] else []
                )
                if not current_versions and source_versions.get(provider):
                    current_versions = [source_versions[provider]]
                outdated = [current for current in current_versions
                            if not self._same_addon_version(current, latest_version)
                            and not newer_addon_version(latest_version, current)]
                if not outdated:
                    continue
                updates.append({
                    "provider": provider,
                    "name": labels[provider],
                    "currentVersion": next(
                        (current for current in outdated if newer_addon_version(current, latest_version)),
                        outdated[0],
                    ),
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
                "releaseUrls": {
                    provider: str(release.get("html_url") or "")
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

    async def _gpu_index_loop(self) -> None:
        await asyncio.sleep(5)
        while True:
            await self._run_background_thread(self.gpu_index_refresh.check)
            await asyncio.sleep(30)

    async def _addon_update_loop(self) -> None:
        await asyncio.sleep(30)
        force = False
        while True:
            if time.monotonic() >= self._next_update_check:
                try:
                    await self._refresh_addon_updates(force=force)
                    await self._refresh_companion_update(force=force)
                except Exception as error:
                    logger.warning("Scheduled update check failed: %s", error)
                self._next_update_check = time.monotonic() + self.config.update_check_interval_hours * 3600
                force = True
            try:
                await self.automatic_updates.run_pending()
            except Exception:
                logger.exception("Automatic update failed")
            await asyncio.sleep(30)

    async def _launch_companion_installer(self, version: str, *, automatic: bool = False) -> Dict:
        if self.component_operation_status:
            self.component_operation_status.update(phase='installing', receivedBytes=0, totalBytes=0)
            await self._broadcast_component_operation()
        kwargs = {"automatic": True} if automatic else {}
        result = await self._run_background_thread(self.companion_updater.launch_installer, version, **kwargs)
        self._update_handoff = True
        # The WebView2 helper may disconnect as soon as setup starts. Once the
        # installer owns the handoff, shutdown must not depend on a socket send.
        if self.on_installer_launched:
            asyncio.get_running_loop().call_soon(self.on_installer_launched)
        return result

    async def _refresh_companion_update(self, *, force: bool) -> Dict:
        async with self.companion_update_lock:
            try:
                status = await self._run_background_thread(
                    self.companion_updater.check, force=force
                )
            except Exception as error:
                logger.warning("Companion update check failed: %s", error)
                status = {
                    "type": "COMPANION_UPDATE_STATUS",
                    "currentVersion": current_version(),
                    "latestVersion": None,
                    "updateAvailable": False,
                    "downloaded": False,
                    "error": str(error),
                }
            self.companion_update_status = status
            if self.clients:
                encoded = json.dumps(status)
                await asyncio.gather(
                    *(client.send(encoded) for client in list(self.clients)),
                    return_exceptions=True,
                )
            return status

    async def _download_companion_update(
        self, websocket: WebSocketServerProtocol, version: str
    ) -> Dict:
        """Download an update without leaving the dashboard silently waiting."""
        await websocket.send(json.dumps({
            "type": "COMPANION_UPDATE_DOWNLOAD_STARTED", "version": version,
        }))
        loop = asyncio.get_running_loop()
        progress: asyncio.Queue[tuple[int, int]] = asyncio.Queue()

        def report_progress(received: int, total: int) -> None:
            loop.call_soon_threadsafe(progress.put_nowait, (received, total))
            if self._component_progress_callback:
                self._component_progress_callback({"phase": "downloading", "receivedBytes": received, "totalBytes": total})

        task = asyncio.create_task(self._run_background_thread(
            self.companion_updater.download,
            version,
            progress_callback=report_progress,
        ))
        try:
            while not task.done():
                try:
                    received, total = await asyncio.wait_for(progress.get(), timeout=0.25)
                except asyncio.TimeoutError:
                    continue
                while not progress.empty():
                    received, total = progress.get_nowait()
                await websocket.send(json.dumps({
                    "type": "COMPANION_UPDATE_DOWNLOAD_PROGRESS",
                    "version": version,
                    "receivedBytes": received,
                    "totalBytes": total,
                }))
        finally:
            if not task.done():
                await asyncio.shield(task)
        return await task

    async def apply_companion_update(self, version: str) -> Dict:
        """Download, verify, and launch an update requested outside the dashboard."""
        if self.component_install_lock.locked() or self.state.update_in_progress:
            raise RuntimeError("Another component download/install is in progress")
        if self._editing_clients or self._active_mutations:
            raise RuntimeError("Close profile editors and wait for settings to save before updating Companion")
        async with self.component_install_lock:
            if not await self._run_background_thread(self.automation.begin_update):
                raise RuntimeError("Wait for scaling to stop before updating Companion")
            self.component_operation_status = {
                "type": "COMPONENT_OPERATION", "provider": "companion", "busy": True,
                "phase": "checking", "receivedBytes": 0, "totalBytes": 0,
            }
            await self._broadcast_component_operation()
            server = self

            class BroadcastProgress:
                async def send(self, encoded):
                    message = json.loads(encoded)
                    server.component_operation_status.update(
                        phase="downloading", receivedBytes=message.get("receivedBytes", 0),
                        totalBytes=message.get("totalBytes", 0),
                    )
                    await server._broadcast_component_operation()

            try:
                normalized = str(version or "").strip().removeprefix("v")
                status = self.companion_update_status or await self._refresh_companion_update(force=True)
                if not status.get("updateAvailable") or status.get("latestVersion") != normalized:
                    raise ValueError("The requested companion update is no longer available")
                if not status.get("downloaded"):
                    await self._download_companion_update(BroadcastProgress(), normalized)
                self.component_operation_status.update(phase="installing", receivedBytes=0, totalBytes=0)
                await self._broadcast_component_operation()
                launched = await self._launch_companion_installer(normalized)
                self.component_operation_status["phase"] = "complete"
            except Exception as error:
                self.component_operation_status.update(phase="failed", error=str(error))
                raise
            finally:
                self.component_operation_status["busy"] = self._update_handoff
                if self._update_handoff:
                    self.component_operation_status["phase"] = "restarting"
                else:
                    await self._run_background_thread(self.automation.end_update)
                await self._broadcast_component_operation()
        return launched

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
        try:
            from .test_binaries import candidate as _test_binary

            candidates.append(_test_binary("tools/LSBenchmark.exe"))
        except Exception:
            pass
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
        self._load_latest_benchmark_result()
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
            "--duration", "30",
            "--warmup", "5",
            "--report-dir", str(self._benchmark_report_root()),
            "--use-default-profile",
            "--yes",
        ]

    async def _run_gpu_calibration(self, workload: Path) -> None:
        """Run benchmark-workload GPU calibration in the background.

        Progress and the final summary go out as GPU_CALIBRATION_STATUS
        broadcasts; a full data update at the end refreshes the GPU
        dropdown and routing warnings with the newly persisted map.
        """
        loop = asyncio.get_running_loop()

        def _broadcast(payload: Dict) -> None:
            encoded = json.dumps(payload)
            asyncio.ensure_future(asyncio.gather(
                *(client.send(encoded) for client in self.clients),
                return_exceptions=True,
            ))

        def progress(stage: str, detail: str = "") -> None:
            loop.call_soon_threadsafe(_broadcast, {
                "type": "GPU_CALIBRATION_STATUS",
                "running": True,
                "stage": stage,
                "detail": detail,
            })

        result = None
        error_message = None
        try:
            result = await asyncio.to_thread(
                self.automation.calibrate_gpu_map, workload, progress=progress
            )
        except Exception as error:
            error_message = str(error)
            logger.exception("GPU calibration failed")
        finally:
            self.state.benchmark_mode_active = False
            self._gpu_calibration_task = None
        if self.process_watcher:
            self.process_watcher.invalidate_foreground_cache()
        _broadcast({
            "type": "GPU_CALIBRATION_STATUS",
            "running": False,
            "result": result,
            "error": error_message,
        })
        await self.broadcast_full_data_update()

    def _benchmark_report_root(self) -> Path:
        return Path(self.profile_manager.config_dir) / "benchmarks"

    def _load_latest_benchmark_result(self) -> None:
        try:
            reports = list(self._benchmark_report_root().glob("*/result.json"))
            if not reports:
                return
            latest = max(reports, key=lambda path: path.stat().st_mtime_ns)
            if latest == self._last_benchmark_report_path:
                return
            payload = json.loads(latest.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                self._last_benchmark_result = payload
                self._last_benchmark_report_path = latest
        except (OSError, json.JSONDecodeError):
            logger.debug("Could not read the latest benchmark result", exc_info=True)

    def _benchmark_result_summary(self, report: Optional[Dict]) -> Optional[Dict]:
        if not report:
            return None
        captures = report.get("presentmon_captures") or []
        output_pid = report.get("lossless_scaling_pid")
        source_pid = report.get("benchmark_pid")
        output = next(
            (item for item in captures if item.get("process_id") == output_pid),
            None,
        )
        source = next(
            (item for item in captures if item.get("process_id") == source_pid),
            None,
        )
        selected = output or source or (captures[0] if captures else None)
        metrics = (selected or {}).get("metrics") or {}
        fps = (metrics.get("displayed_fps") or {}).get("average")

        software_latency = report.get("software_visible_latency_ms") or {}
        latency = software_latency.get("mean")
        latency_label = "Average input-to-visible latency"
        latency_scope = "software-visible"
        if latency is None:
            input_metrics = (source or {}).get("metrics") or {}
            latency = (input_metrics.get("all_input_to_visible_ms") or {}).get("mean")
            latency_scope = "presentmon-input-to-visible"
        if latency is None:
            latency = (metrics.get("display_latency_ms") or {}).get("mean")
            latency_label = "Average display latency"
            latency_scope = "presentmon-display"
        return {
            "averageFps": fps,
            "averageLatencyMs": latency,
            "latencyLabel": latency_label,
            "latencyScope": latency_scope if latency is not None else None,
            "captureProcess": (selected or {}).get("application"),
            "durationSeconds": report.get("duration_seconds"),
            "reportPath": str(self._last_benchmark_report_path or ""),
        }

    def _benchmark_status_payload(self) -> Dict:
        if self._last_benchmark_result is None:
            self._load_latest_benchmark_result()
        workload = self._embedded_benchmark_executable()
        managed_presentmon = self._verified_benchmark_executable("presentmon")
        presentmon = managed_presentmon or self._presentmon_executable()
        running = bool(self.benchmark_task and not self.benchmark_task.done())
        return {
            "type": "BENCHMARK_STATUS",
            "ready": bool(workload and presentmon),
            "running": running,
            "result": self._benchmark_result_summary(self._last_benchmark_result),
            "tools": {
                "workload": {"ready": bool(workload)},
                "presentmon": {
                    "ready": bool(presentmon),
                    "source": "managed" if managed_presentmon else "detected" if presentmon else None,
                },
            },
        }

    def _open_overlay_addon_panel(self) -> bool:
        if not self.proxy_menu or not self.config.addon_manager_enabled:
            return False
        status = addon_runtime_status(self.config.lossless_scaling_exe_path)
        if not status.get("available") or status.get("headless") is not False:
            self.automation.overlay_indicator.log("Update the addon host and restart Lossless Scaling to open its panel", kind="info")
            return False
        result = self.proxy_menu.open(status["pid"])
        if not result.get("opened"):
            self.automation.overlay_indicator.log(result.get("reason", "Addon panel is unavailable"), kind="info")
        return bool(result.get("opened"))

    def _profile_editor_data(self, profile: Profile) -> Dict:
        data = profile.model_dump(mode="json")
        data["addon_config"] = self.automation.addon_configs.effective(
            profile,
            reshade_enabled=bool(profile.graphics.reshade.enabled or (profile.reshade and profile.reshade.enabled)),
            hotkey=self.config.reshade_overlay_hotkey,
        )
        return data

    def _profile_summaries(self) -> list[Dict]:
        return [{
            "id": profile.id,
            "name": profile.name,
            "is_default": profile.is_default,
            "target_domain": profile.target_domain,
            "target_process": profile.target_process,
            "target_executable_path": profile.target_executable_path,
            "target_processes": profile.target_processes,
            "target_executable_paths": profile.target_executable_paths,
            "iconVersion": self.profile_icons.version(profile, self.config.lossless_scaling_exe_path),
            "rtss": {
                "enabled": profile.rtss.enabled,
                "framerate_limit": profile.rtss.framerate_limit,
                "learned_framerate_limit": profile.rtss.learned_framerate_limit,
            },
        } for profile in self.profile_manager.config.profiles]

    def _active_profile_status(self) -> Optional[Dict]:
        return self._active_profile_status_for(self.state.current_active_profile)

    @staticmethod
    def _active_profile_status_for(profile) -> Optional[Dict]:
        if profile is None:
            return None
        return {
            "id": profile.id,
            "name": profile.name,
            "graphics": {"neural_render": {
                "implementation": profile.graphics.neural_render.implementation,
            }},
            "reshade": {"menu_proxy_enabled": profile.reshade.menu_proxy_enabled}
            if profile.reshade else None,
        }

    async def send_full_data_update(self, websocket: WebSocketServerProtocol, visible_windows_only: bool = True) -> None:
        await websocket.send(await self._full_data_payload(visible_windows_only))

    async def broadcast_full_data_update(self, visible_windows_only: bool = True) -> None:
        if not self.clients:
            return
        payload = await self._full_data_payload(visible_windows_only)
        await asyncio.gather(*[client.send(payload) for client in tuple(self.clients)], return_exceptions=True)

    async def _full_data_payload(self, visible_windows_only):
        def collect():
            return {
                **self._notifications_payload(),
                "processes": self.process_watcher.list_running_executables(
                    visible_windows_only=visible_windows_only) if self.process_watcher else [],
                "profiles": self._profile_summaries(),
                "activeProfileId": self.config.active_profile_id,
                "controlSettings": self._control_settings_payload(),
            }
        metadata = await self._run_background_thread(collect)
        # Capture runtime last, after slow enumeration, so a full refresh cannot
        # overwrite a more recent live scaling update with old status.
        return json.dumps({"type": "FULL_DATA_UPDATE", **metadata, **self._runtime_payload()})

    def _notifications_payload(self) -> Dict:
        try:
            recovery = self.automation.deployment_manager.recovery_status(self.config.lossless_scaling_exe_path)
        except (OSError, ValueError, RuntimeError) as error:
            recovery = {"message": str(error), "retryAvailable": False}
        return {"notifications": {
            "settingsIssues": self.profile_manager.validation_issues,
            "repairSession": self.profile_manager.repair_session,
            "restartRequired": self.profile_manager.repair_session and not self.profile_manager.validation_issues,
            "deploymentRecovery": recovery,
        }}

    def _control_settings_payload(self) -> Dict:
        selected_process_lasso_log = ProcessLassoLogTailer.resolve_path(
            self.config.process_lasso_log_path
        )
        process_lasso_detected = bool(
            selected_process_lasso_log and selected_process_lasso_log.is_file()
        )
        gpu_inventory = self.automation.refresh_gpu_selections()
        displayed_override_hotkey = self.config.override_hotkey
        if not self.config.override_lossless_hotkey:
            displayed_override_hotkey = (
                self.ls_settings.read_hotkey() or self.config.global_hotkey
            )
        return {
            "smartAutoScaleEnabled": self.config.disable_native_auto_scale,
            "notifyOnUpdates": self.config.notify_on_updates,
            "automaticallyInstallUpdates": self.config.automatically_install_updates,
            "snapNearFullscreenWindowsToMonitor": (
                self.config.snap_near_fullscreen_windows_to_monitor
            ),
            "minimizeOtherWindowsOnScale": self.config.minimize_other_windows_on_scale,
        "overlayIndicatorEnabled": self.config.overlay_indicator_enabled,
        "overlayHotkeys": dict(self.config.overlay_hotkeys),
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
            "rtssDefaultStaticFramerateLimit": self.config.rtss_default_static_framerate_limit,
            "rtssDefaultGpuTargetPercent": self.config.rtss_default_gpu_target_percent,
            "defaultProfileAutoScale": self.config.default_profile_auto_scale,
            "gpuInventory": gpu_inventory,
            "hasNvidiaGpu": bool(gpu_inventory.get("hasNvidia")),
            "preferredScalingGpuDeviceId": self.config.preferred_scaling_gpu_device_id or "",
            "preferredScalingGpuName": self.config.preferred_scaling_gpu_name or "",
            "gpuRoutingWarnings": self.automation.gpu_selections.warnings,
            "autoRouteGpuToDisplay": self.config.auto_route_gpu_to_display,
            "nvidiaRtxHdrEnabled": self.config.nvidia_rtx_hdr_enabled,
            "reshadeHdr": self.reshade_profiles.hdr_settings_payload(),
            "reshadeOverlayHotkey": self.config.reshade_overlay_hotkey,
            "addonManagerEnabled": self.config.addon_manager_enabled,
            "sunshineMatchEncoderGpu": self.config.sunshine_match_encoder_gpu,
            "gloamHdrEnabled": self.config.gloam_hdr_enabled,
            "gloamHdrStatus": self.automation.gloam_integration.snapshot(),
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
        InputSimulator.vk_from_string(requested.key)
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
        payload = json.dumps({"type": "STATE_UPDATE", **self._runtime_payload()})
        await asyncio.gather(*[client.send(payload) for client in tuple(self.clients)], return_exceptions=True)

    def _runtime_payload(self) -> Dict:
        # No disk recovery scans, GPU enumeration, or configuration writes on
        # this path. Those belong to their explicit status/configuration APIs.
        revision, state = self.state.snapshot()
        profile = state["current_active_profile"]
        return {
            "stateRevision": revision,
            "simulationMode": state["simulation_mode"],
            "isScalingActive": state["is_scaling_active"],
            "losslessScalingFound": self._lossless_scaling_found(),
            "losslessScalingRunning": state["lossless_scaling_running"],
            "activeProfile": self._active_profile_status_for(profile),
            "scalingTarget": state["current_scaled_target"],
            "dynamicLimiter": state["dynamic_limiter_status"],
            "scalingControl": state["scaling_control_status"],
            "gpuIndexMapping": state["gpu_mapping_status"],
            "gloamHdrStatus": self.automation.gloam_integration.snapshot(),
        }
