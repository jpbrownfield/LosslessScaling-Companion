import configparser
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from companion.core.models import Profile
from companion.services import sm86_sr
from companion.services.addon_config import AddonConfigService
from companion.services.addon_schema import ADDONS
from companion.services.asset_store import AssetStore
from companion.services.release_providers import ProviderRegistry


def profile(mode="M-INT8", enabled=True):
    return Profile(name="Game", addon_config={"addons": {
        "DLSS4DLAA": {"_enabled": enabled, "companionSm86Mode": mode, "dlaaPreset": "5"},
        "DLSSNR02": {"_enabled": True, "preset": "2"}}})


class Sm86SrTests(unittest.TestCase):
    def test_catalog_exposes_real_upstream_presets_and_separate_int8_modes(self):
        fields = {field["key"]: field for addon in ADDONS if addon["id"] == "DLSS4DLAA" for field in addon["fields"]}
        self.assertEqual(set(dict(fields["dlaaPreset"]["options"])), {"16", "0", "5", "10", "12", "13"})
        self.assertEqual(set(dict(fields[sm86_sr.MODE_KEY]["options"])), {"Off", "L-INT8", "M-INT8"})

    def test_effective_int8_pins_sr_preset_without_changing_neural_rendering(self):
        manager = SimpleNamespace(config_dir=".", config=SimpleNamespace(default_addon_config={}))
        service = AddonConfigService(manager)
        for mode, preset in (("L-INT8", "12"), ("M-INT8", "13")):
            original = profile(mode)
            result = service.effective(original, reshade_enabled=False)
            self.assertEqual(result["addons"]["DLSS4DLAA"]["dlaaPreset"], preset)
            self.assertEqual(result["addons"]["DLSS4DLAA"]["scalerPerGame"], "0")
            self.assertEqual(result["addons"]["DLSSNR02"]["preset"], "2")
            self.assertEqual(original.addon_config["addons"]["DLSS4DLAA"]["dlaaPreset"], "5")
        stock = service.effective(profile("Off"), reshade_enabled=False)
        self.assertEqual(stock["addons"]["DLSS4DLAA"]["dlaaPreset"], "5")

    def test_mode_does_not_implicitly_enable_the_upscaler_and_unknown_modes_fail(self):
        self.assertEqual(sm86_sr.selection(profile(enabled=False).addon_config), "Off")
        for mode in ("Unknown", ["L-INT8"], {"mode": "L-INT8"}):
            with self.assertRaisesRegex(ValueError, "Off, L-INT8 or M-INT8"):
                sm86_sr.selection(profile(mode).addon_config)

    def test_provider_is_an_offline_pin_not_latest_unreviewed_binaries(self):
        with patch("urllib.request.urlopen", side_effect=AssertionError("No metadata fetch")):
            releases = ProviderRegistry().get(sm86_sr.PROVIDER).list_releases()
        self.assertEqual(len(releases), 1)
        self.assertEqual(releases[0].version, sm86_sr.VERSION)
        self.assertEqual(releases[0].assets[0].digest, f"sha256:{sm86_sr.ARCHIVE_SHA256}")

    def fixture(self, root):
        store = AssetStore(str(root / "store"))
        payload = root / "payload"
        hashes = {}
        for name in sm86_sr.FILES:
            path = payload / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(name.encode())
            hashes[name] = hashlib.sha256(name.encode()).hexdigest()
        package = {"provider": sm86_sr.PROVIDER, "version": sm86_sr.VERSION,
                   "archive_sha256": sm86_sr.ARCHIVE_SHA256, "payload_path": str(payload)}
        return store, payload, hashes, package

    def test_private_sr_only_plan_has_owned_ini_and_returns_to_stock_cleanly(self):
        with tempfile.TemporaryDirectory() as directory:
            store, payload, hashes, package = self.fixture(Path(directory))
            with patch.object(store, "list_packages", return_value=[package]), patch.object(sm86_sr, "FILES", hashes):
                for mode in ("L-INT8", "M-INT8"):
                    plan = sm86_sr.deployment_files(store, profile(mode), host_version="build-5-lsc.2")
                    names = {item["relative_path"] for item in plan}
                    self.assertEqual(names, {f"{sm86_sr.ROOT}/{name}" for name in hashes} | {f"{sm86_sr.ROOT}/dlssg_sm86.ini"})
                    self.assertNotIn("version.dll", names)
                    self.assertFalse(any("fg_kernels" in name or "nvngx_dlssg" in name or "xefg" in name for name in names))
                    ini = configparser.ConfigParser()
                    ini.read(plan[-1]["source_path"])
                    self.assertEqual(ini["SuperResolution"]["Mode"], mode)
                    self.assertEqual(ini["General"]["Enabled"], "0")
                    self.assertEqual(ini["Compatibility"]["SpoofArchToGame"], "0")
                self.assertEqual(sm86_sr.deployment_files(store, profile("Off"), host_version="build-5-lsc.2"), [])
                self.assertEqual(sm86_sr.deployment_files(store, profile(enabled=False), host_version="build-5-lsc.2"), [])
                (payload / "version.dll").write_bytes(b"tampered")
                with self.assertRaisesRegex(ValueError, "failed verification: version.dll"):
                    sm86_sr.deployment_files(store, profile(), host_version="build-5-lsc.2")

    def test_missing_package_and_old_host_cannot_masquerade_as_int8(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AssetStore(directory)
            with self.assertRaisesRegex(ValueError, "current bundled Companion host"):
                sm86_sr.deployment_files(store, profile(), host_version="build-5-lsc.1")
            with self.assertRaisesRegex(ValueError, "Install the SM86"):
                sm86_sr.deployment_files(store, profile(), host_version="build-5-lsc.2")
