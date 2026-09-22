import unittest

from companion.services.window_manager import MonitorWindowManager


class MonitorBoundarySnapTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
