from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from companion.core.profile_manager import ProfileManager
from companion.core.state import AppState
from companion.main import CompanionApplication
from companion.services.automation import AutomationController
from companion.services.ls_inspector import ScaledTargetInfo


class ScalingWindowObservationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        manager = ProfileManager(Path(temporary.name) / "config")
        manager.config.minimize_other_windows_on_scale = True
        self.windows = Mock()
        state = AppState()
        self.automation = AutomationController(manager, state, window_manager=self.windows)
        self.app = SimpleNamespace(state=state, profile_manager=manager, automation=self.automation)

    def observe(self, **kwargs):
        CompanionApplication._observe_scaling_target(self.app, ScaledTargetInfo(**kwargs))

    def test_late_log_identity_reaches_minimizer_once_from_monitor_loop(self):
        with patch.object(self.automation, "reconcile_observed_scaling_state",
                          wraps=self.automation.reconcile_observed_scaling_state) as reconcile:
            self.observe(is_active=True, source="win32_window_hook", pid=99,
                         process_name="LosslessScaling.exe", hwnd=999)
            self.windows.minimize_others_on_target_monitor.assert_not_called()
            for _ in range(10):
                self.observe(is_active=True, source="log_file", pid=42, process_name="Game.exe", hwnd=456)
            self.assertEqual(reconcile.call_count, 2)
            self.windows.minimize_others_on_target_monitor.assert_called_once_with(456, target_pid=42)

    def test_unknown_observation_cannot_stop_active_scaling(self):
        self.observe(is_active=True, source="log_file", pid=42, hwnd=456)
        self.observe(is_active=False, source="unknown")
        self.assertTrue(self.app.state.is_scaling_active)

    def test_changed_verified_source_is_applied_without_a_false_idle_transition(self):
        self.observe(is_active=True, source="log_file", pid=42, hwnd=456)
        self.observe(is_active=True, source="log_file", pid=43, hwnd=457)
        self.assertEqual(self.windows.minimize_others_on_target_monitor.call_count, 2)
        self.windows.minimize_others_on_target_monitor.assert_called_with(457, target_pid=43)

    def test_disabled_setting_does_not_add_source_polling(self):
        self.app.profile_manager.config.minimize_other_windows_on_scale = False
        with patch.object(self.automation, "reconcile_observed_scaling_state",
                          wraps=self.automation.reconcile_observed_scaling_state) as reconcile:
            for _ in range(10):
                self.observe(is_active=True, source="win32_window_hook", pid=99, hwnd=999)
            self.assertEqual(reconcile.call_count, 1)
        self.windows.minimize_others_on_target_monitor.assert_not_called()


if __name__ == "__main__":
    unittest.main()
