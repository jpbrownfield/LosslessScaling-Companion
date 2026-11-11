import tempfile
import ctypes
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from companion.core.models import AppConfig, Profile
from companion.core.state import AppState
from companion.services.overlay_indicator import OverlayIndicator
from companion.services.reshade_overlay import ReShadeOverlayController, menu_hotkey_label
from companion.services.overlay_shortcuts import (parse_shortcut, validate_overlay_hotkeys,
                                                 special_k_menu_hotkey)
from companion.services.hotkey_listener import GlobalHotkeyListener


def environment():
    config = AppConfig()
    profile = Profile(id="game", name="Game")
    profile.graphics.reshade.enabled = True
    manager = SimpleNamespace(config=config, repair_session=False)
    state = AppState()
    state.current_active_profile = profile
    state.is_scaling_active = True
    state.scaling_target_hwnd = 82
    state.scaling_target_pid = 42
    seen = []
    indicator = OverlayIndicator(presenter=seen.append, window_info=lambda hwnd: (1920, 1080, "Game"))
    indicator.set_border("active", anchor_hwnd=82)
    listener = Mock(overlay_registered=dict(config.overlay_hotkeys))
    opener = Mock(return_value=True)
    controller = ReShadeOverlayController(manager, state, indicator, listener, opener,
                                         foreground_check=lambda: True)
    return controller, manager, state, listener, opener, seen


def status(active=True, **extra):
    return {"available": True, "pid": 123, "reshade": {"interactive": active, "hotkeyVk": 119}, **extra}


class ReShadeOverlayTests(unittest.TestCase):
    def test_live_toggle_sets_yellow_border_and_persistent_status_then_restores_scaling(self):
        controller, manager, state, listener, _, seen = environment()
        controller.update(status())
        payload = seen[-1]
        self.assertEqual(payload["border"], "reshade")
        self.assertTrue(payload["lines"][0]["persistent"])
        self.assertEqual(payload["hotkey_hints"][0], {"key": "F8", "label": "Toggle ReShade menu"})
        self.assertIn({"key": "Delete", "label": "Edit current profile"}, payload["hotkey_hints"])
        self.assertIn({"key": "Insert", "label": "Add-on settings"}, payload["hotkey_hints"])
        controller.update(status(False))
        self.assertEqual(seen[-1]["border"], "active")
        self.assertFalse(seen[-1]["hotkey_hints"])
        listener.set_overlay_actions.assert_called_with({})

    def test_shortcuts_open_current_profile_and_headless_addon_settings(self):
        controller, _, _, _, opener, _ = environment()
        controller.update(status())
        self.assertTrue(controller.dispatch_action("profile_editor"))
        opener.assert_called_with("game", None)
        self.assertTrue(controller.dispatch_action("addon_manager"))
        opener.assert_called_with("game", "addons")

    def test_shortcuts_do_not_open_after_focus_profile_or_session_changes(self):
        controller, _, state, _, opener, _ = environment()
        controller.update(status())
        controller.foreground_check = lambda: False
        self.assertFalse(controller.dispatch_action("profile_editor"))
        controller.foreground_check = lambda: True
        state.current_active_profile = Profile(id="other", name="Other")
        self.assertFalse(controller.dispatch_action("profile_editor"))
        controller.update({"available": False})
        self.assertFalse(controller.dispatch_action("addon_manager"))
        opener.assert_not_called()

    def test_unavailable_telemetry_disabled_overlay_and_benchmark_clear_controls(self):
        for setting in ("telemetry", "overlay", "benchmark"):
            controller, manager, state, listener, _, seen = environment()
            controller.update(status())
            if setting == "overlay":
                manager.config.overlay_indicator_enabled = False
            if setting == "benchmark":
                state.benchmark_mode_active = True
            controller.update({"available": False} if setting == "telemetry" else status())
            self.assertFalse(controller.active)
            listener.set_overlay_actions.assert_called_with({})

    def test_reserved_reshade_key_and_failed_registrations_are_not_advertised(self):
        controller, manager, _, listener, _, seen = environment()
        manager.config.overlay_hotkeys["profile_editor"] = "f8"
        listener.overlay_registered = {}
        controller.update(status())
        self.assertNotIn("profile_editor", listener.set_overlay_actions.call_args.args[0])
        self.assertEqual(len(seen[-1]["hotkey_hints"]), 1)

    def test_special_k_custom_binding_is_passive_and_takes_priority(self):
        controller, _, state, listener, _, seen = environment()
        state.current_active_profile.graphics.special_k.enabled = True
        with patch("companion.services.reshade_overlay.special_k_menu_hotkey", return_value=("ctrl+f10", True)):
            controller.update(status(components=[{"id": "special-k", "loaded": True}]))
        self.assertIn({"key": "Ctrl+F10", "label": "Special K panel"}, seen[-1]["hotkey_hints"])
        self.assertNotIn("special_k_panel", listener.set_overlay_actions.call_args.args[0])


class OverlayShortcutTests(unittest.TestCase):
    def test_defaults_punctuation_modifiers_blank_and_duplicates(self):
        bindings = validate_overlay_hotkeys({})
        self.assertEqual(parse_shortcut(bindings["profile_editor"])[:2], (0, 46))
        self.assertEqual(parse_shortcut("=")[:2], (0, 0xBB))
        self.assertEqual(parse_shortcut("-")[:2], (0, 0xBD))
        self.assertEqual(parse_shortcut("Ctrl+Shift+Backspace")[:2], (6, 8))
        self.assertIsNone(parse_shortcut(""))
        for invalid in ({"profile_editor": "insert"}, {"unknown": "f8"}, {"profile_editor": "bad+key"}):
            with self.assertRaises(ValueError):
                validate_overlay_hotkeys(invalid)

    def test_special_k_reads_private_utf16_config_and_native_default(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "LosslessScaling.exe"
            self.assertEqual(special_k_menu_hotkey(str(executable)), ("ctrl+shift+backspace", False))
            ini = Path(directory) / ".companion-special-k/Global/osd.ini"
            ini.parent.mkdir(parents=True)
            ini.write_text("[ImGui.Global]\nControlPanelToggle=Ctrl+F10\n", encoding="utf-16")
            self.assertEqual(special_k_menu_hotkey(str(executable)), ("ctrl+f10", True))

    def test_key_labels_use_runtime_binding_with_modifiers(self):
        self.assertEqual(menu_hotkey_label({"hotkeyVk": 35, "ctrl": True}, "f8"), "Ctrl+End")

    def test_listener_refreshes_only_scoped_registrations_and_releases_them(self):
        manager = SimpleNamespace(config=AppConfig())
        listener = GlobalHotkeyListener(manager, AppState(), lambda: None)
        fake = Mock()
        fake.RegisterHotKey.return_value = True
        with patch("companion.services.hotkey_listener.ctypes.windll.user32", fake):
            listener.set_overlay_actions({"profile_editor": {"binding": "delete", "callback": lambda: None}})
            listener._sync_registration()
            self.assertEqual(listener.overlay_registered, {"profile_editor": "delete"})
            calls = fake.RegisterHotKey.call_count
            listener._sync_registration()
            self.assertEqual(fake.RegisterHotKey.call_count, calls)
            listener.set_overlay_actions({})
            listener._sync_registration()
            self.assertEqual(listener.overlay_registered, {})
            fake.UnregisterHotKey.assert_called_once()


@unittest.skipUnless(sys.platform == 'win32', 'Win32 overlay controls')
class NativeOverlayControlsTests(unittest.TestCase):
    def test_yellow_border_hints_graph_and_status_have_independent_lifetimes(self):
        from companion.services import overlay_window as overlay
        presenter = overlay.Win32OverlayPresenter()
        u, g = presenter._user32, presenter._gdi32
        u.GetGuiResources.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        u.GetGuiResources.restype = ctypes.c_ulong
        anchor = u.CreateWindowExW(0, 'STATIC', 'Overlay test target', 0x80000000,
                                  100, 100, 1280, 720, None, None, None, None)
        dc = None
        try:
            self.assertTrue(presenter._create_windows())
            presenter._pending = {
                'lines': [{'id': 'status', 'text': 'Overlay active', 'persistent': True}],
                'border': 'reshade', 'anchor_hwnd': anchor, 'border_anchor_hwnd': anchor,
                'hotkey_hints': [{'key': 'F8', 'label': 'ReShade menu'},
                                 {'key': 'Delete', 'label': 'Edit current profile'}],
                'performance_graph': {'samples': [16, 17, 16.5] * 30, 'fps': 60, 'frame_ms': 16.67},
            }
            presenter._has_pending = True
            presenter._apply_pending()
            self.assertEqual(presenter._border_color, overlay.RESHADE_COLOR)
            self.assertTrue(presenter._hint_visible)
            self.assertTrue(presenter._graph_visible)
            self.assertTrue(presenter._header_visible)
            hints = overlay._Rect()
            self.assertTrue(u.GetWindowRect(presenter._hint_hwnd, ctypes.byref(hints)))
            self.assertEqual(hints.bottom, 100 + 720 - presenter._px(76 + 24) + presenter._px(76))
            self.assertEqual(hints.bottom - hints.top, presenter._px(76))
            dc = u.GetDC(anchor)
            # Realize the two fonts and flush Windows' batched GDI work before
            # comparing retained resources across subsequent paints.
            for _ in range(5):
                presenter._paint_hints(dc)
                presenter._paint_graph(dc)
            g.GdiFlush()
            handles = u.GetGuiResources(ctypes.c_void_p(-1), 0)
            for _ in range(30):
                presenter._paint_hints(dc)
                presenter._paint_graph(dc)
            g.GdiFlush()
            self.assertEqual(u.GetGuiResources(ctypes.c_void_p(-1), 0), handles)
            presenter._animate_toasts(10**12)
            self.assertTrue(presenter._header_visible, 'Status remains until the overlay toggle is off')
            presenter._pending = {'lines': [], 'border': 'active', 'border_anchor_hwnd': anchor}
            presenter._has_pending = True
            presenter._apply_pending()
            self.assertFalse(presenter._hint_visible)
            self.assertFalse(presenter._graph_visible)
            self.assertFalse(presenter._header_visible)
            self.assertEqual(presenter._border_color, overlay.ACTIVE_COLOR)
            self.assertTrue(presenter._usable)
        finally:
            if dc:
                u.ReleaseDC(anchor, dc)
            presenter._teardown_windows()
            u.DestroyWindow(anchor)
