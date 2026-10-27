import tempfile
import threading
import unittest
from ctypes import wintypes
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import Mock, patch

from pystray import Menu

from companion.core.profile_manager import ProfileManager
from companion.core.state import AppState
from companion.main import CompanionApplication
from companion.ui.tray import CompanionTrayIcon


class CompanionTrayIconTests(unittest.TestCase):
    def test_menu_refresh_is_marshaled_to_the_native_tray_thread(self):
        from pystray._util import win32
        tray = CompanionTrayIcon.__new__(CompanionTrayIcon)
        tray.icon = Mock(_hwnd=101, _message_handlers={win32.WM_NOTIFY: Mock()})
        tray._create_icon_image = Mock(return_value="image")
        tray._tray_title = Mock(return_value="title")
        tray._build_menu = Mock(return_value=Menu())
        tray._install_topmost_menu_hook(tray.icon)
        with patch("companion.ui.tray.ctypes.windll.user32.PostMessageW", return_value=1) as post:
            tray.update_menu()
            tray.update_menu()  # Coalesce monitor ticks until the UI consumes it.
            post.assert_called_once_with(101, tray.WM_REFRESH_MENU, 0, 0)
            tray.icon.update_menu.assert_not_called()
            tray.icon._message_handlers[tray.WM_REFRESH_MENU](0, 0)
        tray.icon.update_menu.assert_called_once()
        self.assertFalse(tray._pending_menu_refresh)

    def test_animation_does_not_replace_native_icon_from_worker_thread(self):
        from pystray._util import win32
        tray = CompanionTrayIcon.__new__(CompanionTrayIcon)
        tray.icon = Mock(_hwnd=101, _message_handlers={win32.WM_NOTIFY: Mock()}, icon="old")
        tray._visual_state = Mock(return_value="closed")
        tray._create_icon_image = Mock(return_value="frame")
        tray._animation_stop = Mock()
        tray._animation_stop.is_set.side_effect = [False, True]
        tray._install_topmost_menu_hook(tray.icon)
        with patch("companion.ui.tray.ctypes.windll.user32.PostMessageW", return_value=1) as post:
            tray._animate_icon()
            self.assertEqual(tray.icon.icon, "old")
            post.assert_called_once_with(101, tray.WM_ICON_FRAME, 0, 0)
            tray.icon._message_handlers[tray.WM_ICON_FRAME](0, 0)
        self.assertEqual(tray.icon.icon, "frame")

    def test_visual_state_follows_lossless_scaling_runtime(self):
        tray = CompanionTrayIcon.__new__(CompanionTrayIcon)
        tray.state = AppState()

        self.assertEqual(tray._visual_state(), "closed")
        tray.state.lossless_scaling_running = True
        self.assertEqual(tray._visual_state(), "open")
        tray.state.is_scaling_active = True
        self.assertEqual(tray._visual_state(), "scaling")

    def test_runtime_status_is_shown_directly_below_title(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = ProfileManager(Path(directory) / "config")
            executable = Path(directory) / "LosslessScaling.exe"
            executable.write_bytes(b"exe")
            manager.config.lossless_scaling_exe_path = str(executable)
            watcher = Mock()
            watcher.list_running_executables.return_value = []
            state = AppState()
            tray = CompanionTrayIcon(manager, state, watcher, automation=Mock())

            menu = tray._build_menu()
            self.assertEqual(menu.items[0].text, "LS Companion")
            self.assertEqual(menu.items[1].text, "LS Disconnected")
            self.assertFalse(menu.items[1].enabled)

            state.lossless_scaling_running = True
            self.assertEqual(tray._build_menu().items[1].text, "LS Active")

            profile = manager.create_profile_from_process("game.exe", "Game Profile")
            state.current_active_profile = profile
            state.current_scaled_target = {"processName": "game.exe"}
            state.is_scaling_active = True
            scaling_menu = tray._build_menu()
            self.assertEqual(scaling_menu.items[1].text, "Scaling: game.exe")
            self.assertEqual(scaling_menu.items[2].text, "Profile: Game Profile")
            self.assertEqual(tray._tray_title(), "Scaling: game.exe\nProfile: Game Profile")

            state.is_scaling_active = False
            state.lossless_scaling_running = False
            executable.unlink()
            self.assertEqual(tray._build_menu().items[1].text, "LS Not Found")

    def test_tray_popup_owners_are_promoted_above_taskbar(self):
        native_icon = Mock(_hwnd=101, _menu_hwnd=202)
        with patch("companion.ui.tray.ctypes.windll.user32.SetWindowPos") as set_position:
            CompanionTrayIcon._pin_tray_menu_above_taskbar(native_icon)

        self.assertEqual(set_position.call_count, 2)
        self.assertEqual(set_position.call_args_list[0].args, (101, -1, 0, 0, 0, 0, 3))
        self.assertEqual(set_position.call_args_list[1].args, (202, -1, 0, 0, 0, 0, 3))

    def test_tray_menu_anchors_above_bottom_taskbar_on_secondary_monitor(self):
        work = wintypes.RECT(-1920, 0, 0, 1040)

        placement = CompanionTrayIcon._tray_menu_placement(-8, 1078, work)

        self.assertEqual(placement, (-8, 1036, True, True, 1032))

    def test_tray_menu_anchors_below_top_taskbar(self):
        work = wintypes.RECT(0, 48, 1920, 1080)

        placement = CompanionTrayIcon._tray_menu_placement(20, 12, work)

        self.assertEqual(placement, (20, 52, False, False, 1024))

    def test_tray_menu_uses_work_area_height_cap_and_dispatches_selection(self):
        tray = CompanionTrayIcon.__new__(CompanionTrayIcon)
        selected = Mock()
        native_icon = Mock(_hwnd=101, _menu_hwnd=202, _menu_handle=(303, [selected]))
        win32 = Mock(TPM_RETURNCMD=0x100, TPM_RIGHTALIGN=8, TPM_LEFTALIGN=0,
                     TPM_BOTTOMALIGN=0x20, TPM_TOPALIGN=0)
        win32.GetCursorPos.side_effect = lambda pointer: (
            setattr(pointer._obj, "x", 1910), setattr(pointer._obj, "y", 1080)
        )
        win32.TrackPopupMenuEx.return_value = 1
        with (
            patch.object(tray, "_work_area_at_cursor", return_value=wintypes.RECT(0, 0, 1920, 1040)),
            patch("companion.ui.tray.ctypes.windll.user32.SetMenuInfo", return_value=1) as set_info,
        ):
            self.assertTrue(tray._show_tray_menu_in_work_area(native_icon, win32))

        self.assertEqual(win32.TrackPopupMenuEx.call_args.args, (303, 0x128, 1910, 1036, 202, None))
        self.assertEqual(set_info.call_args.args[1]._obj.cyMax, 1032)
        selected.assert_called_once_with(native_icon)

    def test_right_click_uses_work_area_menu_instead_of_default_popup(self):
        from pystray._util import win32

        tray = CompanionTrayIcon.__new__(CompanionTrayIcon)
        original = Mock(return_value="default")
        native_icon = Mock(_message_handlers={win32.WM_NOTIFY: original})
        with (
            patch.object(tray, "_pin_tray_menu_above_taskbar"),
            patch.object(tray, "_show_tray_menu_in_work_area", return_value=True) as show,
        ):
            tray._install_topmost_menu_hook(native_icon)
            result = native_icon._message_handlers[win32.WM_NOTIFY](0, win32.WM_RBUTTONUP)

        self.assertIsNone(result)
        show.assert_called_once_with(native_icon, win32)
        original.assert_not_called()

    def test_run_passes_a_built_menu_to_pystray(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = ProfileManager(Path(directory))
            watcher = Mock()
            watcher.list_running_executables.return_value = []
            native_icon = Mock()

            tray = CompanionTrayIcon(
                manager,
                AppState(),
                watcher,
                automation=Mock(),
            )
            with patch("companion.ui.tray.pystray.Icon", return_value=native_icon) as icon_type:
                tray.run()

            menu = icon_type.call_args.kwargs["menu"]
            self.assertIsInstance(menu, Menu)
            self.assertEqual(menu.items[0].text, "LS Companion")
            self.assertFalse(menu.items[0].enabled)
            self.assertEqual(menu.items[1].text, "LS Not Found")
            self.assertFalse(menu.items[1].enabled)
            labels = [entry.text for entry in menu.items if hasattr(entry, "text")]
            self.assertIn("Run at Windows Sign-In", labels)
            self.assertIn("Check for Updates", labels)
            native_icon.run.assert_called_once_with()

    def test_startup_tray_entry_updates_the_task_and_saved_setting(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = ProfileManager(Path(directory))
            watcher = Mock()
            watcher.list_running_executables.return_value = []
            startup = Mock()
            startup.is_enabled.return_value = False
            startup.set_enabled.return_value = {"enabled": True}
            tray = CompanionTrayIcon(
                manager, AppState(), watcher, automation=Mock(), startup_manager=startup
            )
            tray.icon = Mock()

            with patch("companion.ui.tray.threading.Thread") as thread_type:
                tray._on_toggle_startup(tray.icon, None)
                thread_type.call_args.kwargs["target"]()

            startup.set_enabled.assert_called_once_with(True)
            self.assertTrue(manager.config.run_at_startup)
            self.assertTrue(tray._startup_enabled)

    @patch("companion.ui.tray.open_dashboard_window")
    def test_update_tray_entry_opens_settings_when_update_is_available(self, open_window):
        with tempfile.TemporaryDirectory() as directory:
            manager = ProfileManager(Path(directory))
            watcher = Mock()
            watcher.list_running_executables.return_value = []
            future = Future()
            tray = CompanionTrayIcon(
                manager,
                AppState(),
                watcher,
                automation=Mock(),
                on_check_updates_callback=lambda: future,
                on_install_update_callback=Mock(),
            )
            tray.icon = Mock()

            tray._on_check_updates(tray.icon, None)
            future.set_result({
                "currentVersion": "1.0.0",
                "latestVersion": "1.1.0",
                "updateAvailable": True,
            })

            open_window.assert_called_once()
            self.assertIn("view=settings", open_window.call_args.args[0])
            self.assertFalse(tray._update_check_running)
            labels = [
                entry.text for entry in tray._build_menu().items
                if hasattr(entry, "text")
            ]
            self.assertIn("Download and Install Update", labels)

    def test_update_tray_entry_downloads_and_installs_detected_version(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = ProfileManager(Path(directory))
            watcher = Mock()
            watcher.list_running_executables.return_value = []
            future = Future()
            install = Mock(return_value=future)
            tray = CompanionTrayIcon(
                manager,
                AppState(),
                watcher,
                automation=Mock(),
                on_install_update_callback=install,
            )
            tray.icon = Mock()
            tray._update_status = {
                "latestVersion": "1.1.0",
                "updateAvailable": True,
                "downloaded": False,
            }

            tray._on_install_update(tray.icon, None)
            future.set_result({
                "type": "COMPANION_INSTALLER_LAUNCHED",
                "version": "1.1.0",
            })

            install.assert_called_once_with("1.1.0")
            self.assertFalse(tray._update_check_running)

    @patch("companion.ui.tray.close_dashboard_window")
    def test_exit_closes_dashboard_and_stops_application(self, close_dashboard):
        tray = CompanionTrayIcon.__new__(CompanionTrayIcon)
        tray.icon = Mock()
        tray.on_exit_callback = Mock()

        tray._on_exit(tray.icon, None)

        tray.icon.stop.assert_called_once_with()
        close_dashboard.assert_called_once_with()
        tray.on_exit_callback.assert_called_once_with()

    @patch("companion.ui.tray.close_dashboard_window", side_effect=OSError("window unavailable"))
    def test_exit_still_stops_application_when_dashboard_close_fails(self, _close_dashboard):
        tray = CompanionTrayIcon.__new__(CompanionTrayIcon)
        tray.icon = Mock()
        tray.on_exit_callback = Mock()

        tray._on_exit(tray.icon, None)

        tray.icon.stop.assert_called_once_with()
        tray.on_exit_callback.assert_called_once_with()

    @patch("companion.ui.tray.close_dashboard_window")
    def test_exit_still_signals_application_when_native_tray_stop_fails(self, close_dashboard):
        tray = CompanionTrayIcon.__new__(CompanionTrayIcon)
        tray.icon = Mock()
        tray.icon.stop.side_effect = OSError("tray window unavailable")
        tray.on_exit_callback = Mock()

        tray._on_exit(tray.icon, None)

        tray.on_exit_callback.assert_called_once_with()
        close_dashboard.assert_called_once_with()


class CompanionApplicationShutdownTests(unittest.TestCase):
    @staticmethod
    def application_stub():
        app = CompanionApplication.__new__(CompanionApplication)
        app.running = True
        app._shutdown_lock = threading.Lock()
        app._shutdown_complete = threading.Event()
        app._shutdown_watchdog_started = False
        app.loop = Mock()
        app.loop.is_running.return_value = True
        app.hotkey_listener = Mock()
        app.monitor_thread = None
        app.automation = Mock()
        app.dynamic_limiter = Mock()
        app.server_thread = Mock()
        app.server_thread.is_alive.return_value = False
        app._instance_guard = Mock()
        return app

    def test_tray_stop_request_only_signals_and_wakes_the_server(self):
        app = self.application_stub()
        app._start_shutdown_watchdog = Mock()

        app.request_stop()

        self.assertFalse(app.running)
        app._start_shutdown_watchdog.assert_called_once_with()
        app.loop.call_soon_threadsafe.assert_called_once()
        app.automation.shutdown.assert_not_called()

    @patch("companion.ui.dashboard_window.close_dashboard_window")
    def test_graceful_shutdown_is_idempotent_and_releases_instance(self, close_dashboard):
        app = self.application_stub()
        app._start_shutdown_watchdog = Mock()
        instance_guard = app._instance_guard

        app.stop()
        app.stop()

        app.hotkey_listener.stop.assert_called_once_with()
        app.automation.shutdown.assert_called_once_with()
        app.dynamic_limiter.close.assert_called_once_with()
        app.server_thread.join.assert_called_once_with(timeout=3)
        instance_guard.release.assert_called_once_with()
        close_dashboard.assert_called_once_with()
        self.assertTrue(app._shutdown_complete.is_set())

    @patch("companion.ui.dashboard_window.close_dashboard_window")
    def test_installer_handoff_stops_tray_and_application(self, close_dashboard):
        app = self.application_stub()
        app.request_stop = Mock()
        app.tray = Mock()

        app.request_update_shutdown()

        app.request_stop.assert_called_once_with()
        app.tray.icon.stop.assert_called_once_with()
        close_dashboard.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
