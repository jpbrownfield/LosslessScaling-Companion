import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from companion.core.profile_manager import ProfileManager
from companion.core.state import AppState
from companion.services.proxy_menu import LosslessProxyMenuController
from companion.ui.tray import CompanionTrayIcon


class LosslessProxyMenuControllerTests(unittest.TestCase):
    def test_proxy_requirement_tracks_only_the_two_profile_features(self):
        state = AppState()
        controller = LosslessProxyMenuController(state)
        profile = SimpleNamespace(
            graphics=SimpleNamespace(
                neural_render=SimpleNamespace(implementation="disabled")
            ),
            reshade=SimpleNamespace(menu_proxy_enabled=False),
        )
        state.current_active_profile = profile
        self.assertFalse(controller.active_profile_uses_proxy())

        profile.graphics.neural_render.implementation = "lsp_neural_render"
        self.assertTrue(controller.active_profile_uses_proxy())
        profile.graphics.neural_render.implementation = "disabled"
        profile.reshade.menu_proxy_enabled = True
        self.assertTrue(controller.active_profile_uses_proxy())

    def test_open_restores_and_focuses_hidden_manager(self):
        state = AppState()
        state.current_active_profile = SimpleNamespace(
            graphics=SimpleNamespace(
                neural_render=SimpleNamespace(implementation="lsp_neural_render")
            ),
            reshade=None,
        )
        controller = LosslessProxyMenuController(state)
        user32 = SimpleNamespace(
            ShowWindowAsync=Mock(), ShowWindow=Mock(), BringWindowToTop=Mock(),
            SetForegroundWindow=Mock(return_value=1),
        )
        with (
            patch.object(controller, "_find_windows", return_value=[8123]),
            patch("companion.services.proxy_menu.ctypes.windll.user32", user32),
        ):
            result = controller.open()

        self.assertTrue(result["opened"])
        user32.ShowWindowAsync.assert_called_once_with(8123, controller.SW_RESTORE)
        user32.BringWindowToTop.assert_called_once_with(8123)
        user32.SetForegroundWindow.assert_called_once_with(8123)

    def test_open_refuses_profiles_without_a_proxy_feature(self):
        controller = LosslessProxyMenuController(AppState())
        with patch.object(controller, "_find_windows") as find_windows:
            result = controller.open()
        self.assertFalse(result["opened"])
        find_windows.assert_not_called()


class ProxyTrayMenuTests(unittest.TestCase):
    def test_proxy_menu_entry_is_conditional_on_active_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = ProfileManager(Path(directory))
            watcher = Mock()
            watcher.list_running_executables.return_value = []
            controller = Mock()
            controller.active_profile_uses_proxy.side_effect = [False, True]
            tray = CompanionTrayIcon(
                manager, AppState(), watcher, automation=Mock(), proxy_menu=controller
            )

            labels = [item.text for item in tray._build_menu().items if hasattr(item, "text")]
            self.assertNotIn("Open Proxy Add-on Menu", labels)
            labels = [item.text for item in tray._build_menu().items if hasattr(item, "text")]
            self.assertIn("Open Proxy Add-on Menu", labels)


if __name__ == "__main__":
    unittest.main()
