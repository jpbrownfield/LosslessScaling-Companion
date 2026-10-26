import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from companion.core.models import Profile
from companion.core.profile_manager import ProfileManager
from companion.services.gpu_selections import GpuSelectionService


class ProfileSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.manager = ProfileManager(self.directory)
        self.profile = Profile(name="Game", target_process="game.exe",
                               native_scaling_settings={"PreferredGpuId": "1", "OutputDisplayId": "0", "ScalingType": "NIS"},
                               preferred_gpu_device_id="old-gpu", preferred_gpu_name="NVIDIA RTX")
        self.manager.add_or_update_profile(self.profile)
        self.path = self.manager.profile_settings.path(self.profile)

    def read(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_readable_snapshot_contains_friendly_choice_and_native_setting_labels(self):
        data = self.read()
        self.assertEqual(data["profileName"], "Game")
        self.assertEqual(data["settings"]["scalingGpu"]["friendlyName"], "NVIDIA RTX")
        self.assertEqual(data["settings"]["scalingGpu"]["resolvedNativeId"], 1)
        self.assertEqual(data["settings"]["scalingGpu"]["mode"], "explicit")
        self.assertEqual(data["settings"]["outputDisplay"]["mode"], "automatic")
        self.assertEqual(data["nativeSettings"]["ScalingType"], {"label": "Scaling Type", "value": "NIS"})

    def test_remapping_and_renaming_keep_the_same_profile_file_in_sync(self):
        GpuSelectionService(self.manager).refresh({"gpus": [
            dict(deviceId="amd", name="AMD", lsGpuId=1),
            dict(deviceId="new-gpu", name="NVIDIA RTX", lsGpuId=2),
        ], "displays": []})
        data = self.read()
        self.assertEqual(data["settings"]["scalingGpu"]["deviceId"], "new-gpu")
        self.assertEqual(data["settings"]["scalingGpu"]["resolvedNativeId"], 2)
        self.assertEqual(data["nativeSettings"]["PreferredGpuId"]["value"], "2")
        self.profile.name = "Renamed"
        self.manager.add_or_update_profile(self.profile)
        self.assertEqual(self.read()["profileName"], "Renamed")

    def test_matching_snapshot_recovers_missing_identity_metadata(self):
        main = json.loads(self.manager.config_file.read_text(encoding="utf-8"))
        profile = next(p for p in main["profiles"] if p["id"] == self.profile.id)
        profile.pop("preferred_gpu_device_id")
        profile.pop("preferred_gpu_name")
        self.manager.config_file.write_text(json.dumps(main), encoding="utf-8")
        restored = ProfileManager(self.directory).get_profile_by_id(self.profile.id)
        self.assertEqual(restored.preferred_gpu_device_id, "old-gpu")
        self.assertEqual(restored.preferred_gpu_name, "NVIDIA RTX")

    def test_interrupted_snapshot_write_cannot_restore_a_cleared_override(self):
        # An unavailable saved GPU already has native ID 0. Clearing that
        # identity also leaves ID 0, so native numbers alone cannot detect a
        # stale snapshot; the intent revision must differ.
        self.profile.native_scaling_settings["PreferredGpuId"] = "0"
        self.manager.save_config()
        self.profile.preferred_gpu_device_id = None
        self.profile.preferred_gpu_name = None
        with patch.object(self.manager.profile_settings, "sync", side_effect=OSError("interrupted")):
            with self.assertLogs("LSCompanion.ProfileManager", level="ERROR"):
                self.manager.save_config()
        reloaded = ProfileManager(self.directory).get_profile_by_id(self.profile.id)
        self.assertIsNone(reloaded.preferred_gpu_device_id)
        self.assertIsNone(reloaded.preferred_gpu_name)

    def test_corrupt_snapshot_does_not_discard_main_profiles(self):
        self.path.write_text("not json", encoding="utf-8")
        with self.assertLogs("LSCompanion.ProfileSettings", level="WARNING"):
            reloaded = ProfileManager(self.directory)
        self.assertEqual(reloaded.get_profile_by_id(self.profile.id).name, "Game")

    def test_missing_legacy_identity_falls_back_without_changing_other_settings(self):
        legacy = self.profile.model_copy(deep=True)
        legacy.preferred_gpu_device_id = None
        legacy.preferred_gpu_name = None
        legacy.profile_settings_revision = None
        self.manager.config.profiles = [legacy]
        self.path.unlink()
        self.manager.config.gpu_inventory_snapshot = {}
        GpuSelectionService(self.manager).refresh({"gpus": [dict(deviceId="different", name="AMD", lsGpuId=1)], "displays": []})
        self.assertEqual(legacy.native_scaling_settings["PreferredGpuId"], "0")
        self.assertEqual(legacy.native_scaling_settings["ScalingType"], "NIS")
        self.assertEqual(self.read()["settings"]["scalingGpu"]["mode"], "general")

    def test_clone_has_a_separate_snapshot_and_delete_removes_only_the_owned_file(self):
        clone = self.profile.model_copy(deep=True, update={"id": "cloned-profile", "name": "Clone"})
        self.manager.add_or_update_profile(clone)
        clone_path = self.manager.profile_settings.path(clone)
        unrelated = clone_path.parent / "user-notes.json"
        unrelated.write_text("{}", encoding="utf-8")
        self.assertEqual(json.loads(clone_path.read_text(encoding="utf-8"))["profileName"], "Clone")
        self.assertTrue(self.manager.delete_profile(clone.id))
        self.assertFalse(clone_path.exists())
        self.assertTrue(self.path.is_file())
        self.assertTrue(unrelated.is_file())

    def test_unchanged_profile_snapshots_are_not_rewritten(self):
        modified = self.path.stat().st_mtime_ns
        self.manager.save_config()
        self.assertEqual(self.path.stat().st_mtime_ns, modified)

    def test_startup_native_refresh_cannot_turn_follow_general_into_fixed_gpu(self):
        default = self.manager.ensure_lossless_default_profile({
            "Title": "Default", "PreferredGpuId": "0", "OutputDisplayId": "0", "ScalingType": "NIS"})
        self.manager.ensure_lossless_default_profile({
            "Title": "Default", "PreferredGpuId": "2", "OutputDisplayId": "3", "ScalingType": "LS1"})
        self.assertEqual(default.native_scaling_settings["PreferredGpuId"], "0")
        self.assertEqual(default.native_scaling_settings["OutputDisplayId"], "0")
        self.assertEqual(default.native_scaling_settings["ScalingType"], "LS1")

    def test_refreshing_native_profile_keeps_companion_routing_intent(self):
        self.profile.native_scaling_settings["PreferredGpuId"] = "0"
        self.profile.preferred_gpu_device_id = None
        self.profile.preferred_gpu_name = None
        self.manager.save_config()
        refreshed = self.manager.import_native_profile({
            "Title": "Game", "PreferredGpuId": "2", "ScalingType": "FSR"}, existing_profile_id=self.profile.id)
        self.assertEqual(refreshed.native_scaling_settings["PreferredGpuId"], "0")
        self.assertEqual(refreshed.native_scaling_settings["ScalingType"], "FSR")
