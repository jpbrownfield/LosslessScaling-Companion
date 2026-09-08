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


if __name__ == "__main__":
    unittest.main()
