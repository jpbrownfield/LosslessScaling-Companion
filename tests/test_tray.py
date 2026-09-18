import tempfile
import threading
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import Mock, patch

from pystray import Menu

from companion.core.profile_manager import ProfileManager
from companion.core.state import AppState
from companion.main import CompanionApplication
from companion.ui.tray import CompanionTrayIcon


class CompanionTrayIconTests(unittest.TestCase):
    def test_visual_state_follows_lossless_scaling_runtime(self):
        tray = CompanionTrayIcon.__new__(CompanionTrayIcon)
        tray.state = AppState()

        self.assertEqual(tray._visual_state(), "closed")
        tray.state.lossless_scaling_running = True
        self.assertEqual(tray._visual_state(), "open")
        tray.state.is_scaling_active = True
        self.assertEqual(tray._visual_state(), "scaling")

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

    def test_graceful_shutdown_is_idempotent_and_releases_instance(self):
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
