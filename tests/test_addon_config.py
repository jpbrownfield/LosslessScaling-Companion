import unittest
import tempfile
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock

from companion.core.models import Profile, ReshadeConfig
from companion.services.addon_config import AddonConfigService
from companion.services.proxy_menu import AddonManagerMenuController
from companion.services.release_providers import ProviderRegistry


class AddonConfigServiceTests(unittest.TestCase):
    def test_native_fps_corner_survives_panel_capture_and_profile_materialization(self):
        import json
        for corner in range(4):
            with self.subTest(corner=corner), tempfile.TemporaryDirectory() as directory:
                manager = SimpleNamespace(config_dir=directory, config=SimpleNamespace(default_addon_config={}), save_config=Mock())
                service = AddonConfigService(manager)
                live = Path(directory) / "addons/config.json"
                live.parent.mkdir()
                feature = {"_enabled": True, "fps_counter_corner": str(corner)}
                live.write_text(json.dumps({"addons": {"LosslessScalingOverrides": feature}}))
                profile = Profile(name="Game")
                self.assertTrue(service.capture(profile, str(Path(directory) / "LosslessScaling.exe")))
                materialized = service.materialize(profile, reshade_enabled=False)
                result = json.loads(materialized.read_text())
                self.assertEqual(result["addons"]["LosslessScalingOverrides"], feature)

    def test_panel_upscaler_selection_updates_scaling_alias_and_keeps_advanced_values(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = SimpleNamespace(config_dir=directory, config=SimpleNamespace(default_addon_config={}), save_config=Mock())
            service = AddonConfigService(manager)
            live = Path(directory) / "addons/config.json"
            live.parent.mkdir()
            live.write_text('{"addons":{"XESSUPSC":{"_enabled":true,"sharpen":"0.7","scalerEdges":"0.4"}}}')
            profile = Profile(name="Game", native_scaling_settings={"ScalingType": "LS1"})
            self.assertTrue(service.capture(profile, str(Path(directory) / "LosslessScaling.exe")))
            self.assertEqual(profile.native_scaling_settings["ScalingType"], "NIS")
            self.assertEqual(profile.addon_config["addons"]["XESSUPSC"]["scalerEdges"], "0.4")
            config = service.effective(profile, reshade_enabled=False)
            self.assertFalse(config["global"]["companion"]["headless"])
            self.assertTrue(config["global"]["companion"]["managed"])
            self.assertFalse(config["global"]["ui"]["open_on_start"])

    def test_hidden_controls_replace_unsafe_saved_values_without_enabling_processors(self):
        from companion.services.addon_schema import ADDONS, MANAGED_DEFAULTS

        saved = {addon_id: dict.fromkeys(defaults, "unexpected")
                 for addon_id, defaults in MANAGED_DEFAULTS.items()}
        saved["DLSSNR02"].update(_enabled=True, intensity="0.7", futureTuning="keep")
        profile = Profile(name="Imported", addon_config={"addons": saved})
        result = self.service().effective(profile, reshade_enabled=False)["addons"]
        for addon_id, defaults in MANAGED_DEFAULTS.items():
            for key, value in defaults.items():
                self.assertEqual(result[addon_id][key], value)
                field = next(f for addon in ADDONS if addon["id"] == addon_id
                             for f in addon["fields"] if f["key"] == key)
                self.assertTrue(field["hidden"])
        self.assertTrue(result["DLSSNR02"]["_enabled"])
        self.assertFalse(result["FSR3UPSC"]["_enabled"])
        self.assertEqual(result["DLSSNR02"]["intensity"], "0.7")
        self.assertEqual(result["DLSSNR02"]["futureTuning"], "keep")
        self.assertEqual(profile.addon_config["addons"]["DLSSNR02"]["enabled"], "unexpected")

    def test_supported_features_are_opt_in_and_unknown_settings_survive(self):
        service = self.service()
        baseline = service.effective(Profile(name="Game"), reshade_enabled=False)
        self.assertEqual(set(baseline["addons"]), {"DLSSNR02", "DLSS4DLAA", "FSR3UPSC", "XESSUPSC", "WindowedMode", "ReShadePassthrough"})
        self.assertTrue(all(not entry["_enabled"] for entry in baseline["addons"].values()))
        profile = Profile(name="Game", addon_config={"global": {"future": 7}, "addons": {"Future": {"_enabled": True}}})
        result = service.effective(profile, reshade_enabled=False)
        self.assertEqual(result["global"]["future"], 7)
        self.assertTrue(result["addons"]["Future"]["_enabled"])
        self.assertFalse(result["global"]["companion"]["headless"])
        self.assertTrue(result["global"]["companion"]["managed"])

    def test_echo_attribution_is_shipped_and_visible(self):
        root = Path(__file__).resolve().parents[1]
        notice = (root / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
        dashboard = (root / "companion" / "ui" / "dashboard.html").read_text(encoding="utf-8")
        spec = (root / "LosslessCompanion.spec").read_text(encoding="utf-8")
        self.assertIn("Copyright (c) 2026 Echo-Storm", notice)
        self.assertIn("FrankBarretta", notice)
        self.assertIn("FriedSnowman/ls-addon-manager/blob/build-5/NOTICE.md", dashboard)
        self.assertIn('("THIRD_PARTY_NOTICES.md", ".")', spec)
        self.assertIn('copy_metadata("py7zr", recursive=True)', spec)

    def service(self):
        manager = SimpleNamespace(
            config_dir=".",
            config=SimpleNamespace(default_addon_config={}),
        )
        return AddonConfigService(manager)

    def test_effective_config_preserves_unknown_echo_settings(self):
        profile = Profile(
            name="Game",
            addon_config={
                "global": {"custom": {"keep": 7}},
                "addons": {"FutureAddon": {"setting": "value"}},
            },
        )
        result = self.service().effective(
            profile, reshade_enabled=False, hotkey_vk=35
        )
        self.assertEqual(result["global"]["custom"]["keep"], 7)
        self.assertEqual(result["addons"]["FutureAddon"]["setting"], "value")
        self.assertFalse(result["global"]["ui"]["open_on_start"])
        self.assertFalse(result["global"]["companion"]["headless"])

    def test_reshade_forces_echo_passthrough(self):
        profile = Profile(name="Game", reshade=ReshadeConfig(enabled=True))
        result = self.service().effective(
            profile, reshade_enabled=True, hotkey_vk=35
        )
        passthrough = result["addons"]["ReShadePassthrough"]
        self.assertTrue(passthrough["_enabled"])
        self.assertEqual(passthrough["hotkey_vk"], "35")
        self.assertEqual(passthrough["companion_control"], "1")

    def test_current_menu_key_replaces_stale_profile_key_and_modifier_flags(self):
        profile = Profile(name="Game", addon_config={"addons": {"ReShadePassthrough": {
            "hotkey_vk": "36", "hotkey_ctrl": "1", "hotkey_alt": "1", "hotkey_shift": "1"}}})
        result = self.service().effective(profile, reshade_enabled=True, hotkey_vk=35)
        binding = result["addons"]["ReShadePassthrough"]
        self.assertEqual(binding["hotkey_vk"], "35")
        self.assertEqual([binding[key] for key in ("hotkey_ctrl", "hotkey_alt", "hotkey_shift")], ["0"] * 3)

    def test_native_passthrough_supports_the_same_keys_as_the_settings_picker(self):
        from companion.services.automation import AutomationController

        self.assertEqual(AutomationController._echo_hotkey_vk("f24"), 0x87)
        self.assertEqual(AutomationController._echo_hotkey_vk("1"), ord("1"))

    def test_reshade_combination_keeps_native_modifiers_in_sync(self):
        from companion.services.overlay_shortcuts import reshade_key_data

        for shortcut, expected in (("end", (35, 0, 0, 0)),
                                   ("ctrl+alt+f9", (120, 1, 0, 1)),
                                   ("shift+f10", (121, 0, 1, 0))):
            with self.subTest(shortcut=shortcut):
                profile = Profile(name="Game")
                config = self.service().effective(profile, reshade_enabled=True, hotkey=shortcut)
                native = config["addons"]["ReShadePassthrough"]
                self.assertEqual(reshade_key_data(shortcut), expected)
                self.assertEqual(tuple(int(native[key]) for key in
                    ("hotkey_vk", "hotkey_ctrl", "hotkey_shift", "hotkey_alt")), expected)
        for shortcut in ("", "ctrl", "ctrl+ctrl+f9", "win+f9"):
            with self.subTest(shortcut=shortcut), self.assertRaises(ValueError):
                reshade_key_data(shortcut)

    def test_legacy_neural_selection_does_not_implicitly_enable_echo_dlss5(self):
        profile = Profile(name="Game")
        profile.graphics.neural_render.implementation = "lsp_neural_render"
        profile.graphics.neural_render.sampling_resolution_preset = "quality"
        result = self.service().effective(
            profile, reshade_enabled=False
        )
        neural = result["addons"]["DLSSNR02"]
        self.assertFalse(neural["_enabled"])

    def test_missing_addon_entries_are_explicitly_disabled(self):
        result = self.service().effective(Profile(name="Game"), reshade_enabled=False)
        self.assertTrue(result["addons"])
        self.assertTrue(all(section["_enabled"] is False for section in result["addons"].values()))

    def test_internal_enabled_setting_does_not_opt_in_to_loading_an_addon(self):
        profile = Profile(name="Game", addon_config={"addons": {"DLSSNR02": {"enabled": True}}})
        section = self.service().effective(profile, reshade_enabled=False)["addons"]["DLSSNR02"]
        self.assertFalse(section["_enabled"])
        self.assertTrue(section["enabled"])

    def test_unconfigured_profile_does_not_inherit_enabled_default_processors(self):
        service = self.service()
        service.profile_manager.config.default_addon_config = {
            "addons": {"DLSSNR02": {"_enabled": True, "workingScale": "0.7"}}
        }
        neural = service.effective(Profile(name="New"), reshade_enabled=False)["addons"]["DLSSNR02"]
        self.assertFalse(neural["_enabled"])
        self.assertEqual(neural["workingScale"], "0.7")

    def test_pending_editor_changes_are_not_overwritten_by_live_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = SimpleNamespace(config_dir=directory, config=SimpleNamespace(default_addon_config={}), save_config=Mock())
            service = AddonConfigService(manager)
            executable = Path(directory) / "LosslessScaling.exe"
            live = Path(directory) / "addons" / "config.json"
            live.parent.mkdir()
            live.write_text('{"addons":{"DLSSNR02":{"_enabled":true}}}', encoding="utf-8")
            profile = Profile(name="Game", addon_config={"addons": {"DLSSNR02": {"_enabled": False}}})
            service.mark_edited(profile)
            self.assertFalse(service.capture(profile, str(executable)))
            self.assertFalse(profile.addon_config["addons"]["DLSSNR02"]["_enabled"])
            service.mark_applied(profile)
            self.assertTrue(service.capture(profile, str(executable)))
            self.assertTrue(profile.addon_config["addons"]["DLSSNR02"]["_enabled"])

    def test_newly_discovered_addon_is_off_even_if_upstream_defaults_it_on(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "addons" / "FutureProcessor" / "addon.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text('{"name":"Future processor","enabled_by_default":true}', encoding="utf-8")
            service = self.service()
            catalog = service.catalog([{
                "provider": "ls-addon-manager", "payload_path": directory,
                "files": [{"relative_path": "addons/FutureProcessor/addon.json"}],
            }])
            self.assertNotIn("FutureProcessor", {item["id"] for item in catalog})
            result = service.effective(Profile(name="Game"), reshade_enabled=False)
            self.assertFalse(result["addons"]["FutureProcessor"]["_enabled"])

    def test_editor_retains_manual_addons_without_exposing_undocumented_controls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "addons" / "ManualAddon" / "addon.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text('{"name":"Manual add-on"}', encoding="utf-8")
            service = self.service()
            service.profile_manager.config.lossless_scaling_exe_path = str(root / "LosslessScaling.exe")
            self.assertNotIn("ManualAddon", {item["id"] for item in service.catalog()})
            config = service.effective(Profile(name="Game"), reshade_enabled=False)
            self.assertFalse(config["addons"]["ManualAddon"]["_enabled"])

    def test_internal_gpu_reporter_is_enabled_without_exposing_a_processor_toggle(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "addons/LSC-AdapterReport/addon.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text('{"name":"GPU reporter"}', encoding="utf-8")
            service = self.service()
            catalog = service.catalog([{"provider": "ls-addon-manager", "payload_path": directory,
                "files": [{"relative_path": "addons/LSC-AdapterReport/addon.json"}]}])
            self.assertNotIn("LSC-AdapterReport", {item["id"] for item in catalog})
            config = service.effective(Profile(name="Game"), reshade_enabled=False)
            self.assertTrue(config["addons"]["LSC-AdapterReport"]["_enabled"])
            self.assertFalse(config["addons"]["DLSSNR02"]["_enabled"])

    def test_existing_echo_neural_settings_are_authoritative(self):
        profile = Profile(
            name="Game",
            addon_config={
                "global": {},
                "addons": {"DLSSNR02": {
                    "_enabled": False,
                    "workingScale": "0.73",
                    "futureSetting": "preserved",
                }},
            },
        )
        profile.graphics.neural_render.implementation = "lsp_neural_render"
        profile.graphics.neural_render.sampling_resolution_preset = "quality"

        neural = self.service().effective(
            profile, reshade_enabled=False
        )["addons"]["DLSSNR02"]

        self.assertFalse(neural["_enabled"])
        self.assertEqual(neural["workingScale"], "0.73")
        self.assertEqual(neural["futureSetting"], "preserved")

    def test_echo_host_and_dashboard_contract(self):
        self.assertEqual(AddonManagerMenuController.WINDOW_CLASS, "LSAddonManagerClass")
        self.assertIn("ls-addon-manager", ProviderRegistry().list_provider_ids())
        dashboard = (
            Path(__file__).resolve().parents[1] / "companion" / "ui" / "dashboard.html"
        ).read_text(encoding="utf-8")
        self.assertIn('id="addonManagerEnabled"', dashboard)
        self.assertIn("Open advanced panel", dashboard)
        self.assertIn("Add-ons for this profile", dashboard)
        self.assertIn('id="addonEditorCards"', dashboard)
        self.assertNotIn('id="echoConfigJson"', dashboard)


if __name__ == "__main__":
    unittest.main()
