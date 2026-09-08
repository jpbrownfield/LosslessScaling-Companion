import tempfile
import unittest
from pathlib import Path

from companion.core.models import Profile
from companion.core.profile_manager import ProfileManager


class ProfileManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.manager = ProfileManager(Path(self.temp_dir.name))

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_domain_matching_respects_hostname_boundary(self):
        youtube = self.manager.get_profile_by_id("youtube-profile")
        self.assertEqual(self.manager.match_profile(domain="www.youtube.com"), youtube)
        self.assertNotEqual(self.manager.match_profile(domain="notyoutube.com"), youtube)

    def test_explicit_match_has_no_fallback(self):
        self.assertIsNone(self.manager.match_target_profile(process_name="unrelated.exe"))

    def test_explicit_path_match_precedes_same_named_process(self):
        first = Profile(
            id="first-game",
            name="First",
            target_process="game.exe",
            target_executable_path=r"C:\Games\First\game.exe",
        )
        second = Profile(
            id="second-game",
            name="Second",
            target_process="game.exe",
            target_executable_path=r"D:\Games\Second\game.exe",
        )
        self.manager.add_or_update_profile(first)
        self.manager.add_or_update_profile(second)

        matched = self.manager.match_target_profile(
            process_name="game.exe", executable_path=r"D:\Games\Second\game.exe"
        )
        self.assertEqual(matched.id, second.id)

    def test_empty_profile_list_is_supported(self):
        for profile in list(self.manager.config.profiles):
            self.manager.delete_profile(profile.id)
        self.assertIsNone(self.manager.match_profile(process_name="chrome.exe"))

    def test_native_default_profile_is_added_once_and_cannot_be_deleted(self):
        native = {
            "Title": "Lossless Default",
            "Path": "",
            "ScalingType": "LS1",
            "PreferredGpuId": "0",
        }

        first = self.manager.ensure_lossless_default_profile(native)
        second = self.manager.ensure_lossless_default_profile(native)

        self.assertEqual(first.id, second.id)
        self.assertTrue(first.is_default)
        self.assertEqual(first.lossless_profile_title, "Lossless Default")
        self.assertEqual(first.native_scaling_settings["ScalingType"], "LS1")
        self.assertEqual(
            len([profile for profile in self.manager.config.profiles if profile.is_default]),
            1,
        )
        self.assertFalse(self.manager.delete_profile(first.id))
        self.assertIsNotNone(self.manager.get_profile_by_id(first.id))

    def test_native_import_preview_prefers_executable_filename_before_title(self):
        profile = Profile(
            id="game-profile",
            name="Unrelated helper name",
            target_process="ExampleGame.exe",
        )
        self.manager.add_or_update_profile(profile)

        preview = self.manager.preview_native_imports(
            [{"Title": "Native title", "Path": r"D:\Games\ExampleGame.exe"}]
        )[0]

        self.assertEqual(preview["suggestedProfileId"], profile.id)
        self.assertEqual(preview["matchBasis"], "filename")

    def test_native_refresh_preserves_helper_only_fields(self):
        profile = Profile(
            id="linked-profile",
            name="Helper name",
            target_domain="example.com",
            custom_notes="keep me",
            auto_scale=True,
        )
        self.manager.add_or_update_profile(profile)

        refreshed = self.manager.import_native_profile(
            {
                "Title": "Native profile",
                "Path": r"D:\Games\ExampleGame.exe",
                "ScalingType": "LS1",
                "AutoScale": "true",
                "FutureSetting": "preserve",
            },
            existing_profile_id=profile.id,
        )

        self.assertEqual(refreshed.target_domain, "example.com")
        self.assertEqual(refreshed.custom_notes, "keep me")
        self.assertTrue(refreshed.auto_scale)
        self.assertEqual(refreshed.native_scaling_settings["FutureSetting"], "preserve")
        self.assertEqual(refreshed.native_scaling_settings["AutoScale"], "true")

    def test_global_autoscale_default_applies_only_to_new_profiles(self):
        default_created = self.manager.create_profile_from_process("DefaultGame.exe")
        self.assertTrue(self.manager.config.default_profile_auto_scale)
        self.assertTrue(default_created.auto_scale)
        imported_before_change = self.manager.import_native_profile(
            {"Title": "Existing LS", "Path": r"D:\Games\ExistingLS.exe"}
        )
        self.assertFalse(imported_before_change.auto_scale)
        existing = Profile(id="existing", name="Existing", auto_scale=True)
        self.manager.add_or_update_profile(existing)
        self.manager.config.default_profile_auto_scale = False

        created = self.manager.create_profile_from_process("NewGame.exe")
        imported = self.manager.import_native_profile(
            {"Title": "Imported", "Path": r"D:\Games\Imported.exe"}
        )

        self.assertFalse(created.auto_scale)
        self.assertFalse(imported.auto_scale)
        self.assertTrue(self.manager.get_profile_by_id("existing").auto_scale)

    def test_new_profile_clones_default_settings_without_identity_or_runtime_state(self):
        default = self.manager.ensure_lossless_default_profile({
            "Title": "Default",
            "ScalingType": "LS1",
        })
        default.graphics.special_k.enabled = True
        default.rtss.enabled = True
        default.rtss.framerate_limit = 72
        default.rtss.learned_framerate_limit = 68
        default.rtss.managed_target_process = "old.exe"
        default.custom_notes = "Template note"
        self.manager.save_config()

        created = self.manager.create_profile_from_process("new-game.exe")

        self.assertNotEqual(created.id, default.id)
        self.assertFalse(created.is_default)
        self.assertEqual(created.target_process, "new-game.exe")
        self.assertIsNone(created.lossless_profile_title)
        self.assertEqual(created.native_scaling_settings["ScalingType"], "LS1")
        self.assertTrue(created.graphics.special_k.enabled)
        self.assertTrue(created.rtss.enabled)
        self.assertEqual(created.rtss.framerate_limit, 72)
        self.assertIsNone(created.rtss.learned_framerate_limit)
        self.assertIsNone(created.rtss.managed_target_process)
        self.assertEqual(created.custom_notes, "Template note")

    def test_profile_schema_contains_only_the_single_autoscale_field(self):
        dumped = Profile(name="Game").model_dump()
        self.assertFalse(dumped["auto_scale"])
        self.assertNotIn("auto_scale_on_fullscreen", dumped)
        self.assertNotIn("auto_scale_on_demaximize", dumped)
        self.assertNotIn("auto_scale_on_focus", dumped)
        self.assertNotIn("auto_scale_on_blur", dumped)


if __name__ == "__main__":
    unittest.main()
