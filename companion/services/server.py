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

from ..core.models import AppConfig, BrowserFullscreenEvent, HotkeyConfig, Profile
from ..core.profile_manager import ProfileManager
from ..core.state import AppState
from .process_watcher import ProcessWatcher
from .automation import AutomationController
from .ls_settings import LosslessSettingsXml
from .asset_store import AssetStore
from .release_providers import ReleaseManager

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

            if msg_type == "SAVE_CONTROL_SETTINGS":
                hotkey = HotkeyConfig.model_validate(msg.get("hotkey") or {})
                mode = str(msg.get("mode") or "")
                if mode not in {"helper_controls_lossless", "follow_lossless", "warn_only"}:
                    raise ValueError("Unsupported hotkey synchronization mode")
                self.config.global_hotkey = hotkey
                self.config.hotkey_sync_mode = mode
                self.config.disable_native_auto_scale = bool(msg.get("disableNativeAutoScale", True))
                self.profile_manager.save_config()
                synchronized = await asyncio.to_thread(
                    self.process_watcher.enforce_helper_scaling_control
                ) if self.process_watcher else False
                await websocket.send(json.dumps({
                    "type": "CONTROL_SETTINGS_SAVED",
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
                if self.config.disable_native_auto_scale:
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
                    if self.state.current_active_profile and self.state.current_active_profile.id == prof_id:
                        await asyncio.to_thread(self.automation.activate_profile, None)
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

        profile = self.profile_manager.match_profile(process_name=process_name, domain=domain)
        await asyncio.to_thread(
            self.automation.activate_profile,
            profile,
            profile.target_executable_path if profile else None,
        )

        logger.info(f"Received {event_type} from {domain} (matched profile: {profile.name if profile else 'Default'})")

        if not profile:
            return

        if event_type == "FULLSCREEN_ENTER" and profile.auto_scale_on_fullscreen:
            logger.info(f"Triggering Lossless Scaling for fullscreen enter in {profile.hotkey.activation_delay_ms}ms...")
            # Run delay asynchronously without blocking the event loop
            await asyncio.sleep(profile.hotkey.activation_delay_ms / 1000.0)
            
            # Fire hardware hotkey
            await asyncio.to_thread(self.automation.set_scaling, True, profile, reason="fullscreen")
            await self.broadcast_state()

        elif event_type == "FULLSCREEN_EXIT" and profile.auto_scale_on_demaximize:
            logger.info("Demaximize/Fullscreen exit detected. Toggling Lossless Scaling off...")
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
        })
        await asyncio.gather(*[client.send(payload) for client in self.clients], return_exceptions=True)

    def _control_settings_payload(self) -> Dict:
        return {
            "mode": self.config.hotkey_sync_mode,
            "disableNativeAutoScale": self.config.disable_native_auto_scale,
            "hotkey": self.config.global_hotkey.model_dump(),
        }

    async def broadcast_state(self) -> None:
        if not self.clients:
            return
        payload = json.dumps({
            "type": "STATE_UPDATE",
            "isScalingActive": self.state.is_scaling_active,
            "activeProfile": self.state.current_active_profile.model_dump() if self.state.current_active_profile else None,
            "scalingTarget": self.state.current_scaled_target,
        })
        await asyncio.gather(*[client.send(payload) for client in self.clients], return_exceptions=True)
