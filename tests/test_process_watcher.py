import unittest
import tempfile
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from companion.core.models import HotkeyConfig
from companion.core.state import AppState
from companion.services.process_watcher import ProcessInfo, ProcessWatcher


class ProcessWatcherScalingControlTests(unittest.TestCase):
    def make_watcher(self):
        config = SimpleNamespace(
            lossless_control_configured=True,
            disable_native_auto_scale=True,
            hotkey_sync_mode="helper_controls_lossless",
            global_hotkey=HotkeyConfig(modifiers=["ctrl", "alt"], key="s"),
            auto_launch_lossless_scaling=True,
            lossless_scaling_exe_path=r"C:\Lossless Scaling\LosslessScaling.exe",
        )
        manager = SimpleNamespace(config=config, save_config=Mock())
        settings = Mock()
        return ProcessWatcher(manager, AppState(), lossless_settings=settings), settings

    def test_enforcement_restarts_running_lossless_scaling(self):
        watcher, settings = self.make_watcher()
        settings.read_hotkey.return_value = HotkeyConfig(modifiers=["alt"], key="x")
        settings.control_changes_required.return_value = {"hotkey": True, "auto_scale": True}
        settings.update_control_settings.return_value = {"hotkey": True, "auto_scale": True}

        with (
            patch.object(watcher, "check_is_lossless_scaling_running", return_value=True),
            patch.object(watcher, "stop_lossless_scaling", return_value=True) as stop,
            patch.object(watcher, "launch_lossless_scaling", return_value=True) as launch,
        ):
            self.assertTrue(watcher.enforce_helper_scaling_control())

        stop.assert_called_once_with()
        self.assertEqual(watcher.profile_manager.config.global_hotkey.key, "x")
        self.assertEqual(watcher.profile_manager.config.global_hotkey.modifiers, ["alt"])
        self.assertEqual(watcher.profile_manager.config.hotkey_sync_mode, "follow_lossless")
        settings.update_control_settings.assert_called_once_with(disable_auto_scale=True)
        launch.assert_called_once_with(force=True)

    def test_enforcement_leaves_running_process_alone_when_already_disabled(self):
        watcher, settings = self.make_watcher()
        settings.read_hotkey.return_value = watcher.profile_manager.config.global_hotkey
        settings.control_changes_required.return_value = {"hotkey": False, "auto_scale": False}

        with (
            patch.object(watcher, "stop_lossless_scaling") as stop,
            patch.object(watcher, "launch_lossless_scaling") as launch,
        ):
            self.assertTrue(watcher.enforce_helper_scaling_control())

        stop.assert_not_called()
        launch.assert_not_called()

    def test_override_replaces_native_hotkey_with_hidden_f24(self):
        watcher, settings = self.make_watcher()
        watcher.profile_manager.config.override_lossless_hotkey = True
        watcher.profile_manager.config.override_hotkey = HotkeyConfig(
            modifiers=["ctrl", "shift"], key="g"
        )
        settings.control_changes_required.return_value = {"hotkey": True, "auto_scale": False}
        settings.update_control_settings.return_value = {"hotkey": True, "auto_scale": False}

        with (
            patch.object(watcher, "check_is_lossless_scaling_running", return_value=False),
            patch.object(watcher, "stop_lossless_scaling") as stop,
        ):
            self.assertTrue(watcher.enforce_helper_scaling_control())

        hidden = watcher.profile_manager.config.global_hotkey
        self.assertEqual((hidden.modifiers, hidden.key), ([], "f24"))
        settings.read_hotkey.assert_not_called()
        settings.update_control_settings.assert_called_once()
        self.assertEqual(settings.update_control_settings.call_args.kwargs["hotkey"].key, "f24")
        stop.assert_not_called()

    def test_unconfigured_install_does_not_touch_native_settings(self):
        watcher, settings = self.make_watcher()
        watcher.profile_manager.config.lossless_control_configured = False

        self.assertFalse(watcher.enforce_helper_scaling_control())

        settings.ensure_initial_backup.assert_not_called()
        settings.control_changes_required.assert_not_called()
        settings.update_control_settings.assert_not_called()

    def test_lossless_scaling_launch_is_hidden(self):
        watcher, settings = self.make_watcher()
        settings.native_auto_scale_enabled.return_value = False
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "LosslessScaling.exe"
            executable.write_bytes(b"exe")
            watcher.profile_manager.config.lossless_scaling_exe_path = str(executable)
            osd = executable.parent / ".companion-special-k/Global/osd.ini"
            osd.parent.mkdir(parents=True)
            osd.write_text("[SpecialK.OSD]\nShow=false", encoding="utf-8")
            process = SimpleNamespace(pid=4242)
            launch_events = []
            with (
                patch.object(watcher, "check_is_lossless_scaling_running", return_value=False),
                patch("companion.services.process_watcher.launch_hidden", return_value=None),
                patch(
                    "companion.services.process_watcher.subprocess.Popen",
                    side_effect=lambda *args, **kwargs: (launch_events.append("popen"), process)[1],
                ) as popen,
                patch(
                    "companion.services.process_watcher.time.sleep",
                    side_effect=lambda *_args: launch_events.append("sleep"),
                ),
                patch.object(
                    watcher,
                    "_suppress_startup_window",
                    side_effect=lambda _pid: launch_events.append("suppress"),
                ) as suppress,
            ):
                self.assertTrue(watcher.launch_lossless_scaling(force=True))

            kwargs = popen.call_args.kwargs
            self.assertEqual(popen.call_args.args[0], [str(executable)])
            self.assertEqual(kwargs["startupinfo"].wShowWindow, 0)
            self.assertTrue(kwargs["startupinfo"].dwFlags & subprocess.STARTF_USESHOWWINDOW)
            self.assertEqual(kwargs["env"]["SPECIALK_PATH"], str(osd.parent.parent.resolve()))
            suppress.assert_called_once_with(4242)
            self.assertEqual(launch_events, ["popen", "suppress", "sleep"])

    def test_startup_suppression_keeps_watching_after_first_window(self):
        watcher, _settings = self.make_watcher()
        hidden = []

        class ImmediateThread:
            def __init__(self, target, **_kwargs):
                self.target = target

            def start(self):
                self.target()

        with (
            patch.object(watcher, "_hide_process_windows", side_effect=lambda _pid: hidden.append(1)),
            patch("companion.services.process_watcher.threading.Thread", ImmediateThread),
            patch("companion.services.process_watcher.psutil.pid_exists", side_effect=[True, True, False]),
            patch("companion.services.process_watcher.time.sleep"),
        ):
            watcher._suppress_startup_window(4242)

        self.assertEqual(len(hidden), 3)

    def test_native_guard_launch_does_not_also_popen(self):
        watcher, settings = self.make_watcher()
        settings.native_auto_scale_enabled.return_value = False
        with (
            patch.object(watcher, "check_is_lossless_scaling_running", return_value=False),
            patch("companion.services.process_watcher.Path.is_file", return_value=True),
            patch("companion.services.process_watcher.launch_hidden", return_value=4242) as guard,
            patch("companion.services.process_watcher.subprocess.Popen") as popen,
            patch("companion.services.process_watcher.time.sleep"),
            patch.object(watcher, "_suppress_startup_window") as fallback,
        ):
            self.assertTrue(watcher.launch_lossless_scaling(force=True))
        guard.assert_called_once()
        popen.assert_not_called()
        fallback.assert_called_once_with(4242)
        self.assertTrue(watcher.state.lossless_scaling_running)

    def test_lossless_focus_does_not_switch_profile_or_restart_for_default(self):
        profile = SimpleNamespace(id="game")
        manager = SimpleNamespace(config=SimpleNamespace(), match_target_profile=Mock())
        state = AppState()
        state.current_active_profile = profile
        callback = Mock()
        watcher = ProcessWatcher(manager, state, on_profile_changed=callback)
        watcher._last_foreground_pid = 41
        watcher._last_foreground_hwnd = 81
        window = ProcessInfo(99, "LOSSLESSSCALING.EXE", r"C:\LS\LosslessScaling.exe", "LS", 82)
        with patch.object(watcher, "get_foreground_window_info", return_value=window):
            self.assertIs(watcher.check_foreground_and_update(), window)
        manager.match_target_profile.assert_not_called()
        callback.assert_not_called()
        self.assertIs(state.current_active_profile, profile)
        self.assertEqual(watcher._last_foreground_pid, 41)
        self.assertEqual(watcher._last_foreground_hwnd, 81)

    def test_startup_suppression_skips_overlay_and_echo_windows(self):
        watcher, _settings = self.make_watcher()
        visible = {10: True, 20: True, 30: True, 40: True}
        classes = {10: "HwndWrapper[LosslessScaling]", 20: "LSOverlay", 30: "LSAddonManagerClass", 40: "HwndWrapper[OtherProcess]"}
        styles = {10: 0x00C00000, 20: 0x80000000, 30: 0x00C00000, 40: 0x00C00000}
        user32 = Mock()

        def enum_windows(callback, _extra):
            for hwnd in visible:
                callback(hwnd, 0)
            return True

        def set_pid(hwnd, pointer):
            pointer._obj.value = 9999 if hwnd == 40 else 4242
            return 1

        def set_class(hwnd, buffer, _length):
            buffer.value = classes[hwnd]
            return len(classes[hwnd])

        def hide(hwnd, _command):
            visible[hwnd] = False
            return 1

        user32.EnumWindows.side_effect = enum_windows
        user32.GetWindowThreadProcessId.side_effect = set_pid
        user32.IsWindowVisible.side_effect = lambda hwnd: visible[hwnd]
        user32.GetClassNameW.side_effect = set_class
        user32.GetWindowLongW.side_effect = lambda hwnd, _index: styles[hwnd]
        user32.ShowWindowAsync.side_effect = hide
        with patch("companion.services.process_watcher.user32", user32):
            self.assertEqual(watcher._hide_process_windows(4242), 1)

        user32.ShowWindowAsync.assert_called_once_with(10, 0)
        self.assertTrue(visible[20])
        self.assertTrue(visible[30])

    def test_running_check_ignores_a_different_simulation_executable(self):
        watcher, _settings = self.make_watcher()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configured = root / "real" / "LosslessScaling.exe"
            configured.parent.mkdir()
            configured.write_bytes(b"real")
            simulation = root / "simulation" / "LosslessScaling.exe"
            simulation.parent.mkdir()
            simulation.write_bytes(b"simulation")
            watcher.profile_manager.config.lossless_scaling_exe_path = str(configured)
            process = SimpleNamespace(info={"name": "LosslessScaling.exe", "exe": str(simulation)})

            with patch("companion.services.process_watcher.psutil.process_iter", return_value=[process]):
                self.assertFalse(watcher.check_is_lossless_scaling_running())

    def test_running_check_matches_the_configured_executable(self):
        watcher, _settings = self.make_watcher()
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "LosslessScaling.exe"
            executable.write_bytes(b"real")
            watcher.profile_manager.config.lossless_scaling_exe_path = str(executable)
            process = SimpleNamespace(info={"name": "LosslessScaling.exe", "exe": str(executable)})

            with patch("companion.services.process_watcher.psutil.process_iter", return_value=[process]):
                self.assertTrue(watcher.check_is_lossless_scaling_running())

    def test_new_window_for_active_profile_refreshes_runtime_route(self):
        profile = SimpleNamespace(id="game")
        manager = SimpleNamespace(
            config=SimpleNamespace(),
            match_target_profile=Mock(return_value=profile),
        )
        state = AppState()
        state.current_active_profile = profile
        callback = Mock()
        watcher = ProcessWatcher(manager, state, on_profile_changed=callback)
        watcher._last_foreground_pid = 41
        watcher._last_foreground_hwnd = 81
        window = ProcessInfo(42, "game.exe", r"C:\Game\game.exe", "Game", 82)

        with patch.object(watcher, "get_foreground_window_info", return_value=window):
            watcher.check_foreground_and_update()

        callback.assert_called_once_with(
            profile,
            r"C:\Game\game.exe",
            target_pid=42,
            target_hwnd=82,
        )

    def test_manual_scaling_target_survives_lossless_scaling_taking_focus(self):
        watcher, _settings = self.make_watcher()
        watcher.profile_manager.match_target_profile = Mock(return_value=None)
        game = ProcessInfo(42, "Game.exe", r"C:\Games\Game.exe", "Game", 81)
        lossless = ProcessInfo(99, "LosslessScaling.exe", "", "Lossless", 82)
        user32 = Mock()
        user32.IsWindow.return_value = 1
        user32.IsWindowVisible.return_value = 1
        user32.GetWindowThreadProcessId.side_effect = lambda _hwnd, pointer: setattr(pointer._obj, "value", 42)

        with (
            patch.object(watcher, "get_foreground_window_info", side_effect=[game, lossless]),
            patch.object(watcher, "foreground_window_identity", return_value=(99, 82)),
            patch("companion.services.process_watcher.user32", user32),
            patch("companion.services.process_watcher.psutil.Process", side_effect=lambda pid: SimpleNamespace(name=lambda: {42: "Game.exe", 99: "LosslessScaling.exe"}.get(pid, "Other.exe"))),
        ):
            watcher.check_foreground_and_update()
            watcher.check_foreground_and_update()
            self.assertIs(watcher.recent_unscaled_foreground_target("Game.exe"), game)
            self.assertIsNone(watcher.recent_unscaled_foreground_target("Other.exe"))

            with patch.object(watcher, "foreground_window_identity", return_value=(77, 83)):
                self.assertIsNone(watcher.recent_unscaled_foreground_target("Game.exe"))

            watcher._recent_unscaled_foreground = (0.0, game)
            self.assertIsNone(watcher.recent_unscaled_foreground_target("Game.exe"))


if __name__ == "__main__":
    unittest.main()
