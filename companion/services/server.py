"""
Async WebSocket server for communication with Chrome Extension and UI clients.
"""

import asyncio
import json
import logging
from typing import Set, Optional
import websockets
from websockets.server import WebSocketServerProtocol

from ..core.models import AppConfig, BrowserFullscreenEvent, Profile
from ..core.profile_manager import ProfileManager
from ..core.state import AppState
from .input_simulator import InputSimulator
from .process_watcher import ProcessWatcher

logger = logging.getLogger("LosslessCompanion.Server")


class CompanionWebSocketServer:
    def __init__(self, profile_manager: ProfileManager, state: AppState, process_watcher: Optional[ProcessWatcher] = None):
        self.profile_manager = profile_manager
        self.state = state
        self.process_watcher = process_watcher
        self.config: AppConfig = profile_manager.config
        self.clients: Set[WebSocketServerProtocol] = set()
        self.server = None

    async def start(self) -> None:
        logger.info(f"Starting WebSocket server on {self.config.host}:{self.config.port}...")
        self.server = await websockets.serve(
            self.handle_client,
            self.config.host,
            self.config.port
        )
        logger.info(f"WebSocket server listening on ws://{self.config.host}:{self.config.port}/ws")

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            logger.info("WebSocket server stopped.")

    async def handle_client(self, websocket: WebSocketServerProtocol, path: str = "") -> None:
        self.clients.add(websocket)
        self.state.connected_clients = len(self.clients)
        logger.info(f"Client connected. Total clients: {self.state.connected_clients}")

        # Send initial handshake / status
        await websocket.send(json.dumps({
            "type": "INITIAL_STATE",
            "isScalingActive": self.state.is_scaling_active,
            "activeProfile": self.state.current_active_profile.model_dump() if self.state.current_active_profile else None
        }))

        try:
            async for message in websocket:
                await self.process_message(websocket, message)
        except websockets.ConnectionClosed:
            pass
        finally:
            self.clients.remove(websocket)
            self.state.connected_clients = len(self.clients)
            logger.info(f"Client disconnected. Total clients: {self.state.connected_clients}")

    async def process_message(self, websocket: WebSocketServerProtocol, raw_message: str) -> None:
        try:
            msg = json.loads(raw_message)
            msg_type = msg.get("type")

            if msg_type == "PING":
                await websocket.send(json.dumps({"type": "PONG", "timestamp": msg.get("timestamp")}))
                return

            if msg_type == "BROWSER_FULLSCREEN_EVENT":
                await self.handle_fullscreen_event(msg)
                return

            if msg_type == "MANUAL_TRIGGER":
                await self.handle_manual_trigger(msg)
                return

            if msg_type == "GET_STATE_AND_PROCESSES":
                visible_only = msg.get("visibleOnly", True)
                await self.send_full_data_update(websocket, visible_windows_only=visible_only)
                return

            if msg_type == "SAVE_PROFILE":
                prof_data = msg.get("profile")
                if prof_data:
                    prof = Profile.model_validate(prof_data)
                    self.profile_manager.add_or_update_profile(prof)
                    logger.info(f"Saved profile: {prof.name}")
                    await self.broadcast_full_data_update()
                return

            if msg_type == "DELETE_PROFILE":
                prof_id = msg.get("profileId")
                if prof_id:
                    self.profile_manager.delete_profile(prof_id)
                    logger.info(f"Deleted profile ID: {prof_id}")
                    await self.broadcast_full_data_update()
                return

        except Exception as e:
            logger.error(f"Error processing message: {e}", exc_info=True)

    async def handle_fullscreen_event(self, data: dict) -> None:
        if not self.state.auto_scale_enabled:
            logger.info("Auto-scaling is disabled globally in companion. Skipping event.")
            return

        event_type = data.get("event")  # FULLSCREEN_ENTER or FULLSCREEN_EXIT
        domain = data.get("domain", "")
        process_name = data.get("processName", "chrome.exe")

        profile = self.profile_manager.match_profile(process_name=process_name, domain=domain)
        self.state.current_active_profile = profile

        logger.info(f"Received {event_type} from {domain} (matched profile: {profile.name if profile else 'Default'})")

        if not profile:
            return

        hotkey = profile.hotkey

        if event_type == "FULLSCREEN_ENTER" and profile.auto_scale_on_fullscreen:
            logger.info(f"Triggering Lossless Scaling for fullscreen enter in {hotkey.activation_delay_ms}ms...")
            # Run delay asynchronously without blocking the event loop
            await asyncio.sleep(hotkey.activation_delay_ms / 1000.0)
            
            # Fire hardware hotkey
            InputSimulator.trigger_hotkey(
                modifiers=hotkey.modifiers,
                key=hotkey.key,
                hold_ms=hotkey.hold_delay_ms,
                activation_delay_ms=0
            )
            self.state.mark_scaling_toggled(True)
            await self.broadcast_state()

        elif event_type == "FULLSCREEN_EXIT" and profile.auto_scale_on_demaximize:
            # If scaling was active, toggle it off
            logger.info("Demaximize/Fullscreen exit detected. Toggling Lossless Scaling off...")
            await asyncio.sleep(0.15)
            InputSimulator.trigger_hotkey(
                modifiers=hotkey.modifiers,
                key=hotkey.key,
                hold_ms=hotkey.hold_delay_ms,
                activation_delay_ms=0
            )
            self.state.mark_scaling_toggled(False)
            await self.broadcast_state()

    async def handle_manual_trigger(self, data: dict) -> None:
        profile = self.state.current_active_profile or self.profile_manager.config.profiles[0]
        hotkey = profile.hotkey
        logger.info(f"Manual scale trigger invoked using profile: {profile.name}")
        InputSimulator.trigger_hotkey(
            modifiers=hotkey.modifiers,
            key=hotkey.key,
            hold_ms=hotkey.hold_delay_ms,
            activation_delay_ms=0
        )
        self.state.mark_scaling_toggled()
        await self.broadcast_state()

    async def send_full_data_update(self, websocket: WebSocketServerProtocol, visible_windows_only: bool = True) -> None:
        procs = self.process_watcher.list_running_executables(visible_windows_only=visible_windows_only) if self.process_watcher else []
        payload = json.dumps({
            "type": "FULL_DATA_UPDATE",
            "processes": procs,
            "profiles": [p.model_dump() for p in self.profile_manager.config.profiles],
            "activeProfileId": self.profile_manager.config.active_profile_id,
            "isScalingActive": self.state.is_scaling_active
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
            "isScalingActive": self.state.is_scaling_active
        })
        await asyncio.gather(*[client.send(payload) for client in self.clients], return_exceptions=True)

    async def broadcast_state(self) -> None:
        if not self.clients:
            return
        payload = json.dumps({
            "type": "STATE_UPDATE",
            "isScalingActive": self.state.is_scaling_active,
            "activeProfile": self.state.current_active_profile.model_dump() if self.state.current_active_profile else None
        })
        await asyncio.gather(*[client.send(payload) for client in self.clients], return_exceptions=True)
