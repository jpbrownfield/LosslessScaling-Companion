"""General GPU choices must apply through the calibrated map, like profile edits."""
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

from companion.core.profile_manager import ProfileManager
from companion.core.state import AppState
from companion.services.automation import AutomationController
from companion.services.gpu_router import GpuRouter
from companion.services.ls_settings import LosslessSettingsXml


RTX_3090 = r"PCI\VEN_10DE&DEV_2203"
RTX_5050 = r"PCI\VEN_10DE&DEV_2D83"


@contextmanager
def controller():
    with tempfile.TemporaryDirectory() as directory:
        manager = ProfileManager(Path(directory))
        config = manager.config
        config.lossless_scaling_exe_path = str(Path(directory) / "LosslessScaling.exe")
        config.ls_gpu_id_map = {RTX_3090: 2, RTX_5050: 6}
        profile = config.profiles[0]
        profile.lossless_profile_title = "Game"
        profile.native_scaling_settings = {"PreferredGpuId": "0", "OutputDisplayId": "0"}
        router = GpuRouter(lambda: [
            {"deviceName": "DISPLAY1", "name": "RTX 3090", "deviceId": RTX_3090,
             "displayName": "Main monitor", "displayDeviceId": "MONITOR1"},
            {"deviceName": "DISPLAY2", "name": "RTX 5050", "deviceId": RTX_5050,
             "displayName": "Secondary monitor", "displayDeviceId": "MONITOR2"},
        ], lambda hwnd: "DISPLAY2", ls_gpu_id_map=lambda: config.ls_gpu_id_map)
        config.gpu_inventory_snapshot = router.detect()
        state = AppState()
        state.current_active_profile = profile
        state.current_foreground_hwnd = 82
        settings = LosslessSettingsXml(str(Path(directory) / "Settings.xml"))
        settings.path.write_text("<Settings><GameProfiles><Profile><Title>Game</Title>"
            "<PreferredGpuId>2</PreferredGpuId><OutputDisplayId>1</OutputDisplayId>"
            "</Profile></GameProfiles></Settings>", encoding="utf-8")
        watcher = Mock()
        host = {"running": True}
        watcher.check_is_lossless_scaling_running.side_effect = lambda: host["running"]
        def stop():
            host["running"] = False
            return True
        def launch(**_kwargs):
            host["running"] = True
            return True
        watcher.stop_lossless_scaling.side_effect = stop
        watcher.launch_lossless_scaling.side_effect = launch
        watcher.maintain_scaling_target_foreground.return_value = True
        automation = AutomationController(manager, state, watcher, gpu_router=router, lossless_settings=settings)
        automation.resolve_deployment_files = Mock(return_value=[])
        automation.deployment_manager = Mock()
        automation.deployment_manager.needs_change.return_value = False
        automation.deployment_manager.runtime_packages_need_state.return_value = False
        automation._cleanup_profile = Mock()
        automation._trigger_profile_runtime_actions = Mock()
        automation.overlay_indicator = Mock()
        automation.set_scaling = Mock(return_value=True)
        automation._last_gpu_route_relaunch_at = time.monotonic()
        yield automation, manager, state, watcher, settings


class GeneralGpuSettingsTests(unittest.TestCase):
    @staticmethod
    def pin(manager):
        manager.config.auto_route_gpu_to_display = False
        manager.config.preferred_scaling_gpu_device_id = RTX_5050
        manager.config.preferred_scaling_gpu_name = "RTX 5050"

    def test_idle_global_pin_applies_calibrated_id_despite_recent_calibration_restart(self):
        with controller() as (automation, manager, state, watcher, settings):
            self.pin(manager)
            automation.apply_general_gpu_settings()
            self.assertEqual(settings.get_profile("Game")["PreferredGpuId"], "6")
            self.assertEqual(state.current_active_profile.native_scaling_settings["PreferredGpuId"], "0")
            watcher.stop_lossless_scaling.assert_called_once_with()
            watcher.launch_lossless_scaling.assert_called_once_with(force=True)
            self.assertFalse(state.is_scaling_active)

    def test_idle_follow_monitor_applies_calibrated_window_gpu_and_output(self):
        with controller() as (automation, manager, _, _, settings):
            manager.config.auto_route_gpu_to_display = True
            automation.apply_general_gpu_settings()
            values = settings.get_profile("Game")
            self.assertEqual((values["PreferredGpuId"], values["OutputDisplayId"]), ("6", "2"))

    def test_active_general_edit_waits_for_original_application_then_reloads_once(self):
        with controller() as (automation, manager, state, watcher, settings):
            self.pin(manager)
            state.is_scaling_active = True
            state.scaling_target_pid, state.scaling_target_hwnd = 42, 82
            automation.apply_general_gpu_settings()
            watcher.stop_lossless_scaling.assert_not_called()
            automation._pending_profile_restart["ready_at"] = 0
            watcher.maintain_scaling_target_foreground.return_value = False
            automation.reconcile_pending_addon_settings()
            watcher.stop_lossless_scaling.assert_not_called()
            watcher.maintain_scaling_target_foreground.return_value = True
            automation.reconcile_pending_addon_settings()
            self.assertEqual(settings.get_profile("Game")["PreferredGpuId"], "6")
            watcher.stop_lossless_scaling.assert_called_once_with()
            automation.set_scaling.assert_called_once_with(
                True, state.current_active_profile, reason="profile_edit", target_pid=42, target_hwnd=82)
            self.assertIsNone(automation._pending_profile_restart)

    def test_explicit_profile_gpu_remains_authoritative(self):
        with controller() as (automation, manager, state, watcher, settings):
            self.pin(manager)
            profile = state.current_active_profile
            profile.native_scaling_settings["PreferredGpuId"] = "2"
            profile.preferred_gpu_device_id, profile.preferred_gpu_name = RTX_3090, "RTX 3090"
            automation.apply_general_gpu_settings()
            self.assertEqual(settings.get_profile("Game")["PreferredGpuId"], "2")
            watcher.stop_lossless_scaling.assert_not_called()

    def test_periodic_handoff_includes_global_pin_and_profile_with_fixed_output(self):
        with controller() as (automation, manager, state, _, settings):
            self.pin(manager)
            profile = state.current_active_profile
            output = manager.config.gpu_inventory_snapshot["displays"][0]
            profile.native_scaling_settings["OutputDisplayId"] = "1"
            profile.output_display_device_id, profile.output_display_name = output["deviceId"], output["name"]
            state.is_scaling_active = True
            state.scaling_target_pid, state.scaling_target_hwnd = 42, 82
            automation._last_gpu_route_relaunch_at = 0
            self.assertTrue(automation.reconcile_gpu_route())
            values = settings.get_profile("Game")
            self.assertEqual((values["PreferredGpuId"], values["OutputDisplayId"]), ("6", "1"))

    def test_periodic_handoff_does_not_interrupt_gpu_testing_or_a_queued_edit(self):
        with controller() as (automation, manager, state, watcher, _):
            self.pin(manager)
            state.is_scaling_active = True
            state.scaling_target_pid, state.scaling_target_hwnd = 42, 82
            state.benchmark_mode_active = True
            self.assertFalse(automation.reconcile_gpu_route())
            state.benchmark_mode_active = False
            automation._pending_profile_restart = {"profile_id": state.current_active_profile.id}
            self.assertFalse(automation.reconcile_gpu_route())
            watcher.stop_lossless_scaling.assert_not_called()

    def test_cached_trial_route_does_not_hide_restored_native_indexes(self):
        with controller() as (automation, manager, state, watcher, settings):
            self.pin(manager)
            state.is_scaling_active = True
            state.scaling_target_pid, state.scaling_target_hwnd = 42, 82
            state.current_gpu_route_key = "6:2:"
            automation._last_gpu_route_relaunch_at = 0
            self.assertTrue(automation.reconcile_gpu_route())
            self.assertEqual(settings.get_profile("Game")["PreferredGpuId"], "6")
            self.assertFalse(automation.reconcile_gpu_route())
            watcher.stop_lossless_scaling.assert_called_once_with()
