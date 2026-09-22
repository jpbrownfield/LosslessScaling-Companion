import tempfile
import unittest
import os
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

    def test_auto_detection_finds_current_processlasso_log_name(self):
        with tempfile.TemporaryDirectory() as folder:
            local = Path(folder)
            path = local / "ProcessLasso" / "processlasso.log"
            path.parent.mkdir()
            path.write_text("", encoding="utf-8")
            with (
                patch.object(ProcessLassoLogTailer, "runtime_paths", return_value=iter(())),
                patch.object(ProcessLassoLogTailer, "registry_paths", return_value=iter(())),
                patch.dict(os.environ, {"LOCALAPPDATA": str(local), "APPDATA": "", "PROGRAMDATA": ""}),
            ):
                self.assertEqual(ProcessLassoLogTailer.resolve_path(None), path)

    def test_auto_detection_prefers_newest_live_log(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            current = root / "processlasso.log"
            legacy = root / "prolasso.log"
            current.write_text("current", encoding="utf-8")
            legacy.write_text("legacy", encoding="utf-8")
            os.utime(legacy, (1, 1))
            os.utime(current, (2, 2))
            self.assertEqual(ProcessLassoLogTailer.resolve_path(str(root)), current)

    def test_log_folder_command_line_supports_quoted_paths(self):
        match = _LOG_FOLDER_ARGUMENT_RE.search(
            'ProcessGovernor.exe /LogFolder="C:\\Process Lasso\\Logs"'
        )
        self.assertEqual(match.group(1), r"C:\Process Lasso\Logs")


class ProcessLassoScalingTests(unittest.TestCase):
    @staticmethod
    def make_event(active=True, pid=4312):
        verb = "engaged" if active else "disengaged"
        return parse_performance_mode_line(
            f'"Game.exe","{pid}","Performance Mode {verb}"'
        )

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

    def test_already_auto_scaled_profile_does_not_emit_second_hotkey(self):
        profile = Profile(
            id="game", name="Game", target_process="Game.exe", auto_scale=True
        )
        manager = SimpleNamespace(
            config=SimpleNamespace(
                process_lasso_performance_mode_scaling=True,
                process_lasso_log_path=None,
            ),
            match_target_profile=Mock(return_value=profile),
        )
        state = AppState()
        state.current_active_profile = profile
        state.is_scaling_active = True
        state.scaling_trigger = "focus"
        watcher = Mock()
        watcher.find_visible_window_for_process.return_value = ProcessInfo(
            4312, "Game.exe", r"C:\Games\Game.exe", "Game", 99
        )
        automation = Mock()
        monitor = ProcessLassoScalingMonitor(manager, state, watcher, automation)

        monitor._handle_event(self.make_event())

        automation.activate_profile.assert_not_called()
        automation.set_scaling.assert_not_called()
        watcher.focus_window.assert_not_called()
        self.assertFalse(monitor._pending_events)
        self.assertEqual(state.scaling_trigger, "focus")

    def test_event_before_visible_window_is_retried_instead_of_lost(self):
        profile = Profile(id="game", name="Game", target_process="Game.exe")
        manager = SimpleNamespace(
            config=SimpleNamespace(
                process_lasso_performance_mode_scaling=True,
                process_lasso_log_path=None,
            ),
            match_target_profile=Mock(return_value=profile),
        )
        state = AppState()
        watcher = Mock()
        watcher.find_visible_window_for_process.return_value = None
        watcher.focus_window.return_value = True
        automation = Mock()

        def activate(selected, _path, **_kwargs):
            state.current_active_profile = selected

        def scale(_active, _profile, **kwargs):
            state.is_scaling_active = True
            state.scaling_trigger = kwargs["reason"]
            return True

        automation.activate_profile.side_effect = activate
        automation.set_scaling.side_effect = scale
        monitor = ProcessLassoScalingMonitor(manager, state, watcher, automation)
        event = self.make_event()

        monitor._handle_event(event)
        self.assertIn(event.key, monitor._pending_events)
        automation.set_scaling.assert_not_called()

        watcher.find_visible_window_for_process.return_value = ProcessInfo(
            4312, "Game.exe", r"C:\Games\Game.exe", "Game", 99
        )
        monitor._pending_events[event.key].retry_at = 0
        monitor._retry_pending_events()

        automation.set_scaling.assert_called_once()
        self.assertFalse(monitor._pending_events)
        self.assertEqual(
            state.scaling_trigger, "process_lasso_performance_mode:4312"
        )

    def test_performance_mode_exit_cancels_pending_start(self):
        manager = SimpleNamespace(
            config=SimpleNamespace(
                process_lasso_performance_mode_scaling=True,
                process_lasso_log_path=None,
            )
        )
        state = AppState()
        watcher = Mock()
        watcher.find_visible_window_for_process.return_value = None
        monitor = ProcessLassoScalingMonitor(manager, state, watcher, Mock())
        start = self.make_event()

        monitor._handle_event(start)
        self.assertIn(start.key, monitor._pending_events)
        monitor._handle_event(self.make_event(active=False))

        self.assertFalse(monitor._pending_events)

    def test_unconfirmed_hotkey_is_not_retried_as_an_ambiguous_toggle(self):
        profile = Profile(id="game", name="Game", target_process="Game.exe")
        manager = SimpleNamespace(
            config=SimpleNamespace(
                process_lasso_performance_mode_scaling=True,
                process_lasso_log_path=None,
            ),
            match_target_profile=Mock(return_value=profile),
        )
        state = AppState()
        watcher = Mock()
        watcher.find_visible_window_for_process.return_value = ProcessInfo(
            4312, "Game.exe", r"C:\Games\Game.exe", "Game", 99
        )
        watcher.focus_window.return_value = True
        automation = Mock()
        automation.activate_profile.side_effect = (
            lambda selected, _path, **_kwargs: setattr(
                state, "current_active_profile", selected
            )
        )
        automation.set_scaling.return_value = False
        monitor = ProcessLassoScalingMonitor(manager, state, watcher, automation)

        monitor._handle_event(self.make_event())
        monitor._retry_pending_events()

        automation.set_scaling.assert_called_once()
        self.assertFalse(monitor._pending_events)


if __name__ == "__main__":
    unittest.main()
