import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pystray import Menu

from companion.core.profile_manager import ProfileManager
from companion.core.state import AppState
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


if __name__ == "__main__":
    unittest.main()
