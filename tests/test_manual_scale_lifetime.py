"""A manual stop suppresses every automatic trigger for one process lifetime."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import psutil

from companion.core.profile_manager import ProfileManager
from companion.core.state import AppState
from companion.services.automation import AutomationController


class ManualScaleLifetimeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.manager = ProfileManager(Path(directory.name))
        self.state = AppState()
        self.watcher = Mock()
        self.watcher.stop_lossless_scaling.return_value = True
        self.watcher.check_is_lossless_scaling_running.return_value = True
        self.automation = AutomationController(
            self.manager, self.state, self.watcher,
            hotkey_trigger=Mock(return_value=True), gpu_router=Mock())
        self.births = {42: 100.0, 43: 200.0}
        self.automation._process_birth_time = Mock(side_effect=lambda pid: self.births[pid])
        self.automation._verify_gpu_after_scaling = Mock(return_value="confirmed")
        self.automation._live_deployment_stale = Mock(return_value=False)
        self.profile = self.manager.config.profiles[0]
        self.state.current_active_profile = self.profile

    def stop(self, reason="override_hotkey"):
        self.state.is_scaling_active = True
        self.state.scaling_target_pid, self.state.scaling_target_hwnd = 42, 84
        self.assertTrue(self.automation.set_scaling(False, reason=reason, force=True))

    def test_manual_stop_survives_alt_tab_and_all_automatic_triggers(self):
        self.stop()
        for reason in ("focus", "browser_video", "process_lasso_performance_mode:game"):
            with self.subTest(reason=reason):
                self.assertFalse(self.automation.set_scaling(
                    True, reason=reason, target_pid=42, target_hwnd=85, force=True))
        self.automation.hotkey_trigger.assert_not_called()
        self.assertFalse(self.state.is_scaling_active)

    def test_other_app_with_same_profile_can_still_auto_scale(self):
        self.stop("manual")
        self.assertTrue(self.automation.set_scaling(
            True, reason="focus", target_pid=43, target_hwnd=86))
        self.assertEqual(self.state.scaling_target_pid, 43)

    def test_explicit_restart_is_allowed_without_erasing_lifetime_intent(self):
        self.stop()
        self.assertTrue(self.automation.set_scaling(
            True, reason="override_hotkey", target_pid=42, target_hwnd=84))
        self.assertTrue(self.automation._auto_scaling_suppressed(42))

    def test_recycled_pid_is_a_new_application_lifetime(self):
        self.stop()
        self.births[42] = 300.0
        self.assertTrue(self.automation.set_scaling(
            True, reason="focus", target_pid=42, target_hwnd=84))
        self.assertNotIn(42, self.automation._manually_stopped_processes)

    def test_automatic_handoff_and_fullscreen_stops_do_not_suppress(self):
        for reason in ("fullscreen_exit", "display_gpu_handoff", "override_hotkey_switch"):
            self.stop(reason)
            self.assertFalse(self.automation._auto_scaling_suppressed(42))

    def test_failed_stop_does_not_suppress(self):
        self.state.is_scaling_active = True
        self.state.scaling_target_pid = 42
        self.watcher.stop_lossless_scaling.return_value = False
        self.automation.hotkey_trigger.return_value = False
        self.assertFalse(self.automation.set_scaling(False, reason="manual", force=True))
        self.assertFalse(self.automation._auto_scaling_suppressed(42))

    def test_gpu_pin_does_not_move_a_window_that_follows_application_monitor(self):
        self.profile.native_scaling_settings = {"PreferredGpuId": "1", "OutputDisplayId": "0"}
        self.automation.window_manager = Mock()
        self.assertTrue(self.automation.set_scaling(
            True, reason="manual", target_pid=42, target_hwnd=84))
        self.automation.window_manager.move_window_to_display.assert_not_called()

    def test_exited_process_is_pruned_but_inaccessible_process_keeps_intent(self):
        self.stop()
        self.automation._process_birth_time.side_effect = psutil.AccessDenied(42)
        self.assertTrue(self.automation._auto_scaling_suppressed(42))
        self.automation._process_birth_time.side_effect = psutil.NoSuchProcess(42)
        self.assertFalse(self.automation._auto_scaling_suppressed(42))

    def test_focus_callback_does_not_schedule_after_manual_stop(self):
        self.stop()
        self.profile.is_default = False
        self.profile.auto_scale = True
        self.manager.config.disable_native_auto_scale = True
        self.automation.set_scaling = Mock()
        self.automation._trigger_profile_runtime_actions(
            self.profile, reshade_swapped=False, target_pid=42, target_hwnd=84)
        self.automation.set_scaling.assert_not_called()

    def test_native_hotkey_stop_is_remembered_only_after_observed_deactivation(self):
        self.state.is_scaling_active = True
        self.state.scaling_target_pid, self.state.scaling_target_hwnd = 42, 84
        self.automation.observe_native_scaling_hotkey(42, 84)
        self.assertFalse(self.automation._auto_scaling_suppressed(42))
        self.automation.reconcile_observed_scaling_state(False)
        self.assertTrue(self.automation._auto_scaling_suppressed(42))
        self.assertFalse(self.automation.set_scaling(
            True, reason="focus", target_pid=42, target_hwnd=84))

    def test_observed_alt_tab_stop_has_no_manual_intent(self):
        self.state.is_scaling_active = True
        self.state.scaling_target_pid, self.state.scaling_target_hwnd = 42, 84
        self.automation.reconcile_observed_scaling_state(False)
        self.assertFalse(self.automation._auto_scaling_suppressed(42))

    def test_stale_or_other_targets_native_stop_does_not_suppress(self):
        for pressed_at, pid, hwnd in ((1, 42, 84), (100, 43, 86)):
            self.state.is_scaling_active = True
            self.state.scaling_target_pid, self.state.scaling_target_hwnd = 42, 84
            with patch("companion.services.automation.time.monotonic", return_value=pressed_at):
                self.automation.observe_native_scaling_hotkey(pid, hwnd)
            with patch("companion.services.automation.time.monotonic", return_value=101):
                self.automation.reconcile_observed_scaling_state(False)
            self.assertFalse(self.automation._auto_scaling_suppressed(42))
