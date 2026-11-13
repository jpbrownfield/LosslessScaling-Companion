"""Instant stop on toggle + Scale Now (5s) delayed scaling."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from types import SimpleNamespace

from companion.core.profile_manager import ProfileManager
from companion.core.state import AppState
from companion.services.automation import AutomationController


def controller(state_active=False):
    manager = ProfileManager(Path(tempfile.mkdtemp()))
    state = AppState()
    state.is_scaling_active = state_active
    watcher = Mock()
    watcher.check_is_lossless_scaling_running.return_value = True
    watcher.stop_lossless_scaling.return_value = True
    watcher.launch_lossless_scaling.return_value = True
    automation = AutomationController(
        manager, state, watcher, hotkey_trigger=Mock(return_value=True)
    )
    return automation, state, watcher


class InstantStopTests(unittest.TestCase):
    def test_hotkey_toggle_off_kills_ls_instead_of_waiting(self):
        automation, state, watcher = controller(state_active=True)
        self.assertTrue(
            automation.set_scaling(False, reason="override_hotkey", force=True)
        )
        watcher.stop_lossless_scaling.assert_called_once_with()
        # No toggle hotkey emitted for the stop.
        automation.hotkey_trigger.assert_not_called()
        self.assertFalse(state.is_scaling_active)
        self.assertIsNone(state.scaling_target_pid)
        self.assertEqual(
            state.scaling_control_status["state"], "confirmed"
        )
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if watcher.launch_lossless_scaling.called:
                break
            time.sleep(0.05)
        watcher.launch_lossless_scaling.assert_called_once_with(force=True)

    def test_manual_toggle_off_kills_ls(self):
        automation, state, watcher = controller(state_active=True)
        self.assertTrue(automation.set_scaling(False, reason="manual", force=True))
        watcher.stop_lossless_scaling.assert_called_once_with()
        automation.hotkey_trigger.assert_not_called()
        self.assertFalse(state.is_scaling_active)

    def test_stop_without_confirmed_scaling_uses_hotkey_path(self):
        automation, state, watcher = controller(state_active=False)
        self.assertTrue(
            automation.set_scaling(False, reason="override_hotkey", force=True)
        )
        watcher.stop_lossless_scaling.assert_not_called()
        automation.hotkey_trigger.assert_called_once()
        self.assertFalse(state.is_scaling_active)

    def test_automatic_stop_keeps_graceful_path(self):
        automation, state, watcher = controller(state_active=True)
        self.assertTrue(
            automation.set_scaling(False, reason="fullscreen_exit", force=True)
        )
        watcher.stop_lossless_scaling.assert_not_called()
        automation.hotkey_trigger.assert_called_once()

    def test_failed_kill_falls_back_to_hotkey(self):
        automation, state, watcher = controller(state_active=True)
        watcher.stop_lossless_scaling.return_value = False
        watcher.check_is_lossless_scaling_running.return_value = True
        self.assertTrue(
            automation.set_scaling(False, reason="override_hotkey", force=True)
        )
        automation.hotkey_trigger.assert_called_once()

    def test_instant_stop_keeps_runtime_hotloaded(self):
        automation, state, watcher = controller(state_active=True)
        automation.deployment_manager = Mock()
        self.assertTrue(
            automation.set_scaling(False, reason="manual", force=True)
        )
        automation.deployment_manager.set_runtime_packages_active.assert_not_called()
        self.assertFalse(state.is_scaling_active)

    def test_same_profile_rescale_needs_no_restart(self):
        automation, state, watcher = controller()
        profile = automation.profile_manager.config.profiles[0]
        self.assertTrue(
            automation.set_scaling(True, profile, reason="manual", force=True)
        )
        watcher.stop_lossless_scaling.assert_not_called()
        watcher.launch_lossless_scaling.assert_not_called()
        automation.hotkey_trigger.assert_called_once()
        self.assertTrue(state.is_scaling_active)


class ScaleNowTests(unittest.TestCase):
    def test_scale_now_and_hotkey_launch_closed_ls_even_with_stale_active_state(self):
        for action in ("hotkey", "scale_now"):
            for stale in (False, True):
                with self.subTest(action=action, stale=stale):
                    automation, state, watcher = controller(state_active=stale)
                    config = automation.profile_manager.config
                    config.auto_launch_lossless_scaling = False
                    config.override_lossless_hotkey = action == "hotkey"
                    config.lossless_scaling_exe_path = "C:/LS/LosslessScaling.exe"
                    watcher.get_foreground_window_info.return_value = SimpleNamespace(
                        name="game.exe", exe_path="C:/Games/game.exe", pid=42, hwnd=82)
                    running = [False]
                    events = []
                    watcher.check_is_lossless_scaling_running.side_effect = lambda: running[0]

                    def launch(**kwargs):
                        running[0] = True
                        events.append("launch")
                        return True

                    watcher.launch_lossless_scaling.side_effect = launch
                    automation.activate_profile = Mock()
                    automation._live_deployment_stale = Mock(return_value=False)
                    automation._verify_gpu_after_scaling = Mock(return_value="confirmed")
                    automation._wait_for_ls_ready = Mock(side_effect=lambda: events.append("ready") or True)
                    automation.hotkey_trigger.side_effect = lambda **kwargs: events.append("hotkey") or True
                    if action == "hotkey":
                        self.assertTrue(automation.handle_override_hotkey())
                    else:
                        with patch("companion.services.automation.threading.Timer") as timer:
                            automation.scale_focused_after_delay(1)
                            timer.call_args.args[1]()
                    watcher.launch_lossless_scaling.assert_called_once_with(force=True)
                    self.assertEqual(events, ["launch", "ready", "hotkey"])
                    self.assertTrue(state.is_scaling_active)

    def test_start_failure_and_readiness_timeout_do_not_emit_scaling_key(self):
        for launch_ok in (False, True):
            with self.subTest(launch_ok=launch_ok):
                automation, state, watcher = controller()
                watcher.check_is_lossless_scaling_running.return_value = False
                watcher.launch_lossless_scaling.return_value = launch_ok
                automation._wait_for_ls_ready = Mock(return_value=False)
                self.assertFalse(automation.set_scaling(True, reason="manual", force=True))
                automation.hotkey_trigger.assert_not_called()
                self.assertFalse(state.is_scaling_active)
                self.assertEqual(state.scaling_control_status["state"], "error")

    def test_schedule_returns_delay_and_arms_timer(self):
        automation, _, _ = controller()
        self.assertEqual(automation.scale_focused_after_delay(5.0), 5.0)
        try:
            self.assertIsNotNone(automation._delayed_scale_timer)
            self.assertTrue(automation._delayed_scale_timer.is_alive())
            self.assertEqual(
                automation.state.scaling_control_status["state"], "pending"
            )
        finally:
            automation._cancel_delayed_scale()
        self.assertIsNone(getattr(automation, "_delayed_scale_timer", None))

    def test_reschedule_cancels_previous_timer(self):
        automation, _, _ = controller()
        try:
            automation.scale_focused_after_delay(5.0)
            first = automation._delayed_scale_timer
            automation.scale_focused_after_delay(5.0)
            self.assertIsNot(first, automation._delayed_scale_timer)
            self.assertFalse(first.is_alive())
        finally:
            automation._cancel_delayed_scale()

    def test_timer_fires_override_flow_for_focused_window(self):
        automation, _, _ = controller()
        automation._scale_foreground = Mock(return_value=True)
        try:
            automation.scale_focused_after_delay(1.0)
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if automation._scale_foreground.called:
                    break
                time.sleep(0.05)
            automation._scale_foreground.assert_called_once_with()
        finally:
            automation._cancel_delayed_scale()

    def test_timer_skips_when_already_scaling(self):
        automation, state, _ = controller()
        automation._scale_foreground = Mock(return_value=True)
        try:
            automation.scale_focused_after_delay(1.0)
            state.is_scaling_active = True
            time.sleep(1.6)
            automation._scale_foreground.assert_not_called()
        finally:
            automation._cancel_delayed_scale()


if __name__ == "__main__":
    unittest.main()
