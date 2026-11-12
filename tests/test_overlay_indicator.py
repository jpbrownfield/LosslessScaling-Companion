"""Toast cards and border: lifetimes, game-like gating, wiring."""
import ctypes
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from companion.core.models import Profile, ReshadeConfig
from companion.core.profile_manager import ProfileManager
from companion.core.state import AppState
from companion.services import overlay_indicator
from companion.services.automation import AutomationController
from companion.services.overlay_indicator import OverlayIndicator, OverlayState
from companion.services.process_watcher import ProcessInfo, ProcessWatcher


class Script:
    def __init__(self, now=100.0):
        self.now = now
        self.seen = []

    def present(self, payload):
        self.seen.append(payload)

    def lines(self):
        out = []
        for payload in self.seen:
            if payload:
                out.extend(item["text"] for item in payload.get("lines", []))
        return out

    def borders(self):
        return [p["border"] if p else None for p in self.seen]


def make_automation(**config_overrides):
    manager = ProfileManager(Path(tempfile.mkdtemp()))
    for key, value in config_overrides.items():
        setattr(manager.config, key, value)
    script = Script()
    watcher = Mock()
    watcher.check_is_lossless_scaling_running.return_value = True
    watcher.stop_lossless_scaling.return_value = True
    watcher.launch_lossless_scaling.return_value = True
    watcher.get_foreground_window_info.return_value = None
    settings = Mock()
    settings.gpu_route_changes_required.return_value = False
    settings.update_gpu_route.return_value = False
    automation = AutomationController(
        manager, AppState(), watcher, lossless_settings=settings,
        overlay_indicator=OverlayIndicator(
            presenter=script.present,
            window_info=lambda hwnd: (1920, 1080, "The Game"),
            stable_seconds=0.0))
    automation.deployment_manager = Mock()
    automation.deployment_manager.needs_change.return_value = False
    automation.asset_store.write_profile_manifest = Mock()
    return automation, manager, script


class OverlayStateTests(unittest.TestCase):
    def test_lines_expire_oldest_first(self):
        state = OverlayState()
        state.log("first", now=100.0)
        state.log("second", now=101.0)
        state.tick(now=106.9)
        self.assertEqual([item.text for item in state.current],
                         ["first", "second"])
        state.tick(now=107.1)
        self.assertEqual([item.text for item in state.current], ["second"])

    def test_errors_also_last_seven_seconds(self):
        state = OverlayState()
        state.log("boom", kind="error", now=100.0)
        state.tick(now=106.9)
        self.assertEqual(len(state.current), 1)
        state.tick(now=107.0)
        self.assertEqual(state.current, [])

    def test_identical_messages_have_distinct_lifetimes(self):
        state = OverlayState()
        state.log("Scaling stopped", now=100.0)
        state.log("Scaling stopped", now=101.0)
        first, second = state.current
        self.assertNotEqual(first.id, second.id)
        self.assertEqual((first.created_at, first.expires_at), (100.0, 107.0))
        self.assertEqual((second.created_at, second.expires_at), (101.0, 108.0))

    def test_max_five_lines(self):
        state = OverlayState()
        for index in range(7):
            state.log(f"line-{index}", now=100.0)
        state.tick(now=100.0)
        self.assertEqual(len(state.current), 5)
        self.assertEqual(state.current[0].text, "line-2")

    def test_clear_resets_border(self):
        state = OverlayState()
        state.log("x", now=100.0)
        state.set_border("pending", anchor_hwnd=9, now=100.0)
        state.clear()
        self.assertEqual(state.current, [])
        state.tick(now=101.0)
        self.assertEqual(state.current, [])
        self.assertIsNone(state.border.state)


class GameLikenessTests(unittest.TestCase):
    def test_rejects_splash_and_tiny_windows(self):
        from companion.services.overlay_indicator import is_game_like_window

        self.assertFalse(is_game_like_window(320, 200, "Game"))
        self.assertFalse(is_game_like_window(1920, 1080, "Game Splash"))
        self.assertFalse(is_game_like_window(1920, 1080, "Setup - Game"))
        self.assertFalse(is_game_like_window(1920, 1080, "Patcher"))
        # Big untitled windows still qualify; size + stability gates apply.
        self.assertTrue(is_game_like_window(1920, 1080, None))
        self.assertTrue(is_game_like_window(1920, 1080, "Cyberpunk 2077"))
        self.assertTrue(is_game_like_window(1280, 720, "Chrome"))

    def test_border_waits_for_stable_game_window(self):
        seen = []

        def fake_window(_hwnd):
            return (1920, 1080, "The Game")

        indicator = OverlayIndicator(presenter=seen.append,
                                     window_info=fake_window,
                                     stable_seconds=3.0)
        indicator.set_border("pending", anchor_hwnd=77, now=1000.0)
        indicator.tick(now=1000.0)
        self.assertTrue(all(item is None for item in seen))
        indicator.tick(now=1003.5)
        shown = [item for item in seen if item is not None]
        self.assertTrue(shown)
        self.assertEqual(shown[-1]["border"], "pending")
        self.assertEqual(shown[-1]["border_anchor_hwnd"], 77)

    def test_border_hidden_for_splash(self):
        seen = []

        def fake_window(_hwnd):
            return (1920, 1080, "Game Launcher")

        indicator = OverlayIndicator(presenter=seen.append,
                                     window_info=fake_window,
                                     stable_seconds=0.0)
        indicator.set_border("pending", anchor_hwnd=77)
        indicator.tick(now=1000.0)
        self.assertTrue(all(item is None for item in seen))


class FocusSafetyTests(unittest.TestCase):
    def test_no_focus_stealing_calls(self):
        import pathlib

        source = (pathlib.Path(__file__).resolve().parents[1]
                  / "companion" / "services" / "overlay_window.py").read_text(
                      encoding="utf-8")
        for forbidden in ("SetForegroundWindow", "SetActiveWindow",
                          "BringWindowToTop", "AttachThreadInput",
                          "SetFocus(", "SetCapture"):
            self.assertNotIn(forbidden, source)


class OverlayWiringTests(unittest.TestCase):
    def test_unmatched_windows_are_quiet_after_leaving_a_profile_with_deferred_cleanup(self):
        automation, manager, script = make_automation()
        manager.config.lossless_scaling_exe_path = "C:/LS/LosslessScaling.exe"
        automation.state.current_active_profile = Profile(id="game", name="The Game")
        automation.resolve_deployment_files = Mock(return_value=[])
        automation.deployment_manager.needs_change.return_value = True
        transition = Mock(wraps=automation._apply_profile_transition)
        automation._apply_profile_transition = transition
        watcher = ProcessWatcher(
            manager, automation.state, on_profile_changed=automation.activate_profile)
        windows = [ProcessInfo(41, "notepad.exe", "C:/Windows/notepad.exe", "Notes", 81),
                   ProcessInfo(42, "explorer.exe", "C:/Windows/explorer.exe", "Files", 82)]
        with patch.object(watcher, "get_foreground_window_info", side_effect=windows):
            for _ in windows:
                watcher.check_foreground_and_update()

        self.assertEqual(script.lines(), [])
        self.assertIsNone(automation.state.current_active_profile)
        self.assertEqual(automation.state.current_foreground_process, "explorer.exe")
        transition.assert_called_once()
        automation.process_watcher.stop_lossless_scaling.assert_not_called()

    def test_unmatched_focus_with_no_active_profile_does_not_prepare_a_deployment(self):
        automation, _, script = make_automation()
        automation.resolve_deployment_files = Mock(return_value=[])
        automation.activate_profile(None, "C:/Windows/notepad.exe", target_pid=41, target_hwnd=81)
        self.assertEqual(script.lines(), [])
        automation.resolve_deployment_files.assert_not_called()

    def test_scaling_request_precedes_setup_and_confirmation_with_or_without_host_restart(self):
        for restart in (False, True):
            with self.subTest(restart=restart):
                automation, manager, script = make_automation(disable_native_auto_scale=True)
                manager.config.lossless_scaling_exe_path = "C:/LS/LosslessScaling.exe"
                manager.config.global_hotkey.activation_delay_ms = 0
                automation.resolve_deployment_files = Mock(return_value=[])
                automation._desired_gpu_route = Mock(return_value=None)
                automation.deployment_manager.needs_change.return_value = restart
                automation.deployment_manager.runtime_packages_need_state.return_value = False
                automation._live_deployment_stale = Mock(side_effect=[True, False])
                automation.addon_configs.has_pending = Mock(return_value=False)
                automation.hotkey_trigger = Mock(return_value=True)
                automation._verify_gpu_after_scaling = Mock(return_value="confirmed")
                profile = Profile(id="game", name="The Game", auto_scale=True,
                                  addon_config={"global": {"auto_load": True}})
                self.assertTrue(automation.set_scaling(
                    True, profile, reason="focus", target_pid=42, target_hwnd=82))
                messages = list(dict.fromkeys(script.lines()))
                self.assertEqual(messages[:2], [
                    "Scaling requested: The Game", "Using profile: The Game",
                ])
                self.assertLess(messages.index("Applying addon configuration"),
                                messages.index("Scaling started: The Game"))
                self.assertEqual(script.borders()[-1], "active")

    def test_failed_setup_clears_pending_border(self):
        automation, _, _ = make_automation(disable_native_auto_scale=True)
        automation._apply_profile_transition = Mock(side_effect=RuntimeError("Deployment failed"))
        profile = Profile(id="game", name="The Game", auto_scale=True)
        automation.activate_profile(profile, target_pid=42, target_hwnd=82)
        self.assertIsNone(automation.overlay_indicator._state.border.state)

    def test_idle_profile_preparation_is_quiet_including_browser_route_addons_and_preset(self):
        for process, auto_scale, globally_enabled in (
            ("game.exe", False, True), ("game.exe", True, False),
            ("chrome.exe", False, True), ("chrome.exe", True, True)):
            for force in (False, True):
                with self.subTest(process=process, auto_scale=auto_scale, force=force):
                    automation, manager, script = make_automation(disable_native_auto_scale=True)
                    manager.config.lossless_scaling_exe_path = "C:/LS/LosslessScaling.exe"
                    automation.state.auto_scale_enabled = globally_enabled
                    automation.resolve_deployment_files = Mock(return_value=[])
                    automation._desired_gpu_route = Mock(return_value={"lsGpuId": 1, "lsDisplayId": 1})
                    automation.lossless_settings.gpu_route_changes_required.return_value = True
                    automation.lossless_settings.update_gpu_route.return_value = True
                    automation.deployment_manager.needs_change.return_value = True
                    automation.set_scaling = Mock()
                    profile = Profile(
                        id="profile", name="Profile", target_process=process, auto_scale=auto_scale,
                        addon_config={"global": {"auto_load": True}},
                        reshade=ReshadeConfig(enabled=True, preset_path="C:/LS/preset.ini"))
                    with patch("companion.services.automation.ReshadeManager.swap_preset", return_value=True):
                        automation.activate_profile(profile, force=force, target_pid=42, target_hwnd=82)
                    self.assertIs(automation.state.current_active_profile, profile)
                    self.assertEqual(script.lines(), [])
                    self.assertFalse(any(payload is not None for payload in script.seen))
                    automation.set_scaling.assert_not_called()

    def test_cancelled_focus_auto_scale_never_opens_overlay(self):
        automation, _, script = make_automation(disable_native_auto_scale=True)
        automation.process_watcher.target_is_foreground.return_value = False
        profile = Profile(id="game", name="Game", auto_scale=True)
        automation.activate_profile(profile, target_pid=42, target_hwnd=82)
        self.assertEqual(script.lines(), [])
        self.assertFalse(automation.state.is_scaling_active)

    def test_focus_auto_scale_is_quiet_until_it_calls_set_scaling(self):
        automation, manager, script = make_automation(disable_native_auto_scale=True)
        manager.config.global_hotkey.activation_delay_ms = 0
        automation._live_deployment_stale = Mock(return_value=False)
        automation.addon_configs.has_pending = Mock(return_value=False)
        automation.deployment_manager.runtime_packages_need_state.return_value = False
        automation.hotkey_trigger = Mock(return_value=True)
        automation._verify_gpu_after_scaling = Mock(return_value="confirmed")
        profile = Profile(id="game", name="The Game", auto_scale=True)
        scale = automation.set_scaling

        def begin(*args, **kwargs):
            self.assertEqual(script.lines(), [])
            return scale(*args, **kwargs)

        automation.set_scaling = Mock(side_effect=begin)
        automation.activate_profile(profile, target_pid=42, target_hwnd=82)
        automation.set_scaling.assert_called_once()
        self.assertIn("Scaling requested: The Game", script.lines())
        self.assertIn("Scaling started: The Game", script.lines())

    def test_hotkey_flash_and_border_pending(self):
        automation, _, script = make_automation(override_lossless_hotkey=True)
        automation.state.is_scaling_active = False
        automation.set_scaling = Mock(return_value=True)
        automation.handle_override_hotkey()
        self.assertTrue(any("Hotkey received" in line
                            for line in script.lines()))
        # Explicit user action requests the border immediately (visibility
        # itself is still gated by the game-window check).
        self.assertEqual(automation.overlay_indicator._state.border.state,
                         "pending")

    def test_active_border_skips_stability_wait(self):
        from companion.services.overlay_indicator import OverlayIndicator

        seen = []

        def fake_window(_hwnd):
            return (1920, 1080, "The Game")

        indicator = OverlayIndicator(presenter=seen.append,
                                     window_info=fake_window,
                                     stable_seconds=60.0)
        indicator.set_border("active", anchor_hwnd=77, now=1000.0)
        indicator.tick(now=1000.0)
        shown = [item for item in seen if item is not None]
        self.assertTrue(shown)
        self.assertEqual(shown[-1]["border"], "active")

    def test_pending_border_waits_by_default(self):
        from companion.services.overlay_indicator import OverlayIndicator

        seen = []

        def fake_window(_hwnd):
            return (1920, 1080, "The Game")

        indicator = OverlayIndicator(presenter=seen.append,
                                     window_info=fake_window,
                                     stable_seconds=60.0)
        indicator.set_border("pending", anchor_hwnd=77, now=1000.0)
        indicator.tick(now=1000.0)
        self.assertTrue(all(item is None for item in seen))
        indicator.set_border("pending", anchor_hwnd=77, now=1000.0,
                             immediate=True)
        indicator.tick(now=1000.0)
        shown = [item for item in seen if item is not None]
        self.assertTrue(shown)
        self.assertEqual(shown[-1]["border"], "pending")

    def test_confirm_shows_active_with_maroon_border(self):
        automation, _, script = make_automation()
        profile = Profile(id="game", name="The Game")
        with patch("companion.services.automation.InputSimulator"
                   ".trigger_hotkey", return_value=True):
            result = automation.set_scaling(
                True, profile, reason="manual_profile_switch",
                target_pid=42, target_hwnd=82)
        self.assertTrue(result)
        self.assertTrue(any(line.startswith("Scaling started")
                            for line in script.lines()))
        self.assertIn("active", script.borders())

    def test_deactivation_dismisses_all_cards_before_focusing_another_window(self):
        for reason in ("manual_profile_switch", "manual"):
            with self.subTest(reason=reason):
                automation, manager, script = make_automation()
                profile = Profile(id="game", name="The Game")
                automation.state.is_scaling_active = True
                automation.state.current_active_profile = profile
                indicator = automation.overlay_indicator
                indicator.log("Scaling started", anchor_hwnd=82)
                indicator.set_border("active", anchor_hwnd=82)
                indicator.set_reshade_context(True, anchor_hwnd=82)
                indicator.set_performance_graph({"fps": 60})
                self.assertIsNotNone(script.seen[-1])
                with patch("companion.services.automation.InputSimulator.trigger_hotkey", return_value=True), \
                     patch("companion.services.automation.threading.Thread"):
                    result = automation.set_scaling(
                        False, profile, reason=reason, target_pid=42, target_hwnd=82)
                self.assertTrue(result)
                self.assertIsNone(script.seen[-1])
                automation.activate_profile(None, target_pid=99, target_hwnd=199)
                indicator.tick(anchor_hwnd=199)
                self.assertIsNone(script.seen[-1])
                self.assertEqual(indicator._state.current, [])
                with patch("companion.services.automation.InputSimulator.trigger_hotkey", return_value=True):
                    self.assertTrue(automation.set_scaling(
                        True, profile, reason="manual_profile_switch", force=True,
                        target_pid=42, target_hwnd=82))
                self.assertEqual(script.seen[-1]["anchor_hwnd"], 82)
                self.assertTrue(script.seen[-1]["lines"])

    def test_native_stop_clears_toasts_and_a_new_native_start_can_show_them(self):
        automation, _, script = make_automation()
        automation.state.is_scaling_active = True
        indicator = automation.overlay_indicator
        indicator.log("Scaling started", anchor_hwnd=82)
        automation.reconcile_observed_scaling_state(False)
        self.assertIsNone(script.seen[-1])
        indicator.log("Late cleanup", anchor_hwnd=199)
        indicator.tick(anchor_hwnd=199)
        self.assertIsNone(script.seen[-1])
        automation.reconcile_observed_scaling_state(True, {"pid": 42, "hwnd": 82, "processName": "game.exe"})
        self.assertEqual(script.seen[-1]["anchor_hwnd"], 82)
        self.assertIn("Scaling started (native hotkey): game.exe", script.lines())

    def test_transition_logs_route_preset_addons(self):
        automation, _, script = make_automation()
        profile = Profile(id="game", name="Game",
                          lossless_profile_title="Gaming",
                          addon_config={"global": {"auto_load": True}})
        automation.activate_profile(profile, target_pid=42, target_hwnd=82, for_scaling=True)
        lines = script.lines()
        self.assertTrue(any("Applying addon configuration" in line
                            for line in lines), lines)


class OverlayHygieneTests(unittest.TestCase):
    """Default wiring must not spawn threads/windows (leaked pumps broke
    sleep-asserting tests via the globally-patched time.sleep)."""

    def test_ended_session_rejects_late_messages_until_another_scaling_event(self):
        seen = []
        indicator = OverlayIndicator(presenter=seen.append, window_info=lambda hwnd: (1920, 1080, "Game"))
        indicator.begin_scaling()
        indicator.log("Original session", anchor_hwnd=77, now=100.0)
        indicator.set_border("active", anchor_hwnd=77, now=100.0)
        indicator.set_reshade_context(True, anchor_hwnd=77)
        indicator.end_scaling()
        indicator.log("Overlay closed", anchor_hwnd=88, now=100.1)
        indicator.set_reshade_context(True, anchor_hwnd=77)
        indicator.set_border("active", anchor_hwnd=77, now=100.1)
        indicator.set_performance_graph({"fps": 60})
        indicator.tick(anchor_hwnd=88, now=100.2)
        self.assertIsNone(seen[-1])
        self.assertEqual(indicator._state.current, [])
        self.assertIsNone(indicator._reshade_context)
        self.assertIsNone(indicator._performance_graph)
        indicator.begin_scaling()
        indicator.log("New session", anchor_hwnd=99, now=101.0)
        self.assertEqual(seen[-1]["anchor_hwnd"], 99)
        self.assertEqual([line["text"] for line in seen[-1]["lines"]], ["New session"])
        self.assertEqual(seen[-1]["lines"][0]["expires_at"], 108.0)

    def _pump_threads(self):
        return [thread for thread in threading.enumerate()
                if thread.name == "LSOverlayPump"]

    def test_default_automation_overlay_is_inert(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = ProfileManager(Path(directory) / "config")
            manager.config.overlay_indicator_enabled = False
            watcher = Mock()
            before = set(self._pump_threads())
            automation = AutomationController(manager, AppState(), watcher)
            indicator = automation.overlay_indicator
            self.assertFalse(indicator._pump_enabled)
            self.assertIsNone(indicator._presenter)
            indicator.log("hello")
            indicator.set_border("armed", anchor_hwnd=123)
            indicator.tick()
            self.assertIsNone(indicator._presenter)
            indicator.stop()
            leaked = set(self._pump_threads()) - before
            self.assertEqual(leaked, set())

    def test_production_indicator_enables_pump(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = ProfileManager(Path(directory) / "config")
            indicator = overlay_indicator.production_indicator(manager)
            self.assertTrue(indicator._pump_enabled)
            self.assertTrue(indicator._enabled())
            manager.config.overlay_indicator_enabled = False
            self.assertFalse(indicator._enabled())

    def test_static_lines_represent_on_heartbeat(self):
        # Unchanged content still re-presents every REFOLLOW_SECONDS so the
        # presenter re-reads the live window rect (monitor switches).
        seen = []
        indicator = OverlayIndicator(presenter=seen.append)
        indicator.log("steady", now=100.0)
        first = len(seen)
        self.assertGreater(first, 0)
        indicator.tick(now=100.2)
        self.assertEqual(len(seen), first)
        indicator.tick(now=100.6)
        self.assertGreater(len(seen), first)

    def test_toast_keeps_target_window_on_heartbeat(self):
        seen = []
        indicator = OverlayIndicator(presenter=seen.append)
        indicator.log("targeted toast", anchor_hwnd=77, now=100.0)
        indicator.tick(now=100.6)
        self.assertEqual(seen[-1]["anchor_hwnd"], 77)

    def test_hide_clears_toast_target(self):
        seen = []
        indicator = OverlayIndicator(presenter=seen.append)
        indicator.log("targeted toast", anchor_hwnd=77, now=100.0)
        indicator.hide()
        indicator.log("untargeted toast", now=101.0)
        self.assertIsNone(seen[-1]["anchor_hwnd"])


@unittest.skipUnless(sys.platform == "win32", "Win32 overlay windows")
class OverlayWin32VisibilityTests(unittest.TestCase):
    """The layered-window implementation silently never showed anything
    (bad class registration, font signature, missing blend constants, and
    UpdateLayeredWindow error 87). This test creates the real windows and
    asserts they become visible; the opaque rewrite must keep it green."""

    def test_toast_and_border_use_their_own_anchors(self):
        from companion.services.overlay_window import Win32OverlayPresenter

        presenter = Win32OverlayPresenter()
        presenter._draw_pill = Mock()
        presenter._draw_border = Mock()
        presenter._hide_border = Mock()
        for border, border_anchor in ((None, None), ("active", 88)):
            presenter._pending = {
                "lines": [{"text": "targeted toast"}],
                "anchor_hwnd": 77,
                "border": border,
                "border_anchor_hwnd": border_anchor,
            }
            presenter._has_pending = True
            presenter._apply_pending()
            self.assertEqual(presenter._draw_pill.call_args.args[1], 77)
        self.assertEqual(presenter._draw_border.call_args.args[1], 88)

    def test_pill_and_border_become_visible(self):
        from companion.services import overlay_window as overlay_window
        user32 = ctypes.windll.user32
        presenter = overlay_window.Win32OverlayPresenter()
        try:
            self.assertTrue(presenter._create_windows())
            presenter._draw_pill(
                [{"text": "visibility probe", "accent": (0, 200, 255)}], None)
            self.assertTrue(presenter._usable)
            self.assertTrue(presenter._pill_visible)
            self.assertTrue(bool(user32.IsWindowVisible(
                ctypes.c_void_p(presenter._hwnd))))
            rect = overlay_window._Rect()
            self.assertTrue(presenter._user32.GetWindowRect(
                ctypes.c_void_p(presenter._hwnd), ctypes.byref(rect)))
            self.assertEqual((rect.right - rect.left, rect.bottom - rect.top),
                             (presenter._px(overlay_window.WIDTH), presenter._px(overlay_window.HEIGHT)))
            presenter._draw_border(
                "pending", int(user32.GetDesktopWindow()), False)
            self.assertTrue(presenter._border_visible)
            self.assertTrue(bool(user32.IsWindowVisible(
                ctypes.c_void_p(presenter._border_hwnd))))
        finally:
            presenter._teardown_windows()
        self.assertFalse(bool(user32.IsWindow(
            ctypes.c_void_p(presenter._hwnd or 0))))


if __name__ == "__main__":
    unittest.main()
