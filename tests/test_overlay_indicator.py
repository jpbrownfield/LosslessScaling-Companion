"""Overlay pill + border: log terminal, game-like gating, wiring."""
import ctypes
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from companion.core.models import Profile
from companion.core.profile_manager import ProfileManager
from companion.core.state import AppState
from companion.services import overlay_indicator
from companion.services.automation import AutomationController
from companion.services.overlay_indicator import OverlayIndicator, OverlayState


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
        state.tick(now=101.9)
        self.assertEqual([item.text for item in state.current],
                         ["first", "second"])
        state.tick(now=102.1)
        self.assertEqual([item.text for item in state.current], ["second"])

    def test_errors_linger_longer(self):
        state = OverlayState()
        state.log("boom", kind="error", now=100.0)
        state.tick(now=103.5)
        self.assertEqual(len(state.current), 1)
        state.tick(now=104.5)
        self.assertEqual(state.current, [])

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
    def test_activate_logs_detection_and_config(self):
        automation, _, script = make_automation()
        previous = Profile(id="a", name="Browser", auto_scale=False)
        current = Profile(id="b", name="The Game", auto_scale=True,
                          lossless_profile_title="Gaming")
        automation.state.current_active_profile = previous
        automation.activate_profile(current, target_pid=42, target_hwnd=82)
        lines = script.lines()
        self.assertTrue(any("Game window detected" in line for line in lines),
                        lines)
        self.assertTrue(any("Configuring for profile: The Game" in line
                            for line in lines), lines)
        self.assertIn("pending", script.borders())

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

    def test_deactivation_clears_border(self):
        automation, manager, script = make_automation()
        profile = Profile(id="game", name="The Game")
        automation.state.is_scaling_active = True
        automation.state.current_active_profile = profile
        with patch("companion.services.automation.InputSimulator"
                   ".trigger_hotkey", return_value=True):
            result = automation.set_scaling(
                False, profile, reason="manual_profile_switch",
                target_pid=42, target_hwnd=82)
        self.assertTrue(result)
        self.assertIn(None, script.borders())

    def test_transition_logs_route_preset_addons(self):
        automation, _, script = make_automation()
        profile = Profile(id="game", name="Game",
                          lossless_profile_title="Gaming",
                          addon_config={"global": {"auto_load": True}})
        automation.activate_profile(profile, target_pid=42, target_hwnd=82)
        lines = script.lines()
        self.assertTrue(any("Applying addon configuration" in line
                            for line in lines), lines)


class OverlayHygieneTests(unittest.TestCase):
    """Default wiring must not spawn threads/windows (leaked pumps broke
    sleep-asserting tests via the globally-patched time.sleep)."""

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


@unittest.skipUnless(sys.platform == "win32", "Win32 overlay windows")
class OverlayWin32VisibilityTests(unittest.TestCase):
    """The layered-window implementation silently never showed anything
    (bad class registration, font signature, missing blend constants, and
    UpdateLayeredWindow error 87). This test creates the real windows and
    asserts they become visible; the opaque rewrite must keep it green."""

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
            presenter._draw_border(
                "pending", int(user32.GetDesktopWindow()), False)
            self.assertTrue(presenter._border_visible)
            self.assertTrue(bool(user32.IsWindowVisible(
                ctypes.c_void_p(presenter._border_hwnd))))
        finally:
            presenter.close()
        self.assertFalse(bool(user32.IsWindow(
            ctypes.c_void_p(presenter._hwnd or 0))))


if __name__ == "__main__":
    unittest.main()
