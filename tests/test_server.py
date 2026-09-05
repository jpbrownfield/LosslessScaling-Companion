import asyncio
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from companion.core.profile_manager import ProfileManager
from companion.core.state import AppState
from companion.services.automation import AutomationController
from companion.services.server import CompanionWebSocketServer


class ServerTests(unittest.IsolatedAsyncioTestCase):
    class FakeWebSocket:
        def __init__(self):
            self.messages = []

        async def send(self, message):
            self.messages.append(message)

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.manager = ProfileManager(Path(self.temp_dir.name))
        self.state = AppState()
        self.automation = AutomationController(self.manager, self.state)
        self.server = CompanionWebSocketServer(self.manager, self.state, automation=self.automation)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_origin_allowlist(self):
        self.assertTrue(self.server.is_origin_allowed(None))
        self.assertFalse(self.server.is_origin_allowed("null"))
        self.assertTrue(self.server.is_origin_allowed("chrome-extension://abc123"))
        self.assertTrue(self.server.is_origin_allowed("http://127.0.0.1:24892"))
        self.assertTrue(self.server.is_origin_allowed("http://localhost:24892"))
        self.assertFalse(self.server.is_origin_allowed("https://malicious.example"))

    async def test_dashboard_is_served_over_local_http(self):
        self.manager.config.port = 0
        await self.server.start()
        try:
            port = self.server.server.sockets[0].getsockname()[1]
            response = await asyncio.to_thread(
                urllib.request.urlopen,
                f"http://127.0.0.1:{port}/dashboard",
            )
            body = response.read().decode("utf-8")
            self.assertIn("Lossless Companion Profile Manager", body)
            self.assertIn(self.server.dashboard_token, body)
            self.assertNotIn("__COMPANION_TOKEN__", body)
        finally:
            await self.server.stop()

    async def test_readonly_local_client_cannot_mutate_profiles(self):
        websocket = self.FakeWebSocket()
        initial_count = len(self.manager.config.profiles)

        await self.server.process_message(
            websocket,
            '{"type":"DELETE_PROFILE","profileId":"youtube-profile"}',
        )

        self.assertEqual(len(self.manager.config.profiles), initial_count)
        self.assertIn("Dashboard capability token required", websocket.messages[-1])

    @patch("companion.services.automation.InputSimulator.trigger_hotkey", return_value=True)
    async def test_fullscreen_enter_and_exit_are_idempotent(self, trigger_hotkey):
        profile = self.manager.get_profile_by_id("youtube-profile")
        profile.hotkey.activation_delay_ms = 0
        event = {
            "event": "FULLSCREEN_ENTER",
            "domain": "www.youtube.com",
            "processName": "chrome.exe",
        }
        await self.server.handle_fullscreen_event(event)
        await self.server.handle_fullscreen_event(event)
        self.assertTrue(self.state.is_scaling_active)
        self.assertEqual(trigger_hotkey.call_count, 1)

        event["event"] = "FULLSCREEN_EXIT"
        await self.server.handle_fullscreen_event(event)
        await self.server.handle_fullscreen_event(event)
        self.assertFalse(self.state.is_scaling_active)
        self.assertEqual(trigger_hotkey.call_count, 2)

    @patch("companion.services.automation.InputSimulator.trigger_hotkey", return_value=True)
    async def test_quick_fullscreen_exit_cancels_pending_activation(self, trigger_hotkey):
        profile = self.manager.get_profile_by_id("youtube-profile")
        profile.hotkey.activation_delay_ms = 200
        event = {
            "event": "FULLSCREEN_ENTER",
            "domain": "youtube.com",
            "processName": "chrome.exe",
        }
        self.server.schedule_fullscreen_event(event)
        await asyncio.sleep(0.02)
        self.server.schedule_fullscreen_event({**event, "event": "FULLSCREEN_EXIT"})
        await asyncio.sleep(0.2)
        self.assertFalse(self.state.is_scaling_active)
        trigger_hotkey.assert_not_called()


if __name__ == "__main__":
    unittest.main()
