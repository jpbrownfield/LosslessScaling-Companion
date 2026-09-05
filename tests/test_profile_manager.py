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
            auto_scale_on_focus=True,
        )
        self.manager.add_or_update_profile(profile)

        refreshed = self.manager.import_native_profile(
            {
                "Title": "Native profile",
                "Path": r"D:\Games\ExampleGame.exe",
                "ScalingType": "LS1",
                "FutureSetting": "preserve",
            },
            existing_profile_id=profile.id,
        )

        self.assertEqual(refreshed.target_domain, "example.com")
        self.assertEqual(refreshed.custom_notes, "keep me")
        self.assertTrue(refreshed.auto_scale_on_focus)
        self.assertEqual(refreshed.native_scaling_settings["FutureSetting"], "preserve")
        self.assertEqual(refreshed.native_scaling_settings["AutoScale"], "false")


if __name__ == "__main__":
    unittest.main()
