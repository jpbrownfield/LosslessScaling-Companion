"""Resolution-scaled native surfaces, layout, and font rendering."""
import ctypes
import sys
import unittest
from unittest.mock import Mock, patch

from companion.services import overlay_window as overlay


@unittest.skipUnless(sys.platform == "win32", "Win32 overlay sizing")
class OverlayResolutionTests(unittest.TestCase):
    def presenter(self, resolution):
        presenter = overlay.Win32OverlayPresenter()
        presenter._user32 = Mock()
        presenter._gdi32 = Mock()
        presenter._toast_hwnds = [10, 11]
        presenter._header_hwnd, presenter._hint_hwnd, presenter._graph_hwnd = 12, 13, 14

        def monitor_info(monitor, pointer):
            info = pointer._obj
            info.rcMonitor = overlay._Rect(0, 0, *resolution)
            info.rcWork = overlay._Rect(0, 0, resolution[0], resolution[1] - 40)
            return True

        presenter._user32.GetMonitorInfoW.side_effect = monitor_info
        presenter._user32.GetWindowRect.side_effect = lambda hwnd, pointer: (
            setattr(pointer._obj, "right", resolution[0]) or
            setattr(pointer._obj, "bottom", resolution[1]) or True)
        presenter._user32.IsIconic.return_value = False
        return presenter

    def test_1080p_1440p_4k_and_portrait_have_proportional_sizes(self):
        for resolution, scale in (((1920, 1080), 1), ((2560, 1440), 4 / 3),
                                  ((3840, 2160), 2), ((2160, 3840), 2)):
            with self.subTest(resolution=resolution):
                presenter = self.presenter(resolution)
                presenter._update_ui_scale(99)
                self.assertAlmostEqual(presenter._ui_scale, scale)
                presenter._place_pill(99)
                self.assertEqual(presenter._toast_origin,
                                 (resolution[0] - round(436 * scale), round(48 * scale)))
                if scale != 1:
                    sizes = [call.args[4:6] for call in presenter._user32.SetWindowPos.call_args_list]
                    self.assertIn((round(420 * scale), round(52 * scale)), sizes)
                    self.assertIn((round(156 * scale), round(32 * scale)), sizes)

    def test_4k_hints_graph_and_toast_spacing_follow_same_scale(self):
        presenter = self.presenter((3840, 2160))
        presenter._hwnd = 10
        with patch.object(overlay._time, "monotonic", return_value=100):
            presenter._draw_pill([{"id": "a", "text": "First", "persistent": True},
                                  {"id": "b", "text": "Second", "persistent": True}], 99)
        presenter._animate_toasts(101)
        positions = [call.args[3] for call in presenter._user32.SetWindowPos.call_args_list
                     if call.args[0].value in (10, 11) and call.args[6] & overlay.SWP_NOSIZE]
        self.assertEqual(positions[-2:], [96, 216])
        presenter._draw_hints([{"key": "Delete", "label": "Edit profile"}], 99)
        hint = presenter._user32.SetWindowPos.call_args.args
        self.assertEqual(hint[2:6], (3840 - 712, 2160 - 148, 680, 100))
        presenter._draw_graph({"samples": []}, 99)
        graph = presenter._user32.SetWindowPos.call_args.args
        self.assertEqual(graph[2:4], (48, 2160 - 396))

    def test_mixed_monitor_move_resizes_existing_windows_without_changing_toast_age(self):
        presenter = self.presenter((3840, 2160))
        presenter._draw_pill([{"id": "a", "text": "Still fading"}], 99)
        created_at = presenter._toast_stack.toasts[0].created_at
        presenter._user32.GetMonitorInfoW.side_effect = self.presenter((1920, 1080))._user32.GetMonitorInfoW.side_effect
        presenter._place_pill(99)
        self.assertEqual(presenter._ui_scale, 1)
        self.assertEqual(presenter._toast_stack.toasts[0].created_at, created_at)
        self.assertIn((420, 52), [call.args[4:6] for call in presenter._user32.SetWindowPos.call_args_list])

    def test_native_4k_paint_doubles_glyph_size_and_restores_dc_mapping(self):
        from PIL import Image

        class BitmapInfo(ctypes.Structure):
            _fields_ = [("size", ctypes.c_ulong), ("width", ctypes.c_long), ("height", ctypes.c_long),
                        ("planes", ctypes.c_ushort), ("bits", ctypes.c_ushort), ("compression", ctypes.c_ulong),
                        ("image_size", ctypes.c_ulong), ("xppm", ctypes.c_long), ("yppm", ctypes.c_long),
                        ("used", ctypes.c_ulong), ("important", ctypes.c_ulong)]

        presenter = overlay.Win32OverlayPresenter()
        u, g, _ = overlay._handles()
        g.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint,
                               ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
        g.GetDIBits.restype = ctypes.c_int
        g.GetMapMode.argtypes = [ctypes.c_void_p]
        g.GetMapMode.restype = ctypes.c_int
        screen = u.GetDC(None)
        dc = g.CreateCompatibleDC(screen)
        bitmap = g.CreateCompatibleBitmap(screen, 840, 104)
        previous = g.SelectObject(dc, bitmap)
        presenter._user32 = Mock(wraps=u)
        presenter._user32.BeginPaint.return_value = dc
        presenter._user32.EndPaint.return_value = True
        presenter._toast_lines[99] = {"text": "Scaling started: Game"}
        bounds = []
        try:
            for scale in (1, 2):
                presenter._ui_scale = scale
                presenter._paint_window(99)
                self.assertTrue(presenter._usable)
                self.assertEqual(g.GetMapMode(dc), 1)
                pixels = ctypes.create_string_buffer(840 * 104 * 4)
                info = BitmapInfo(ctypes.sizeof(BitmapInfo), 840, -104, 1, 32)
                g.SelectObject(dc, previous)
                self.assertTrue(g.GetDIBits(dc, bitmap, 0, 104, pixels, ctypes.byref(info), 0))
                g.SelectObject(dc, bitmap)
                picture = Image.frombytes("RGB", (840, 104), pixels.raw, "raw", "BGRX")
                picture = picture.crop((0, 0, round(420 * scale), round(52 * scale)))
                # Text is the only bright foreground; the card and marker are dark.
                mask = picture.convert("L").point(lambda value: 255 if value > 160 else 0)
                bounds.append(mask.getbbox())
                self.assertEqual(picture.getpixel((round(416 * scale), round(26 * scale))), overlay.BG_COLOR)
            width = lambda box: box[2] - box[0]
            height = lambda box: box[3] - box[1]
            self.assertAlmostEqual(width(bounds[1]) / width(bounds[0]), 2, delta=0.1)
            self.assertAlmostEqual(height(bounds[1]) / height(bounds[0]), 2, delta=0.25)
        finally:
            g.SelectObject(dc, previous)
            g.DeleteObject(bitmap)
            g.DeleteDC(dc)
            u.ReleaseDC(None, screen)
