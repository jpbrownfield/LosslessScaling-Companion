import unittest
from unittest.mock import Mock, patch

from companion.services.startup_manager import StartupTaskManager, StartupTaskError


class StartupTaskManagerTests(unittest.TestCase):
    @patch("companion.services.startup_manager.subprocess.run")
    def test_enable_creates_fixed_highest_onlogon_task(self, run):
        run.return_value = Mock(returncode=0, stdout="", stderr="")

        result = StartupTaskManager().set_enabled(True)

        command = run.call_args.args[0]
        self.assertEqual(command[0:4], ["schtasks.exe", "/Create", "/TN", "LosslessScalingHelper"])
        self.assertIn("ONLOGON", command)
        self.assertIn("HIGHEST", command)
        self.assertNotIn("cmd.exe", command)
        self.assertTrue(result["enabled"])

    @patch("companion.services.startup_manager.subprocess.run")
    def test_disable_removes_only_the_fixed_task(self, run):
        run.side_effect = [
            Mock(returncode=0, stdout="exists", stderr=""),
            Mock(returncode=0, stdout="deleted", stderr=""),
        ]

        result = StartupTaskManager().set_enabled(False)

        self.assertEqual(
            run.call_args_list[-1].args[0],
            ["schtasks.exe", "/Delete", "/TN", "LosslessScalingHelper", "/F"],
        )
        self.assertFalse(result["enabled"])

    @patch("companion.services.startup_manager.subprocess.run")
    def test_scheduler_failure_is_reported(self, run):
        run.return_value = Mock(returncode=1, stdout="", stderr="access denied")

        with self.assertRaisesRegex(StartupTaskError, "access denied"):
            StartupTaskManager().set_enabled(True)


if __name__ == "__main__":
    unittest.main()
