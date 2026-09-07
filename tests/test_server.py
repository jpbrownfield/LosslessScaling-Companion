import asyncio
import tempfile
import unittest
import urllib.request
import json
from pathlib import Path
from unittest.mock import Mock, patch

from companion.core.profile_manager import ProfileManager
from companion.core.state import AppState
from companion.services.automation import AutomationController
from companion.services.server import CompanionWebSocketServer
from companion.services.process_watcher import ProcessInfo
from companion.core.models import RtssLimiterConfig
from companion.services.ls_settings import LosslessSettingsXml


class FakeRtssManager:
    def __init__(self):
        self.configured_path = None
        self.removed = []
        self.applied = []

    def status(self, configured_path=None):
        return {"installed": True, "path": "C:/RTSS", "running": True, "downloadUrl": "https://example.test"}

    @staticmethod
    def _target_process(profile):
        return profile.target_process

    def apply_profile(self, profile, previous=None, configured_path=None, framerate_limit=None):
        profile.rtss.managed_target_process = profile.target_process
        profile.rtss.managed_install_path = configured_path or "C:/RTSS"
        self.applied.append(profile.id)

    def remove_profile(self, profile, configured_path=None):
        self.removed.append(profile.id)
        return True

    @staticmethod
    def clear_metadata(profile):
        profile.rtss.managed_target_process = None


class FakeStartupManager:
    TASK_NAME = "LosslessScalingHelper"

    def is_enabled(self):
        return False

    def set_enabled(self, enabled):
        return {"enabled": enabled, "taskName": self.TASK_NAME}


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

    async def test_rtss_profile_delete_requires_confirmation(self):
        fake_rtss = FakeRtssManager()
        self.server.rtss_manager = fake_rtss
        profile = self.manager.get_profile_by_id("youtube-profile")
        profile.rtss = RtssLimiterConfig(enabled=True, framerate_limit=70)
        self.manager.save_config()
        websocket = self.FakeWebSocket()
        self.server.client_authority[websocket] = "dashboard"

        await self.server.process_message(
            websocket,
            json.dumps({"type": "DELETE_PROFILE", "profileId": profile.id}),
        )
        self.assertIsNotNone(self.manager.get_profile_by_id(profile.id))
        self.assertIn("Confirm deletion", websocket.messages[-1])

        await self.server.process_message(
            websocket,
            json.dumps({
                "type": "DELETE_PROFILE",
                "profileId": profile.id,
                "confirmRtssRemoval": True,
            }),
        )
        self.assertIsNone(self.manager.get_profile_by_id(profile.id))
        self.assertEqual(fake_rtss.removed, [profile.id])

    async def test_global_rtss_switch_suspends_and_reapplies_profile_settings(self):
        fake_rtss = FakeRtssManager()
        self.server.rtss_manager = fake_rtss
        self.server.startup_manager = FakeStartupManager()
        profile = self.manager.get_profile_by_id("youtube-profile")
        profile.rtss = RtssLimiterConfig(
            enabled=True,
            framerate_limit=70,
            limit_method="front_edge_sync",
            managed_target_process=profile.target_process,
            managed_install_path="C:/RTSS",
        )
        self.manager.config.rtss_frame_limiting_enabled = True
        self.manager.config.rtss_install_path = "C:/RTSS"
        self.manager.save_config()
        websocket = self.FakeWebSocket()
        self.server.client_authority[websocket] = "dashboard"
        base = {
            "type": "SAVE_GENERAL_SETTINGS",
            "runAtStartup": False,
            "mode": "helper_controls_lossless",
            "hotkey": self.manager.config.global_hotkey.model_dump(),
            "rtssInstallPath": "C:/RTSS",
            "defaultProfileAutoScale": False,
        }

        await self.server.process_message(
            websocket,
            json.dumps({
                **base,
                "rtssFrameLimitingEnabled": False,
                "confirmRtssGlobalChange": True,
            }),
        )
        stored = self.manager.get_profile_by_id(profile.id)
        self.assertFalse(self.manager.config.rtss_frame_limiting_enabled)
        self.assertFalse(self.manager.config.default_profile_auto_scale)
        self.assertTrue(stored.rtss.enabled)
        self.assertEqual(stored.rtss.framerate_limit, 70)
        self.assertEqual(stored.rtss.limit_method, "front_edge_sync")
        self.assertEqual(fake_rtss.removed, [profile.id])

        await self.server.process_message(
            websocket,
            json.dumps({**base, "rtssFrameLimitingEnabled": True}),
        )
        self.assertTrue(self.manager.config.rtss_frame_limiting_enabled)
        self.assertTrue(self.manager.get_profile_by_id(profile.id).rtss.enabled)
        self.assertEqual(fake_rtss.applied, [profile.id])

    async def test_revert_all_requires_confirmation_and_clears_managed_integrations(self):
        websocket = self.FakeWebSocket()
        self.server.client_authority[websocket] = "dashboard"
        cleanup_automation = Mock()
        cleanup_automation.revert_managed_addons.return_value = {"files": []}
        self.server.automation = cleanup_automation
        watcher = Mock()
        watcher.check_is_lossless_scaling_running.return_value = True
        watcher.stop_lossless_scaling.return_value = True
        watcher.launch_lossless_scaling.return_value = True
        self.server.process_watcher = watcher
        fake_rtss = FakeRtssManager()
        self.server.rtss_manager = fake_rtss
        self.server.startup_manager = FakeStartupManager()

        settings_path = Path(self.temp_dir.name) / "Settings.xml"
        settings_path.write_text(
            "<Settings><Hotkey>S</Hotkey><GameProfiles /></Settings>",
            encoding="utf-8",
        )
        settings = LosslessSettingsXml(str(settings_path))
        original = settings_path.read_bytes()
        settings.ensure_initial_backup()
        settings_path.write_text("<Settings><Hotkey>F12</Hotkey></Settings>", encoding="utf-8")
        self.server.ls_settings = settings

        profile = self.manager.get_profile_by_id("youtube-profile")
        profile.graphics.reshade.enabled = True
        profile.rtss = RtssLimiterConfig(
            enabled=True,
            managed_profile_created=True,
            managed_target_process="chrome.exe",
            managed_install_path="C:/RTSS",
        )
        self.manager.config.lossless_control_configured = True
        self.manager.config.rtss_frame_limiting_enabled = True
        self.manager.config.active_profile_id = profile.id
        self.manager.save_config()

        await self.server.process_message(websocket, '{"type":"REVERT_ALL_CHANGES"}')
        self.assertIn("Confirm revert", websocket.messages[-1])
        cleanup_automation.revert_managed_addons.assert_not_called()

        await self.server.process_message(
            websocket,
            json.dumps({"type": "REVERT_ALL_CHANGES", "confirmRevertAll": True}),
        )

        response = json.loads(websocket.messages[-1])
        self.assertEqual(response["type"], "ALL_CHANGES_REVERTED")
        self.assertTrue(response["ok"])
        self.assertTrue(response["settingsRestored"])
        self.assertEqual(settings_path.read_bytes(), original)
        self.assertEqual(fake_rtss.removed, [profile.id])
        self.assertFalse(profile.rtss.enabled)
        self.assertFalse(profile.graphics.reshade.enabled)
        self.assertFalse(self.manager.config.lossless_control_configured)
        self.assertFalse(self.manager.config.rtss_frame_limiting_enabled)
        self.assertFalse(self.manager.config.run_at_startup)
        self.assertTrue(response["startupTaskRemoved"])
        self.assertIsNone(self.manager.config.active_profile_id)
        watcher.stop_lossless_scaling.assert_called_once()
        watcher.launch_lossless_scaling.assert_called_once_with(force=True)

    @patch("companion.services.automation.InputSimulator.trigger_hotkey", return_value=True)
    async def test_fullscreen_enter_and_exit_are_idempotent(self, trigger_hotkey):
        profile = self.manager.get_profile_by_id("youtube-profile")
        profile.hotkey.activation_delay_ms = 0
        watcher = Mock()
        watcher.get_foreground_window_info.return_value = ProcessInfo(
            123,
            "chrome.exe",
            r"C:\Program Files\Chrome\chrome.exe",
            "YouTube",
            456,
        )
        watcher.focus_window_identity.return_value = True
        self.server.process_watcher = watcher
        self.automation.process_watcher = watcher
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
