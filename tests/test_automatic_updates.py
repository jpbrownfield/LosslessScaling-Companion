import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from companion.core.models import AppConfig
from companion.core.profile_manager import ProfileManager
from companion.core.state import AppState
from companion.services.automatic_updates import newer_addon_version
from companion.services.server import CompanionWebSocketServer
from companion.services.self_update import CHECKSUM_NAME, INSTALLER_NAME, CompanionUpdateService


class AutomaticUpdateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.manager = ProfileManager(Path(self.temp.name))
        self.manager.config.lossless_scaling_exe_path = str(Path(self.temp.name) / 'LosslessScaling.exe')
        self.manager.config.lossless_settings_xml_path = str(Path(self.temp.name) / 'Settings.xml')
        self.server = CompanionWebSocketServer(self.manager, AppState())
        self.server.broadcast_full_data_update = AsyncMock()
        self.server._refresh_addon_updates = AsyncMock()
        self.manager.config.automatically_install_updates = True
        self.server.addon_update_status = {"updates": [{"provider": "ls-addon-manager", "currentVersion": "1.0.0", "latestVersion": "1.1.0"}]}

    async def test_automatic_updates_are_opt_in_and_notification_setting_does_not_gate_them(self):
        self.assertFalse(AppConfig().automatically_install_updates)
        self.server._process_message = AsyncMock()
        self.manager.config.automatically_install_updates = False
        await self.server.automatic_updates.run_pending()
        self.server._process_message.assert_not_awaited()
        self.manager.config.automatically_install_updates = True
        self.manager.config.notify_on_updates = False
        await self.server.automatic_updates.run_pending()
        self.server._process_message.assert_awaited_once()
        self.assertFalse(self.server.state.update_in_progress)

    async def test_scaling_repair_simulation_and_inflight_edits_defer_updates(self):
        self.server._process_message = AsyncMock()
        self.server.state.is_scaling_active = True
        await self.server.automatic_updates.run_pending()
        self.server.state.is_scaling_active = False
        self.server.state.simulation_mode = True
        await self.server.automatic_updates.run_pending()
        self.server.state.simulation_mode = False
        self.server._active_mutations = 1
        await self.server.automatic_updates.run_pending()
        self.server._active_mutations = 0
        self.server._editing_clients.add(object())
        await self.server.automatic_updates.run_pending()
        self.server._process_message.assert_not_awaited()
        self.assertFalse(self.server.state.update_in_progress)

    async def test_update_preferences_persist_together_without_rewriting_profile_toggles(self):
        self.server.startup_manager = Mock(TASK_NAME='test-startup')
        self.server.startup_manager.set_enabled.return_value = {'enabled': False, 'taskName': 'test-startup'}
        self.server._control_settings_payload = Mock(side_effect=lambda: {
            'notifyOnUpdates': self.manager.config.notify_on_updates,
            'automaticallyInstallUpdates': self.manager.config.automatically_install_updates,
        })
        client = Mock(send=AsyncMock())
        self.server.client_authority[client] = 'dashboard'
        profiles_before = [(profile.id, profile.auto_scale) for profile in self.manager.config.profiles]
        await self.server.process_message(client, json.dumps({'type':'SAVE_GENERAL_SETTINGS',
            'notifyOnUpdates':True, 'automaticallyInstallUpdates':True}))
        messages = [json.loads(call.args[0]) for call in client.send.call_args_list]
        self.assertFalse(any(item['type'] == 'ERROR' for item in messages), messages)
        self.assertTrue(self.manager.config.notify_on_updates)
        self.assertTrue(self.manager.config.automatically_install_updates)
        reloaded = ProfileManager(self.manager.config_dir)
        self.assertTrue(reloaded.config.automatically_install_updates)
        self.assertEqual([(profile.id, profile.auto_scale) for profile in self.manager.config.profiles], profiles_before)

    async def test_locked_update_rejects_edits_from_other_clients_and_unlocks_on_failure(self):
        started, finish = asyncio.Event(), asyncio.Event()

        async def work(client, raw):
            self.assertTrue(self.server.state.update_in_progress)
            started.set()
            await finish.wait()
            await client.send(json.dumps({"type": "ERROR", "message": "Checksum mismatch"}))
            return ValueError("Checksum mismatch")

        self.server._process_message = AsyncMock(side_effect=work)
        task = asyncio.create_task(self.server.automatic_updates.run_pending())
        await started.wait()
        client = Mock(send=AsyncMock())
        self.server.client_authority[client] = "dashboard"
        await self.server.process_message(client, '{"type":"SAVE_GENERAL_SETTINGS"}')
        self.assertIn("installing updates", client.send.call_args.args[0])
        self.assertEqual(self.server._process_message.await_count, 1)
        self.assertFalse(self.server.automation.handle_override_hotkey())
        finish.set()
        await task
        self.assertFalse(self.server.state.update_in_progress)
        self.assertFalse(self.server.component_operation_status["locked"])
        self.assertEqual(self.server.component_operation_status["phase"], "failed")
        await self.server.automatic_updates.run_pending()
        self.assertEqual(self.server._process_message.await_count, 1, "Failed versions should back off")

    async def test_echo_update_preserves_disabled_global_integration_and_profiles(self):
        self.manager.config.addon_manager_enabled = False
        self.manager.config.addon_manager.enabled = False
        profiles_before = [profile.model_dump() for profile in self.manager.config.profiles]
        self.server.release_manager = Mock()
        self.server.release_manager.check.return_value = [{"version": "1.1.0", "assets": [{"name": "manager.zip"}]}]
        self.server.release_manager.stage_release.return_value = {"provider": "ls-addon-manager", "version": "1.1.0"}
        await self.server.automatic_updates.run_pending()
        self.assertFalse(self.manager.config.addon_manager_enabled)
        self.assertFalse(self.manager.config.addon_manager.enabled)
        self.assertEqual([profile.model_dump() for profile in self.manager.config.profiles], profiles_before)
        self.assertEqual(self.manager.config.addon_manager.version, "1.1.0")

    async def discover_reshade_update(self, selected_version, *, other_version=None):
        profile = self.manager.config.profiles[0]
        profile.graphics.reshade.enabled = True
        profile.graphics.reshade.version = selected_version
        if other_version:
            other = self.manager.config.profiles[1]
            other.graphics.reshade.enabled = True
            other.graphics.reshade.version = other_version
            self.server.state.current_active_profile = other
        self.server.asset_store.list_packages = Mock(return_value=[
            {"provider": "reshade", "version": "6.8.0"},
            {"provider": "reshade", "version": "6.9.0"},
        ])
        self.server.release_manager = Mock()
        self.server.release_manager.check.side_effect = lambda provider, **_: [
            {"version": "6.8.0", "assets": [{"name": "ReShade_Setup_6.8.0_Addon.exe"}]}
        ] if provider == "reshade" else []
        self.server.release_manager.stage_release.return_value = {"provider": "reshade", "version": "6.8.0"}
        with patch("companion.services.server.GraphicsSourceDetector.status", return_value={
            "reshade": {"detected": True, "version": "6.0.0"},
        }):
            return await CompanionWebSocketServer._refresh_addon_updates(self.server, force=True)

    async def test_cached_latest_package_does_not_hide_an_older_profile_pin(self):
        status = await self.discover_reshade_update("6.7.0")
        self.assertEqual(status["updates"][0]["currentVersion"], "6.7.0")
        await self.server.automatic_updates.run_pending()
        self.assertEqual(self.manager.config.profiles[0].graphics.reshade.version, "6.8.0")

    async def test_old_external_installer_does_not_trigger_a_profile_downgrade(self):
        status = await self.discover_reshade_update("6.9.0")
        self.assertEqual(status["updates"], [])
        await self.server.automatic_updates.run_pending()
        self.server.release_manager.stage_release.assert_not_called()
        self.assertEqual(self.manager.config.profiles[0].graphics.reshade.version, "6.9.0")

    async def test_mixed_profile_versions_update_only_older_stable_pins(self):
        for preserved_version in ("6.9.0", "6.8.0-beta"):
            with self.subTest(version=preserved_version):
                self.server.automatic_updates.retry_after.clear()
                status = await self.discover_reshade_update("6.7.0", other_version=preserved_version)
                self.assertEqual(status["updates"][0]["currentVersion"], "6.7.0")
                await self.server.automatic_updates.run_pending()
                older, newer = self.manager.config.profiles[:2]
                self.assertEqual(older.graphics.reshade.version, "6.8.0")
                self.assertEqual(newer.graphics.reshade.version, preserved_version)
                self.assertFalse(self.server.automation.addon_configs.has_pending(newer))

    async def test_stale_update_status_cannot_downgrade_echo(self):
        self.manager.config.addon_manager.version = "1.2.0"
        self.server.release_manager = Mock()
        self.server.release_manager.check.return_value = [{"version": "1.1.0", "assets": [{"name": "manager.zip"}]}]
        await self.server.automatic_updates.run_pending()
        self.server.release_manager.stage_release.assert_not_called()
        self.assertEqual(self.manager.config.addon_manager.version, "1.2.0")

    async def test_prerelease_profile_does_not_hide_an_older_stable_profile(self):
        status = await self.discover_reshade_update("6.9.0-beta", other_version="6.7.0")
        self.assertEqual(status["updates"][0]["currentVersion"], "6.7.0")
        await self.server.automatic_updates.run_pending()
        self.assertEqual(self.manager.config.profiles[0].graphics.reshade.version, "6.9.0-beta")
        self.assertEqual(self.manager.config.profiles[1].graphics.reshade.version, "6.8.0")

    async def test_automatic_apply_reuses_verified_installer_offline_after_restart(self):
        payload = b"verified cached installer"
        digest = hashlib.sha256(payload).hexdigest()
        releases = Mock()
        releases.check.return_value = [{"version": "2.0.0", "assets": [
            {"name": INSTALLER_NAME}, {"name": CHECKSUM_NAME},
        ]}]
        service = CompanionUpdateService(releases, self.manager.config_dir, enabled=True)
        with patch.object(service, "_download_bytes", side_effect=[
            f"{digest}  {INSTALLER_NAME}".encode(), payload,
        ]):
            service.download("2.0.0")
        self.server.companion_updater = CompanionUpdateService(releases, self.manager.config_dir, enabled=True)
        self.server.addon_update_status = {"updates": []}
        self.server.companion_update_status = {"supported": True, "updateAvailable": True,
                                               "latestVersion": "2.0.0", "downloaded": True}
        self.server.on_installer_launched = Mock()
        releases.check.reset_mock()
        releases.check.side_effect = OSError("Network unavailable")
        with patch("companion.services.self_update.urllib.request.urlopen", side_effect=OSError("Network unavailable")) as network, patch(
            "companion.services.self_update.ctypes.windll.shell32.ShellExecuteW", return_value=42,
        ) as launch:
            await self.server.automatic_updates.run_pending()
            await asyncio.sleep(0)
        releases.check.assert_not_called()
        network.assert_not_called()
        launch.assert_called_once()
        self.assertIn("/AUTOMATICUPDATE=1", launch.call_args.args[3])
        self.server.on_installer_launched.assert_called_once_with()
        self.assertEqual(self.server.component_operation_status["phase"], "restarting")

    async def test_companion_handoff_is_silent_and_remains_locked_until_shutdown(self):
        self.server.addon_update_status = {"updates": []}
        self.server.companion_update_status = {"supported": True, "updateAvailable": True, "latestVersion": "2.0.0", "downloaded": False}
        self.server.companion_updater = Mock()
        self.server.companion_updater.download.return_value = {"type": "COMPANION_UPDATE_DOWNLOADED", "version": "2.0.0"}
        self.server.companion_updater.launch_installer.return_value = {"type": "COMPANION_INSTALLER_LAUNCHED", "version": "2.0.0"}
        self.server.on_installer_launched = Mock()
        await self.server.automatic_updates.run_pending()
        await asyncio.sleep(0)
        self.server.companion_updater.launch_installer.assert_called_once_with("2.0.0", automatic=True)
        self.server.on_installer_launched.assert_called_once()
        self.assertTrue(self.server.state.update_in_progress)
        self.assertTrue(self.server.component_operation_status["locked"])
        self.assertEqual(self.server.component_operation_status["phase"], "restarting")

    async def test_rejected_companion_installer_unlocks_the_application(self):
        self.server.addon_update_status = {'updates': []}
        self.server.companion_update_status = {'supported': True, 'updateAvailable': True, 'latestVersion': '2.0.0', 'downloaded': True}
        self.server.companion_updater = Mock()
        self.server.companion_updater.launch_installer.side_effect = OSError('Windows rejected the installer')
        await self.server.automatic_updates.run_pending()
        self.assertFalse(self.server.state.update_in_progress)
        self.assertFalse(self.server._update_handoff)
        self.assertFalse(self.server.component_operation_status['locked'])
        self.assertEqual(self.server.component_operation_status['phase'], 'failed')

    async def test_source_companion_and_community_runtime_are_not_automatically_installed(self):
        self.server.addon_update_status = {"updates": [{"provider": "lsp-neural-render", "currentVersion": "1.0.0", "latestVersion": "2.0.0"}]}
        self.server.companion_update_status = {"supported": False, "updateAvailable": True, "latestVersion": "2.0.0"}
        self.server._process_message = AsyncMock()
        await self.server.automatic_updates.run_pending()
        self.server._process_message.assert_not_awaited()

    def test_addon_comparison_never_downgrades_or_guesses_prerelease_versions(self):
        self.assertTrue(newer_addon_version("1.9.0", "1.10.0"))
        self.assertFalse(newer_addon_version("2.0.0", "1.10.0"))
        self.assertFalse(newer_addon_version("1.0", "1.0.0"))
        self.assertFalse(newer_addon_version("2.0-beta", "2.1"))


if __name__ == '__main__':
    unittest.main()
