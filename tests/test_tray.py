import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pystray import Menu

from companion.core.profile_manager import ProfileManager
from companion.core.state import AppState
from companion.main import CompanionApplication
from companion.ui.tray import CompanionTrayIcon


class CompanionTrayIconTests(unittest.TestCase):
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
            native_icon.run.assert_called_once_with()

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


if __name__ == "__main__":
    unittest.main()
