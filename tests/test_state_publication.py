import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock

from websockets.legacy.client import connect

from companion.core.profile_manager import ProfileManager
from companion.core.state import AppState
from companion.services.server import CompanionWebSocketServer


class StatePublicationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.manager = ProfileManager(Path(self.directory.name))
        self.manager.config.port = 0
        self.state = AppState()
        self.server = CompanionWebSocketServer(self.manager, self.state)
        self.server._addon_update_loop = AsyncMock()
        self.server._detect_presentmon_on_startup = AsyncMock()
        self.server._gpu_index_loop = AsyncMock()
        self.server.on_runtime_changed = Mock()
        await self.server.start()
        port = self.server.server.sockets[0].getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        self.socket = await connect(f"ws://127.0.0.1:{port}/ws?token={self.server.dashboard_token}", origin=origin)
        self.initial = json.loads(await self.socket.recv())

    async def asyncTearDown(self):
        await self.socket.close()
        await self.server.stop()
        self.directory.cleanup()

    async def receive(self, message_type):
        async def read():
            while True:
                value = json.loads(await self.socket.recv())
                if value["type"] == message_type:
                    return value
        return await asyncio.wait_for(read(), 0.75)

    async def test_worker_change_pushes_complete_revision_without_monitor(self):
        self.server._notifications_payload = Mock(side_effect=AssertionError("Runtime must not scan disk"))
        def change():
            self.state.lossless_scaling_running = True
            self.state.current_scaled_target = {"processName": "game.exe", "windowTitle": "New title"}
            self.state.is_scaling_active = True
        await asyncio.to_thread(change)
        update = await self.receive("STATE_UPDATE")
        self.assertTrue(update["isScalingActive"])
        self.assertEqual(update["scalingTarget"]["windowTitle"], "New title")
        self.assertGreater(update["stateRevision"], self.initial["stateRevision"])
        self.server.on_runtime_changed.assert_called_once()

    async def test_tray_config_save_pushes_profiles_and_startup(self):
        def save():
            self.manager.config.run_at_startup = True
            self.manager.create_profile_from_process("game.exe", "New Game")
        await asyncio.to_thread(save)
        update = await self.receive("CONFIG_UPDATE")
        self.assertTrue(update["runAtStartup"])
        self.assertIn("New Game", [profile["name"] for profile in update["profiles"]])
        self.server.on_runtime_changed.assert_called()

    async def test_healthcheck_bypasses_a_waiting_gpu_request(self):
        self.state.gpu_mapping_in_progress = True
        await self.socket.send(json.dumps({"type": "GET_PROFILE_SUMMARIES"}))
        await self.socket.send(json.dumps({"type": "PING", "healthcheck": True, "timestamp": 123}))
        try:
            pong = await self.receive("PONG")
            self.assertEqual(pong["timestamp"], 123)
        finally:
            self.state.gpu_mapping_in_progress = False


class AppStateTests(unittest.TestCase):
    def test_unchanged_values_do_not_publish_and_snapshot_is_independent(self):
        state = AppState()
        changed = Mock()
        unsubscribe = state.subscribe(changed)
        state.lossless_scaling_running = True
        state.lossless_scaling_running = True
        state.current_scaled_target = {"processName": "game.exe"}
        revision, snapshot = state.snapshot()
        snapshot["current_scaled_target"]["processName"] = "edited"
        self.assertEqual(state.current_scaled_target["processName"], "game.exe")
        self.assertEqual(changed.call_count, 2)
        unsubscribe()
        state.lossless_scaling_running = False
        self.assertGreater(state.snapshot()[0], revision)
        self.assertEqual(changed.call_count, 2)
