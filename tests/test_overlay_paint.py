"""Validate the native paint ABI and detect Win32 writes beyond its allocation."""
import ctypes
import sys
import unittest


@unittest.skipUnless(sys.platform == "win32", "Win32 paint ABI")
class OverlayPaintTests(unittest.TestCase):
    def test_paint_layout_matches_windows_sdk(self):
        from companion.services.overlay_window import _PaintStruct

        pointer_size = ctypes.sizeof(ctypes.c_void_p)
        self.assertEqual(ctypes.sizeof(_PaintStruct), 72 if pointer_size == 8 else 64)
        self.assertEqual(_PaintStruct.update.offset, 32 if pointer_size == 8 else 28)
        self.assertEqual(_PaintStruct.reserved.offset, 36 if pointer_size == 8 else 32)

    def test_begin_paint_does_not_overwrite_following_memory(self):
        from companion.services.overlay_window import _PaintStruct, _user32

        # Keep extra allocated memory beyond the canary even with the old,
        # undersized structure: reproducing the bug must not corrupt Python.
        class GuardedPaint(ctypes.Structure):
            _fields_ = [("paint", _PaintStruct), ("canary", ctypes.c_ubyte * 64)]

        user32 = _user32()["user32"]
        # A hidden, private STATIC window. No focus, overlays, or game interaction.
        hwnd = user32.CreateWindowExW(
            0, "STATIC", "LS Companion paint ABI test", 0x80000000,
            0, 0, 32, 32, None, None, None, None,
        )
        self.assertTrue(hwnd)
        try:
            for _ in range(20):
                guarded = GuardedPaint()
                guarded.canary[:] = b"\xa5" * 64
                user32.InvalidateRect(hwnd, None, True)
                hdc = user32.BeginPaint(hwnd, ctypes.byref(guarded.paint))
                self.assertTrue(hdc)
                try:
                    self.assertEqual(bytes(guarded.canary), b"\xa5" * 64,
                                     "BeginPaint wrote past the PAINTSTRUCT allocation")
                finally:
                    user32.EndPaint(hwnd, ctypes.byref(guarded.paint))
        finally:
            user32.DestroyWindow(hwnd)


if __name__ == "__main__":
    unittest.main()
