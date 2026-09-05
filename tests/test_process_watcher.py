import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from companion.core.models import HotkeyConfig
from companion.core.state import AppState
from companion.services.process_watcher import ProcessWatcher


class ProcessWatcherScalingControlTests(unittest.TestCase):
    def make_watcher(self):
        config = SimpleNamespace(
            disable_native_auto_scale=True,
            hotkey_sync_mode="helper_controls_lossless",
            global_hotkey=HotkeyConfig(modifiers=["ctrl", "alt"], key="s"),
            auto_launch_lossless_scaling=True,
            lossless_scaling_exe_path=r"C:\Lossless Scaling\LosslessScaling.exe",
        )
        manager = SimpleNamespace(config=config, save_config=Mock())
        settings = Mock()
        return ProcessWatcher(manager, AppState(), lossless_settings=settings), settings

    def test_enforcement_restarts_running_lossless_scaling(self):
        watcher, settings = self.make_watcher()
        settings.read_hotkey.return_value = HotkeyConfig(modifiers=["alt"], key="x")
        settings.control_changes_required.return_value = {"hotkey": True, "auto_scale": True}
        settings.update_control_settings.return_value = {"hotkey": True, "auto_scale": True}

        with (
            patch.object(watcher, "check_is_lossless_scaling_running", return_value=True),
            patch.object(watcher, "stop_lossless_scaling", return_value=True) as stop,
            patch.object(watcher, "launch_lossless_scaling", return_value=True) as launch,
        ):
            self.assertTrue(watcher.enforce_helper_scaling_control())

        stop.assert_called_once_with()
        settings.update_control_settings.assert_called_once_with(
            hotkey=watcher.profile_manager.config.global_hotkey,
            disable_auto_scale=True,
        )
        launch.assert_called_once_with(force=True)

    def test_enforcement_leaves_running_process_alone_when_already_disabled(self):
        watcher, settings = self.make_watcher()
        settings.read_hotkey.return_value = watcher.profile_manager.config.global_hotkey
        settings.control_changes_required.return_value = {"hotkey": False, "auto_scale": False}

        with (
            patch.object(watcher, "stop_lossless_scaling") as stop,
            patch.object(watcher, "launch_lossless_scaling") as launch,
        ):
            self.assertTrue(watcher.enforce_helper_scaling_control())

        stop.assert_not_called()
        launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
