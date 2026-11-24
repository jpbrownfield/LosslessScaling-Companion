import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from companion.core.profile_manager import ProfileManager
from companion.services.reshade_profiles import BUILTIN_PROFILES, LILIUM_CAS_EFFECT, ReshadeProfileService


class DefaultPresetPreparationTests(unittest.TestCase):
    def test_preparation_downloads_all_lilium_defaults_once(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("hdr-master/Shaders/lilium__inverse_tone_mapping.fx",
                             "technique lilium__inverse_tone_mapping { pass {} }")
            package.writestr("hdr-master/Shaders/lilium__include/include_main.fxh", "// support")
            package.writestr("hdr-master/Shaders/lilium__cas_hdr.fx",
                             "technique lilium__cas_hdr_ps { pass {} }")
        with tempfile.TemporaryDirectory() as directory:
            service = ReshadeProfileService(ProfileManager(Path(directory)))
            service._catalog = [{"id": "22", "name": "Lilium", "shaders": ["lilium__inverse_tone_mapping.fx", LILIUM_CAS_EFFECT],
                "downloadUrl": "https://github.com/example/hdr/archive/master.zip"}]
            with patch("companion.services.reshade_profiles.urllib.request.urlopen",
                       return_value=io.BytesIO(archive.getvalue())) as download:
                payload = service.prepare_builtin_profiles()
                service.prepare_builtin_profiles()
            self.assertEqual(download.call_count, 1)
            self.assertEqual(len(payload["profiles"]), 3)
            for profile in BUILTIN_PROFILES:
                self.assertEqual(service._missing_selected_shaders(profile), set())
                self.assertTrue((service._folder(profile.id) / "ReShade.ini").is_file())
            basic = service._folder("lsc-basic-sharpening") / "packages/22/Shaders" / LILIUM_CAS_EFFECT
            self.assertIn("technique lilium__cas_hdr_ps", basic.read_text(encoding="utf-8"))
            self.assertEqual([item["id"] for item in payload["catalog"]], ["22"])

    def test_failed_download_does_not_report_defaults_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            service = ReshadeProfileService(ProfileManager(Path(directory)))
            service._catalog = [{"id": "22", "name": "Lilium", "shaders": ["lilium__inverse_tone_mapping.fx", LILIUM_CAS_EFFECT],
                "downloadUrl": "https://github.com/example/hdr/archive/master.zip"}]
            with patch("companion.services.reshade_profiles.urllib.request.urlopen", side_effect=OSError("offline")):
                with self.assertRaisesRegex(ValueError, "default presets could not be prepared"):
                    service.prepare_builtin_profiles()
            self.assertEqual(service._missing_selected_shaders(BUILTIN_PROFILES[0]), {LILIUM_CAS_EFFECT})
