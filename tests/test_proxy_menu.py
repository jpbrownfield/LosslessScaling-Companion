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

    def test_open_can_show_configuration_without_an_active_profile(self):
        controller = LosslessProxyMenuController(AppState())
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

    def test_embed_reparents_echo_and_detach_restores_it(self):
        controller = LosslessProxyMenuController(AppState())
        user32 = SimpleNamespace(
            IsWindow=Mock(return_value=True),
            GetWindowRect=Mock(return_value=1),
            GetParent=Mock(return_value=0),
            SetParent=Mock(return_value=0),
            ShowWindow=Mock(),
            SetWindowPos=Mock(return_value=1),
        )
        kernel32 = SimpleNamespace(SetLastError=Mock(), GetLastError=Mock(return_value=0))
        with (
            patch.object(controller, "_find_windows", return_value=[8123]),
            patch.object(controller, "_window_long", side_effect=[0x80C40000, 0]),
            patch.object(controller, "_set_window_long") as set_long,
            patch("companion.services.proxy_menu.ctypes.windll.user32", user32),
            patch("companion.services.proxy_menu.ctypes.windll.kernel32", kernel32),
        ):
            result = controller.embed(
                9001, left=10, top=20, width=800, height=600
            )
            detached = controller.detach()

        self.assertTrue(result["embedded"])
        self.assertTrue(detached)
        self.assertEqual(
            user32.SetParent.call_args_list,
            [unittest.mock.call(8123, 9001), unittest.mock.call(8123, 0)],
        )
        self.assertEqual(set_long.call_count, 3)
        self.assertEqual(user32.SetWindowPos.call_args_list[0].args[2:6], (10, 20, 800, 600))

    def test_partial_embed_clips_native_window_and_detach_clears_clip(self):
        controller = LosslessProxyMenuController(AppState())
        user32 = SimpleNamespace(
            IsWindow=Mock(return_value=True), GetWindowRect=Mock(return_value=1),
            GetParent=Mock(return_value=0), SetParent=Mock(return_value=0),
            ShowWindow=Mock(), SetWindowPos=Mock(return_value=1),
            SetWindowRgn=Mock(return_value=1),
        )
        gdi32 = SimpleNamespace(CreateRectRgn=Mock(return_value=55), DeleteObject=Mock())
        kernel32 = SimpleNamespace(SetLastError=Mock(), GetLastError=Mock(return_value=0))
        with (
            patch.object(controller, "_find_windows", return_value=[8123]),
            patch.object(controller, "_window_long", side_effect=[0x80C40000, 0]),
            patch.object(controller, "_set_window_long"),
            patch("companion.services.proxy_menu.ctypes.windll.user32", user32),
            patch("companion.services.proxy_menu.ctypes.windll.gdi32", gdi32),
            patch("companion.services.proxy_menu.ctypes.windll.kernel32", kernel32),
        ):
            result = controller.embed(
                9001, left=10, top=-100, width=800, height=600,
                clip=(0, 100, 800, 500),
            )
            detached = controller.detach()

        self.assertTrue(result["embedded"])
        self.assertTrue(detached)
        gdi32.CreateRectRgn.assert_called_once_with(0, 100, 800, 500)
        self.assertEqual(user32.SetWindowRgn.call_args_list, [
            unittest.mock.call(8123, 55, True),
            unittest.mock.call(8123, 0, True),
        ])


class ProxyTrayMenuTests(unittest.TestCase):
    def test_addon_menu_entry_is_available_only_while_scaling(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = ProfileManager(Path(directory))
            watcher = Mock()
            watcher.list_running_executables.return_value = []
            controller = Mock()
            state = AppState()
            tray = CompanionTrayIcon(manager, state, watcher, automation=Mock(), proxy_menu=controller)

            labels = [item.text for item in tray._build_menu().items if hasattr(item, "text")]
            self.assertNotIn("Show Add-on Menu", labels)
            state.is_scaling_active = True
            labels = [item.text for item in tray._build_menu().items if hasattr(item, "text")]
            self.assertIn("Show Add-on Menu", labels)


if __name__ == "__main__":
    unittest.main()
