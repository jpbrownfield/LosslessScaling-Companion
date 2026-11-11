import json
import os
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import Mock

from companion.core.models import Profile
from companion.core.profile_manager import ProfileManager
from companion.core.state import AppState
from companion.services.automation import AutomationController
from companion.services.graphics_resolver import GraphicsResolutionError


def x64_image(payload=b"model"):
    image = bytearray(0x86)
    image[:2] = b"MZ"
    image[0x3C:0x40] = (0x80).to_bytes(4, "little")
    image[0x80:0x84] = b"PE\0\0"
    image[0x84:0x86] = (0x8664).to_bytes(2, "little")
    return bytes(image) + payload


class NeuralToggleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.manager = ProfileManager(self.root / "config")
        self.state = AppState()
        self.automation = AutomationController(self.manager, self.state)
        self.resolver = self.automation.graphics_resolver
        self.executable = self.root / "LosslessScaling.exe"
        self.executable.write_bytes(x64_image())
        (self.root / "Lossless.dll").write_bytes(x64_image(b"original"))
        self.manager.config.lossless_scaling_exe_path = str(self.executable)
        archive = self.root / "host.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("Lossless.dll", x64_image(b"host"))
            bundle.writestr("lossless-scaling-deployment.json", json.dumps({
                "schema_version": 1, "files": [{"source": "Lossless.dll",
                    "destination": "Lossless.dll", "role": "addon_manager_host"}]}))
        self.automation.asset_store.import_release_archive(str(archive),
            provider="ls-addon-manager", version="build-5-lsc.5", source_metadata={})

    def import_model(self, payload=b"model", imported_at=100):
        source = self.root / "nvngx_dlssnr.dll"
        source.write_bytes(x64_image(payload))
        item = self.automation.asset_store.import_file(str(source))
        metadata = Path(item["path"]).parent / "asset.json"
        os.utime(metadata, (imported_at, imported_at))
        return item

    def enabled_profile(self):
        return Profile(name="Game", target_process="game.exe", addon_config={
            "global": {}, "addons": {"DLSSNR02": {"_enabled": True}}})

    def plan(self, profile):
        return self.resolver.resolve(profile, lossless_scaling_exe=str(self.executable),
            addon_manager_enabled=True, addon_manager_config=self.manager.config.addon_manager)

    def test_download_then_card_toggle_attaches_and_deploys_model_without_live_restart(self):
        model = self.import_model()
        profile = self.manager.create_profile_from_process("game.exe")
        self.state.current_active_profile = profile
        self.state.is_scaling_active = True
        self.automation.activate_profile = Mock()
        saved = self.automation.save_addon_config(profile.id, self.enabled_profile().addon_config)
        self.assertEqual(saved.graphics.neural_render.runtime_asset_sha256, model["sha256"])
        self.assertEqual(saved.graphics.neural_render.implementation, "lsp_neural_render")
        reloaded = ProfileManager(self.manager.config_dir).get_profile_by_id(profile.id)
        self.assertEqual(reloaded.graphics.neural_render.runtime_asset_sha256, model["sha256"])
        deployed = next(item for item in self.plan(saved) if item["role"] == "neural_runtime")
        self.assertEqual(Path(deployed["source_path"]), Path(model["path"]))
        self.assertEqual(deployed["relative_path"], "nvngx_dlssnr.dll")
        self.automation.activate_profile.assert_not_called()
        self.assertTrue(self.automation.profile_reload_pending(profile.id))

    def test_deployment_repairs_previously_enabled_unpinned_profile(self):
        model = self.import_model()
        profile = self.enabled_profile()
        files = self.plan(profile)
        self.assertEqual(profile.graphics.neural_render.runtime_asset_sha256, model["sha256"])
        self.assertEqual(sum(item["role"] == "neural_runtime" for item in files), 1)

    def test_newest_valid_import_is_selected_and_existing_pin_is_preserved(self):
        older = self.import_model(b"older", 100)
        newer = self.import_model(b"newer", 200)
        profile = self.enabled_profile()
        self.resolver.select_neural_runtime(profile)
        self.assertEqual(profile.graphics.neural_render.runtime_asset_sha256, newer["sha256"])
        profile.graphics.neural_render.runtime_asset_sha256 = older["sha256"]
        self.resolver.select_neural_runtime(profile)
        self.assertEqual(profile.graphics.neural_render.runtime_asset_sha256, older["sha256"])

    def test_missing_model_is_an_actionable_deployment_error(self):
        with self.assertRaisesRegex(GraphicsResolutionError, "Download.*Neural Rendering model"):
            self.plan(self.enabled_profile())

    def test_newer_incompatible_import_does_not_displace_valid_model(self):
        valid = self.import_model()
        source = self.root / "nvngx_dlssnr.dll"
        image = bytearray(x64_image(b"incompatible"))
        image[0x84:0x86] = (0x14C).to_bytes(2, "little")
        source.write_bytes(image)
        incompatible = self.automation.asset_store.import_file(str(source))
        os.utime(Path(incompatible["path"]).parent / "asset.json", (200, 200))
        profile = self.enabled_profile()
        self.resolver.select_neural_runtime(profile)
        self.assertEqual(profile.graphics.neural_render.runtime_asset_sha256, valid["sha256"])

    def test_disabled_profile_does_not_require_or_attach_a_model(self):
        self.import_model()
        profile = Profile(name="Disabled")
        self.assertFalse(any(item["role"] == "neural_runtime" for item in self.plan(profile)))
        self.assertIsNone(profile.graphics.neural_render.runtime_asset_sha256)

    def test_backend_specific_model_path_does_not_require_an_import(self):
        for key in ("modelPath", "amdModelPath"):
            with self.subTest(key=key):
                profile = self.enabled_profile()
                profile.addon_config["addons"]["DLSSNR02"][key] = "C:/Models/custom.dll"
                self.assertFalse(any(item["role"] == "neural_runtime" for item in self.plan(profile)))

    def test_unavailable_explicit_pin_is_not_silently_replaced(self):
        self.import_model()
        profile = self.enabled_profile()
        profile.graphics.neural_render.runtime_asset_sha256 = "a" * 64
        with self.assertRaisesRegex(GraphicsResolutionError, "not in the managed asset store"):
            self.plan(profile)
