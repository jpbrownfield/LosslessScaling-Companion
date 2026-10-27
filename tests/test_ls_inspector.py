import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from companion.services.ls_inspector import LosslessScalingInspector


class LosslessScalingInspectorTests(unittest.TestCase):
    def echo_inspector(self, history=""):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        log = root / "logs/LSAddonManager.log"
        log.parent.mkdir()
        log.write_text(history, encoding="utf-8")
        with patch.object(LosslessScalingInspector, "_candidate_log_files", return_value=[]):
            inspector = LosslessScalingInspector(lossless_exe_path=str(root / "LosslessScaling.exe"))
        return inspector, log

    @staticmethod
    def echo_line(status=2, *, error=0, stamp=None):
        stamp = stamp or datetime.now()
        return f"[{stamp.strftime('%H:%M:%S.%f')[:-3]}] [INFO] [Core] Scaling status {status} (window 0000000000211812, 1280x720 -> 3840x2160, resized 0, scale 3.00, error {error})\n"

    def test_echo_fresh_events_confirm_actual_target_then_stop(self):
        inspector, log = self.echo_inspector()
        identity = dict(pid=42, process_name="game.exe", exe_path="C:/game.exe", window_title="Game")
        with patch.object(inspector, "_candidate_log_files", return_value=[]), patch.object(
            inspector, "detect_lossless_scaling_overlay_window", return_value=(False, None)
        ), patch.object(inspector, "_echo_target_identity", return_value=identity):
            inspector.begin_scaling_confirmation(True)
            with log.open("a", encoding="utf-8") as stream:
                stream.write(self.echo_line())
            observation = inspector.probe_scaling_observation()
            self.assertTrue(observation["isActive"])
            self.assertEqual(observation["pid"], 42)
            self.assertEqual(observation["exePath"], "C:/game.exe")
            self.assertEqual(observation["detectionSource"], "log_file")
            inspector.end_scaling_confirmation()
            inspector.begin_scaling_confirmation(False)
            self.assertIsNone(inspector.probe_scaling_state())
            with log.open("a", encoding="utf-8") as stream:
                stream.write(self.echo_line(0))
            self.assertFalse(inspector.probe_scaling_state())

    def test_echo_history_and_replayed_stale_events_are_ignored(self):
        inspector, log = self.echo_inspector(self.echo_line())
        self.assertIsNone(inspector._log_state)
        log.write_text(self.echo_line(stamp=datetime.now() - timedelta(minutes=10)), encoding="utf-8")
        inspector._parse_echo_updates()
        self.assertIsNone(inspector._log_state)

    def test_echo_countdown_pause_and_addon_chatter_do_not_confirm(self):
        inspector, log = self.echo_inspector()
        with log.open("a", encoding="utf-8") as stream:
            stream.write(self.echo_line(3) + self.echo_line(1) + "[INFO] [Addon] Scaling started; PID=42\nNrEngine ready\n")
        self.assertFalse(inspector._parse_echo_updates())
        self.assertIsNone(inspector._log_state)

    def test_echo_same_size_rewrite_and_partial_event_are_handled(self):
        inspector, log = self.echo_inspector()
        with patch.object(inspector, "_echo_target_identity", return_value=dict(pid=42, process_name="game.exe")):
            line = self.echo_line()
            log.write_text(line[:-1], encoding="utf-8")
            self.assertFalse(inspector._parse_echo_updates())
            with log.open("a", encoding="utf-8") as stream:
                stream.write("\n")
            self.assertTrue(inspector._parse_echo_updates())
            self.assertTrue(inspector._log_state)
            log.write_text(self.echo_line(0), encoding="utf-8")
            self.assertTrue(inspector._parse_echo_updates())
            self.assertFalse(inspector._log_state)

    def test_echo_errors_or_invalid_target_cannot_confirm_activation(self):
        inspector, log = self.echo_inspector()
        with patch.object(inspector, "_echo_target_identity", return_value=None):
            log.write_text(self.echo_line(), encoding="utf-8")
            self.assertFalse(inspector._parse_echo_updates())
        with log.open("a", encoding="utf-8") as stream:
            stream.write(self.echo_line(error=3))
        inspector._parse_echo_updates()
        self.assertIs(inspector._log_state, False)

    def test_echo_temporary_pause_does_not_release_active_session(self):
        inspector, log = self.echo_inspector()
        with patch.object(inspector, "_echo_target_identity", return_value=dict(pid=42, process_name="game.exe")):
            log.write_text(self.echo_line(), encoding="utf-8")
            inspector._parse_echo_updates()
            with log.open("a", encoding="utf-8") as stream:
                stream.write(self.echo_line(1))
            self.assertFalse(inspector._parse_echo_updates())
            self.assertTrue(inspector._log_state)

    def test_echo_log_does_not_block_overlay_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "LSAddonManager.log"
            log.write_text("Addon Manager starting\nApplySettings: scalingMode=0\n", encoding="utf-8")
            with patch("companion.services.ls_inspector.glob.glob", return_value=[str(log)]):
                inspector = LosslessScalingInspector()
                self.assertEqual(inspector._candidate_log_files(), [])
                inspector.begin_scaling_confirmation(True)
                with patch.object(inspector, "detect_lossless_scaling_overlay_window", return_value=(True, 123)):
                    self.assertIsNone(inspector.probe_scaling_state())
                    self.assertTrue(inspector.probe_scaling_state())

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

    def test_scaling_transition_is_written_to_companion_log(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "LosslessScaling.log"
            log_path.write_text("", encoding="utf-8")
            inspector = LosslessScalingInspector(custom_log_path=str(log_path))

            with log_path.open("a", encoding="utf-8") as stream:
                stream.write("Scaling session: active; PID=42; Proc=game.exe\n")

            with self.assertLogs("LSCompanion.LSInspector", level="INFO") as logs:
                inspector.parse_log_updates()

            self.assertTrue(any("state=active" in message for message in logs.output))
            self.assertTrue(any("LosslessScaling.log" in message for message in logs.output))

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
