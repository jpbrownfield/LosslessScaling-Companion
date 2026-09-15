import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from companion.core.models import ManagedReshadeProfile, Profile, ReshadeConfig
from companion.core.profile_manager import ProfileManager
from companion.services.reshade_manager import ReshadeManager
from companion.services.hdr_display import HdrDisplayDetector
from companion.services.reshade_profiles import ReshadeProfileService


class ReshadeProfileFileTests(unittest.TestCase):
    def test_payload_seeds_low_cost_profiles_and_preserves_combined_order(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            manager = ProfileManager(config_dir=tmp_path / "config")
            service = ReshadeProfileService(
                manager,
                HdrDisplayDetector(lambda: [{
                    "deviceName": "HDR Display",
                    "colorSpace": 12,
                    "maxLuminanceNits": 1200,
                }]),
            )
            service._catalog = [{
                "id": "22",
                "name": "Lilium",
                "description": "",
                "repositoryUrl": "https://github.com/example/lilium",
                "downloadUrl": "https://github.com/example/lilium/archive/master.zip",
                "shaders": ["lilium__cas_hdr.fx", "lilium__inverse_tone_mapping.fx"],
            }]

            def install(profile, _catalog):
                package = service._folder(profile.id) / "packages" / "22" / "Shaders"
                package.mkdir(parents=True, exist_ok=True)
                if "lilium__inverse_tone_mapping.fx" in profile.shaders["22"]:
                    (package / "lilium__inverse_tone_mapping.fx").write_text(
                        "technique lilium__inverse_tone_mapping { pass {} }", encoding="utf-8"
                    )
                if "lilium__cas_hdr.fx" in profile.shaders["22"]:
                    (package / "lilium__cas_hdr.fx").write_text(
                        "technique lilium__cas_hdr_ps { pass {} }", encoding="utf-8"
                    )

            with patch.object(service, "_install_selected_shaders", side_effect=install):
                first = service.payload()
                second = service.payload()

            names = {profile["name"] for profile in first["profiles"]}
            self.assertEqual(len(first["profiles"]), 3)
            self.assertEqual(len(second["profiles"]), 3)
            self.assertIn("Basic Sharpening (Low Cost)", names)
            self.assertIn("SDR to HDR (Low Cost)", names)
            self.assertIn("Sharpening + SDR to HDR (Low Cost)", names)
            preset = (
                tmp_path / "config" / "reshade-profiles" / "lsc-sharpening-sdr-to-hdr"
                / "Sharpening SDR to HDR Low Cost.ini"
            )
            self.assertIn(
                "Techniques=lilium__inverse_tone_mapping@lilium__inverse_tone_mapping.fx,"
                "lilium__cas_hdr_ps@lilium__cas_hdr.fx",
                preset.read_text(encoding="utf-8"),
            )
            self.assertIn("TargetBrightness=1200.000000", preset.read_text(encoding="utf-8"))

    def test_named_profile_generates_overlay_disabled_config(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            manager = ProfileManager(config_dir=tmp_path / "config")
            service = ReshadeProfileService(manager)
            service._catalog = []

            saved = service.save(ManagedReshadeProfile(id="hdr-clean", name="HDR Clean"))

            config = (tmp_path / "config" / "reshade-profiles" / "hdr-clean" / "ReShade.ini").read_text()
            preset = tmp_path / "config" / "reshade-profiles" / "hdr-clean" / "HDR Clean.ini"
            self.assertEqual(saved["name"], "HDR Clean")
            self.assertTrue(preset.is_file())
            self.assertIn("KeyOverlay=0,0,0,0", config)
            self.assertIn("TutorialProgress=4", config)
            self.assertIn(f"CurrentPresetPath={preset.resolve()}", config)

    def test_selected_shader_archive_filters_unchecked_effects(self):
        archive_bytes = io.BytesIO()
        with zipfile.ZipFile(archive_bytes, "w") as archive:
            archive.writestr("pack-main/Shaders/Wanted.fx", "technique WantedPass { pass {} }")
            archive.writestr("pack-main/Shaders/Unchecked.fx", "unchecked")
            archive.writestr("pack-main/Shaders/Common.fxh", "include")
            archive.writestr("pack-main/Textures/noise.png", b"png")

        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            manager = ProfileManager(config_dir=tmp_path / "config")
            service = ReshadeProfileService(manager)
            service._catalog = [{
                "id": "pack",
                "name": "Pack",
                "description": "",
                "repositoryUrl": "https://github.com/example/pack",
                "downloadUrl": "https://github.com/example/pack/archive/main.zip",
                "shaders": ["Wanted.fx", "Unchecked.fx"],
            }]
            with patch(
                "companion.services.reshade_profiles.urllib.request.urlopen",
                return_value=io.BytesIO(archive_bytes.getvalue()),
            ):
                service.save(ManagedReshadeProfile(
                    id="filtered", name="Filtered", shaders={"pack": ["Wanted.fx"]}
                ))

            package = tmp_path / "config" / "reshade-profiles" / "filtered" / "packages" / "pack"
            self.assertTrue((package / "Shaders" / "Wanted.fx").is_file())
            self.assertFalse((package / "Shaders" / "Unchecked.fx").exists())
            self.assertTrue((package / "Shaders" / "Common.fxh").is_file())
            self.assertTrue((package / "Textures" / "noise.png").is_file())
            preset = tmp_path / "config" / "reshade-profiles" / "filtered" / "Filtered.ini"
            self.assertIn("Techniques=WantedPass@Wanted.fx", preset.read_text(encoding="utf-8"))

    def test_imported_ini_is_copied_into_named_profile_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            source = tmp_path / "Cinema.ini"
            source.write_text("Techniques=FilmGrain@FilmGrain.fx\n", encoding="utf-8")
            manager = ProfileManager(config_dir=tmp_path / "config")
            service = ReshadeProfileService(manager)

            saved = service.import_preset(str(source))

            imported = manager.config.reshade_profiles[-1]
            stored = Path(saved["presetPath"])
            self.assertEqual(saved["name"], "Cinema")
            self.assertTrue(imported.imported_preset)
            self.assertEqual(stored.read_text(encoding="utf-8"), source.read_text(encoding="utf-8"))
            self.assertIn(
                f"CurrentPresetPath={stored.resolve()}",
                Path(saved["configPath"]).read_text(encoding="utf-8"),
            )

            manager.config.reshade_hdr_peak_nits = 1400
            service.refresh_hdr_peak_settings()
            # Imported presets without a recognized HDR shader section remain untouched.
            self.assertEqual(stored.read_text(encoding="utf-8"), source.read_text(encoding="utf-8"))

    def test_imported_lilium_preset_receives_global_peak_nits(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            source = tmp_path / "HDR.ini"
            source.write_text(
                "Techniques=lilium__inverse_tone_mapping@lilium__inverse_tone_mapping.fx\n\n"
                "[lilium__inverse_tone_mapping.fx]\nTargetBrightness=600.000000\nGammaIn=0.000000\n",
                encoding="utf-8",
            )
            manager = ProfileManager(config_dir=tmp_path / "config")
            manager.config.reshade_hdr_peak_nits = 1500
            service = ReshadeProfileService(manager)
            saved = service.import_preset(str(source))

            service.refresh_hdr_peak_settings()

            stored = Path(saved["presetPath"]).read_text(encoding="utf-8")
            self.assertIn("TargetBrightness=1500.000000", stored)
            self.assertIn("GammaIn=0.000000", stored)

    def test_referenced_named_profile_cannot_be_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            manager = ProfileManager(config_dir=tmp_path / "config")
            service = ReshadeProfileService(manager)
            service._catalog = []
            service.save(ManagedReshadeProfile(id="used", name="Used"))
            manager.add_or_update_profile(Profile(
                id="game", name="Game", reshade=ReshadeConfig(enabled=True, managed_profile_id="used")
            ))

            with self.assertRaisesRegex(ValueError, "still selected"):
                service.delete("used")

    def test_apply_managed_config_is_lossless_scaling_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            lossless = tmp_path / "LosslessScaling.exe"
            active = tmp_path / "ReShade.ini"
            managed = tmp_path / "managed" / "ReShade.ini"
            lossless.write_bytes(b"")
            active.write_text("original", encoding="utf-8")
            managed.parent.mkdir()
            managed.write_text("[INPUT]\nKeyOverlay=0,0,0,0\n", encoding="utf-8")

            self.assertTrue(ReshadeManager.apply_managed_config(str(active), str(managed)))
            self.assertIn("KeyOverlay=0,0,0,0", active.read_text(encoding="utf-8"))
            self.assertEqual(active.with_suffix(".ini.bak").read_text(encoding="utf-8"), "original")


if __name__ == "__main__":
    unittest.main()
