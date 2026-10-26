import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from companion.core.models import Profile
from companion.core.profile_manager import ProfileManager
from companion.services.gpu_router import GpuRouter
from companion.services.gpu_selections import GpuSelectionService, match_device
from companion.core.state import AppState
from companion.services.automation import AutomationController


def inventory(nvidia_id="nvidia-old", *, reverse=False):
    cards = [(nvidia_id, "NVIDIA RTX", "NVIDIA"), ("amd", "AMD Radeon", "AMD")]
    if reverse:
        cards.reverse()
    return {
        "gpus": [dict(deviceId=key, name=name, vendor=vendor, lsGpuId=index,
                      connectedDisplays=[name + " monitor"]) for index, (key, name, vendor) in enumerate(cards, 1)],
        "displays": [dict(deviceId=vendor + "-monitor", deviceName=f"DISPLAY{index}", name=name + " monitor",
                          gpuDeviceId=key, lsGpuId=index, lsDisplayId=index)
                     for index, (key, name, vendor) in enumerate(cards, 1)],
        "hasNvidia": True,
    }


class GpuSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.manager = ProfileManager(Path(self.temp.name))
        self.profile = Profile(name="Game", native_scaling_settings={"PreferredGpuId": "1", "OutputDisplayId": "1"})
        self.manager.config.profiles = [self.profile]
        self.manager.save_config = Mock()
        self.service = GpuSelectionService(self.manager)

    def bind(self):
        self.service.capture_profile(self.profile, inventory())
        self.service.refresh(inventory())

    def test_reordering_maps_both_gpu_and_monitor_by_identity_not_old_number(self):
        self.bind()
        self.service.refresh(inventory(reverse=True))
        self.assertEqual(self.profile.native_scaling_settings["PreferredGpuId"], "2")
        self.assertEqual(self.profile.native_scaling_settings["OutputDisplayId"], "2")
        self.assertEqual(self.profile.preferred_gpu_device_id, "nvidia-old")
        self.assertEqual(self.profile.output_display_device_id, "NVIDIA-monitor")

    def test_driver_id_change_recovers_by_unique_friendly_name(self):
        self.bind()
        self.service.refresh(inventory("nvidia-new", reverse=True))
        self.assertEqual(self.profile.preferred_gpu_device_id, "nvidia-new")
        self.assertEqual(self.profile.native_scaling_settings["PreferredGpuId"], "2")

    def test_global_preference_is_rebound_and_name_saved(self):
        self.manager.config.auto_route_gpu_to_display = False
        self.manager.config.preferred_scaling_gpu_device_id = "nvidia-old"
        self.service.refresh(inventory())
        self.service.refresh(inventory("nvidia-new", reverse=True))
        self.assertEqual(self.manager.config.preferred_scaling_gpu_device_id, "nvidia-new")
        self.assertEqual(self.manager.config.preferred_scaling_gpu_name, "NVIDIA RTX")

    def test_pci_model_match_survives_instance_suffix_change(self):
        old = r"PCI\VEN_10DE&DEV_1234&SUBSYS_5678\old-instance"
        new = r"PCI\VEN_10DE&DEV_1234&SUBSYS_5678\new-instance"
        selected = {"deviceId": new, "name": "New driver label"}
        self.assertIs(match_device([selected], old, "Old driver label", gpu=True), selected)

    def test_ambiguous_friendly_name_follows_general_settings_instead_of_guessing(self):
        self.bind()
        duplicate = inventory("new-one")
        duplicate["gpus"].append(dict(duplicate["gpus"][0], deviceId="new-two", lsGpuId=3))
        self.service.refresh(duplicate)
        self.assertEqual(self.profile.native_scaling_settings["PreferredGpuId"], "0")
        self.assertEqual(self.profile.preferred_gpu_device_id, "nvidia-old")
        self.assertTrue(self.service.warnings)

    def test_legacy_number_with_history_uses_historical_mapping(self):
        self.manager.config.gpu_inventory_snapshot = inventory()
        self.service.refresh(inventory("nvidia-new", reverse=True))
        self.assertEqual(self.profile.native_scaling_settings["PreferredGpuId"], "2")
        self.assertEqual(self.profile.preferred_gpu_name, "NVIDIA RTX")

    def test_legacy_number_without_history_follows_general_settings_even_when_number_exists(self):
        self.service.refresh(inventory(reverse=True))
        self.assertEqual(self.profile.native_scaling_settings["PreferredGpuId"], "0")
        self.assertIsNone(self.profile.preferred_gpu_device_id)

    def test_unavailable_gpu_returns_when_hardware_reappears(self):
        self.bind()
        missing = inventory()
        missing["gpus"] = missing["gpus"][1:]
        self.service.refresh(missing)
        self.assertEqual(self.profile.native_scaling_settings["PreferredGpuId"], "0")
        self.service.refresh(inventory(reverse=True))
        self.assertEqual(self.profile.native_scaling_settings["PreferredGpuId"], "2")

    def test_empty_inventory_does_not_erase_selections_or_history(self):
        self.bind()
        before = self.manager.config.model_dump()
        self.manager.save_config.reset_mock()
        self.assertFalse(self.service.refresh({"gpus": [], "displays": []}))
        self.assertEqual(self.manager.config.model_dump(), before)
        self.manager.save_config.assert_not_called()

    def test_unchanged_inventory_does_not_repeatedly_save(self):
        self.bind()
        self.manager.save_config.reset_mock()
        self.assertFalse(self.service.refresh(inventory()))
        self.manager.save_config.assert_not_called()

    def test_unchanged_foreground_polls_do_not_serialize_large_profile_configs(self):
        self.bind()
        with patch.object(type(self.manager.config), "model_dump", side_effect=AssertionError("unnecessary serialization")):
            self.assertFalse(self.service.refresh(inventory()))

    def test_failed_persistence_is_retried(self):
        self.manager.save_config.side_effect = [OSError("temporarily unavailable"), None]
        with self.assertRaises(OSError):
            self.service.refresh(inventory())
        self.service.refresh(inventory())
        self.assertEqual(self.manager.save_config.call_count, 2)

    def test_stale_dashboard_numeric_choice_is_resolved_using_its_identity(self):
        self.bind()
        draft = self.profile.model_copy(deep=True)
        self.service.capture_profile(draft, inventory("nvidia-new", reverse=True), self.profile)
        self.assertEqual(draft.native_scaling_settings["PreferredGpuId"], "2")
        self.assertEqual(draft.preferred_gpu_device_id, "nvidia-new")

    def test_selecting_follow_general_clears_saved_override(self):
        self.bind()
        self.profile.native_scaling_settings["PreferredGpuId"] = "0"
        self.service.capture_profile(self.profile, inventory())
        self.assertIsNone(self.profile.preferred_gpu_device_id)
        self.assertIsNone(self.profile.preferred_gpu_name)

    def test_missing_explicit_global_gpu_does_not_silently_select_auto(self):
        router = GpuRouter(lambda: [])
        with self.assertRaisesRegex(RuntimeError, "General Settings"):
            router.route_for_gpu("missing")
        self.assertEqual(router.route_for_gpu(None)["lsGpuId"], 0)

    def test_names_and_inventory_survive_config_reload(self):
        self.bind()
        self.manager.save_config = ProfileManager.save_config.__get__(self.manager)
        self.manager.save_config()
        reloaded = ProfileManager(Path(self.temp.name))
        profile = reloaded.get_profile_by_id(self.profile.id)
        self.assertEqual(profile.preferred_gpu_name, "NVIDIA RTX")
        self.assertEqual(reloaded.config.gpu_inventory_snapshot, inventory())

    def test_actual_routing_refreshes_profile_identity_before_resolving_numbers(self):
        self.profile.preferred_gpu_device_id = "nvidia-old"
        self.profile.preferred_gpu_name = "NVIDIA RTX"
        self.profile.native_scaling_settings["OutputDisplayId"] = "0"
        self.manager.config.auto_route_gpu_to_display = False
        router = GpuRouter(lambda: [
            dict(deviceId="amd", name="AMD Radeon", deviceName="DISPLAY1"),
            dict(deviceId="nvidia-new", name="NVIDIA RTX", deviceName="DISPLAY2"),
        ])
        automation = AutomationController(self.manager, AppState(), gpu_router=router)
        route = automation._desired_gpu_route(self.profile, None)
        self.assertEqual(route["lsGpuId"], 2)
        self.assertEqual(route["gpuDeviceId"], "nvidia-new")
        self.assertEqual(self.profile.native_scaling_settings["PreferredGpuId"], "2")

    def test_unavailable_profile_id_cannot_reach_lossless_scaling(self):
        router = GpuRouter(lambda: [])
        with self.assertRaisesRegex(RuntimeError, "GPU ID"):
            router.route_for_ls_ids(123, 0)
