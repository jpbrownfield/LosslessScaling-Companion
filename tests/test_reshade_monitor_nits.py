import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from pydantic import ValidationError
from companion.core.models import ManagedReshadeProfile, Profile, ReshadeSdrWhiteSettings
from companion.core.profile_manager import ProfileManager
from companion.core.state import AppState
from companion.services.automation import AutomationController
from companion.services.reshade_profiles import ReshadeProfileService, LILIUM_INPUT_EFFECT


class ReShadeMonitorNitsTests(unittest.TestCase):
    def test_default_and_override_validation(self):
        self.assertEqual(ReshadeSdrWhiteSettings().default_nits, 400)
        settings = ReshadeSdrWhiteSettings(monitors={r"MONITOR\A": 650})
        self.assertEqual(settings.monitors, {r"monitor\a": 650})
        for invalid in ({"default_nits": 0}, {"default_nits": 1001},
                        {"default_nits": 400.5}, {"monitors": {"a": 79}},
                        {"monitors": {"": 400}}, {"monitors": {"a": float("nan")}}):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                ReshadeSdrWhiteSettings(**invalid)

    def test_monitor_variants_preserve_shared_preset_and_other_shader_tuning(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = ProfileManager(config_dir=Path(directory))
            manager.config.reshade_sdr_white = ReshadeSdrWhiteSettings(monitors={"A": 600, "B": 250})
            manager.config.gpu_inventory_snapshot = {"displays": [
                {"deviceId": "B", "name": "Second", "deviceName": "DISPLAY2"},
                {"deviceId": "A", "name": "First", "deviceName": "DISPLAY1"}]}
            profile = ManagedReshadeProfile(id="shared-hdr", name="Shared HDR",
                shaders={"22": ["lilium__inverse_tone_mapping.fx"]})
            manager.config.reshade_profiles = [profile]
            service = ReshadeProfileService(manager)
            package = service._folder(profile.id) / "packages/22"
            package.mkdir(parents=True)
            (package / "lilium__inverse_tone_mapping.fx").write_text(
                "technique lilium__inverse_tone_mapping { pass {} }", encoding="utf-8")
            service._write_files(profile)
            preset = service._folder(profile.id) / "Shared HDR.ini"
            with preset.open("a", encoding="utf-8") as stream:
                stream.write("\n[Other.fx]\nUserTuning=0.75\n")
            for monitor, expected in (("A", 600), ("B", 250), ("unknown", 400), (None, 400)):
                plan = service.deployment_files(profile.id, monitor_id=monitor)
                source = next(item for item in plan if item["role"] == "reshade_preset")
                content = Path(source["source_path"]).read_text(encoding="utf-8")
                self.assertIn(f"SdrWhiteNits={expected:.6f}", content)
                self.assertIn("UserTuning=0.75", content)
                self.assertTrue(source["relative_path"].endswith("Shared HDR.ini"))
            self.assertIn("SdrWhiteNits=400.000000", preset.read_text(encoding="utf-8"))
            manager.config.reshade_sdr_white.default_nits = 450
            service.refresh_hdr_peak_settings()
            self.assertIn("SdrWhiteNits=450.000000", preset.read_text(encoding="utf-8"))
            payload = service.hdr_settings_payload()
            self.assertEqual([monitor["id"] for monitor in payload["monitors"]], ["b", "a"])
            self.assertEqual(payload["monitorSdrWhiteNits"], {"a": 600, "b": 250})
            manager.save_config()
            self.assertEqual(ProfileManager(config_dir=Path(directory)).config.reshade_sdr_white.default_nits, 450)

    def test_deployment_uses_target_output_monitor_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = ProfileManager(config_dir=Path(directory))
            manager.config.reshade_sdr_white.monitors = {"monitor-a": 500}
            controller = AutomationController.__new__(AutomationController)
            controller.capture_reshade_settings = Mock(return_value=False)
            controller.profile_manager = manager
            controller.state = AppState()
            controller.asset_store = SimpleNamespace(list_packages=lambda: [])
            controller.graphics_resolver = Mock()
            controller.graphics_resolver.resolve.return_value = []
            controller.reshade_profiles = Mock()
            controller.reshade_profiles.deployment_files.return_value = []
            controller.gpu_router = Mock()
            controller.gpu_router.detect.return_value = {"displays": [
                {"deviceId": "monitor-b", "deviceName": "DISPLAY2"},
                {"deviceId": "monitor-a", "deviceName": "DISPLAY1"}]}
            controller._desired_gpu_route = Mock(return_value={"deviceName": "DISPLAY1"})
            profile = Profile(name="Game", reshade={"enabled": True, "managed_profile_id": "shared"})
            controller.resolve_deployment_files(profile, target_hwnd=123)
            controller._desired_gpu_route.assert_called_once_with(profile, 123, refresh_selections=False)
            controller.reshade_profiles.deployment_files.assert_called_once_with("shared", monitor_id="monitor-a")
