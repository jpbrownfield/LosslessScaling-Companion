"""Animation continuity, real gradient drawing, and native resource ownership."""
import ctypes
import sys
import unittest
from unittest.mock import patch


@unittest.skipUnless(sys.platform == "win32", "Win32 border renderer")
class BorderAnimationTests(unittest.TestCase):
    def test_perimeter_wrap_has_no_color_jump(self):
        from companion.services.overlay_window import _border_light, PENDING_COLOR
        for elapsed in (0, 0.5, 2.6, 5.1999, 5.2, 15):
            self.assertEqual(_border_light(PENDING_COLOR, 0, elapsed),
                             _border_light(PENDING_COLOR, 1, elapsed))
            before = _border_light(PENDING_COLOR, 0.5, elapsed)
            after = _border_light(PENDING_COLOR, 0.50001, elapsed)
            self.assertLessEqual(max(abs(a - b) for a, b in zip(before, after)), 1)

    def test_state_change_eases_between_blue_and_burgundy(self):
        from companion.services.overlay_window import (
            _border_tint, PENDING_COLOR, ACTIVE_COLOR, BORDER_COLOR_SECONDS,
        )
        self.assertEqual(_border_tint(PENDING_COLOR, ACTIVE_COLOR, 0), PENDING_COLOR)
        self.assertEqual(_border_tint(PENDING_COLOR, ACTIVE_COLOR, BORDER_COLOR_SECONDS), ACTIVE_COLOR)
        for step in range(21):
            color = _border_tint(PENDING_COLOR, ACTIVE_COLOR, step * BORDER_COLOR_SECONDS / 20)
            for start, end, channel in zip(PENDING_COLOR, ACTIVE_COLOR, color):
                self.assertTrue(min(start, end) <= channel <= max(start, end))

    def test_gradient_structures_match_native_abi(self):
        from companion.services.overlay_window import _GradientVertex, _GradientTriangle
        self.assertEqual(ctypes.sizeof(_GradientVertex), 16)
        self.assertEqual(ctypes.sizeof(_GradientTriangle), 12)
        self.assertEqual(_GradientVertex.red.offset, 8)
        self.assertEqual(_GradientVertex.alpha.offset, 14)

    def test_rounded_region_has_connected_corners_and_wider_stroke(self):
        from companion.services import overlay_window as overlay

        presenter = overlay.Win32OverlayPresenter()
        gdi = presenter._gdi32
        gdi.PtInRegion.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        gdi.PtInRegion.restype = ctypes.c_bool
        region = presenter._rounded_border_region(640, 360)
        try:
            for point in ((320, 7), (7, 180), (10, 10), (630, 10), (630, 350), (10, 350)):
                self.assertTrue(gdi.PtInRegion(region, *point), point)
            for point in ((0, 0), (639, 0), (639, 359), (0, 359), (320, 8), (320, 180)):
                self.assertFalse(gdi.PtInRegion(region, *point), point)
        finally:
            gdi.DeleteObject(region)

    def test_mesh_closes_without_position_or_color_seams(self):
        from companion.services import overlay_window as overlay

        presenter = overlay.Win32OverlayPresenter()
        for width, height in ((120, 120), (640, 360), (3840, 2160)):
            samples, points, _triangles = overlay._border_mesh(width, height)
            self.assertEqual(samples[0][1:], samples[-1][1:])
            self.assertEqual(points[:6], points[-6:])
            self.assertTrue(all(a[0] < b[0] for a, b in zip(samples, samples[1:])))
            vertices, _triangles = presenter._border_gradients(width, height, overlay.PENDING_COLOR, 1.2)
            self.assertEqual(b"".join(bytes(vertex) for vertex in vertices[:6]),
                             b"".join(bytes(vertex) for vertex in vertices[-6:]))

    def test_native_animation_preserves_center_and_releases_gdi_resources(self):
        from companion.services import overlay_window as overlay

        presenter = overlay.Win32OverlayPresenter()
        user32, gdi32, _kernel = overlay._handles()
        user32.GetGuiResources.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        user32.GetGuiResources.restype = ctypes.c_ulong
        gdi32.GetPixel.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        gdi32.GetPixel.restype = ctypes.c_ulong
        hwnd = user32.CreateWindowExW(0, "STATIC", "Border drawing test", 0x80000000,
                                     0, 0, 640, 360, None, None, None, None)
        self.assertTrue(hwnd)
        screen = user32.GetDC(hwnd)
        target = gdi32.CreateCompatibleDC(screen)
        bitmap = gdi32.CreateCompatibleBitmap(screen, 640, 360)
        original = gdi32.SelectObject(target, bitmap)
        brush = gdi32.CreateSolidBrush(presenter._rgb((12, 18, 28)))
        user32.FillRect(target, ctypes.byref(overlay._Rect(0, 0, 640, 360)), brush)
        gdi32.DeleteObject(brush)
        center = gdi32.GetPixel(target, 320, 180)
        presenter._border_anim_t0 = 100
        presenter._border_color_t0 = 0
        samples = set()
        try:
            with patch.object(overlay._time, "monotonic", return_value=100):
                presenter._paint_border_frame(target, hwnd, overlay.PENDING_COLOR)
            self.assertTrue(presenter._usable)
            for x, y in ((0, 0), (639, 0), (639, 359), (0, 359)):
                self.assertEqual(gdi32.GetPixel(target, x, y), center)
            # Check every segment around the entire ribbon, including arcs:
            # shared vertices must leave no black gaps between the four sides.
            path_samples, _points, _triangles = overlay._border_mesh(640, 360)
            for _position, x, y, nx, ny in path_samples:
                pixel = gdi32.GetPixel(target, round(x + nx * 4), round(y + ny * 4))
                self.assertNotIn(pixel, (0, center), (x, y))
            warm_handles = user32.GetGuiResources(ctypes.c_void_p(-1), 0)
            for frame in range(90):
                with patch.object(overlay._time, "monotonic", return_value=100 + frame / 30):
                    presenter._paint_border_frame(target, hwnd, overlay.PENDING_COLOR)
                self.assertTrue(presenter._usable)
                self.assertEqual(gdi32.GetPixel(target, 320, 180), center)
                samples.add(gdi32.GetPixel(target, 240, 2))
            self.assertGreater(len(samples), 10, "The light trail must actually animate")
            self.assertEqual(user32.GetGuiResources(ctypes.c_void_p(-1), 0), warm_handles)
            for width, height in ((620, 340), (640, 360)) * 5:
                user32.SetWindowPos(hwnd, None, 0, 0, width, height, 0x0014)
                presenter._paint_border_frame(target, hwnd, overlay.ACTIVE_COLOR)
            self.assertTrue(presenter._usable)
            self.assertEqual(user32.GetGuiResources(ctypes.c_void_p(-1), 0), warm_handles)
            presenter._release_border_buffer()
            self.assertEqual(user32.GetGuiResources(ctypes.c_void_p(-1), 0), warm_handles - 3)
        finally:
            presenter._release_border_buffer()
            gdi32.SelectObject(target, original)
            gdi32.DeleteObject(bitmap)
            gdi32.DeleteDC(target)
            user32.ReleaseDC(hwnd, screen)
            user32.DestroyWindow(hwnd)


if __name__ == "__main__":
    unittest.main()
