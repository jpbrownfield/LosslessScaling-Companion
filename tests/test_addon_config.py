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
    def test_echo_attribution_is_shipped_and_visible(self):
        root = Path(__file__).resolve().parents[1]
        notice = (root / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
        dashboard = (root / "companion" / "ui" / "dashboard.html").read_text(encoding="utf-8")
        spec = (root / "LosslessCompanion.spec").read_text(encoding="utf-8")
        self.assertIn("Copyright (c) 2026 Echo-Storm", notice)
        self.assertIn("FrankBarretta", notice)
        self.assertIn("Echo-Storm/ls-addon-manager/blob/main/NOTICE.md", dashboard)
        self.assertIn('("THIRD_PARTY_NOTICES.md", ".")', spec)

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
        self.assertFalse(result["global"]["updates"]["check"])

    def test_reshade_forces_echo_passthrough(self):
        profile = Profile(name="Game", reshade=ReshadeConfig(enabled=True))
        result = self.service().effective(
            profile, reshade_enabled=True, hotkey_vk=35
        )
        passthrough = result["addons"]["ReShadePassthrough"]
        self.assertTrue(passthrough["_enabled"])
        self.assertEqual(passthrough["hotkey_vk"], "35")

    def test_legacy_neural_selection_does_not_implicitly_enable_echo_dlss5(self):
        profile = Profile(name="Game")
        profile.graphics.neural_render.implementation = "lsp_neural_render"
        profile.graphics.neural_render.sampling_resolution_preset = "quality"
        result = self.service().effective(
            profile, reshade_enabled=False
        )
        neural = result["addons"]["DLSS5NR01"]
        self.assertFalse(neural["_enabled"])

    def test_missing_addon_entries_are_explicitly_disabled(self):
        result = self.service().effective(Profile(name="Game"), reshade_enabled=False)
        self.assertTrue(result["addons"])
        self.assertTrue(all(section["_enabled"] is False for section in result["addons"].values()))

    def test_internal_enabled_setting_does_not_opt_in_to_loading_an_addon(self):
        profile = Profile(name="Game", addon_config={"addons": {"DLSS5NR01": {"enabled": True}}})
        section = self.service().effective(profile, reshade_enabled=False)["addons"]["DLSS5NR01"]
        self.assertFalse(section["_enabled"])
        self.assertTrue(section["enabled"])

    def test_unconfigured_profile_does_not_inherit_enabled_default_processors(self):
        service = self.service()
        service.profile_manager.config.default_addon_config = {
            "addons": {"DLSS5NR01": {"_enabled": True, "workingScale": "0.7"}}
        }
        neural = service.effective(Profile(name="New"), reshade_enabled=False)["addons"]["DLSS5NR01"]
        self.assertFalse(neural["_enabled"])
        self.assertEqual(neural["workingScale"], "0.7")

    def test_pending_editor_changes_are_not_overwritten_by_live_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = SimpleNamespace(config_dir=directory, config=SimpleNamespace(default_addon_config={}), save_config=Mock())
            service = AddonConfigService(manager)
            executable = Path(directory) / "LosslessScaling.exe"
            live = Path(directory) / "addons" / "config.json"
            live.parent.mkdir()
            live.write_text('{"addons":{"DLSS5NR01":{"_enabled":true}}}', encoding="utf-8")
            profile = Profile(name="Game", addon_config={"addons": {"DLSS5NR01": {"_enabled": False}}})
            service.mark_edited(profile)
            self.assertFalse(service.capture(profile, str(executable)))
            self.assertFalse(profile.addon_config["addons"]["DLSS5NR01"]["_enabled"])
            service.mark_applied(profile)
            self.assertTrue(service.capture(profile, str(executable)))
            self.assertTrue(profile.addon_config["addons"]["DLSS5NR01"]["_enabled"])

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
            self.assertTrue(next(item for item in catalog if item["id"] == "FutureProcessor")["installed"])
            result = service.effective(Profile(name="Game"), reshade_enabled=False)
            self.assertFalse(result["addons"]["FutureProcessor"]["_enabled"])

    def test_editor_discovers_manually_installed_addons_without_loading_them(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "addons" / "ManualAddon" / "addon.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text('{"name":"Manual add-on"}', encoding="utf-8")
            service = self.service()
            service.profile_manager.config.lossless_scaling_exe_path = str(root / "LosslessScaling.exe")
            self.assertIn("ManualAddon", {item["id"] for item in service.catalog()})
            config = service.effective(Profile(name="Game"), reshade_enabled=False)
            self.assertFalse(config["addons"]["ManualAddon"]["_enabled"])

    def test_existing_echo_neural_settings_are_authoritative(self):
        profile = Profile(
            name="Game",
            addon_config={
                "global": {},
                "addons": {"DLSS5NR01": {
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
        )["addons"]["DLSS5NR01"]

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
        self.assertIn("Show Add-on Menu", dashboard)
        self.assertIn("Echo Add-on Manager Configuration", dashboard)
        self.assertIn('id="addonEditorCards"', dashboard)
        self.assertNotIn('id="echoConfigJson"', dashboard)


if __name__ == "__main__":
    unittest.main()
