import unittest
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
