import json
import tempfile
import unittest
from pathlib import Path

from companion.core.profile_manager import ProfileManager
from companion.services.asset_store import AssetStore
from companion.services.deployment_manager import DeploymentManager
from companion.services.reshade_profiles import BUILTIN_PROFILES, ReshadeProfileService, LILIUM_INPUT_EFFECT


class ReShadePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.manager = ProfileManager(config_dir=self.root / "config")
        self.manager.config.reshade_profiles = [profile.model_copy(deep=True) for profile in BUILTIN_PROFILES]
        self.service = ReshadeProfileService(self.manager)
        self.service._catalog = []
        for profile in self.manager.config.reshade_profiles:
            package = self.service._folder(profile.id) / "packages/22/Shaders"
            package.mkdir(parents=True)
            for filename in profile.shaders["22"]:
                name = "lilium__cas_hdr_ps" if "cas_hdr" in filename else "lilium__inverse_tone_mapping"
                (package / filename).write_text(f"technique {name} {{ pass {{}} }}", encoding="utf-8")
            self.service._write_files(profile)
        self.host = self.root / "LS" / "LosslessScaling.exe"
        self.host.parent.mkdir()
        self.host.write_bytes(b"test host")
        self.deployment = DeploymentManager(AssetStore(str(self.root / "store")))

    def deploy(self, profile, **kwargs):
        plan = self.service.deployment_files(profile.id, **kwargs)
        manifest = self.deployment.apply(profile_id="game", lossless_scaling_exe=str(self.host), files=plan)
        entry = next(item for item in manifest["files"] if item["role"] == "reshade_preset")
        return manifest, self.host.parent / entry["relative_path"]

    def test_managed_menu_shortcut_serializes_ctrl_shift_alt_in_reshade_order(self):
        profile = self.manager.config.reshade_profiles[0]
        for shortcut, expected in (("ctrl+alt+f9", "120,1,0,1"), ("shift+f10", "121,0,1,0")):
            with self.subTest(shortcut=shortcut):
                self.manager.config.reshade_overlay_hotkey = shortcut
                config = self.service.config_path(profile.id, interactive=True).read_text(encoding="utf-8")
                self.assertIn(f"KeyOverlay={expected}", config)
                passive = self.service.config_path(profile.id).read_text(encoding="utf-8")
                self.assertIn("KeyOverlay=0,0,0,0", passive)

    def shared(self, profile):
        return self.service._folder(profile.id) / self.service._preset_filename(profile.name)

    def edit(self, preset, value, *, disable=False):
        parser = self.service._parse_preset(preset.read_text(encoding="utf-8"))
        if not parser.has_section("Shader.fx"):
            parser.add_section("Shader.fx")
        parser["Shader.fx"]["Strength"] = value
        if disable:
            parser["__LSC_PRESET_ROOT__"]["Techniques"] = ""
        preset.write_text(self.service._preset_text(parser), encoding="utf-8")

    def test_all_three_builtins_start_enabled_and_keep_runtime_tuning_across_restart_and_redeploy(self):
        for profile in self.manager.config.reshade_profiles:
            with self.subTest(profile=profile.id):
                initial = self.service._parse_preset(self.shared(profile).read_text(encoding="utf-8"))
                enabled = initial["__LSC_PRESET_ROOT__"]["Techniques"]
                for effect in profile.shaders["22"]:
                    self.assertIn("@" + effect, enabled)
                config = self.service.config_path(profile.id, interactive=True).read_text(encoding="utf-8")
                path = f".\\reshade-shaders\\LSCompanion\\{profile.id}\\{self.shared(profile).name}"
                self.assertIn(f"\nPresetPath={path}\n", config)
                self.assertIn(f"\nStartupPresetPath={path}\n", config)
                self.assertIn("AutoSavePreset=1", config)
                self.assertIn("PerformanceMode=0", config)
                manifest, live = self.deploy(profile)
                self.edit(live, "0.42", disable=True)
                self.assertTrue(self.service.capture_deployed(str(self.host), manifest))
                self.assertFalse(self.service.capture_deployed(str(self.host), manifest))
                # A new Companion process uses the persistent capture receipt,
                # rather than replaying changes against the original baseline.
                restarted = ReshadeProfileService(self.manager)
                self.edit(live, "0.75")
                self.assertTrue(restarted.capture_deployed(str(self.host), manifest))
                self.service._write_files(profile)
                saved = self.service._parse_preset(self.shared(profile).read_text(encoding="utf-8"))
                self.assertEqual(saved["Shader.fx"]["Strength"], "0.75")
                self.assertEqual(saved["__LSC_PRESET_ROOT__"]["Techniques"], "")
                _, redeployed = self.deploy(profile)
                self.assertIn("Strength=0.75", redeployed.read_text(encoding="utf-8"))

    def test_runtime_edits_keep_general_nits_and_newer_shared_arguments(self):
        profile = self.manager.config.reshade_profiles[1]
        self.manager.config.reshade_sdr_white.monitors = {"monitor-a": 600}
        manifest, live = self.deploy(profile, monitor_id="monitor-a")
        self.edit(live, "0.5")
        self.manager.config.reshade_hdr_peak_nits = 900
        self.service.refresh_hdr_peak_settings()
        self.assertTrue(self.service.capture_deployed(str(self.host), manifest))
        shared = self.shared(profile)
        saved = self.service._parse_preset(shared.read_text(encoding="utf-8"))
        self.assertEqual(saved[LILIUM_INPUT_EFFECT]["SdrWhiteNits"], "400.000000")
        self.assertEqual(saved["lilium__inverse_tone_mapping.fx"]["TargetBrightness"], "900.000000")
        self.edit(shared, "0.9")
        self.edit(live, "0.6")
        with self.assertLogs("companion.services.reshade_profiles", level="WARNING"):
            self.service.capture_deployed(str(self.host), manifest)
        self.assertIn("Strength=0.9", shared.read_text(encoding="utf-8"))
        receipts = list(shared.parent.glob(".runtime-capture-*.json"))
        self.assertTrue(any("Strength=0.6" in json.loads(p.read_text())["runtimeText"] for p in receipts))

    def test_newly_selected_effect_starts_enabled_without_reenabling_saved_disabled_effects(self):
        profile = self.manager.config.reshade_profiles[0]
        shared = self.shared(profile)
        self.edit(shared, "0.8", disable=True)
        package = shared.parent / "packages/22/Shaders"
        (package / "New.fx").write_text("technique NewEffect { pass {} }", encoding="utf-8")
        updated = profile.model_copy(deep=True)
        updated.shaders["22"].append("New.fx")
        self.service._write_files(updated)
        saved = self.service._parse_preset(shared.read_text(encoding="utf-8"))
        self.assertEqual(saved["__LSC_PRESET_ROOT__"]["Techniques"], "NewEffect@New.fx")
        self.assertEqual(saved["Shader.fx"]["Strength"], "0.8")

    def test_partial_or_unowned_runtime_preset_is_not_imported(self):
        profile = self.manager.config.reshade_profiles[0]
        manifest, live = self.deploy(profile)
        previous = self.shared(profile).read_bytes()
        live.write_text("[Shader.fx]\nStrength=0.99\n", encoding="utf-8")
        self.assertFalse(self.service.capture_deployed(str(self.host), manifest))
        self.assertEqual(self.shared(profile).read_bytes(), previous)
        self.edit(live, "0.99")
        self.assertFalse(self.service.capture_deployed(str(self.host), {"files": []}))
