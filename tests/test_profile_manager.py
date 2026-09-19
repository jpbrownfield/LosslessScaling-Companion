import tempfile
import unittest
from pathlib import Path

from companion.core.models import Profile
from companion.core.profile_manager import (
    BROWSER_DEFAULT_PROFILE_NAME,
    DEFAULT_BROWSER_EXECUTABLES,
    GAME_DEFAULT_PROFILE_NAME,
    ProfileManager,
)


class ProfileManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.manager = ProfileManager(Path(self.temp_dir.name))

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_domain_matching_respects_hostname_boundary(self):
        youtube = Profile(
            id="youtube-profile",
            name="YouTube",
            target_process="chrome.exe",
            target_domain="youtube.com",
        )
        self.manager.add_or_update_profile(youtube)
        self.assertEqual(self.manager.match_profile(domain="www.youtube.com"), youtube)
        self.assertNotEqual(self.manager.match_profile(domain="notyoutube.com"), youtube)

    def test_domain_profile_precedes_browser_executable_profile(self):
        browser = self.manager.get_profile_by_id("default-browser")
        youtube = Profile(
            id="youtube-profile",
            name="YouTube",
            target_process="chrome.exe",
            target_domain="youtube.com",
        )
        self.manager.add_or_update_profile(youtube)

        self.assertEqual(
            self.manager.match_profile(
                process_name="chrome.exe", domain="www.youtube.com"
            ),
            youtube,
        )
        self.assertEqual(
            self.manager.match_target_profile(
                process_name="chrome.exe", domain="www.youtube.com"
            ),
            youtube,
        )
        self.assertEqual(
            self.manager.match_target_profile(process_name="chrome.exe"), browser
        )

    def test_new_configuration_has_only_game_and_browser_seed_profiles(self):
        self.assertEqual(
            [profile.id for profile in self.manager.config.profiles],
            ["default-game", "default-browser"],
        )
        self.assertTrue(self.manager.get_profile_by_id("default-game").is_default)
        self.assertEqual(self.manager.get_profile_by_id("default-game").name, GAME_DEFAULT_PROFILE_NAME)
        self.assertEqual(self.manager.get_profile_by_id("default-browser").name, BROWSER_DEFAULT_PROFILE_NAME)
        self.assertEqual(
            self.manager.get_profile_by_id("default-browser").target_process,
            "chrome.exe",
        )
        self.assertEqual(
            self.manager.get_profile_by_id("default-browser").target_processes,
            DEFAULT_BROWSER_EXECUTABLES,
        )

    def test_default_browser_matches_every_well_known_executable(self):
        browser = self.manager.get_profile_by_id("default-browser")
        for executable in DEFAULT_BROWSER_EXECUTABLES:
            with self.subTest(executable=executable):
                self.assertEqual(
                    self.manager.match_target_profile(process_name=executable),
                    browser,
                )

    def test_new_profiles_use_configured_default_static_rtss_limit(self):
        self.manager.config.rtss_default_static_framerate_limit = 144

        profile = self.manager.new_profile_from_default(
            "New Game", target_process="game.exe"
        )

        self.assertEqual(profile.rtss.framerate_limit, 144)

    def test_comma_separated_target_lists_are_normalized(self):
        profile = Profile(
            name="Browsers",
            target_processes="chrome.exe, msedge.exe, CHROME.EXE",
            target_executable_paths=r"C:\Apps\one.exe, D:\Apps\two.exe",
        )
        self.assertEqual(profile.target_processes, ["chrome.exe", "msedge.exe"])
        self.assertEqual(
            profile.target_executable_paths,
            [r"C:\Apps\one.exe", r"D:\Apps\two.exe"],
        )

    def test_fixed_gpu_selection_is_preserved(self):
        self.manager.config.preferred_scaling_gpu_device_id = r"PCI\VEN_10DE&DEV_1234"
        self.manager.config.auto_route_gpu_to_display = False
        self.manager.save_config()

        reloaded = ProfileManager(Path(self.temp_dir.name))

        self.assertEqual(
            reloaded.config.preferred_scaling_gpu_device_id,
            r"PCI\VEN_10DE&DEV_1234",
        )

    def test_empty_legacy_gpu_selection_becomes_auto_route(self):
        self.manager.config.preferred_scaling_gpu_device_id = None
        self.manager.config.auto_route_gpu_to_display = False
        self.manager.save_config()

        reloaded = ProfileManager(Path(self.temp_dir.name))

        self.assertTrue(reloaded.config.auto_route_gpu_to_display)

    def test_untouched_legacy_seed_profiles_are_migrated(self):
        self.manager.config.profiles = [
            Profile(
                id="default-chrome",
                name="Chrome Video Auto-Scaler",
                target_process="chrome.exe",
                custom_notes="Default profile for browser fullscreen video scaling.",
            ),
            Profile(
                id="youtube-profile",
                name="YouTube 2x Enhancer",
                target_process="chrome.exe",
                target_domain="youtube.com",
                custom_notes="Optimized profile for YouTube videos.",
            ),
        ]
        self.manager.config.active_profile_id = "default-chrome"
        self.manager.save_config()

        migrated = ProfileManager(Path(self.temp_dir.name))

        self.assertIsNone(migrated.get_profile_by_id("default-chrome"))
        self.assertIsNone(migrated.get_profile_by_id("youtube-profile"))
        self.assertIsNotNone(migrated.get_profile_by_id("default-game"))
        self.assertIsNotNone(migrated.get_profile_by_id("default-browser"))
        self.assertEqual(migrated.config.active_profile_id, "default-game")

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
        self.manager.config.profiles = []
        self.manager.config.active_profile_id = None
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
        self.assertEqual(first.name, GAME_DEFAULT_PROFILE_NAME)
        self.assertEqual(first.lossless_profile_title, "Lossless Default")
        self.assertEqual(first.native_scaling_settings["ScalingType"], "LS1")
        self.assertEqual(
            len([profile for profile in self.manager.config.profiles if profile.is_default]),
            1,
        )
        self.assertFalse(self.manager.delete_profile(first.id))
        self.assertIsNotNone(self.manager.get_profile_by_id(first.id))

    def test_legacy_default_names_migrate_to_canonical_alias_names(self):
        self.manager.get_profile_by_id("default-game").name = "Default Game"
        self.manager.get_profile_by_id("default-browser").name = "Default Browser"
        self.manager.save_config()

        migrated = ProfileManager(Path(self.temp_dir.name))

        self.assertEqual(migrated.get_profile_by_id("default-game").name, "Game Default")
        self.assertEqual(migrated.get_profile_by_id("default-browser").name, "Browser Default")

    def test_default_alias_update_cannot_change_native_identity_or_targets(self):
        default = self.manager.ensure_lossless_default_profile({
            "Title": "True Native Default", "Path": "", "ScalingType": "LS1",
        })
        changed = default.model_copy(deep=True)
        changed.name = "Dangerous Rename"
        changed.lossless_profile_title = "Duplicate Native Profile"
        changed.target_process = "game.exe"
        changed.auto_scale = True

        self.manager.add_or_update_profile(changed)

        saved = self.manager.get_profile_by_id(default.id)
        self.assertEqual(saved.name, "Game Default")
        self.assertEqual(saved.lossless_profile_title, "True Native Default")
        self.assertIsNone(saved.target_process)
        self.assertFalse(saved.auto_scale)

    def test_corrupt_default_flags_are_repaired_without_duplicating_profiles(self):
        game = self.manager.get_profile_by_id("default-game")
        browser = self.manager.get_profile_by_id("default-browser")
        game.is_default = False
        browser.is_default = True
        duplicate = Profile(id="duplicate-default", name="Duplicate", is_default=True)
        self.manager.config.profiles.append(duplicate)
        self.manager.save_config()

        repaired = ProfileManager(Path(self.temp_dir.name))

        defaults = [profile for profile in repaired.config.profiles if profile.is_default]
        self.assertEqual(len(defaults), 1)
        self.assertEqual(defaults[0].id, "default-game")
        self.assertEqual(defaults[0].name, "Game Default")

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

    def test_global_automatic_scaling_applies_to_new_profiles(self):
        default_created = self.manager.create_profile_from_process("DefaultGame.exe")
        self.assertTrue(self.manager.config.default_profile_auto_scale)
        self.assertTrue(default_created.auto_scale)
        imported_before_change = self.manager.import_native_profile(
            {"Title": "Existing LS", "Path": r"D:\Games\ExistingLS.exe"}
        )
        self.assertFalse(imported_before_change.auto_scale)
        existing = Profile(id="existing", name="Existing", auto_scale=True)
        self.manager.add_or_update_profile(existing)
        self.manager.config.disable_native_auto_scale = False

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
        self.manager.config.rtss_default_static_framerate_limit = 144
        self.manager.save_config()

        created = self.manager.create_profile_from_process("new-game.exe")

        self.assertNotEqual(created.id, default.id)
        self.assertFalse(created.is_default)
        self.assertEqual(created.target_process, "new-game.exe")
        self.assertIsNone(created.lossless_profile_title)
        self.assertEqual(created.native_scaling_settings["ScalingType"], "LS1")
        self.assertTrue(created.graphics.special_k.enabled)
        self.assertTrue(created.rtss.enabled)
        self.assertEqual(created.rtss.framerate_limit, 144)
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

    def test_blank_profile_name_uses_executable_name(self):
        profile = Profile(name="   ", target_executable_path=r"D:\Games\Example\game.exe")
        self.assertEqual(profile.name, "game.exe")

    def test_blank_profile_without_target_gets_stable_placeholder(self):
        self.assertEqual(Profile(name="").name, "Untitled Profile")

    def test_profile_hotkey_is_excluded_from_serialized_payloads(self):
        dumped = Profile(name="Game").model_dump()
        self.assertNotIn("hotkey", dumped)
        # Legacy settings files that still carry a per-profile hotkey must
        # keep parsing; the value is ignored (only the global hotkey fires).
        legacy = Profile.model_validate({
            "name": "Legacy",
            "hotkey": {"modifiers": ["ctrl"], "key": "x", "activation_delay_ms": 999},
        })
        self.assertEqual(legacy.hotkey.activation_delay_ms, 999)
        self.assertNotIn("hotkey", legacy.model_dump())


if __name__ == "__main__":
    unittest.main()
