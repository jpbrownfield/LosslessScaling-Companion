import asyncio
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch
import zipfile

from companion.core.profile_manager import ProfileManager
from companion.core.state import AppState
from companion.services.asset_store import AssetStore, UnsafeAssetError
from companion.services.release_providers import ProviderRegistry, ReleaseManager
from companion.services.server import CompanionWebSocketServer


class Socket:
    def __init__(self):
        self.messages = []

    async def send(self, message):
        self.messages.append(json.loads(message))


class ComponentOperationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.manager = ProfileManager(Path(self.temp.name))
        self.server = CompanionWebSocketServer(self.manager, AppState())
        self.socket = Socket()
        self.server.client_authority[self.socket] = "dashboard"
        self.command = json.dumps({"type": "INSTALL_LATEST_GRAPHICS_ADDON", "provider": "ls-addon-manager"})

    async def test_install_lock_blocks_second_dashboard_and_broadcasts_state(self):
        started, finish = asyncio.Event(), asyncio.Event()

        async def work(*_args):
            started.set()
            await finish.wait()

        self.server._process_message = AsyncMock(side_effect=work)
        second = Socket()
        self.server.clients.add(second)
        self.server.client_authority[second] = "dashboard"
        first_task = asyncio.create_task(self.server.process_message(self.socket, self.command))
        await started.wait()
        await self.server.process_message(second, self.command)
        self.assertEqual(self.server._process_message.await_count, 1)
        self.assertTrue(any(item["type"] == "ERROR" for item in second.messages))
        finish.set()
        await first_task
        self.assertFalse(self.server.component_install_lock.locked())
        self.assertEqual(second.messages[-1]["phase"], "complete")

    async def test_progress_is_forwarded_from_download_thread_and_unlocks_on_failure(self):
        def stage(*_args, progress_callback, **_kwargs):
            progress_callback({"phase": "downloading", "receivedBytes": 50, "totalBytes": 100})
            time.sleep(0.05)
            raise UnsafeAssetError("SHA-256 mismatch")

        self.server.release_manager = Mock()
        self.server.release_manager.check.return_value = [{"version": "1.0.6", "assets": [{"name": "manager.zip"}]}]
        self.server.release_manager.stage_release.side_effect = stage
        await self.server.process_message(self.socket, self.command)
        self.assertTrue(any(item.get("receivedBytes") == 50 for item in self.socket.messages))
        self.assertEqual(self.socket.messages[-1]["phase"], "failed")
        self.assertIn("SHA-256", self.socket.messages[-1]["error"])
        self.assertFalse(self.server.component_install_lock.locked())

    async def test_readonly_client_cannot_start_a_component_operation(self):
        self.server.client_authority.clear()
        self.server.release_manager = Mock()
        await self.server.process_message(self.socket, self.command)
        self.assertEqual(self.socket.messages[-1]["type"], "ERROR")
        self.server.release_manager.check.assert_not_called()
        self.assertIsNone(self.server.component_operation_status)

    async def test_tray_updates_share_the_component_install_lock(self):
        self.server.companion_updater = Mock()
        async with self.server.component_install_lock:
            with self.assertRaisesRegex(RuntimeError, "in progress"):
                await self.server.apply_companion_update("1.0.0")
        self.server.companion_updater.download.assert_not_called()

    async def test_reconnected_dashboard_receives_current_progress(self):
        self.server.component_operation_status = {"type": "COMPONENT_OPERATION", "provider": "ls-addon-manager",
                                                  "busy": True, "phase": "downloading", "receivedBytes": 30, "totalBytes": 100}
        with patch("companion.services.server.GraphicsSourceDetector.status", return_value={}):
            await self.server.process_message(self.socket, '{"type":"GET_GRAPHICS_STATUS"}')
        self.assertEqual(self.socket.messages[-1]["componentOperation"], self.server.component_operation_status)

    async def test_special_k_update_is_queued_without_interrupting_scaling_or_enabling_other_profiles(self):
        active = self.manager.config.profiles[0]
        active.graphics.special_k.enabled = True
        active.graphics.special_k.version = "old"
        other = self.manager.config.profiles[1]
        self.server.state.current_active_profile = active
        self.server.state.is_scaling_active = True
        self.server.automation.activate_profile = Mock()
        self.server.release_manager = Mock()
        self.server.release_manager.check.return_value = [{"version": "new", "assets": [{"name": "SpecialK.7z"}]}]
        self.server.release_manager.stage_release.return_value = {"provider": "special-k", "version": "new"}
        await self.server.process_message(self.socket, json.dumps({"type": "INSTALL_LATEST_GRAPHICS_ADDON", "provider": "special-k"}))
        self.assertEqual(active.graphics.special_k.version, "new")
        self.assertFalse(other.graphics.special_k.enabled)
        self.assertTrue(self.server.automation.addon_configs.has_pending(active))
        self.server.automation.activate_profile.assert_not_called()
        self.assertEqual(self.socket.messages[-1]["phase"], "complete")


class GraphicsDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = AssetStore(str(self.root / "store"))
        self.manager = ReleaseManager(self.store, ProviderRegistry())

    @staticmethod
    def pe():
        image = bytearray(0x86)
        image[:2] = b"MZ"
        image[0x3c:0x40] = (0x80).to_bytes(4, "little")
        image[0x80:0x84] = b"PE\0\0"
        image[0x84:0x86] = (0x8664).to_bytes(2, "little")
        return bytes(image)

    def test_direct_reshade_download_reports_progress_and_extracts_without_executing_setup(self):
        archive = self.root / "ReShade_Setup_6.8.0_Addon.exe"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("ReShade64.dll", self.pe())
        data = archive.read_bytes()

        class Response(io.BytesIO):
            headers = {"Content-Length": str(len(data))}
            def geturl(self):
                return "https://reshade.me/downloads/ReShade_Setup_6.8.0_Addon.exe"

        release = dict(provider="reshade", version="6.8.0", name="ReShade", published_at=None,
                       prerelease=False, html_url="https://reshade.me/", assets=[dict(name=archive.name,
                       url="https://reshade.me/downloads/" + archive.name, size=len(data))])
        progress = []
        with patch.object(self.manager, "check", return_value=[release]), patch(
            "companion.services.release_providers.urllib.request.urlopen", return_value=Response(data)
        ), patch("companion.services.release_providers.subprocess.run") as execute:
            package = self.manager.stage_release("reshade", "6.8.0", archive.name, "download", progress_callback=progress.append)
        execute.assert_not_called()
        self.assertTrue(any(item["receivedBytes"] == len(data) for item in progress))
        self.assertEqual(progress[-1]["phase"], "staging")
        self.assertEqual(package["provider"], "reshade")
        recipe = json.loads((Path(package["payload_path"]) / "lossless-scaling-deployment.json").read_text())
        self.assertEqual(recipe["files"][0]["destination"], "dxgi.dll")

    def test_real_special_k_7z_is_extracted_without_running_an_installer(self):
        import py7zr
        runtime = self.root / "SpecialK64.dll"
        runtime.write_bytes(self.pe())
        archive = self.root / "SpecialK.7z"
        with py7zr.SevenZipFile(archive, "w") as bundle:
            bundle.write(runtime, "x64/SpecialK64.dll")
        staging = self.root / "stage"
        staging.mkdir()
        package = self.manager._stage_graphics_runtime(archive, staging, "special-k", "26.1", {})
        self.assertEqual((Path(package["payload_path"]) / "SpecialK64.dll").read_bytes(), self.pe())
        recipe = json.loads((Path(package["payload_path"]) / "lossless-scaling-deployment.json").read_text())
        self.assertEqual(recipe["files"][0]["destination"], "dxgi.dll")

    def test_special_k_archive_traversal_is_rejected(self):
        archive = self.root / "bad.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("../SpecialK64.dll", self.pe())
        with self.assertRaises(UnsafeAssetError):
            self.manager._stage_graphics_runtime(archive, self.root, "special-k", "26.1", {})
