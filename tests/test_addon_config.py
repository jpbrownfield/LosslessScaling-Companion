import unittest
from types import SimpleNamespace
from pathlib import Path

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

    def test_neural_render_maps_to_echo_dlss5nr01(self):
        profile = Profile(name="Game")
        profile.graphics.neural_render.implementation = "lsp_neural_render"
        profile.graphics.neural_render.sampling_resolution_preset = "quality"
        result = self.service().effective(
            profile, reshade_enabled=False
        )
        neural = result["addons"]["DLSS5NR01"]
        self.assertTrue(neural["_enabled"])
        self.assertEqual(neural["workingScale"], "0.5")

    def test_echo_host_and_dashboard_contract(self):
        self.assertEqual(AddonManagerMenuController.WINDOW_CLASS, "LSAddonManagerClass")
        self.assertIn("ls-addon-manager", ProviderRegistry().list_provider_ids())
        dashboard = (
            Path(__file__).resolve().parents[1] / "companion" / "ui" / "dashboard.html"
        ).read_text(encoding="utf-8")
        self.assertIn('id="addonManagerEnabled"', dashboard)
        self.assertIn("Show Add-on Menu", dashboard)
        self.assertIn("Add-on Manager Configuration", dashboard)


if __name__ == "__main__":
    unittest.main()
