import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from companion.core.profile_manager import ProfileManager
from companion.core.state import AppState
from companion.services.automation import AutomationController


class GpuHandoffTests(unittest.TestCase):
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
