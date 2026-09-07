import tempfile
import unittest
from pathlib import Path

from companion.core.models import Profile, RtssLimiterConfig
from companion.services.rtss_manager import RtssProfileManager


class RtssProfileManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name) / "RivaTuner Statistics Server"
        self.root.mkdir()
        (self.root / "RTSS.exe").write_bytes(b"")
        (self.root / "RTSSHooks64.dll").write_bytes(b"")
        (self.root / "Profiles").mkdir()
        self.manager = RtssProfileManager(str(self.root))

    def tearDown(self):
        self.temp_dir.cleanup()

    def profile(self, **values):
        rtss = RtssLimiterConfig(enabled=True, framerate_limit=70, limit_method="nvidia_reflex")
        return Profile(id="game-profile", name="Game", target_process="Game.exe", rtss=rtss, **values)

    def test_detects_configured_install_and_creates_owned_profile(self):
        profile = self.profile()
        path = self.manager.apply_profile(profile)
        text = path.read_text(encoding="utf-8")
        self.assertIn("Managed by Lossless Scaling Helper profile=game-profile", text)
        self.assertIn("Limit=70", text)
        self.assertIn("LimitDenominator=1", text)
        self.assertIn("SyncLimiter=3", text)
        self.assertTrue(profile.rtss.managed_profile_created)
        self.assertEqual(profile.rtss.managed_target_process, "Game.exe")
        self.assertEqual(Path(profile.rtss.managed_install_path), self.root.resolve())

    def test_owned_profile_is_deleted(self):
        profile = self.profile()
        path = self.manager.apply_profile(profile)
        self.assertTrue(self.manager.remove_profile(profile))
        self.assertFalse(path.exists())

    def test_preexisting_profile_is_restored_without_losing_other_settings(self):
        path = self.root / "Profiles" / "Game.exe.cfg"
        path.write_text(
            "[Framerate]\nLimit=45\nSyncLimiter=1\n[OSD]\nShowOSD=1\n",
            encoding="utf-8",
        )
        profile = self.profile()
        self.manager.apply_profile(profile)
        self.manager.remove_profile(profile)
        text = path.read_text(encoding="utf-8")
        self.assertIn("Limit=45", text)
        self.assertIn("SyncLimiter=1", text)
        self.assertNotIn("LimitDenominator=", text)
        self.assertIn("ShowOSD=1", text)

    def test_rejects_a_path_as_process_name(self):
        profile = self.profile()
        profile.target_process = r"C:\Games\Game.exe"
        with self.assertRaisesRegex(ValueError, "executable filename"):
            self.manager.apply_profile(profile)

    def test_replaced_owned_profile_is_left_unchanged(self):
        profile = self.profile()
        path = self.manager.apply_profile(profile)
        replacement = "[Framerate]\nLimit=99\n"
        path.write_text(replacement, encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "replaced externally"):
            self.manager.remove_profile(profile)
        self.assertEqual(path.read_text(encoding="utf-8"), replacement)


if __name__ == "__main__":
    unittest.main()
