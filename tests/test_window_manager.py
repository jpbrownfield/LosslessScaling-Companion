import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

from companion.services import window_manager as windows_api
from companion.services.window_manager import MonitorWindowManager


class MonitorBoundarySnapTests(unittest.TestCase):
    def test_translates_window_to_negative_coordinate_monitor_without_resizing(self):
        position = MonitorWindowManager._translated_window_position(
            (200, 120, 1200, 820),
            (0, 0, 1920, 1040),
            (-2560, -200, 0, 1240),
        )

        self.assertEqual(position, (-2360, -80))

    def test_translation_keeps_oversized_window_origin_reachable(self):
        position = MonitorWindowManager._translated_window_position(
            (1500, 900, 3500, 2100),
            (0, 0, 1920, 1040),
            (1920, 0, 3200, 1024),
        )

        self.assertEqual(position, (1920, 0))

    def test_accepts_single_digit_edge_errors(self):
        correction = MonitorWindowManager._near_monitor_adjustment(
            (-2, 1, 1923, 1079),
            (0, 0, 1920, 1080),
            8,
        )

        self.assertEqual(correction, (2, -1, -3, 1))

    def test_refuses_window_that_is_meaningfully_smaller_than_monitor(self):
        correction = MonitorWindowManager._near_monitor_adjustment(
            (100, 100, 1820, 980),
            (0, 0, 1920, 1080),
            8,
        )

        self.assertIsNone(correction)

    def test_handles_negative_coordinate_monitor_layout(self):
        correction = MonitorWindowManager._near_monitor_adjustment(
            (-1921, -1, 1, 1081),
            (-1920, 0, 0, 1080),
            8,
        )

        self.assertEqual(correction, (1, 1, -1, -1))


class MonitorTargetIdentityTests(unittest.TestCase):
    def test_minimizer_preserves_other_windows_with_target_process_name(self):
        manager = MonitorWindowManager()
        minimized = set()
        def process(pid):
            return SimpleNamespace(name=lambda: "Game.exe" if pid in (42, 43) else "Browser.exe")

        def enumerate_windows(callback, _extra):
            for hwnd in (101, 102, 103):
                callback(hwnd, 0)

        with (
            patch.object(MonitorWindowManager, "_target_window", return_value=101),
            patch.object(MonitorWindowManager, "_window_pid", side_effect=lambda hwnd: {101: 42, 102: 43, 103: 44}[hwnd]),
            patch.object(MonitorWindowManager, "_class_name", return_value="game_window"),
            patch.object(MonitorWindowManager, "_is_cloaked", return_value=False),
            patch("companion.services.window_manager.psutil.Process", side_effect=process),
            patch("companion.services.window_manager.user32.MonitorFromWindow", return_value=1),
            patch("companion.services.window_manager.user32.IsWindowVisible", return_value=1),
            patch("companion.services.window_manager.user32.IsIconic", side_effect=lambda hwnd: hwnd in minimized),
            patch("companion.services.window_manager.user32.GetWindow", return_value=0),
            patch("companion.services.window_manager.user32.GetForegroundWindow", return_value=101),
            patch("companion.services.window_manager.user32.GetWindowLongW", return_value=0),
            patch("companion.services.window_manager.user32.GetWindowTextLengthW", return_value=4),
            patch("companion.services.window_manager.user32.EnumWindows", side_effect=enumerate_windows),
            patch("companion.services.window_manager.user32.ShowWindowAsync", side_effect=lambda hwnd, _mode: (minimized.add(hwnd), True)[1]),
        ):
            count = manager.minimize_others_on_target_monitor(101, target_pid=42)

        self.assertEqual(count, 1)
        self.assertEqual(minimized, {103})

    def test_verified_preferred_window_is_kept(self):
        with (
            patch("companion.services.window_manager.user32.IsWindow", return_value=1),
            patch("companion.services.window_manager.user32.IsWindowVisible", return_value=1),
            patch.object(MonitorWindowManager, "_window_pid", return_value=42),
            patch.object(MonitorWindowManager, "_replacement_window_for_pid") as replacement,
        ):
            target = MonitorWindowManager._target_window(81, 42)

        self.assertEqual(target, 81)
        replacement.assert_not_called()

    def test_stale_window_is_recovered_only_from_expected_pid(self):
        with (
            patch("companion.services.window_manager.user32.IsWindow", return_value=0),
            patch.object(
                MonitorWindowManager, "_replacement_window_for_pid", return_value=82
            ) as replacement,
        ):
            target = MonitorWindowManager._target_window(81, 42)

        self.assertEqual(target, 82)
        replacement.assert_called_once_with(42)

    def test_wrong_process_window_is_rejected_and_recovered_by_expected_pid(self):
        with (
            patch("companion.services.window_manager.user32.IsWindow", return_value=1),
            patch("companion.services.window_manager.user32.IsWindowVisible", return_value=1),
            patch.object(MonitorWindowManager, "_window_pid", return_value=99),
            patch.object(
                MonitorWindowManager, "_replacement_window_for_pid", return_value=82
            ),
        ):
            target = MonitorWindowManager._target_window(81, 42)

        self.assertEqual(target, 82)

    def test_missing_identity_never_falls_back_to_foreground_window(self):
        with (
            patch("companion.services.window_manager.user32.IsWindow", return_value=0),
            patch("companion.services.window_manager.user32.GetForegroundWindow") as foreground,
        ):
            target = MonitorWindowManager._target_window(None, None)

        self.assertIsNone(target)
        foreground.assert_not_called()


class MonitorMinimizationSafetyTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        # Include handles beyond 32 bits, as Windows handles are pointer-sized.
        self.target = 0x100000001
        self.peer = 0x100000002
        self.other_monitor = 0x100000003
        self.rows = {
            self.target: dict(pid=42, name="Game.exe", monitor=0x200000001),
            self.peer: dict(pid=43, name="Browser.exe", monitor=0x200000001),
            self.other_monitor: dict(pid=44, name="Chat.exe", monitor=0x300000001),
        }
        self.manager = MonitorWindowManager()
        self.mock_method("_target_window", return_value=self.target)
        self.mock_method("_window_pid", side_effect=lambda hwnd: self.rows[hwnd]["pid"])
        self.mock_method("_class_name", side_effect=lambda hwnd: self.rows[hwnd].get("class_name", "App"))
        self.mock_method("_is_cloaked", side_effect=lambda hwnd: self.rows[hwnd].get("cloaked", False))
        self.stack.enter_context(patch.object(windows_api.psutil, "Process", side_effect=lambda pid:
            SimpleNamespace(name=lambda: next(row["name"] for row in self.rows.values() if row["pid"] == pid))))
        self.mock_api("IsWindowVisible", side_effect=lambda hwnd: self.rows[hwnd].get("visible", True))
        self.mock_api("IsIconic", side_effect=lambda hwnd: self.rows[hwnd].get("iconic", False))
        self.mock_api("MonitorFromWindow", side_effect=lambda hwnd, _flags: self.rows[hwnd]["monitor"])
        self.mock_api("GetWindow", side_effect=lambda hwnd, _mode: self.rows[hwnd].get("owner", 0))
        self.mock_api("GetForegroundWindow", return_value=self.target)
        self.mock_api("GetWindowLongW", side_effect=lambda hwnd, _index: self.rows[hwnd].get("style", 0))
        self.mock_api("GetWindowTextLengthW", return_value=8)
        self.mock_api("EnumWindows", side_effect=self.enumerate_windows)
        self.show_async = self.mock_api("ShowWindowAsync", return_value=1)
        self.show_sync = self.mock_api("ShowWindow", side_effect=AssertionError("Must not wait on peer UI threads"))

    def mock_method(self, name, **kwargs):
        return self.stack.enter_context(patch.object(MonitorWindowManager, name, **kwargs))

    def mock_api(self, name, **kwargs):
        return self.stack.enter_context(patch.object(windows_api.user32, name, **kwargs))

    def enumerate_windows(self, callback, _extra):
        for hwnd in self.rows:
            if not callback(hwnd, 0):
                break
        return True

    def test_queues_without_activating_and_only_on_target_monitor(self):
        self.assertEqual(self.manager.minimize_others_on_target_monitor(self.target, target_pid=42), 1)
        self.show_async.assert_called_once_with(self.peer, windows_api.SW_SHOWMINNOACTIVE)
        self.show_sync.assert_not_called()
        self.assertFalse(self.rows[self.peer].get("iconic", False))  # Async completion need not be immediate.

    def test_skips_hidden_minimized_owned_overlay_shell_and_game_windows(self):
        for attributes in (
            dict(visible=False), dict(iconic=True), dict(owner=self.target), dict(cloaked=True),
            dict(style=windows_api.WS_EX_TOOLWINDOW), dict(style=windows_api.WS_EX_NOACTIVATE),
            dict(class_name="workerw"), dict(pid=42, name="Game.exe"),
            dict(name="Game.exe"), dict(name="LosslessScaling.exe"),
        ):
            with self.subTest(attributes=attributes):
                original = dict(self.rows[self.peer])
                self.rows[self.peer].update(attributes)
                self.assertEqual(self.manager.minimize_others_on_target_monitor(self.target, target_pid=42), 0)
                self.rows[self.peer] = original
        self.show_async.assert_not_called()

    def test_minimized_or_cloaked_game_does_not_minimize_other_apps(self):
        for attribute in ("iconic", "cloaked"):
            self.rows[self.target][attribute] = True
            self.assertEqual(self.manager.minimize_others_on_target_monitor(self.target, target_pid=42), 0)
            self.rows[self.target][attribute] = False
        self.show_async.assert_not_called()

    def test_failed_async_request_is_not_counted(self):
        self.show_async.return_value = 0
        self.assertEqual(self.manager.minimize_others_on_target_monitor(self.target, target_pid=42), 0)

    def test_peer_the_user_switched_to_is_not_minimized(self):
        self.mock_api("GetForegroundWindow", return_value=self.peer)
        self.assertEqual(self.manager.minimize_others_on_target_monitor(self.target, target_pid=42), 0)
        self.show_async.assert_not_called()

    def test_game_becoming_cloaked_during_enumeration_stops_minimizing(self):
        def class_name(hwnd):
            if hwnd == self.peer:
                self.rows[self.target]["cloaked"] = True
            return "App"
        self.mock_method("_class_name", side_effect=class_name)
        self.assertEqual(self.manager.minimize_others_on_target_monitor(self.target, target_pid=42), 0)
        self.show_async.assert_not_called()

    def test_recycled_peer_handle_is_not_minimized(self):
        calls = 0
        def pid(hwnd):
            nonlocal calls
            if hwnd == self.peer:
                calls += 1
                return 43 if calls == 1 else 42
            return self.rows[hwnd]["pid"]
        self.mock_method("_window_pid", side_effect=pid)
        self.assertEqual(self.manager.minimize_others_on_target_monitor(self.target, target_pid=42), 0)
        self.show_async.assert_not_called()

    def test_target_monitor_change_during_enumeration_stops_minimizing(self):
        def process(pid):
            if pid == 43:
                self.rows[self.target]["monitor"] = 0x300000001
            return SimpleNamespace(name=lambda: "Game.exe" if pid == 42 else "Browser.exe")
        self.stack.enter_context(patch.object(windows_api.psutil, "Process", side_effect=process))
        self.assertEqual(self.manager.minimize_others_on_target_monitor(self.target, target_pid=42), 0)
        self.show_async.assert_not_called()

    def test_unavailable_peer_does_not_abort_other_windows(self):
        self.rows[self.other_monitor]["monitor"] = self.rows[self.target]["monitor"]
        self.mock_method("_class_name", side_effect=lambda hwnd: (_ for _ in ()).throw(OSError("gone"))
                         if hwnd == self.peer else "App")
        self.assertEqual(self.manager.minimize_others_on_target_monitor(self.target, target_pid=42), 1)
        self.show_async.assert_called_once_with(self.other_monitor, windows_api.SW_SHOWMINNOACTIVE)


if __name__ == "__main__":
    unittest.main()
