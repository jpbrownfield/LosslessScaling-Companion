"""Independent fades, stable lifetimes, animated stacking, and real Win32 cards."""
import ctypes
import sys
import threading
import time
import unittest
from unittest.mock import patch

from companion.services.toast_stack import ToastStack, TOAST_HEIGHT, TOAST_GAP


def line(key, created=100.0):
    return {"id": key, "text": "Scaling started", "accent": (16, 192, 16),
            "created_at": created, "expires_at": created + 5}


class ToastStackTests(unittest.TestCase):
    def test_fades_continuously_for_five_seconds(self):
        stack = ToastStack()
        toast = stack.sync([line(1)], 100)[0]
        self.assertEqual(toast.alpha(100), 255)
        self.assertEqual(toast.alpha(102.5), 128)
        self.assertEqual(toast.alpha(105), 0)
        self.assertEqual(stack.frames(105), [])

    def test_heartbeat_does_not_restart_fade_or_movement(self):
        stack = ToastStack()
        toast = stack.sync([line(1)], 100)[0]
        self.assertIs(stack.sync([line(1)], 102)[0], toast)
        self.assertEqual(toast.created_at, 100)
        self.assertEqual(toast.alpha(102), 153)
        self.assertEqual(toast.y(102), 0)

    def test_expiry_slides_lower_cards_up_without_a_jump(self):
        stack = ToastStack()
        stack.sync([line(1), line(2, 101)], 101)
        second = stack.frames(104.9)[1]
        step = TOAST_HEIGHT + TOAST_GAP
        self.assertEqual(second.y(104.9), step)
        self.assertEqual(stack.frames(105), [second])
        self.assertEqual(second.y(105), step)
        self.assertGreater(second.y(105.12), 0)
        self.assertLess(second.y(105.12), step)
        self.assertAlmostEqual(second.y(105.24), 0)

    def test_retarget_mid_slide_preserves_position(self):
        stack = ToastStack()
        stack.sync([line(1), line(2, 100.1), line(3, 100.2)], 101)
        third = stack.frames(105)[1]
        before = third.y(105.1)
        self.assertEqual(stack.frames(105.1), [third])
        self.assertEqual(third.y(105.1), before)

    def test_identical_text_keeps_separate_cards(self):
        stack = ToastStack()
        toasts = stack.sync([line(1), line(2, 101)], 101)
        self.assertEqual(len(toasts), 2)
        self.assertEqual([toast.key for toast in toasts], [1, 2])
        self.assertNotEqual(toasts[0].alpha(102), toasts[1].alpha(102))

    def test_burst_keeps_newest_five_and_hide_clears_stack(self):
        stack = ToastStack()
        toasts = stack.sync([line(key) for key in range(8)], 100)
        self.assertEqual([toast.key for toast in toasts], [3, 4, 5, 6, 7])
        stack.clear()
        self.assertEqual(stack.frames(100), [])


@unittest.skipUnless(sys.platform == "win32", "Win32 toast opacity")
class NativeToastTests(unittest.TestCase):
    def test_presenter_pump_animates_without_a_border_or_heartbeats(self):
        from companion.services import overlay_window as overlay

        presenter = overlay.Win32OverlayPresenter()
        now = time.monotonic()
        item = line(1, now)
        item["expires_at"] = now + 0.6
        try:
            presenter({"lines": [item], "border": None})
            deadline = now + 2
            while not presenter._toast_shown and time.monotonic() < deadline:
                threading.Event().wait(0.01)
            self.assertTrue(presenter._toast_shown)
            handles = [*presenter._toast_hwnds, presenter._header_hwnd]
            deadline = time.monotonic() + 2
            while presenter._toast_shown and time.monotonic() < deadline:
                threading.Event().wait(0.01)
            self.assertFalse(presenter._toast_shown)
            self.assertFalse(presenter._toast_timer)
            self.assertFalse(presenter._header_visible)
            self.assertIsNotNone(presenter._header_icon)
            self.assertFalse(presenter._border_visible)
            self.assertTrue(presenter._usable)
        finally:
            presenter.close()
        self.assertTrue(all(not presenter._user32.IsWindow(hwnd) for hwnd in handles))

    def test_independent_windows_fade_reflow_and_release_handles(self):
        from companion.services import overlay_window as overlay

        presenter = overlay.Win32OverlayPresenter()
        u = presenter._user32
        u.GetLayeredWindowAttributes.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                                ctypes.POINTER(ctypes.c_ubyte), ctypes.c_void_p]
        u.GetLayeredWindowAttributes.restype = ctypes.c_bool
        handles = []
        try:
            self.assertTrue(presenter._create_windows())
            handles = [*presenter._toast_hwnds, presenter._header_hwnd, presenter._border_hwnd]
            with patch.object(overlay._time, "monotonic", return_value=101):
                presenter._draw_pill([line(1), line(2, 101)], None)
            self.assertTrue(presenter._usable)
            self.assertEqual(len(presenter._toast_shown), 2)
            self.assertTrue(presenter._toast_timer)
            self.assertTrue(presenter._header_visible)
            first, second = presenter._toast_slots[1], presenter._toast_slots[2]
            alpha = ctypes.c_ubyte()
            self.assertTrue(u.GetLayeredWindowAttributes(first, None, ctypes.byref(alpha), None))
            self.assertEqual(alpha.value, 204)
            self.assertTrue(u.GetLayeredWindowAttributes(second, None, ctypes.byref(alpha), None))
            self.assertEqual(alpha.value, 255)
            self.assertTrue(u.GetLayeredWindowAttributes(presenter._header_hwnd, None,
                                                        ctypes.byref(alpha), None))
            self.assertEqual(alpha.value, 255)
            presenter._animate_toasts(104.9)
            before = overlay._Rect()
            self.assertTrue(u.GetWindowRect(second, ctypes.byref(before)))
            self.assertEqual((before.right - before.left, before.bottom - before.top),
                             (presenter._px(420), presenter._px(52)))
            header = overlay._Rect()
            self.assertTrue(u.GetWindowRect(presenter._header_hwnd, ctypes.byref(header)))
            self.assertEqual(header.right, before.right)
            self.assertEqual(header.bottom, presenter._toast_origin[1] - presenter._px(overlay.HEADER_GAP))
            self.assertEqual((header.right - header.left, header.bottom - header.top),
                             (presenter._px(overlay.HEADER_WIDTH), presenter._px(overlay.HEADER_HEIGHT)))
            presenter._animate_toasts(105)
            self.assertNotIn(first, presenter._toast_shown)
            self.assertTrue(presenter._header_visible)
            presenter._animate_toasts(105.24)
            after = overlay._Rect()
            self.assertTrue(u.GetWindowRect(second, ctypes.byref(after)))
            self.assertEqual(before.top - after.top, presenter._px(TOAST_HEIGHT + TOAST_GAP))
            presenter._animate_toasts(106)
            self.assertFalse(presenter._toast_shown)
            self.assertFalse(presenter._toast_timer)
            self.assertFalse(presenter._header_visible)
        finally:
            presenter._teardown_windows()
        self.assertTrue(all(not u.IsWindow(hwnd) for hwnd in handles))

    def test_header_hides_with_toasts_while_scaling_border_stays_visible(self):
        from companion.services import overlay_window as overlay

        presenter = overlay.Win32OverlayPresenter()
        try:
            self.assertTrue(presenter._create_windows())
            with patch.object(overlay._time, "monotonic", return_value=100):
                presenter._draw_pill([line(1)], None)
            self.assertTrue(presenter._header_visible)
            presenter._pending = {"lines": [], "border": "active",
                                  "border_anchor_hwnd": int(ctypes.windll.user32.GetDesktopWindow())}
            presenter._has_pending = True
            presenter._apply_pending()
            self.assertFalse(presenter._header_visible)
            self.assertFalse(presenter._toast_shown)
            self.assertTrue(presenter._border_visible)
            self.assertTrue(presenter._usable)
        finally:
            presenter._teardown_windows()


if __name__ == "__main__":
    unittest.main()
