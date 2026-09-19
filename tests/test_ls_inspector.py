import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from companion.services.ls_inspector import LosslessScalingInspector


class LosslessScalingInspectorTests(unittest.TestCase):
    def test_existing_log_history_is_not_replayed_as_current_state(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "LosslessScaling.log"
            log_path.write_text("Scaling started for window: Old Game\n", encoding="utf-8")
            inspector = LosslessScalingInspector(custom_log_path=str(log_path))

            self.assertIsNone(inspector.parse_log_updates())
            self.assertIsNone(inspector._log_state)

    def test_new_log_transitions_are_authoritative_over_overlay(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "LosslessScaling.log"
            log_path.write_text("", encoding="utf-8")
            inspector = LosslessScalingInspector(custom_log_path=str(log_path))

            with log_path.open("a", encoding="utf-8") as stream:
                stream.write("Scaling started; PID=42; Proc=game.exe\n")
            with patch.object(
                inspector, "detect_lossless_scaling_overlay_window", return_value=(False, None)
            ):
                self.assertTrue(inspector.probe_scaling_state())

            with log_path.open("a", encoding="utf-8") as stream:
                stream.write("Scaling stopped\n")
            with patch.object(
                inspector, "detect_lossless_scaling_overlay_window", return_value=(True, 123)
            ):
                inspector.inspect_current_scaling_target(fallback_foreground=False)
                target = inspector.inspect_current_scaling_target(fallback_foreground=False)

            self.assertFalse(target.is_active)
            self.assertEqual(target.source, "log_file")
            self.assertTrue(inspector._overlay_stable_present)

    def test_confirmation_requires_a_new_event_when_event_log_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "LosslessScaling.log"
            log_path.write_text("", encoding="utf-8")
            inspector = LosslessScalingInspector(custom_log_path=str(log_path))
            inspector.begin_scaling_confirmation(True)

            with patch.object(
                inspector, "detect_lossless_scaling_overlay_window", return_value=(True, 123)
            ):
                self.assertIsNone(inspector.probe_scaling_state())
                self.assertIsNone(inspector.probe_scaling_state())

                with log_path.open("a", encoding="utf-8") as stream:
                    stream.write("Started Lossless Scaling\n")
                self.assertTrue(inspector.probe_scaling_state())

            inspector.end_scaling_confirmation()

    def test_overlay_is_debounced_fallback_when_no_event_log_exists(self):
        inspector = LosslessScalingInspector(custom_log_path="missing.log")
        with patch.object(
            inspector, "detect_lossless_scaling_overlay_window", return_value=(True, 123)
        ):
            self.assertIsNone(inspector.probe_scaling_state())
            self.assertTrue(inspector.probe_scaling_state())


if __name__ == "__main__":
    unittest.main()
