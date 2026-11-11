import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from companion.core.profile_manager import ProfileManager
from companion.core.state import AppState
from companion.services.automation import AutomationController
from companion.services.gpu_router import GpuRouter


class GpuHandoffTests(unittest.TestCase):
    def moved_controller(self, *, fixed_display=False, pinned_gpu=False):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        manager = ProfileManager(Path(directory.name))
        profile = manager.config.profiles[0]
        profile.native_scaling_settings = {
            "PreferredGpuId": "1" if pinned_gpu else "0",
            "OutputDisplayId": "1" if fixed_display else "0"}
        state = AppState()
        state.current_active_profile = profile
        state.is_scaling_active = True
        state.scaling_target_pid, state.scaling_target_hwnd = 42, 84
        # Calibration may map multiple physical outputs to the same LS IDs.
        rows = [{"deviceName": rf"\\.\DISPLAY{index}", "name": "GPU",
                 "deviceId": "PCI\\GPU", "primary": index == 1}
                for index in (1, 2)]
        router = GpuRouter(lambda: rows, lambda hwnd: r"\\.\DISPLAY2",
                           ls_display_id_map={r"\\.\DISPLAY1": 1, r"\\.\DISPLAY2": 1})
        watcher = Mock()
        watcher.stop_lossless_scaling.return_value = True
        watcher.launch_lossless_scaling.return_value = True
        watcher.check_is_lossless_scaling_running.return_value = True
        watcher.maintain_scaling_target_foreground.return_value = True
        settings = Mock()
        settings.gpu_route_changes_required.return_value = False
        automation = AutomationController(manager, state, watcher, gpu_router=router, lossless_settings=settings)
        automation.set_scaling = Mock(return_value=True)
        automation._scaling_display_device = r"\\.\DISPLAY1"
        # A physical window move should work even immediately after a relaunch.
        automation._last_gpu_route_relaunch_at = 99.0
        return automation, watcher, settings

    def test_equal_calibrated_ids_still_follow_physical_monitor_after_settling(self):
        automation, watcher, settings = self.moved_controller()
        with patch("companion.services.automation.time.monotonic", return_value=100):
            self.assertFalse(automation.reconcile_gpu_route())
            automation.set_scaling.assert_not_called()
        with patch("companion.services.automation.time.monotonic", return_value=101):
            self.assertTrue(automation.reconcile_gpu_route())
            self.assertFalse(automation.reconcile_gpu_route())
        watcher.stop_lossless_scaling.assert_called_once()
        watcher.launch_lossless_scaling.assert_called_once_with(force=True)
        settings.update_gpu_route.assert_not_called()
        self.assertEqual(automation._scaling_display_device, r"\\.\DISPLAY2")
        self.assertEqual(automation.set_scaling.call_count, 2)
        self.assertEqual(automation.set_scaling.call_args.kwargs["target_hwnd"], 84)

    def test_rendering_gpu_pin_does_not_pin_output_monitor(self):
        automation, _, _ = self.moved_controller(pinned_gpu=True)
        route = automation._desired_gpu_route(automation.state.current_active_profile, 84)
        self.assertEqual(route["lsGpuId"], 1)
        self.assertEqual(route["deviceName"], r"\\.\DISPLAY2")
        with patch("companion.services.automation.time.monotonic", return_value=100):
            self.assertFalse(automation.reconcile_gpu_route())
        with patch("companion.services.automation.time.monotonic", return_value=101):
            self.assertTrue(automation.reconcile_gpu_route())

    def test_fixed_output_monitor_does_not_follow_window(self):
        automation, watcher, _ = self.moved_controller(fixed_display=True)
        self.assertFalse(automation.reconcile_gpu_route())
        watcher.stop_lossless_scaling.assert_not_called()

    def test_alt_tab_during_move_defers_handoff_until_target_returns(self):
        automation, watcher, _ = self.moved_controller()
        watcher.maintain_scaling_target_foreground.return_value = False
        self.assertFalse(automation.reconcile_gpu_route())
        self.assertIsNone(automation._pending_display_handoff)
        automation.set_scaling.assert_not_called()

    def test_window_returning_to_original_monitor_cancels_pending_move(self):
        automation, watcher, _ = self.moved_controller()
        self.assertFalse(automation.reconcile_gpu_route())
        automation.gpu_router._window_device_resolver = lambda hwnd: r"\\.\DISPLAY1"
        self.assertFalse(automation.reconcile_gpu_route())
        self.assertIsNone(automation._pending_display_handoff)
        watcher.stop_lossless_scaling.assert_not_called()

    def test_active_window_display_change_unscales_updates_and_rescales(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = ProfileManager(Path(directory))
            manager.config.auto_route_gpu_to_display = True
            profile = manager.config.profiles[0]
            profile.lossless_profile_title = "Gaming"
            state = AppState()
            state.current_active_profile = profile
            state.is_scaling_active = True
            state.scaling_target_pid = 42
            state.scaling_target_hwnd = 84
            state.current_gpu_route_key = r"1:1:\\.\DISPLAY1"
            watcher = Mock()
            watcher.check_is_lossless_scaling_running.return_value = True
            watcher.stop_lossless_scaling.return_value = True
            watcher.launch_lossless_scaling.return_value = True
            router = Mock()
            router.route_for_window.return_value = {
                "gpuDeviceId": r"PCI\VEN_1002",
                "lsGpuId": 2,
                "lsDisplayId": 2,
                "deviceName": r"\\.\DISPLAY2",
            }
            settings = Mock()
            settings.gpu_route_changes_required.return_value = True
            settings.update_gpu_route.return_value = True
            automation = AutomationController(
                manager,
                state,
                watcher,
                gpu_router=router,
                lossless_settings=settings,
            )
            automation.set_scaling = Mock(side_effect=[True, True])

            self.assertTrue(automation.reconcile_gpu_route())

            settings.update_gpu_route.assert_called_once_with("Gaming", 2, 2)
            self.assertEqual(automation.set_scaling.call_count, 2)
            self.assertFalse(automation.set_scaling.call_args_list[0].args[0])
            self.assertFalse(automation.set_scaling.call_args_list[0].kwargs["park_runtime_on_stop"])
            self.assertTrue(automation.set_scaling.call_args_list[1].args[0])
            self.assertEqual(automation.set_scaling.call_args_list[1].kwargs["target_hwnd"], 84)


if __name__ == "__main__":
    unittest.main()
