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
from companion.services.ls_inspector import ScaledTargetInfo


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
                                         foreground_check=lambda: True, open_addon_panel=Mock(return_value=True),
                                         passthrough_control=Mock(side_effect=lambda enabled: {
                                             **controller._last_status, "available": True,
                                             "reshade": {**controller._last_status.get("reshade", {}),
                                                         "interactive": enabled, "controlRequested": enabled}}))
    return controller, manager, state, listener, opener, seen


def status(active=True, **extra):
    return {"available": True, "pid": 123, "reshade": {"interactive": active, "hotkeyVk": 119}, **extra}


class ReShadeOverlayTests(unittest.TestCase):
    def test_suspended_overlay_keeps_graph_and_resumes_without_closed_toast(self):
        controller, _, _, listener, _, seen = environment()
        controller.performance_graph = Mock(return_value=True)
        controller.update(status())
        controller.indicator.set_performance_graph({"fps": 60})
        controller.foreground_check = lambda: False
        controller.update(status(False, reshade={"interactive": False, "controlRequested": True}))
        self.assertFalse(controller.active)
        self.assertTrue(controller._native_requested)
        self.assertIsNone(seen[-1])
        controller.performance_graph.assert_not_called()
        self.assertFalse(any(line.text == "Overlay closed" for line in controller.indicator._state.current))
        listener.set_overlay_actions.assert_called_with({})
        controller.foreground_check = lambda: True
        controller.update(status(True, reshade={"interactive": True, "controlRequested": True}))
        self.assertTrue(controller.active)
        self.assertEqual(seen[-1]["performance_graph"], {"fps": 60})

    def test_toggle_cancels_suspended_preference_before_native_resume(self):
        controller, _, _, _, _, _ = environment()
        controller.update(status(False, reshade={"interactive": False, "controlRequested": True}))
        self.assertTrue(controller.dispatch_action("overlay_toggle"))
        controller.passthrough_control.assert_called_once_with(False)
        self.assertFalse(controller._native_requested)
        self.assertFalse(controller.active)

    def test_target_change_and_shutdown_cancel_suspended_preference(self):
        for cleanup in ("target", "shutdown"):
            with self.subTest(cleanup=cleanup):
                controller, _, state, _, _, _ = environment()
                controller.performance_graph = Mock()
                suspended = status(False, reshade={"interactive": False, "controlRequested": True})
                controller.update(suspended)
                if cleanup == "target":
                    state.scaling_target_hwnd = 99
                    controller.update(suspended)
                else:
                    controller.clear()
                controller.passthrough_control.assert_called_once_with(False)
                self.assertFalse(controller._native_requested)
                controller.performance_graph.assert_called_once_with(False)

    def test_alt_tab_hides_scaling_border_and_restores_it_without_stopping_session(self):
        controller, _, state, listener, _, seen = environment()
        controller.update(status(False))
        controller.foreground_check = lambda: False
        controller.indicator.tick()
        self.assertIsNone(seen[-1])
        controller.update(status(False))
        listener.set_overlay_actions.assert_called_with({})
        self.assertTrue(state.is_scaling_active)
        self.assertEqual(controller.indicator._state.border.state, "active")
        controller.foreground_check = lambda: True
        controller.indicator.tick()
        self.assertEqual(seen[-1]["border"], "active")
        controller.update(status(False))
        self.assertIn("overlay_toggle", listener.set_overlay_actions.call_args.args[0])

    def test_alt_tab_hides_manual_overlay_hints_and_graph_then_restores_them(self):
        controller, _, _, _, _, seen = environment()
        controller.update(status(False))
        controller.dispatch_action("overlay_toggle")
        controller.indicator.set_performance_graph({"fps": 60})
        controller.foreground_check = lambda: False
        controller.indicator.tick()
        self.assertIsNone(seen[-1])
        self.assertFalse(controller.dispatch_action("profile_editor"))
        controller.foreground_check = lambda: True
        controller.indicator.tick()
        self.assertTrue(controller.active)
        self.assertEqual(seen[-1]["border"], "reshade")
        self.assertTrue(seen[-1]["hotkey_hints"])
        self.assertEqual(seen[-1]["performance_graph"], {"fps": 60})

    def test_foreground_check_handles_native_target_and_pointer_sized_windows(self):
        controller, _, state, _, _, _ = environment()
        state.scaling_target_pid = None
        state.current_scaled_target = {"pid": 42, "hwnd": 82}
        fake = Mock()
        fake.GetForegroundWindow.return_value = 0x123456789

        def identify(hwnd, pointer):
            self.assertEqual(hwnd, 0x123456789)
            ctypes.cast(pointer, ctypes.POINTER(ctypes.wintypes.DWORD)).contents.value = 42
            return 1

        fake.GetWindowThreadProcessId.side_effect = identify
        with patch("companion.services.reshade_overlay.ctypes.windll.user32", fake):
            self.assertTrue(controller._foreground_allowed())
            self.assertEqual(fake.GetForegroundWindow.restype, ctypes.wintypes.HWND)
            state.scaling_target_pid = 99
            self.assertFalse(controller._foreground_allowed())
            state.scaling_target_pid = None
            state.current_scaled_target = {"pid": 99}
            self.assertFalse(controller._foreground_allowed())
            controller.host_pid = 42
            self.assertTrue(controller._foreground_allowed())
            fake.GetWindowThreadProcessId.side_effect = None
            fake.GetWindowThreadProcessId.return_value = 0
            self.assertFalse(controller._foreground_allowed())
            fake.GetForegroundWindow.return_value = None
            self.assertFalse(controller._foreground_allowed())

    def test_verified_ls_foreground_remains_eligible_when_telemetry_temporarily_loses_pid(self):
        controller, manager, _, _, _, _ = environment()
        manager.config.lossless_scaling_exe_path = "C:/LS/LosslessScaling.exe"
        controller.host_pid = None
        fake = Mock()
        fake.GetForegroundWindow.return_value = 1234

        def identify(_hwnd, pointer):
            ctypes.cast(pointer, ctypes.POINTER(ctypes.wintypes.DWORD)).contents.value = 123
            return 1

        fake.GetWindowThreadProcessId.side_effect = identify
        with patch("companion.services.reshade_overlay.ctypes.windll.user32", fake), patch(
            "companion.services.reshade_overlay.psutil.Process"
        ) as process:
            process.return_value.exe.return_value = "C:/LS/LosslessScaling.exe"
            self.assertTrue(controller._foreground_allowed())
            process.return_value.exe.return_value = "C:/Unrelated/LosslessScaling.exe"
            self.assertFalse(controller._foreground_allowed())

    def test_scaling_stop_cleanup_does_not_create_an_overlay_closed_toast(self):
        controller, _, state, listener, _, seen = environment()
        controller.update(status())
        state.is_scaling_active = False
        controller.indicator.end_scaling()
        controller.update(status())
        self.assertFalse(controller.active)
        self.assertIsNone(seen[-1])
        self.assertEqual(controller.indicator._state.current, [])
        listener.set_overlay_actions.assert_called_with({})
        controller.passthrough_control.assert_called_once_with(False)

    def test_shutdown_releases_native_input_before_clearing_controls(self):
        controller, _, _, listener, _, _ = environment()
        controller.update(status())
        controller.clear()
        controller.passthrough_control.assert_called_once_with(False)
        self.assertFalse(controller.active)
        listener.set_overlay_actions.assert_called_with({})

    def test_live_toggle_sets_yellow_border_and_persistent_status_then_restores_scaling(self):
        controller, manager, state, listener, _, seen = environment()
        controller.update(status())
        payload = seen[-1]
        self.assertEqual(payload["border"], "reshade")
        self.assertTrue(payload["lines"][0]["persistent"])
        self.assertEqual(payload["hotkey_hints"][0], {"key": "F8", "label": "Toggle ReShade menu"})
        self.assertIn({"key": "Delete", "label": "Edit current profile"}, payload["hotkey_hints"])
        self.assertIn({"key": "Insert", "label": "Advanced add-on panel"}, payload["hotkey_hints"])
        controller.update(status(False))
        self.assertEqual(seen[-1]["border"], "active")
        self.assertFalse(seen[-1]["hotkey_hints"])
        self.assertEqual(set(listener.set_overlay_actions.call_args.args[0]), {"overlay_toggle"})

    def test_shortcuts_open_current_profile_and_native_addon_panel(self):
        controller, _, _, _, opener, _ = environment()
        controller.update(status())
        self.assertTrue(controller.dispatch_action("profile_editor"))
        opener.assert_called_with("game", None)
        self.assertTrue(controller.dispatch_action("addon_manager"))
        controller.open_addon_panel.assert_called_once_with()
        opener.assert_called_once_with("game", None)

    def test_disabled_host_removes_panel_shortcut_and_blocks_dispatch(self):
        controller, manager, _, listener, _, _ = environment()
        manager.config.addon_manager_enabled = False
        controller.update(status())
        self.assertNotIn("addon_manager", listener.set_overlay_actions.call_args.args[0])
        self.assertFalse(controller.dispatch_action("addon_manager"))
        controller.open_addon_panel.assert_not_called()

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
            self.assertEqual(set(listener.set_overlay_actions.call_args.args[0]),
                             {"overlay_toggle"} if setting == "telemetry" else set())

    def test_companion_toggle_opens_without_reshade_or_telemetry_and_stays_closed(self):
        controller, _, state, listener, _, seen = environment()
        state.current_active_profile.graphics.reshade.enabled = False
        controller.update({"available": False})
        self.assertFalse(controller.active)
        self.assertEqual(set(listener.set_overlay_actions.call_args.args[0]), {"overlay_toggle"})
        self.assertTrue(controller.dispatch_action("overlay_toggle"))
        self.assertTrue(controller.active)
        self.assertEqual(seen[-1]["lines"][0]["text"], "Overlay active")
        self.assertTrue(controller.dispatch_action("overlay_toggle"))
        controller.update({"available": False})
        self.assertFalse(controller.active)
        self.assertFalse(controller.dispatch_action("profile_editor"))
        controller.clear()
        listener.set_overlay_actions.assert_called_with({})

    def test_detected_scaling_window_can_anchor_overlay_without_host_telemetry(self):
        controller, _, state, _, _, seen = environment()
        state.current_active_profile.graphics.reshade.enabled = False
        state.scaling_target_hwnd = None
        state.current_scaled_target = ScaledTargetInfo(
            is_active=True, pid=42, hwnd=82, process_name="game.exe", source="log_file"
        ).to_dict()
        controller.update({"available": False})
        self.assertTrue(controller.dispatch_action("overlay_toggle"))
        self.assertTrue(controller.active)
        self.assertEqual(seen[-1]["anchor_hwnd"], 82)
        self.assertEqual(seen[-1]["lines"][0]["text"], "Overlay active")

    def test_companion_toggle_is_scoped_to_foreground_and_application_session(self):
        controller, _, state, listener, _, _ = environment()
        controller.update(status(False))
        self.assertTrue(controller.dispatch_action("overlay_toggle"))
        controller.foreground_check = lambda: False
        controller.update(status(False))
        listener.set_overlay_actions.assert_called_with({})
        self.assertFalse(controller.dispatch_action("overlay_toggle"))
        controller.foreground_check = lambda: True
        state.scaling_target_pid = 99
        self.assertFalse(controller.dispatch_action("overlay_toggle"))
        controller.update(status(False))
        self.assertFalse(controller.active)

    def test_overlay_toggle_sets_native_passthrough_on_and_off_without_replaying_menu_key(self):
        controller, _, _, _, _, _ = environment()
        controller.update(status(False))
        with patch("companion.services.reshade_overlay.InputSimulator.trigger_hotkey") as key:
            self.assertTrue(controller.dispatch_action("overlay_toggle"))
            controller.passthrough_control.assert_called_with(True)
            self.assertTrue(controller.active)
            self.assertTrue(controller.dispatch_action("overlay_toggle"))
            controller.passthrough_control.assert_called_with(False)
            self.assertFalse(controller.active)
            key.assert_not_called()

    def test_control_request_uses_the_actual_scaled_window_identity(self):
        controller, manager, state, _, _, _ = environment()
        controller.update(status(False))
        with patch("companion.services.reshade_overlay.set_passthrough", return_value=status()) as request:
            controller._set_passthrough(True)
            request.assert_called_once_with(manager.config.lossless_scaling_exe_path, True,
                                            expected_pid=123, target_pid=42, target_hwnd=82)
            state.scaling_target_pid, state.scaling_target_hwnd = None, None
            state.current_scaled_target = {"pid": 43, "hwnd": 0x123456789}
            controller._set_passthrough(True)
            self.assertEqual(request.call_args.kwargs["target_hwnd"], 0x123456789)

    def test_failed_passthrough_request_does_not_claim_overlay_is_active(self):
        controller, _, _, _, _, seen = environment()
        controller.update(status(False))
        controller.passthrough_control.side_effect = TimeoutError("Host did not enable input")
        self.assertFalse(controller.dispatch_action("overlay_toggle"))
        self.assertFalse(controller.active)
        self.assertEqual(seen[-1]["border"], "active")
        self.assertIn("Overlay input unavailable", seen[-1]["lines"][-1]["text"])

    def test_native_scaling_without_a_matched_profile_can_open_default_profile_controls(self):
        controller, manager, state, _, opener, _ = environment()
        manager.config.profiles = [Profile(id="default", name="Default", is_default=True)]
        state.current_active_profile = None
        controller.update({"available": False, "pid": 123})
        self.assertTrue(controller.dispatch_action("overlay_toggle"))
        self.assertTrue(controller.active)
        self.assertTrue(controller.dispatch_action("profile_editor"))
        opener.assert_called_once_with("default", None)

    def test_reserved_reshade_key_and_failed_registrations_are_not_advertised(self):
        controller, manager, _, listener, _, seen = environment()
        manager.config.overlay_hotkeys["profile_editor"] = "f8"
        listener.overlay_registered = {}
        controller.update(status())
        self.assertNotIn("profile_editor", listener.set_overlay_actions.call_args.args[0])
        self.assertEqual(len(seen[-1]["hotkey_hints"]), 1)

    def test_end_opens_companion_when_reshade_uses_home(self):
        controller, manager, _, listener, _, seen = environment()
        manager.config.overlay_hotkeys["overlay_toggle"] = "end"
        manager.config.reshade_overlay_hotkey = "home"
        listener.overlay_registered = dict(manager.config.overlay_hotkeys)
        controller.update(status(False, reshade={"interactive": False, "hotkeyVk": 36}))
        self.assertEqual(listener.set_overlay_actions.call_args.args[0]["overlay_toggle"]["binding"], "end")
        self.assertTrue(controller.dispatch_action("overlay_toggle"))
        self.assertTrue(controller.active)
        self.assertIn({"key": "Home", "label": "Toggle ReShade menu"}, seen[-1]["hotkey_hints"])

    def test_stale_native_menu_binding_does_not_unregister_companion_toggle(self):
        controller, manager, _, listener, _, _ = environment()
        manager.config.overlay_hotkeys["overlay_toggle"] = "end"
        manager.config.reshade_overlay_hotkey = "home"
        controller.update(status(False, reshade={"interactive": False, "hotkeyVk": 35, "controlAvailable": True}))
        self.assertEqual(listener.set_overlay_actions.call_args.args[0]["overlay_toggle"]["binding"], "end")
        self.assertTrue(controller.dispatch_action("overlay_toggle"))

    def test_disabled_or_unavailable_reshade_does_not_reserve_host_default_end(self):
        for uses_reshade, available in ((False, True), (True, False)):
            with self.subTest(uses_reshade=uses_reshade, available=available):
                controller, manager, state, listener, _, _ = environment()
                manager.config.overlay_hotkeys["overlay_toggle"] = "end"
                manager.config.reshade_overlay_hotkey = "home"
                state.current_active_profile.graphics.reshade.enabled = uses_reshade
                controller.update(status(False, available=available,
                                         reshade={"interactive": False, "hotkeyVk": 35}))
                self.assertIn("overlay_toggle", listener.set_overlay_actions.call_args.args[0])
                self.assertTrue(controller.dispatch_action("overlay_toggle"))
                self.assertTrue(controller.active)

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

    def test_alt_comma_validates_registers_and_labels_the_windows_oem_key(self):
        from companion.core.models import HotkeyConfig
        from companion.services.overlay_shortcuts import reshade_key_data
        self.assertEqual(HotkeyConfig(modifiers=["alt"], key=",").key, ",")
        self.assertEqual(HotkeyConfig(key="OemComma").key, ",")
        self.assertEqual(parse_shortcut("alt+,")[:2], (1, 0xBC))
        self.assertEqual(reshade_key_data("alt+,"), (0xBC, 0, 0, 1))
        self.assertEqual(menu_hotkey_label({"hotkeyVk": 0xBC, "alt": True}, ""), "Alt+,")
        bindings = validate_overlay_hotkeys({"overlay_toggle": "alt+,"})
        listener = GlobalHotkeyListener(SimpleNamespace(config=AppConfig()), AppState(), lambda: None)
        with patch("companion.services.hotkey_listener.ctypes.windll.user32") as fake:
            fake.RegisterHotKey.return_value = True
            listener.set_overlay_actions({"overlay_toggle": {"binding": bindings["overlay_toggle"], "callback": lambda: None}})
            listener._sync_registration()
            self.assertEqual(fake.RegisterHotKey.call_args.args[-2:], (0x4001, 0xBC))

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

    def test_listener_recovers_from_temporary_conflict_without_releasing_other_shortcuts(self):
        listener = GlobalHotkeyListener(SimpleNamespace(config=AppConfig()), AppState(), lambda: None)
        actions = {"overlay_toggle": {"binding": "ctrl+home", "callback": lambda: None},
                   "profile_editor": {"binding": "delete", "callback": lambda: None}}
        fake = Mock()
        fake.RegisterHotKey.side_effect = [False, True, True]
        with patch("companion.services.hotkey_listener.ctypes.windll.user32", fake), patch(
            "companion.services.hotkey_listener.time.monotonic", return_value=10
        ) as clock, patch.object(listener, "refresh") as refresh:
            listener.set_overlay_actions(actions)
            listener._sync_registration()
            self.assertEqual(listener.overlay_registered, {"profile_editor": "delete"})
            refresh.reset_mock()
            listener.set_overlay_actions(actions)
            listener._sync_registration()
            refresh.assert_not_called()
            self.assertEqual(fake.RegisterHotKey.call_count, 2)
            clock.return_value = 11
            listener.set_overlay_actions(actions)
            refresh.assert_called_once()
            listener._sync_registration()
            self.assertEqual(set(listener.overlay_registered), set(actions))
            fake.UnregisterHotKey.assert_not_called()
            listener._sync_registration()
            self.assertEqual(fake.RegisterHotKey.call_count, 3)


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
