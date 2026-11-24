import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from companion.core.profile_manager import ProfileManager
from companion.services.asset_store import AssetStore
from companion.services.deployment_manager import DeploymentManager
from companion.services.reshade_profiles import (
    BUILTIN_PROFILES, ReshadeProfileService, LILIUM_INPUT_EFFECT,
    LILIUM_CAS_EFFECT, LILIUM_CAS_TECHNIQUE, LEGACY_CAS_EFFECT, LEGACY_CAS_PROVIDER,
)


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
            for filename in profile.shaders.get("22", []):
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

    def test_default_refresh_restores_preparation_and_order_and_keeps_tuning_and_backup(self):
        profile = self.manager.config.reshade_profiles[2]
        preset = self.shared(profile)
        self.edit(preset, "0.73", disable=True)
        parser = self.service._parse_preset(preset.read_text(encoding="utf-8"))
        parser["lilium__inverse_tone_mapping.fx"]["InputTrc"] = "4"
        parser["lilium__inverse_tone_mapping.fx"]["GammaIn"] = "0.15"
        preset.write_text(self.service._preset_text(parser), encoding="utf-8")
        before = preset.read_text(encoding="utf-8")
        # A user preset and its tuning are outside the refresh scope.
        custom = profile.model_copy(update={"id": "custom", "name": "Custom"}, deep=True)
        self.manager.config.reshade_profiles.append(custom)
        custom_folder = self.service._folder(custom.id)
        custom_folder.mkdir()
        custom_preset = custom_folder / "Custom.ini"
        custom_preset.write_text("Techniques=custom\n", encoding="utf-8")
        with patch.object(self.service, "_install_selected_shaders") as install:
            result = self.service.refresh_builtin_profiles()
        install.assert_not_called()
        self.assertEqual(result["errors"], [])
        self.assertEqual(len(result["refreshed"]), 3)
        saved = self.service._parse_preset(preset.read_text(encoding="utf-8"))
        expected = ("LSC_PrepareLiliumSDR@LSC_PrepareLiliumSDR.fx,"
                    "lilium__inverse_tone_mapping@lilium__inverse_tone_mapping.fx," +
                    LILIUM_CAS_TECHNIQUE)
        self.assertEqual(saved["__LSC_PRESET_ROOT__"]["Techniques"], expected)
        self.assertEqual(saved["__LSC_PRESET_ROOT__"]["TechniqueSorting"], expected)
        self.assertEqual(saved["Shader.fx"]["Strength"], "0.73")
        self.assertEqual(saved["lilium__inverse_tone_mapping.fx"]["InputTrc"], "2")
        self.assertEqual(saved["lilium__inverse_tone_mapping.fx"]["GammaIn"], "0.15")
        backups = list(preset.parent.glob("default-refresh-backups/*/*.ini"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), before)
        self.assertEqual(custom_preset.read_text(encoding="utf-8"), "Techniques=custom\n")
        deployed = self.service.deployment_files(profile.id)
        self.assertFalse(any("default-refresh-backups" in item["relative_path"] for item in deployed))

    def test_failed_default_acquisition_reports_error_and_keeps_existing_preset(self):
        profile = self.manager.config.reshade_profiles[1]
        before = self.shared(profile).read_bytes()
        missing = self.service._folder(profile.id) / "packages/22/Shaders/lilium__inverse_tone_mapping.fx"
        missing.unlink()
        with patch.object(self.service, "_install_selected_shaders", side_effect=OSError("offline")):
            result = self.service.refresh_builtin_profiles()
        self.assertEqual(result["errors"], [{"id": profile.id, "message": "offline"}])
        self.assertEqual(len(result["refreshed"]), 2)
        self.assertEqual(self.shared(profile).read_bytes(), before)

    def test_amd_cas_migrates_back_to_lilium_with_preparation_first_and_preserves_disabled_cas(self):
        for cas_enabled in (True, False):
            with self.subTest(cas_enabled=cas_enabled):
                current = self.manager.config.reshade_profiles[2]
                legacy = current.model_copy(update={"shaders": {
                    "22": ["lilium__inverse_tone_mapping.fx"],
                    LEGACY_CAS_PROVIDER: [LEGACY_CAS_EFFECT]}}, deep=True)
                self.manager.config.reshade_profiles[2] = legacy
                preset = self.shared(legacy)
                cas = f"LSC_AMD_CAS@{LEGACY_CAS_EFFECT}"
                itm = "lilium__inverse_tone_mapping@lilium__inverse_tone_mapping.fx"
                retired = preset.parent / "packages" / LEGACY_CAS_PROVIDER / LEGACY_CAS_EFFECT
                retired.parent.mkdir(exist_ok=True)
                retired.write_text("technique LSC_AMD_CAS { pass {} }", encoding="utf-8")
                preset.write_text(f"Techniques={cas + ',' if cas_enabled else ''}{itm}\n"
                    f"TechniqueSorting={cas},{itm}\n[LS Companion]\nEffectsInitialized=1\nLiliumInputVersion=1\n"
                    "[lilium__inverse_tone_mapping.fx]\nInputTrc=2\nGammaIn=0.15\n"
                    f"[{LEGACY_CAS_EFFECT}]\nSharpness=0.444444444\nStrength=1.0\n", encoding="utf-8")
                self.service._builtins_checked = False
                with patch.object(self.service, "_install_selected_shaders") as install:
                    self.service.payload()
                install.assert_not_called()
                migrated = self.manager.config.reshade_profiles[2]
                self.assertEqual(migrated.shaders, BUILTIN_PROFILES[2].shaders)
                saved = self.service._parse_preset(preset.read_text(encoding="utf-8"))
                enabled = saved["__LSC_PRESET_ROOT__"]["Techniques"].split(",")
                expected = ["LSC_PrepareLiliumSDR@LSC_PrepareLiliumSDR.fx", itm]
                self.assertEqual(enabled, expected + ([LILIUM_CAS_TECHNIQUE] if cas_enabled else []))
                self.assertEqual(saved["__LSC_PRESET_ROOT__"]["TechniqueSorting"], ",".join(expected + [LILIUM_CAS_TECHNIQUE]))
                self.assertAlmostEqual(float(saved[LILIUM_CAS_EFFECT]["SHARPEN_AMOUNT"]), 0.15, places=6)
                self.assertEqual(saved["lilium__inverse_tone_mapping.fx"]["GammaIn"], "0.15")
                files = self.service.deployment_files(legacy.id)
                self.assertTrue(any(item["relative_path"].endswith(LILIUM_CAS_EFFECT) for item in files))
                self.assertFalse(any(item["relative_path"].endswith(LEGACY_CAS_EFFECT) for item in files))
                self.assertFalse(retired.exists())

    def test_basic_lilium_cas_uses_the_lilium_package(self):
        profile = self.manager.config.reshade_profiles[0]
        self.service._write_files(profile)
        saved = self.service._parse_preset(self.shared(profile).read_text(encoding="utf-8"))
        self.assertEqual(saved["__LSC_PRESET_ROOT__"]["Techniques"], LILIUM_CAS_TECHNIQUE)
        self.assertEqual(profile.shaders, {"22": [LILIUM_CAS_EFFECT]})

    def test_failed_lilium_download_keeps_existing_amd_preset_and_reports_not_ready(self):
        current = self.manager.config.reshade_profiles[0]
        legacy = current.model_copy(update={"shaders": {LEGACY_CAS_PROVIDER: [LEGACY_CAS_EFFECT]}}, deep=True)
        self.manager.config.reshade_profiles[0] = legacy
        folder = self.service._folder(legacy.id)
        (folder / "packages/22/Shaders" / LILIUM_CAS_EFFECT).unlink()
        old_shader = folder / "packages" / LEGACY_CAS_PROVIDER / LEGACY_CAS_EFFECT
        old_shader.parent.mkdir()
        old_shader.write_text("technique LSC_AMD_CAS { pass {} }", encoding="utf-8")
        preset = self.shared(legacy)
        original = f"Techniques=LSC_AMD_CAS@{LEGACY_CAS_EFFECT}\n[LS Companion]\nEffectsInitialized=1\n"
        preset.write_text(original, encoding="utf-8")
        with patch.object(self.service, "_install_selected_shaders", side_effect=OSError("offline")):
            with self.assertRaisesRegex(ValueError, "default presets could not be prepared"):
                self.service.prepare_builtin_profiles()
        self.assertIs(self.manager.config.reshade_profiles[0], legacy)
        self.assertEqual(preset.read_text(encoding="utf-8"), original)
        self.assertTrue(old_shader.is_file())

    def test_amd_migration_keeps_custom_name_other_effects_and_existing_lilium_tuning(self):
        custom = BUILTIN_PROFILES[0].model_copy(update={"id": "custom-cas", "name": "My CAS",
            "shaders": {LEGACY_CAS_PROVIDER: [LEGACY_CAS_EFFECT], "22": ["Extra.fx"]}}, deep=True)
        self.manager.config.reshade_profiles.append(custom)
        folder = self.service._folder(custom.id)
        package = folder / "packages/22/Shaders"
        package.mkdir(parents=True)
        (package / LILIUM_CAS_EFFECT).write_text("technique lilium__cas_hdr_ps { pass {} }", encoding="utf-8")
        (package / "Extra.fx").write_text("technique Extra { pass {} }", encoding="utf-8")
        preset = self.shared(custom)
        preset.write_text(f"Techniques=LSC_AMD_CAS@{LEGACY_CAS_EFFECT},Extra@Extra.fx\n"
            "[LS Companion]\nEffectsInitialized=1\n[lilium__cas_hdr.fx]\nSHARPEN_AMOUNT=0.13\n"
            "[Extra.fx]\nStrength=0.7\n", encoding="utf-8")
        self.service._ensure_builtin_profiles()
        migrated = self.manager.config.reshade_profiles[-1]
        self.assertEqual(migrated.name, "My CAS")
        self.assertEqual(migrated.shaders, {"22": ["Extra.fx", LILIUM_CAS_EFFECT]})
        saved = self.service._parse_preset(preset.read_text(encoding="utf-8"))
        self.assertEqual(saved[LILIUM_CAS_EFFECT]["SHARPEN_AMOUNT"], "0.13")
        self.assertEqual(saved["Extra.fx"]["Strength"], "0.7")
        self.assertEqual(set(saved["__LSC_PRESET_ROOT__"]["Techniques"].split(",")),
                         {"Extra@Extra.fx", LILIUM_CAS_TECHNIQUE})

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
                for effect in [name for files in profile.shaders.values() for name in files]:
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
        updated.shaders.setdefault("22", []).append("New.fx")
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
