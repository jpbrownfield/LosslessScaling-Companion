import asyncio
import tempfile
import unittest
import urllib.request
import json
import threading
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

    async def test_general_settings_save_configures_editable_hotkey_override(self):
        self.server.startup_manager = FakeStartupManager()
        self.server.hotkey_listener = Mock()
        websocket = self.FakeWebSocket()
        self.server.client_authority[websocket] = "dashboard"

        await self.server.process_message(websocket, json.dumps({
            "type": "SAVE_GENERAL_SETTINGS",
            "runAtStartup": False,
            "smartAutoScaleEnabled": True,
            "defaultProfileAutoScale": True,
            "overrideLosslessHotkey": True,
            "overrideHotkey": {"modifiers": ["ctrl", "shift"], "key": "g"},
        }))

        self.assertTrue(self.manager.config.override_lossless_hotkey)
        self.assertEqual(self.manager.config.override_hotkey.modifiers, ["ctrl", "shift"])
        self.assertEqual(self.manager.config.override_hotkey.key, "g")
        self.assertEqual(self.manager.config.global_hotkey.modifiers, [])
        self.assertEqual(self.manager.config.global_hotkey.key, "f24")
        response = json.loads(websocket.messages[-1])
        self.assertTrue(response["controlSettings"]["overrideLosslessHotkey"])
        self.server.hotkey_listener.refresh.assert_called_once_with()

    def test_benchmark_requires_embedded_workload_and_verified_presentmon(self):
        payload = Path(self.temp_dir.name) / "benchmark-tools"
        payload.mkdir()
        workload = payload / "LSBenchmark.exe"
        workload.write_bytes(b"workload")
        (payload / "PresentMon-2.5.1-x64.exe").write_bytes(b"presentmon")
        presentmon_hash = "b" * 64

        def package(provider, filename, digest):
            return {
                "provider": provider,
                "payload_path": str(payload),
                "archive_sha256": digest,
                "files": [{
                    "relative_path": filename,
                    "sha256": digest,
                    "architecture": "x64",
                }],
                "source": {"verification": {
                    "publisher_digest_verified": True,
                    "publisher_sha256": digest,
                    "calculated_sha256": digest,
                }},
            }

        packages = [package("presentmon", "PresentMon-2.5.1-x64.exe", presentmon_hash)]
        self.server.asset_store = Mock()
        self.server.asset_store.list_packages.return_value = packages
        self.server.asset_store.sha256.return_value = presentmon_hash

        with patch.object(self.server, "_embedded_benchmark_executable", return_value=workload):
            self.assertTrue(self.server._benchmark_status_payload()["ready"])
        packages[0]["source"]["verification"]["publisher_digest_verified"] = False
        with patch.object(self.server, "_embedded_benchmark_executable", return_value=workload):
            self.assertFalse(self.server._benchmark_status_payload()["ready"])

    def test_benchmark_launch_uses_default_profile_without_calibration(self):
        command = self.server._benchmark_launch_command(
            Path("C:/tools/LSBenchmark.exe"),
            Path("C:/tools/PresentMon-2.5.1-x64.exe"),
        )
        self.assertIn("--use-default-profile", command)
        self.assertNotIn("--dynamic-limiter", command)
        self.assertNotIn("--minimum-fps", command)
        self.assertNotIn("--maximum-fps", command)

    async def test_dashboard_runs_diagnostics_and_broadcasts_result(self):
        websocket = self.FakeWebSocket()
        self.server.client_authority[websocket] = "dashboard"
        self.server.clients.add(websocket)
        expected = {
            "type": "DIAGNOSTICS_RESULT",
            "running": False,
            "counts": {"pass": 1, "warn": 0, "fail": 0},
            "results": [{"id": "runtime", "name": "Runtime", "status": "pass", "summary": "ok"}],
        }
        self.server.diagnostic_runner.run = Mock(return_value=expected)

        await self.server.process_message(websocket, json.dumps({"type": "RUN_DIAGNOSTICS"}))
        task = self.server.diagnostics_task
        self.assertIsNotNone(task)
        await task

        payloads = [json.loads(message) for message in websocket.messages]
        self.assertTrue(any(item.get("running") is True for item in payloads))
        self.assertIn(expected, payloads)

    async def test_diagnostic_archive_download_requires_dashboard_token(self):
        self.server.diagnostic_runner.root.mkdir(parents=True)
        archive = self.server.diagnostic_runner.root / "diagnostics.zip"
        archive.write_bytes(b"diagnostics")
        self.server._last_diagnostics_archive = archive.resolve()

        forbidden = await self.server.process_http_request(
            "/diagnostics/download?token=wrong", {},
        )
        allowed = await self.server.process_http_request(
            f"/diagnostics/download?token={self.server.dashboard_token}", {},
        )

        self.assertEqual(forbidden[0], 403)
        self.assertEqual(allowed[0], 200)
        self.assertEqual(allowed[2], b"diagnostics")

    async def test_dashboard_checks_downloads_and_launches_companion_update(self):
        websocket = self.FakeWebSocket()
        self.server.client_authority[websocket] = "dashboard"
        self.server.clients.add(websocket)
        status = {
            "type": "COMPANION_UPDATE_STATUS",
            "currentVersion": "1.0.0",
            "latestVersion": "1.1.0",
            "updateAvailable": True,
            "downloaded": False,
        }
        self.server.companion_updater = Mock()
        self.server.companion_updater.check.return_value = status
        self.server.companion_updater.download.return_value = {
            "type": "COMPANION_UPDATE_DOWNLOADED", "version": "1.1.0",
        }
        self.server.companion_updater.launch_installer.return_value = {
            "type": "COMPANION_INSTALLER_LAUNCHED", "version": "1.1.0",
        }
        self.server.on_installer_launched = Mock()

        await self.server.process_message(websocket, json.dumps({"type": "GET_COMPANION_UPDATE"}))
        await self.server.process_message(websocket, json.dumps({
            "type": "DOWNLOAD_COMPANION_UPDATE", "version": "1.1.0",
        }))
        await self.server.process_message(websocket, json.dumps({
            "type": "INSTALL_COMPANION_UPDATE", "version": "1.1.0",
        }))
        await asyncio.sleep(0)

        payloads = [json.loads(message) for message in websocket.messages]
        self.assertIn(status, payloads)
        self.assertTrue(any(item["type"] == "COMPANION_UPDATE_DOWNLOAD_STARTED" for item in payloads))
        self.assertTrue(any(item["type"] == "COMPANION_UPDATE_DOWNLOADED" for item in payloads))
        self.assertTrue(any(item["type"] == "COMPANION_INSTALLER_LAUNCHED" for item in payloads))
        self.server.on_installer_launched.assert_called_once_with()

    async def test_dashboard_can_import_a_detected_graphics_source(self):
        websocket = self.FakeWebSocket()
        self.server.client_authority[websocket] = "dashboard"
        package = {"provider": "reshade", "version": "6.8.0"}
        self.server.graphics_source_importer = Mock()
        self.server.graphics_source_importer.stage.return_value = package

        await self.server.process_message(websocket, json.dumps({
            "type": "IMPORT_GRAPHICS_SOURCE",
            "provider": "reshade",
            "operationId": "test-operation",
        }))

        self.server.graphics_source_importer.stage.assert_called_once_with(
            "reshade", "test-operation"
        )
        self.assertEqual(json.loads(websocket.messages[-1]), {
            "type": "GRAPHICS_RELEASE_STAGED", "package": package,
        })

    def test_startup_detection_accepts_only_release_digest_matched_presentmon(self):
        home = Path(self.temp_dir.name) / "user"
        downloads = home / "Downloads"
        downloads.mkdir(parents=True)
        candidate = downloads / "PresentMon-2.5.1-x64.exe"
        candidate.write_bytes(b"official-presentmon")
        digest = "c" * 64
        self.server.asset_store = Mock()
        self.server.asset_store.list_packages.return_value = []
        self.server.asset_store.pe_architecture.return_value = "x64"
        self.server.asset_store.sha256.return_value = digest
        self.server.release_manager = Mock()
        self.server.release_manager.check.return_value = [{
            "assets": [{"name": candidate.name, "digest": f"sha256:{digest}"}],
        }]

        with patch("companion.services.server.Path.home", return_value=home):
            detected = self.server._discover_official_presentmon()

        self.assertEqual(detected, (candidate.resolve(), digest))

    async def test_gpu_routing_and_rtx_hdr_settings_are_saved(self):
        websocket = self.FakeWebSocket()
        self.server.client_authority[websocket] = "dashboard"
        self.server.startup_manager = FakeStartupManager()
        device_id = r"PCI\VEN_10DE&DEV_2C02"
        self.automation.gpu_router = Mock()
        self.automation.gpu_router.detect.return_value = {
            "gpus": [{
                "deviceId": device_id,
                "name": "NVIDIA GeForce RTX 5080",
                "vendor": "NVIDIA",
                "lsGpuId": 1,
                "connectedDisplays": [r"\\.\DISPLAY1"],
            }],
            "displays": [],
            "hasNvidia": True,
        }
        self.automation.nvidia_profile_manager = Mock()
        self.automation.nvidia_profile_manager.set_lossless_scaling_rtx_hdr.return_value = True

        await self.server.process_message(websocket, json.dumps({
            "type": "SAVE_GENERAL_SETTINGS",
            "runAtStartup": False,
            "smartAutoScaleEnabled": True,
            "preferredScalingGpuDeviceId": device_id,
            "autoRouteGpuToDisplay": False,
            "nvidiaRtxHdrEnabled": True,
            "reshadeHdrPeakNits": 1400,
        }))

        self.assertEqual(self.manager.config.preferred_scaling_gpu_device_id, device_id)
        self.assertFalse(self.manager.config.auto_route_gpu_to_display)
        self.assertTrue(self.manager.config.nvidia_rtx_hdr_enabled)
        self.assertEqual(self.manager.config.reshade_hdr_peak_nits, 1400)
        self.automation.nvidia_profile_manager.set_lossless_scaling_rtx_hdr.assert_called_once()
        response = json.loads(websocket.messages[-1])
        self.assertTrue(response["controlSettings"]["hasNvidiaGpu"])
        self.assertEqual(response["controlSettings"]["reshadeHdr"]["resolvedNits"], 1400)

        await self.server.process_message(websocket, json.dumps({
            "type": "SAVE_GENERAL_SETTINGS",
            "runAtStartup": False,
            "smartAutoScaleEnabled": True,
            "preferredScalingGpuDeviceId": device_id,
            "autoRouteGpuToDisplay": True,
            "nvidiaRtxHdrEnabled": True,
        }))

        self.assertIsNone(self.manager.config.preferred_scaling_gpu_device_id)
        self.assertTrue(self.manager.config.auto_route_gpu_to_display)

    async def test_new_profile_payload_inherits_omitted_default_settings(self):
        default = self.manager.ensure_lossless_default_profile({
            "Title": "Default",
            "ScalingType": "LS1",
        })
        default.graphics.special_k.enabled = True
        default.graphics.special_k.hdr_peak_brightness_nits = 1400
        self.manager.save_config()
        websocket = self.FakeWebSocket()
        self.server.client_authority[websocket] = "dashboard"

        await self.server.process_message(websocket, json.dumps({
            "type": "SAVE_PROFILE",
            "profile": {
                "name": "New Game",
                "target_process": "new-game.exe",
            },
        }))

        created = self.manager.match_target_profile(process_name="new-game.exe")
        self.assertIsNotNone(created)
        self.assertFalse(created.is_default)
        self.assertTrue(created.graphics.special_k.enabled)
        self.assertEqual(created.graphics.special_k.hdr_peak_brightness_nits, 1400)
        self.assertEqual(created.native_scaling_settings["ScalingType"], "LS1")

    async def test_targetless_autosaved_draft_is_returned_without_rtss_application(self):
        websocket = self.FakeWebSocket()
        self.server.client_authority[websocket] = "dashboard"
        self.server.rtss_manager = FakeRtssManager()
        self.manager.config.rtss_frame_limiting_enabled = True

        await self.server.process_message(websocket, json.dumps({
            "type": "SAVE_PROFILE",
            "requestId": "autosave-1",
            "profile": {
                "name": "",
                "rtss": {"enabled": True},
            },
        }))

        saved = next(
            json.loads(message) for message in websocket.messages
            if json.loads(message).get("type") == "PROFILE_SAVED"
        )
        self.assertEqual(saved["requestId"], "autosave-1")
        self.assertEqual(saved["profile"]["name"], "Untitled Profile")
        self.assertIsNone(saved["profile"]["lossless_profile_title"])
        self.assertEqual(self.server.rtss_manager.applied, [])

    async def test_profile_save_updates_matching_native_lossless_settings(self):
        websocket = self.FakeWebSocket()
        self.server.client_authority[websocket] = "dashboard"
        self.server.ls_settings = Mock()
        self.server.ls_settings.path.is_file.return_value = True
        self.server.ls_settings.upsert_profile.return_value = True

        await self.server.process_message(websocket, json.dumps({
            "type": "SAVE_PROFILE",
            "profile": {
                "name": "Native Settings Game",
                "target_process": "native-settings.exe",
                "lossless_profile_title": "Default",
                "native_scaling_settings": {
                    "CaptureApi": "DXGI",
                    "MaxFrameLatency": "1",
                    "QueueTarget": "0",
                },
            },
        }))

        self.server.ls_settings.upsert_profile.assert_called_once_with(
            "Native Settings Game",
            {
                "CaptureApi": "DXGI",
                "MaxFrameLatency": "1",
                "QueueTarget": "0",
            },
            template_title=None,
        )

    async def test_default_alias_save_updates_only_existing_native_default(self):
        native = self.manager.ensure_lossless_default_profile({
            "Title": "True Native Default",
            "Path": "",
            "ScalingType": "LS1",
        })
        websocket = self.FakeWebSocket()
        self.server.client_authority[websocket] = "dashboard"
        self.server.ls_settings = Mock()
        self.server.ls_settings.path.is_file.return_value = True
        self.server.ls_settings.update_profile.return_value = True

        payload = native.model_dump(mode="json")
        payload["name"] = "Attempted Rename"
        payload["lossless_profile_title"] = "Attempted Duplicate"
        payload["auto_scale"] = True
        payload["native_scaling_settings"]["ScalingType"] = "FSR"
        await self.server.process_message(websocket, json.dumps({
            "type": "SAVE_PROFILE", "profile": payload,
        }))

        saved = self.manager.get_profile_by_id(native.id)
        self.assertEqual(saved.name, "Game Default")
        self.assertEqual(saved.lossless_profile_title, "True Native Default")
        self.assertFalse(saved.auto_scale)
        self.server.ls_settings.update_profile.assert_called_once_with(
            "True Native Default", {"ScalingType": "FSR"}
        )
        self.server.ls_settings.upsert_profile.assert_not_called()

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
            self.assertIn("LS Companion", body)
            self.assertIn('id="addProfileBtn"', body)
            self.assertNotIn('id="settingsToggleBtn"', body)
            self.assertNotIn('id="runtimeInfo"', body)
            self.assertIn('id="statusPill" class="status-pill inactive"', body)
            self.assertIn('>LS Inactive</div>', body)
            self.assertIn('data-dashboard-panel="profiles"', body)
            self.assertIn('data-dashboard-panel="general"', body)
            self.assertIn('data-dashboard-panel="lossless-addons"', body)
            self.assertIn('data-dashboard-panel="performance-benchmark"', body)
            self.assertIn('data-dashboard-panel="testing"', body)
            self.assertIn('header { position: sticky;', body)
            self.assertIn("window.scrollTo({ top: Math.max(0, panelTop), behavior: 'smooth' })", body)
            self.assertIn('id="profilesPanel"', body)
            self.assertIn('id="generalSettingsPanel"', body)
            self.assertIn('id="losslessAddonsPanel"', body)
            self.assertIn('id="performanceBenchmarkPanel"', body)
            self.assertIn('id="testingPanel"', body)
            self.assertIn('id="runDiagnosticsBtn"', body)
            self.assertIn('id="downloadDiagnosticsBtn"', body)
            self.assertIn('id="companionUpdateNotice"', body)
            self.assertIn('id="downloadCompanionUpdateBtn"', body)
            self.assertIn('id="installCompanionUpdateBtn"', body)
            self.assertIn('id="companionReleaseLink"', body)
            self.assertIn('const majorDashboardPanels = [profilesPanel, generalSettingsPanel, losslessAddonsPanel, performanceBenchmarkPanel, testingPanel]', body)
            self.assertIn("setRuntimeStatus('LS Not Found', 'not-found')", body)
            self.assertIn("setRuntimeStatus('LS Active', 'active')", body)
            self.assertIn('if (candidate !== panel) candidate.open = false', body)
            self.assertNotIn('id="settingsArea" class="settings-area hidden"', body)
            self.assertNotIn('id="saveControlSettingsBtn"', body)
            self.assertIn('id="browseProcessLassoLogBtn"', body)
            self.assertIn('id="browseRtssInstallBtn"', body)
            self.assertIn('id="preferredScalingGpu"', body)
            self.assertIn("Auto-Route to App Display GPU", body)
            self.assertNotIn('id="autoRouteAppDisplayGpu"', body)
            self.assertNotIn('id="defaultProfileAutoScale"', body)
            self.assertIn('id="rtssDefaultGpuTargetGroup"', body)
            self.assertIn("Default Limiting Mode", body)
            self.assertIn('class="modifier-options"', body)
            self.assertNotIn("issued a new session token", body)
            self.assertIn('id="targetType"', body)
            self.assertIn('id="targetValue"', body)
            self.assertIn('id="browseTargetProgramBtn"', body)
            self.assertIn('onclick="cloneProfile(', body)
            self.assertIn('id="profileAutosaveStatus"', body)
            self.assertIn('profile-item${invalid ? \' invalid-profile\' : \'\'}', body)
            self.assertNotIn('>Save Profile</button>', body)
            self.assertNotIn('>Save ReShade Profile</button>', body)
            self.assertNotIn('id="losslessProfileTitle"', body)
            self.assertIn('class="modal-content profile-editor-content"', body)
            self.assertIn('[hidden] { display: none !important; }', body)
            self.assertIn('#profileModal { background: #020617; backdrop-filter: none; }', body)
            self.assertIn('body.profile-editor-open > header', body)
            self.assertIn('data-native-key="CaptureApi"', body)
            self.assertIn('data-native-key="MaxFrameLatency"', body)
            self.assertIn('data-native-key="QueueTarget"', body)
            self.assertIn('id="nativeFrameGenerationType"', body)
            self.assertIn('data-native-key="LSFG3Multiplier" data-native-default="2" type="number" min="1" max="20" step="1"', body)
            self.assertIn('id="nativeLsfgTargetRow"', body)
            self.assertIn("control.dataset.nativeAvailable = String(available)", body)
            self.assertIn("if (control.dataset.nativeAvailable !== 'true') return", body)
            self.assertIn('id="nativeAdditionalSettingsGroup"', body)
            self.assertIn('id="reshadeShaderSearch"', body)
            self.assertIn('id="browseReshadePresetBtn"', body)
            self.assertNotIn('id="reshadePresetPath"', body)
            self.assertIn("pendingProfileReshadeImport = true", body)
            self.assertIn('id="importReshadePresetBtn"', body)
            self.assertNotIn('class="addon-badge ready">HDR Installer</span>', body)
            self.assertIn('id="dlss5Enabled"', body)
            self.assertNotIn('id="neuralUpdatePolicy"', body)
            self.assertNotIn('id="neuralRuntimeHash"', body)
            self.assertIn("smart-master-setting", body)
            self.assertIn("integration-status detected", body)
            self.assertIn("integration-status missing", body)
            self.assertIn("Revert All Lossless Scaling Settings and Remove Addon Files", body)
            self.assertIn("Lossless Scaling Add-ons", body)
            self.assertIn('data-addon-install="lossless-proxy"', body)
            self.assertIn('data-addon-install="lsp-neural-render"', body)
            self.assertNotIn('id="selectDlssnrRuntimeBtn"', body)
            self.assertIn("Install NeuralRender + runtime", body)
            self.assertIn("Community Runtime Warning", body)
            self.assertIn("Download Full Add-On build", body)
            self.assertIn('data-addon-download="reshade"', body)
            self.assertIn('data-addon-download="special-k"', body)
            self.assertIn('id="addonUpdateNotice"', body)
            self.assertIn("Ignore these versions", body)
            self.assertNotIn('id="graphicsProvider"', body)
            self.assertNotIn('id="manualAssetPath"', body)
            self.assertNotIn("Expected SHA-256", body)
            self.assertNotIn("Lossless Scaling Profiles", body)
            self.assertIn(self.server.dashboard_token, body)
            self.assertNotIn("__COMPANION_TOKEN__", body)
        finally:
            await self.server.stop()

    async def test_dashboard_settings_view_is_served_with_query_string(self):
        self.manager.config.port = 0
        await self.server.start()
        try:
            port = self.server.server.sockets[0].getsockname()[1]
            response = await asyncio.to_thread(
                urllib.request.urlopen,
                f"http://127.0.0.1:{port}/dashboard?view=settings",
            )
            body = response.read().decode("utf-8")
            self.assertIn('id="settingsArea"', body)
        finally:
            await self.server.stop()

    async def test_server_stop_drains_startup_detection_worker(self):
        worker_started = threading.Event()
        allow_worker_to_finish = threading.Event()
        cache_path = self.server.release_manager.cache_path

        def delayed_detection():
            worker_started.set()
            allow_worker_to_finish.wait(timeout=2)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text("{}", encoding="utf-8")
            return None

        with patch.object(
            self.server, "_discover_official_presentmon", side_effect=delayed_detection
        ):
            self.server.PRESENTMON_STARTUP_DELAY_SECONDS = 0
            self.server.presentmon_detection_task = asyncio.create_task(
                self.server._detect_presentmon_on_startup()
            )
            self.assertTrue(await asyncio.to_thread(worker_started.wait, 1))
            stop_task = asyncio.create_task(self.server.stop())
            try:
                await asyncio.sleep(0.05)
                self.assertFalse(stop_task.done())
            finally:
                allow_worker_to_finish.set()
            await stop_task

        self.assertTrue(cache_path.is_file())

    async def test_dashboard_icon_is_served_as_png(self):
        self.manager.config.port = 0
        await self.server.start()
        try:
            port = self.server.server.sockets[0].getsockname()[1]
            response = await asyncio.to_thread(
                urllib.request.urlopen,
                f"http://127.0.0.1:{port}/favicon.png",
            )
            self.assertEqual(response.headers.get_content_type(), "image/png")
            self.assertEqual(response.read()[:8], b"\x89PNG\r\n\x1a\n")
        finally:
            await self.server.stop()

    async def test_dashboard_can_open_configuration_folder(self):
        websocket = self.FakeWebSocket()
        self.server.client_authority[websocket] = "dashboard"

        with patch("companion.services.server.os.startfile") as startfile:
            await self.server.process_message(
                websocket,
                json.dumps({"type": "OPEN_CONFIG_FOLDER"}),
            )

        startfile.assert_called_once_with(str(self.manager.config_dir.resolve()))
        response = json.loads(websocket.messages[-1])
        self.assertEqual(response["type"], "CONFIG_FOLDER_OPENED")

    async def test_dashboard_can_browse_allowlisted_general_setting_paths(self):
        websocket = self.FakeWebSocket()
        self.server.client_authority[websocket] = "dashboard"

        with patch(
            "companion.services.server.choose_general_setting_path",
            return_value=r"C:\Logs\prolasso.log",
        ) as picker:
            await self.server.process_message(
                websocket,
                json.dumps({"type": "BROWSE_GENERAL_PATH", "kind": "process_lasso_log"}),
            )

        picker.assert_called_once()
        response = json.loads(websocket.messages[-1])
        self.assertEqual(response["type"], "GENERAL_PATH_SELECTED")
        self.assertEqual(response["kind"], "process_lasso_log")
        self.assertEqual(response["path"], r"C:\Logs\prolasso.log")

    async def test_dashboard_can_browse_for_profile_program(self):
        websocket = self.FakeWebSocket()
        self.server.client_authority[websocket] = "dashboard"

        with patch(
            "companion.services.server.choose_general_setting_path",
            return_value=r"C:\Games\Example\game.exe",
        ) as picker:
            await self.server.process_message(
                websocket,
                json.dumps({
                    "type": "BROWSE_GENERAL_PATH",
                    "kind": "program_executable",
                    "currentPath": r"C:\Games\Example\old.exe",
                }),
            )

        picker.assert_called_once_with(
            "program_executable", r"C:\Games\Example\old.exe"
        )
        response = json.loads(websocket.messages[-1])
        self.assertEqual(response["type"], "GENERAL_PATH_SELECTED")
        self.assertEqual(response["kind"], "program_executable")
        self.assertEqual(response["path"], r"C:\Games\Example\game.exe")

    async def test_dashboard_rejects_unknown_general_setting_picker(self):
        websocket = self.FakeWebSocket()
        self.server.client_authority[websocket] = "dashboard"
        await self.server.process_message(
            websocket,
            json.dumps({"type": "BROWSE_GENERAL_PATH", "kind": "arbitrary_file"}),
        )
        self.assertIn("Unsupported settings path picker", websocket.messages[-1])

    async def test_dashboard_selects_valid_x64_dlssnr_runtime(self):
        runtime = Path(self.temp_dir.name) / "nvngx_dlssnr.dll"
        image = bytearray(0x86)
        image[0:2] = b"MZ"
        image[0x3C:0x40] = (0x80).to_bytes(4, "little")
        image[0x80:0x84] = b"PE\0\0"
        image[0x84:0x86] = (0x8664).to_bytes(2, "little")
        runtime.write_bytes(image)
        websocket = self.FakeWebSocket()
        self.server.client_authority[websocket] = "dashboard"

        with patch("companion.services.server.choose_dlssnr_runtime", return_value=str(runtime)):
            await self.server.process_message(
                websocket, json.dumps({"type": "SELECT_DLSSNR_RUNTIME"})
            )

        response = json.loads(websocket.messages[-1])
        self.assertEqual(response["type"], "DLSSNR_RUNTIME_SELECTED")
        self.assertEqual(response["asset"]["architecture"], "x64")
        self.assertEqual(response["asset"]["filename"], "nvngx_dlssnr.dll")

    async def test_dashboard_installs_latest_addon_without_asset_selection(self):
        websocket = self.FakeWebSocket()
        self.server.client_authority[websocket] = "dashboard"
        self.server.release_manager = Mock()
        self.server.release_manager.check.return_value = [{
            "version": "v2.0",
            "assets": [
                {"name": "Source.zip"},
                {"name": "LosslessProxy-v2.0.zip"},
            ],
        }]
        self.server.release_manager.stage_release.return_value = {
            "provider": "lossless-proxy", "version": "v2.0"
        }

        await self.server.process_message(websocket, json.dumps({
            "type": "INSTALL_LATEST_GRAPHICS_ADDON",
            "provider": "lossless-proxy",
            "operationId": "operation123",
        }))

        self.server.release_manager.stage_release.assert_called_once_with(
            "lossless-proxy", "v2.0", "LosslessProxy-v2.0.zip", "operation123",
            channel="stable",
        )
        self.assertEqual(
            json.loads(websocket.messages[-1])["type"], "GRAPHICS_RELEASE_STAGED"
        )

    async def test_neural_render_install_also_imports_verified_community_runtime(self):
        websocket = self.FakeWebSocket()
        self.server.client_authority[websocket] = "dashboard"
        payload = Path(self.temp_dir.name) / "runtime-package"
        payload.mkdir()
        runtime = payload / "nvngx_dlssnr.dll"
        image = bytearray(0x86)
        image[0:2] = b"MZ"
        image[0x3C:0x40] = (0x80).to_bytes(4, "little")
        image[0x80:0x84] = b"PE\0\0"
        image[0x84:0x86] = (0x8664).to_bytes(2, "little")
        runtime.write_bytes(image)
        digest = self.server.asset_store.sha256(runtime)
        addon_package = {"provider": "lsp-neural-render", "version": "v2.0"}
        runtime_package = {
            "provider": "dlssnr-community-runtime",
            "version": "310.8.SF-v2",
            "archive_sha256": "a" * 64,
            "payload_path": str(payload),
            "files": [{
                "relative_path": runtime.name,
                "sha256": digest,
                "architecture": "x64",
            }],
        }
        self.server.release_manager = Mock()

        def check(provider, **_kwargs):
            if provider == "lsp-neural-render":
                return [{"version": "v2.0", "assets": [{"name": "NeuralRender.zip"}]}]
            return [{
                "version": "310.8.SF-v2",
                "assets": [{
                    "name": "nvngx_dlssnr_310.8.SF-v2.zip",
                    "digest": "sha256:" + "a" * 64,
                }],
            }]

        self.server.release_manager.check.side_effect = check
        self.server.release_manager.stage_release.side_effect = (
            lambda provider, *_args, **_kwargs:
            addon_package if provider == "lsp-neural-render" else runtime_package
        )

        await self.server.process_message(websocket, json.dumps({
            "type": "INSTALL_LATEST_GRAPHICS_ADDON",
            "provider": "lsp-neural-render",
            "acceptCommunityRuntime": True,
            "operationId": "operation123",
        }))

        response = json.loads(websocket.messages[-1])
        self.assertEqual(response["type"], "NEURAL_RENDER_BUNDLE_STAGED")
        self.assertEqual(response["runtime"]["filename"], "nvngx_dlssnr.dll")
        self.assertEqual(response["runtime"]["source"]["kind"], "community-release")
        self.assertEqual(self.server.release_manager.stage_release.call_count, 2)

    async def test_neural_render_install_requires_community_runtime_confirmation(self):
        websocket = self.FakeWebSocket()
        self.server.client_authority[websocket] = "dashboard"
        self.server.release_manager = Mock()

        await self.server.process_message(websocket, json.dumps({
            "type": "INSTALL_LATEST_GRAPHICS_ADDON",
            "provider": "lsp-neural-render",
            "operationId": "operation123",
        }))

        self.assertIn("Confirm the warning", websocket.messages[-1])
        self.server.release_manager.check.assert_not_called()

    async def test_daily_update_status_compares_installed_and_detected_versions(self):
        self.server.release_manager = Mock()
        versions = {
            "lossless-proxy": "v2.0.0",
            "lsp-neural-render": "v1.0.0",
            "reshade": "6.8.0",
            "special-k": "SK_26_6_13_7",
        }
        self.server.release_manager.check.side_effect = lambda provider, **_: [
            {"version": versions[provider]}
        ]
        self.server.asset_store = Mock()
        self.server.asset_store.list_packages.return_value = [
            {"provider": "lossless-proxy", "version": "v1.0.0"},
            {"provider": "lsp-neural-render", "version": "v1.0.0"},
        ]
        sources = {
            "reshade": {"detected": True, "version": "6.7.0"},
            "special-k": {"detected": True, "version": "26.6.13.7"},
        }
        with patch(
            "companion.services.server.GraphicsSourceDetector.status",
            return_value=sources,
        ):
            status = await self.server._refresh_addon_updates(force=False)

        self.assertEqual(
            [item["provider"] for item in status["updates"]],
            ["lossless-proxy", "reshade"],
        )

    async def test_readonly_local_client_cannot_mutate_profiles(self):
        websocket = self.FakeWebSocket()
        initial_count = len(self.manager.config.profiles)

        await self.server.process_message(
            websocket,
            '{"type":"DELETE_PROFILE","profileId":"default-browser"}',
        )

        self.assertEqual(len(self.manager.config.profiles), initial_count)
        self.assertIn("Dashboard capability token required", websocket.messages[-1])

    async def test_rtss_profile_delete_requires_confirmation(self):
        fake_rtss = FakeRtssManager()
        self.server.rtss_manager = fake_rtss
        profile = self.manager.get_profile_by_id("default-browser")
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

    async def test_default_profile_cannot_be_deleted(self):
        native = self.manager.ensure_lossless_default_profile({"Title": "Default", "Path": ""})
        self.assertTrue(native.is_default)
        websocket = self.FakeWebSocket()
        self.server.client_authority[websocket] = "dashboard"

        await self.server.process_message(
            websocket,
            json.dumps({"type": "DELETE_PROFILE", "profileId": native.id}),
        )
        self.assertIsNotNone(self.manager.get_profile_by_id(native.id))
        self.assertIn("default profile cannot be deleted", websocket.messages[-1].casefold())

    async def test_delete_unknown_profile_is_rejected(self):
        websocket = self.FakeWebSocket()
        self.server.client_authority[websocket] = "dashboard"

        await self.server.process_message(
            websocket,
            json.dumps({"type": "DELETE_PROFILE", "profileId": "does-not-exist"}),
        )
        self.assertIn("unknown profile", websocket.messages[-1].casefold())

    async def test_global_rtss_switch_suspends_and_reapplies_profile_settings(self):
        fake_rtss = FakeRtssManager()
        self.server.rtss_manager = fake_rtss
        self.server.startup_manager = FakeStartupManager()
        profile = self.manager.get_profile_by_id("default-browser")
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
            "smartAutoScaleEnabled": True,
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
        self.assertTrue(self.manager.config.default_profile_auto_scale)
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

        profile = self.manager.get_profile_by_id("default-browser")
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
        self.manager.config.global_hotkey.activation_delay_ms = 0
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
        self.manager.config.global_hotkey.activation_delay_ms = 200
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
