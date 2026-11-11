import configparser
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from companion.core.models import Profile, ManagedPackageConfig
from companion.services.asset_store import AssetStore
from companion.services.graphics_resolver import GraphicsResolver
from companion.services import display_commander
from companion.services.release_providers import DisplayCommanderReleaseProvider
from tests.test_proxy_integrations import x64_pe


class DisplayCommanderTests(unittest.TestCase):
    def test_download_catalog_excludes_unmarked_debug_build_and_x86(self):
        raw = [dict(tag_name=tag, name=tag, assets=[
            dict(name="zzz_display_commander.addon64", browser_download_url="https://github.com/x"),
            dict(name="zzz_display_commander.addon32", browser_download_url="https://github.com/y"),
        ]) for tag in ("latest_debug", "latest")]
        with patch("companion.services.release_providers._request_json", return_value=raw):
            releases = DisplayCommanderReleaseProvider().list_releases()
        self.assertEqual([r.version for r in releases], ["latest"])
        self.assertEqual([a.name for a in releases[0].assets], [display_commander.FILENAME])

    def test_reflex_enables_dependencies_and_has_one_owner(self):
        profile = Profile(name="Game", graphics={
            "display_commander": {"inject_reflex": True},
            "special_k": {"enabled": True, "experimental_reflex": True},
        })
        self.assertTrue(profile.graphics.display_commander.enabled)
        self.assertTrue(profile.reshade.enabled)
        self.assertTrue(profile.reshade.menu_proxy_enabled)
        self.assertTrue(profile.graphics.reshade.enabled)
        self.assertTrue(profile.graphics.lossless_proxy.enabled)
        self.assertFalse(profile.graphics.special_k.experimental_reflex)

    def test_managed_host_deploys_addon_without_shader_preset_and_removes_it_when_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = AssetStore(str(root / "store"))
            executable = root / "LosslessScaling.exe"
            executable.write_bytes(b"exe")
            (root / "Lossless.dll").write_bytes(b"original")
            addon = root / display_commander.FILENAME
            addon.write_bytes(x64_pe(b"DC"))
            store.import_release_file(str(addon), provider="display-commander", version="1.0.6", source_metadata={})
            for provider, filename, version in [("reshade", "ReShade64.dll", "6.7.2"),
                                                 ("ls-addon-manager", "Lossless.dll", "build-5-lsc.5")]:
                binary = root / ("stage-" + filename)
                binary.write_bytes(x64_pe(provider.encode()))
                package = store.import_release_file(str(binary), provider=provider, version=version, source_metadata={})
                recipe = {"schema_version": 1, "files": [{"source": binary.name,
                    "destination": "dxgi.dll" if provider == "reshade" else filename, "role": "injector"}]}
                recipe_path = Path(package["payload_path"]) / "lossless-scaling-deployment.json"
                recipe_path.write_text(json.dumps(recipe), encoding="utf-8")
                package["files"].append({"relative_path": recipe_path.name, "sha256": store.sha256(recipe_path), "size": recipe_path.stat().st_size})
                store._atomic_json(Path(package["payload_path"]).parent / "package.json", package)
            profile = Profile(name="Game", graphics={"display_commander": {"inject_reflex": True}})
            resolver = GraphicsResolver(store)
            plan = resolver.resolve(profile, lossless_scaling_exe=str(executable), addon_manager_enabled=True,
                                    addon_manager_config=ManagedPackageConfig(enabled=True))
            paths = {item["relative_path"]: item for item in plan}
            self.assertIn(display_commander.FILENAME, paths)
            self.assertIn(".companion/components/reshade/ReShade64.dll", paths)
            self.assertIn("KeyOverlay=35,0,0,0", Path(paths["ReShade.ini"]["source_path"]).read_text())
            parser = configparser.ConfigParser()
            parser.read(paths["DisplayCommander.ini"]["source_path"])
            dc = parser["DisplayCommander"]
            self.assertEqual(dc["Reflex.InjectionEnabled"], "1")
            self.assertEqual(dc["Reflex.OnPresentSyncMode"], "0")
            self.assertEqual(dc["FpsLimiter.Enabled"], "0")
            self.assertEqual(dc["FpsLimiter.BackgroundLimitEnabled"], "0")
            profile.graphics.display_commander.enabled = False
            profile.graphics.display_commander.inject_reflex = False
            disabled = resolver.resolve(profile, lossless_scaling_exe=str(executable), addon_manager_enabled=True,
                                        addon_manager_config=ManagedPackageConfig(enabled=True))
            self.assertFalse(any(item["relative_path"] == display_commander.FILENAME for item in disabled))

    def test_old_reshade_and_missing_runtime_report_actionable_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AssetStore(directory)
            profile = Profile(name="Game", graphics={"display_commander": {"enabled": True}})
            with self.assertRaisesRegex(ValueError, "6.6.2"):
                display_commander.deployment_files(store, profile, {"version": "6.5.0"})
            with self.assertRaisesRegex(ValueError, "Install Display Commander"):
                display_commander.deployment_files(store, profile, {"version": "6.7.2"})

    def test_rolling_release_selects_new_download_and_rejects_modified_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = AssetStore(str(root / "store"))
            payloads = []
            for index in (1, 2):
                source = root / str(index) / display_commander.FILENAME
                source.parent.mkdir()
                source.write_bytes(x64_pe(str(index).encode()))
                package = store.import_release_file(str(source), provider="display-commander", version="latest",
                    source_metadata={"release": {"published_at": "2026-11-01"}})
                os.utime(Path(package["payload_path"]).parent / "package.json", ns=(index * 10**9, index * 10**9))
                payloads.append(Path(package["payload_path"]) / display_commander.FILENAME)
            profile = Profile(name="Game", graphics={"display_commander": {"enabled": True}})
            plan = display_commander.deployment_files(store, profile, {"version": "6.8.0"})
            self.assertEqual(Path(plan[0]["source_path"]), payloads[1])
            payloads[1].write_bytes(x64_pe(b"modified"))
            with self.assertRaisesRegex(ValueError, "hash changed"):
                display_commander.deployment_files(store, profile, {"version": "6.8.0"})
