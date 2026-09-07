import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from companion.core.models import Profile
from companion.core.state import AppState
from companion.services.process_lasso_monitor import (
    _LOG_FOLDER_ARGUMENT_RE,
    ProcessLassoLogTailer,
    ProcessLassoScalingMonitor,
    parse_performance_mode_line,
)
from companion.services.process_watcher import ProcessInfo


class ProcessLassoParserTests(unittest.TestCase):
    def test_parses_started_csv_event_and_adjacent_pid(self):
        event = parse_performance_mode_line(
            '"2026-09-05 12:00:00","Game.exe","4312","Performance Mode engaged"'
        )
        self.assertIsNotNone(event)
        self.assertTrue(event.active)
        self.assertEqual(event.process_name, "Game.exe")
        self.assertEqual(event.pid, 4312)

    def test_parses_ended_event_with_explicit_pid(self):
        event = parse_performance_mode_line(
            '"Performance Mode disengaged for Game.exe (PID: 4312)"'
        )
        self.assertIsNotNone(event)
        self.assertFalse(event.active)
        self.assertEqual(event.pid, 4312)

    def test_ignores_unrelated_log_entry(self):
        self.assertIsNone(parse_performance_mode_line('"Game.exe","priority changed"'))

    def test_tailer_starts_at_eof_and_reads_only_appended_records(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "prolasso.log"
            path.write_text('"Old.exe","Performance Mode engaged"\n', encoding="utf-8")
            events = []
            tailer = ProcessLassoLogTailer(events.append)
            self.assertEqual(tailer.poll(str(path)), 0)
            with path.open("a", encoding="utf-8") as stream:
                stream.write('"Game.exe","4312","Performance Mode engaged"\n')
            self.assertEqual(tailer.poll(str(path)), 1)
            self.assertEqual([event.process_name for event in events], ["Game.exe"])

    def test_auto_detection_uses_registry_log_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "prolasso.log"
            path.write_text("", encoding="utf-8")
            with (
                patch.object(ProcessLassoLogTailer, "runtime_paths", return_value=iter(())),
                patch.object(
                    ProcessLassoLogTailer,
                    "registry_paths",
                    return_value=iter((path,)),
                ),
            ):
                self.assertEqual(ProcessLassoLogTailer.resolve_path(None), path)

    def test_log_folder_command_line_supports_quoted_paths(self):
        match = _LOG_FOLDER_ARGUMENT_RE.search(
            'ProcessGovernor.exe /LogFolder="C:\\Process Lasso\\Logs"'
        )
        self.assertEqual(match.group(1), r"C:\Process Lasso\Logs")


class ProcessLassoScalingTests(unittest.TestCase):
    def test_scales_only_a_profiled_visible_window(self):
        profile = Profile(id="game", name="Game", target_process="Game.exe")
        config = SimpleNamespace(
            process_lasso_performance_mode_scaling=True,
            process_lasso_log_path=None,
        )
        manager = SimpleNamespace(
            config=config,
            match_target_profile=Mock(return_value=profile),
        )
        state = AppState()
        watcher = Mock()
        window = ProcessInfo(4312, "Game.exe", r"C:\Games\Game.exe", "Game", 99)
        watcher.find_visible_window_for_process.return_value = window
        watcher.focus_window.return_value = True
        automation = Mock()

        def activate(selected, _path, **_kwargs):
            state.current_active_profile = selected

        automation.activate_profile.side_effect = activate
        monitor = ProcessLassoScalingMonitor(manager, state, watcher, automation)
        monitor._handle_event(
            parse_performance_mode_line(
                '"Game.exe","4312","Performance Mode engaged"'
            )
        )

        automation.set_scaling.assert_called_once_with(
            True,
            profile,
            reason="process_lasso_performance_mode:4312",
            target_pid=4312,
            target_hwnd=99,
        )

    def test_performance_mode_exit_does_not_unscale(self):
        config = SimpleNamespace(
            process_lasso_performance_mode_scaling=True,
            process_lasso_log_path=None,
        )
        manager = SimpleNamespace(config=config)
        state = AppState()
        state.is_scaling_active = True
        state.scaling_trigger = "process_lasso_performance_mode:4312"
        automation = Mock()
        monitor = ProcessLassoScalingMonitor(manager, state, Mock(), automation)
        monitor._owned_triggers["game.exe"] = state.scaling_trigger

        monitor._handle_event(
            parse_performance_mode_line(
                '"Game.exe","Performance Mode disengaged"'
            )
        )

        automation.set_scaling.assert_not_called()
        self.assertNotIn("game.exe", monitor._owned_triggers)


if __name__ == "__main__":
    unittest.main()
