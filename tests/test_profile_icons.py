import ctypes
from io import BytesIO
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from companion.core.models import Profile
from companion.services.profile_icons import ProfileIconService, executable_icon_png


class ProfileIconTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.exe = Path(self.directory.name) / "Game.exe"
        self.exe.write_bytes(b"test resource")
        self.service = ProfileIconService()

    def test_existing_profile_gets_an_icon_without_config_migration(self):
        profile = Profile(name="Saved game", target_executable_path=str(self.exe))
        before = profile.model_dump()
        with patch("companion.services.profile_icons.executable_icon_png", return_value=b"png") as extract:
            self.assertEqual(self.service.png(profile), b"png")
            extract.assert_called_once_with(self.exe)
        self.assertEqual(profile.model_dump(), before)

    def test_icon_cache_is_shared_and_invalidates_when_application_changes(self):
        profile = Profile(name="Game", target_executable_path=str(self.exe))
        with patch("companion.services.profile_icons.executable_icon_png", side_effect=[b"old", b"new"]) as extract:
            self.assertEqual(self.service.png(profile), b"old")
            self.assertEqual(self.service.png(profile.model_copy(update={"id": "other"})), b"old")
            self.exe.write_bytes(b"updated icon resource")
            self.assertEqual(self.service.png(profile), b"new")
            self.assertEqual(extract.call_count, 2)

    def test_multiple_targets_skip_missing_files_and_iconless_executables(self):
        first = self.exe.with_name("NoIcon.exe")
        first.write_bytes(b"no resource")
        profile = Profile(name="Game", target_executable_path=str(first.with_name("Missing.exe")),
                          target_executable_paths=[str(first), str(self.exe)])
        with patch("companion.services.profile_icons.executable_icon_png", side_effect=[None, b"png"]):
            self.assertEqual(self.service.png(profile), b"png")

    def test_native_import_path_is_used_when_legacy_profile_only_names_process(self):
        profile = Profile(name="Imported", target_process="Game.exe", lossless_profile_path=str(self.exe))
        with patch("companion.services.profile_icons.executable_icon_png", return_value=b"png"), \
                patch.object(self.service, "_resolve_name") as resolve:
            self.assertEqual(self.service.png(profile), b"png")
            resolve.assert_not_called()

    def test_process_only_profile_resolves_running_application_and_caches_lookup(self):
        profile = Profile(name="Legacy", target_process="Game.exe")
        process = SimpleNamespace(info={"name": "GAME.EXE", "exe": str(self.exe)})
        with patch("companion.services.profile_icons.psutil.process_iter", return_value=[process]) as scan, \
                patch("companion.services.profile_icons.shutil.which", return_value=None), \
                patch("companion.services.profile_icons.executable_icon_png", return_value=b"png"):
            self.assertEqual(self.service.png(profile), b"png")
            self.assertEqual(self.service.png(profile), b"png")
            scan.assert_called_once()

    def test_retargeted_profile_does_not_use_its_previous_native_template_icon(self):
        actual = self.exe.with_name("NewGame.exe")
        actual.write_bytes(b"new application")
        profile = Profile(name="Retargeted", target_process="NewGame.exe", lossless_profile_path=str(self.exe))
        with patch.object(self.service, "_resolve_name", return_value=[str(actual)]), \
                patch("companion.services.profile_icons.executable_icon_png", return_value=b"new png") as extract:
            self.assertEqual(self.service.png(profile), b"new png")
            extract.assert_called_once_with(actual)

    def test_missing_process_is_retried_after_negative_cache_expires(self):
        profile = Profile(name="Legacy", target_process="NotRegisteredProfileIconTest.exe")
        process = SimpleNamespace(info={"name": profile.target_process, "exe": str(self.exe)})
        with patch("companion.services.profile_icons.time.monotonic", return_value=100) as now, \
                patch("companion.services.profile_icons.psutil.process_iter", side_effect=[[], [process]]) as scan, \
                patch("companion.services.profile_icons.shutil.which", return_value=None), \
                patch("companion.services.profile_icons.executable_icon_png", return_value=b"png"):
            self.assertIsNone(self.service.png(profile))
            self.assertIsNone(self.service.png(profile))
            now.return_value = 131
            self.assertEqual(self.service.png(profile), b"png")
            self.assertEqual(scan.call_count, 2)

    def test_web_profiles_do_not_probe_local_browsers(self):
        profile = Profile(name="Video", target_domain="example.test", target_process="browser.exe")
        with patch.object(self.service, "_resolve_name") as resolve:
            self.assertIsNone(self.service.png(profile))
            resolve.assert_not_called()

    def test_default_profile_uses_lossless_scaling_and_retargeting_changes_url_version(self):
        profile = Profile(name="Default", is_default=True)
        with patch("companion.services.profile_icons.executable_icon_png", return_value=b"png"):
            self.assertEqual(self.service.png(profile, str(self.exe)), b"png")
        self.assertNotEqual(self.service.version(profile, str(self.exe)), self.service.version(profile, "elsewhere.exe"))
        profile = Profile(name="Game", target_executable_path=str(self.exe))
        self.assertNotEqual(self.service.version(profile, None), self.service.version(
            profile.model_copy(update={"target_executable_path": "other.exe"}), None))

    @unittest.skipUnless(os.name == "nt", "Windows executable icon resources")
    def test_native_extraction_preserves_transparency_and_releases_gdi_objects(self):
        executable = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32" / "notepad.exe"
        if not executable.is_file():
            self.skipTest("System Notepad unavailable")
        data = executable_icon_png(executable)
        self.assertTrue(data)
        image = Image.open(BytesIO(data))
        self.assertEqual(image.size, (64, 64))
        self.assertEqual(image.mode, "RGBA")
        low, high = image.getchannel("A").getextrema()
        self.assertEqual(low, 0)
        self.assertGreater(high, 200)
        kernel = ctypes.WinDLL("kernel32")
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        user = ctypes.WinDLL("user32")
        user.GetGuiResources.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        process = kernel.GetCurrentProcess()
        before = [user.GetGuiResources(process, kind) for kind in (0, 1)]
        for _ in range(12):
            self.assertTrue(executable_icon_png(executable))
        self.assertEqual([user.GetGuiResources(process, kind) for kind in (0, 1)], before)
        self.assertIsNone(executable_icon_png(self.exe))
