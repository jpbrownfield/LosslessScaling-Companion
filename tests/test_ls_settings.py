import shutil
import tempfile
import unittest
from pathlib import Path

from companion.core.models import HotkeyConfig
from companion.services.ls_settings import LosslessSettingsXml


FIXTURE = Path(__file__).parent / "fixtures" / "lossless_scaling" / "Settings.xml"


class LosslessSettingsXmlTests(unittest.TestCase):
    def test_initialization_backup_is_an_untouched_byte_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "Settings.xml"
            shutil.copy2(FIXTURE, target)
            original = target.read_bytes()
            manager = LosslessSettingsXml(str(target))

            backup = manager.ensure_initial_backup()

            self.assertEqual(backup, target.with_suffix(".xml.bak"))
            self.assertEqual(backup.read_bytes(), original)
            self.assertEqual(target.read_bytes(), original)

    def test_invalid_settings_are_not_accepted_as_initial_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "Settings.xml"
            target.write_text("<invalid", encoding="utf-8")
            manager = LosslessSettingsXml(str(target))

            self.assertIsNone(manager.ensure_initial_backup())
            self.assertFalse(target.with_suffix(".xml.bak").exists())

    def test_restore_initial_backup_is_atomic_and_retains_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "Settings.xml"
            shutil.copy2(FIXTURE, target)
            manager = LosslessSettingsXml(str(target))
            original = target.read_bytes()
            backup = manager.ensure_initial_backup()
            target.write_text("<Settings><Hotkey>F12</Hotkey></Settings>", encoding="utf-8")

            self.assertTrue(manager.restore_initial_backup())

            self.assertEqual(target.read_bytes(), original)
            self.assertEqual(backup.read_bytes(), original)

    def test_reads_public_fixture(self):
        settings = LosslessSettingsXml(str(FIXTURE)).read()
        self.assertTrue(settings["exists"])
        self.assertEqual(settings["hotkey"], "S")
        self.assertEqual(settings["profiles"][0]["CaptureApi"], "WGC")
        self.assertEqual(settings["profiles"][0]["LSFG3Multiplier"], "2")

    def test_atomic_update_preserves_unknown_fields_and_creates_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "Settings.xml"
            shutil.copy2(FIXTURE, target)
            manager = LosslessSettingsXml(str(target))
            self.assertTrue(manager.update_profile("Fixture Default", {"CaptureApi": "DXGI", "FutureField": 7}))
            profile = manager.get_profile("Fixture Default")
            self.assertEqual(profile["CaptureApi"], "DXGI")
            self.assertEqual(profile["FutureField"], "7")
            self.assertEqual(profile["LSFG3Multiplier"], "2")
            self.assertTrue(target.with_suffix(".xml.bak").exists())

    def test_disables_native_auto_scale_for_every_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "Settings.xml"
            shutil.copy2(FIXTURE, target)
            manager = LosslessSettingsXml(str(target))
            self.assertTrue(manager.update_profile("Fixture Default", {"AutoScale": True}))
            self.assertTrue(manager.native_auto_scale_enabled())

            self.assertTrue(manager.disable_native_auto_scale())

            self.assertFalse(manager.native_auto_scale_enabled())
            self.assertEqual(manager.get_profile("Fixture Default")["AutoScale"], "false")

    def test_hotkey_sync_is_canonical_and_coalesced_with_auto_scale(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "Settings.xml"
            shutil.copy2(FIXTURE, target)
            manager = LosslessSettingsXml(str(target))
            desired = HotkeyConfig(modifiers=["shift", "control"], key="f10")

            changes = manager.update_control_settings(
                hotkey=desired,
                disable_auto_scale=True,
            )

            self.assertEqual(changes, {"hotkey": True, "auto_scale": False})
            self.assertEqual(manager.read_hotkey().key, "f10")
            self.assertEqual(manager.read_hotkey().modifiers, ["ctrl", "shift"])


if __name__ == "__main__":
    unittest.main()
