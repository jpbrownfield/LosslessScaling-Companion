import ctypes
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from companion.core.state import AppState
from companion.services.cursor_recovery import CursorRecovery, refresh_game_cursor


def state():
    value = AppState()
    value.is_scaling_active = True
    value.scaling_target_pid, value.scaling_target_hwnd = 42, 82
    return value


def api():
    user = Mock()
    user.IsWindow.return_value = user.IsWindowVisible.return_value = True
    user.IsIconic.return_value = user.IsChild.return_value = False
    user.GetForegroundWindow.return_value = user.ChildWindowFromPointEx.return_value = 82
    user.GetAncestor.return_value = 82

    def position(pointer):
        pointer._obj.x = pointer._obj.y = 50
        return True

    def bounds(_hwnd, pointer):
        pointer._obj.left = pointer._obj.top = 0
        pointer._obj.right = pointer._obj.bottom = 100
        return True

    def send(_hwnd, _msg, _wp, _lp, _flags, _timeout, pointer):
        pointer._obj.value = 1
        return 1

    user.GetCursorPos.side_effect = position
    user.GetClientRect.side_effect = bounds
    user.ScreenToClient.return_value = True
    user.SendMessageTimeoutW.side_effect = send
    return SimpleNamespace(user=user, pid=Mock(return_value=42),
                           snapshot=Mock(side_effect=[{"showing": False, "handle": 0},
                                                     {"showing": True, "handle": 123456}]))


class CursorRefreshTests(unittest.TestCase):
    def test_requests_game_owned_shape_without_global_cursor_or_input_changes(self):
        native = api()
        evidence = refresh_game_cursor(42, 82, api=native)
        self.assertEqual(evidence["outcome"], "refreshed")
        self.assertEqual(evidence["after"]["handle"], 123456)
        args = native.user.SendMessageTimeoutW.call_args.args
        self.assertEqual(args[:6], (82, 0x20, 82, 1 | (0x200 << 16), 0x23, 30))
        for name in ("SetCursor", "ShowCursor", "SetSystemCursor", "SystemParametersInfoW",
                     "SetCursorPos", "SendInput", "SetForegroundWindow", "ClipCursor"):
            getattr(native.user, name).assert_not_called()

    def test_closed_hidden_minimized_or_recycled_target_is_rejected(self):
        for condition in ("closed", "hidden", "minimized", "recycled"):
            with self.subTest(condition=condition):
                native = api()
                if condition == "closed": native.user.IsWindow.return_value = False
                if condition == "hidden": native.user.IsWindowVisible.return_value = False
                if condition == "minimized": native.user.IsIconic.return_value = True
                if condition == "recycled": native.pid.return_value = 99
                self.assertEqual(refresh_game_cursor(42, 82, api=native)["outcome"], "target_unavailable")
                native.user.SendMessageTimeoutW.assert_not_called()

    def test_another_foreground_app_and_same_process_unrelated_window_are_rejected(self):
        for foreign_process in (True, False):
            native = api()
            native.user.GetForegroundWindow.return_value = 99
            native.user.GetAncestor.return_value = 99
            native.pid.side_effect = lambda hwnd: 99 if foreign_process and hwnd == 99 else 42
            self.assertEqual(refresh_game_cursor(42, 82, api=native)["outcome"], "focus_changed")
            native.user.SendMessageTimeoutW.assert_not_called()

    def test_child_window_can_own_its_own_cursor(self):
        native = api()
        native.user.ChildWindowFromPointEx.return_value = 83
        self.assertEqual(refresh_game_cursor(42, 82, api=native)["recipient"], 83)
        self.assertEqual(native.user.SendMessageTimeoutW.call_args.args[:3], (83, 0x20, 83))

    def test_foreign_child_and_focus_race_do_not_receive_a_message(self):
        native = api()
        native.user.ChildWindowFromPointEx.return_value = 83
        native.pid.side_effect = lambda hwnd: 999 if hwnd == 83 else 42
        self.assertEqual(refresh_game_cursor(42, 82, api=native)["outcome"], "target_changed")
        native.user.SendMessageTimeoutW.assert_not_called()
        native = api()
        native.user.GetForegroundWindow.side_effect = [82, 99]
        self.assertEqual(refresh_game_cursor(42, 82, api=native)["outcome"], "focus_changed")
        native.user.SendMessageTimeoutW.assert_not_called()

    def test_pointer_on_another_window_or_monitor_is_left_alone(self):
        native = api()
        native.user.ScreenToClient.side_effect = lambda _hwnd, p: setattr(p._obj, "x", -1) or True
        self.assertEqual(refresh_game_cursor(42, 82, api=native)["outcome"], "pointer_outside_game")
        native.user.SendMessageTimeoutW.assert_not_called()

    def test_inaccessible_desktop_and_hung_game_are_bounded_and_explained(self):
        native = api()
        native.user.GetCursorPos.side_effect = None
        native.user.GetCursorPos.return_value = False
        with patch.object(ctypes, "get_last_error", return_value=5):
            self.assertEqual(refresh_game_cursor(42, 82, api=native), {"outcome": "desktop_unavailable", "error": 5})
        native.user.SendMessageTimeoutW.assert_not_called()
        native = api()
        native.user.SendMessageTimeoutW.side_effect = None
        native.user.SendMessageTimeoutW.return_value = 0
        with patch.object(ctypes, "get_last_error", return_value=1460):
            result = refresh_game_cursor(42, 82, api=native)
        self.assertEqual((result["outcome"], result["error"]), ("message_failed", 1460))


class CursorRecoveryTests(unittest.TestCase):
    def environment(self):
        clock = Mock(return_value=0)
        refresh = Mock(return_value={"outcome": "refreshed"})
        return CursorRecovery(refresh, clock), state(), refresh, clock

    def test_only_focus_return_triggers_three_bounded_attempts(self):
        recovery, value, refresh, clock = self.environment()
        recovery.update(value, 42, 82)
        recovery.update(value, 42, 82)
        refresh.assert_not_called()
        recovery.update(value, 99, 100)
        recovery.update(value, 42, 82)
        refresh.assert_called_once_with(42, 82)
        for now, count in ((.1, 1), (.2, 2), (.3, 2), (.6, 3), (10, 3)):
            clock.return_value = now
            recovery.update(value, 42, 82)
            self.assertEqual(refresh.call_count, count)

    def test_leaving_again_cancels_delayed_repairs(self):
        recovery, value, refresh, clock = self.environment()
        recovery.update(value, 42, 82)
        recovery.update(value, 99, 100)
        recovery.update(value, 42, 82)
        clock.return_value = .3
        recovery.update(value, 99, 100)
        clock.return_value = 2
        recovery.update(value, 99, 100)
        refresh.assert_called_once()

    def test_stop_resize_new_session_and_benchmark_cancel_old_requests(self):
        for change in ("stop", "target", "session", "benchmark", "update"):
            with self.subTest(change=change):
                recovery, value, refresh, clock = self.environment()
                recovery.update(value, 42, 82)
                recovery.update(value, 99, 100)
                recovery.update(value, 42, 82)
                if change == "stop": value.is_scaling_active = False
                if change == "target": value.scaling_target_hwnd = 83
                if change == "session": value.last_scale_toggle_time += 1
                if change == "benchmark": value.benchmark_mode_active = True
                if change == "update": value.update_in_progress = True
                clock.return_value = 2
                recovery.update(value, 42, 82)
                refresh.assert_called_once()

    def test_unknown_target_does_not_guess_game(self):
        recovery, value, refresh, clock = self.environment()
        value.scaling_target_pid = value.scaling_target_hwnd = None
        for pid, hwnd in ((42, 82), (99, 100), (42, 82)):
            recovery.update(value, pid, hwnd)
        refresh.assert_not_called()

    def test_inspector_target_is_supported_without_requiring_overlay_or_reshade(self):
        recovery, value, refresh, clock = self.environment()
        value.scaling_target_pid = value.scaling_target_hwnd = None
        value.current_scaled_target = {"pid": 42, "hwnd": 82}
        recovery.update(value, 42, 82)
        recovery.update(value, 99, 100)
        recovery.update(value, 42, 82)
        refresh.assert_called_once_with(42, 82)

    def test_lossless_overlay_focus_also_allows_a_refresh_when_game_returns(self):
        recovery, value, refresh, clock = self.environment()
        recovery.update(value, 42, 82)
        recovery.update(value, 123, 456)
        recovery.update(value, 42, 82)
        refresh.assert_called_once_with(42, 82)

    def test_process_monitor_observes_ls_and_missing_focus_before_profile_shortcuts(self):
        from companion.services.process_watcher import ProcessInfo, ProcessWatcher
        watcher = ProcessWatcher(SimpleNamespace(config=SimpleNamespace()), state())
        watcher._cursor_recovery = Mock()
        lossless = ProcessInfo(123, "LosslessScaling.exe", "LS.exe", "LS", 456)
        with patch.object(watcher, "get_foreground_window_info", side_effect=[lossless, None]):
            self.assertIs(watcher.check_foreground_and_update(), lossless)
            self.assertIsNone(watcher.check_foreground_and_update())
        self.assertEqual([call.args[1:] for call in watcher._cursor_recovery.update.call_args_list],
                         [(123, 456), (0, 0)])


if __name__ == "__main__":
    unittest.main()
