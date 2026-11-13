"""A second LS installation or the add-on panel cannot confirm managed scaling."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from companion.services.ls_inspector import LosslessScalingInspector


class ScalingInstanceScopeTests(unittest.TestCase):
    def overlay(self, windows, *, configured='C:/Managed/LS/LosslessScaling.exe'):
        def handle(value):
            return value.value if hasattr(value, 'value') else value

        api = Mock()
        api.IsWindow.return_value = True
        api.IsWindowVisible.return_value = True
        by_handle = {entry['hwnd']: entry for entry in windows}

        def enumerate_windows(callback, argument):
            for entry in windows:
                if not callback(entry['hwnd'], argument):
                    break
            return True

        def pid(hwnd, pointer):
            pointer._obj.value = by_handle[handle(hwnd)]['pid']

        def rect(hwnd, pointer):
            pointer._obj.left = pointer._obj.top = 0
            pointer._obj.right, pointer._obj.bottom = 1920, 1080
            return True

        def window_class(hwnd, buffer, _):
            buffer.value = by_handle[handle(hwnd)].get('class', 'LSRenderSurface')
            return len(buffer.value)

        api.EnumWindows.side_effect = enumerate_windows
        api.GetWindowThreadProcessId.side_effect = pid
        api.GetWindowRect.side_effect = rect
        api.GetClassNameW.side_effect = window_class
        api.GetWindowLongW.side_effect = lambda hwnd, kind: by_handle[handle(hwnd)].get('style', 0x80000000) if kind == -16 else 8
        processes = {entry['pid']: Mock(exe=Mock(return_value=entry['exe']))
                     for entry in windows}
        for process in processes.values():
            process.name = Mock(return_value='LosslessScaling.exe')
        with patch('companion.services.ls_inspector.user32', api), patch(
            'companion.services.ls_inspector.psutil.Process', side_effect=lambda process_id: processes[process_id]
        ), patch.object(LosslessScalingInspector, '_window_title', return_value=''):
            return LosslessScalingInspector.detect_lossless_scaling_overlay_window(configured)

    def test_source_scaling_and_managed_panel_do_not_count_as_managed_capture(self):
        windows = [dict(hwnd=101, pid=11, exe='C:/Steam/LS/LosslessScaling.exe'),
                   dict(hwnd=102, pid=12, exe='C:/Managed/LS/LosslessScaling.exe', **{'class': 'LSAddonManagerClass'})]
        self.assertEqual(self.overlay(windows), (False, None))
        windows.append(dict(hwnd=103, pid=12, exe='C:/Managed/LS/LosslessScaling.exe'))
        self.assertEqual(self.overlay(windows), (True, 103))
        self.assertEqual(self.overlay(windows, configured=None), (True, 101))

    def test_topmost_settings_windows_and_framed_or_embedded_panels_are_excluded(self):
        for window in [dict(**{'class': 'HwndWrapper[LosslessScaling;;id]'}),
                       dict(style=0x80C00000), dict(style=0xC0000000),
                       dict(**{'class': 'LSAddonManagerClass'})]:
            with self.subTest(window=window):
                self.assertEqual(self.overlay([dict(hwnd=100, pid=10, exe='C:/Managed/LS/LosslessScaling.exe', **window)]), (False, None))

    def test_configured_inspector_ignores_foreign_and_global_scaling_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            managed, source, global_data = root / 'managed', root / 'source', root / 'global'
            for path in [managed / 'logs', source / 'logs', global_data / 'Lossless Scaling/logs']:
                path.mkdir(parents=True)
            local_log = managed / 'logs/scaling.log'
            local_log.write_text('')
            source_log = source / 'logs/scaling.log'
            global_log = global_data / 'Lossless Scaling/logs/scaling.log'
            for path in (source_log, global_log):
                path.write_text('')
            process = SimpleNamespace(info={'name': 'LosslessScaling.exe', 'exe': str(source / 'LosslessScaling.exe')})
            expand = lambda value: value.replace('%LOCALAPPDATA%', str(global_data)).replace('%APPDATA%', str(global_data))
            with patch('companion.services.ls_inspector.psutil.process_iter', return_value=[process]), patch(
                'companion.services.ls_inspector.os.path.expandvars', side_effect=expand
            ), patch.object(LosslessScalingInspector, 'detect_lossless_scaling_overlay_window', return_value=(False, None)) as detect:
                inspector = LosslessScalingInspector(lossless_exe_path=str(managed / 'LosslessScaling.exe'))
                self.assertEqual(inspector._candidate_log_files(), [local_log])
                for path in (source_log, global_log):
                    with path.open('a') as stream:
                        stream.write('Scaling started; PID=42; Proc=game.exe\n')
                self.assertFalse(inspector.inspect_current_scaling_target(fallback_foreground=False).is_active)
                detect.assert_called_with(str(managed / 'LosslessScaling.exe'))
                with local_log.open('a') as stream:
                    stream.write('Scaling started; PID=42; Proc=game.exe\n')
                self.assertTrue(inspector.inspect_current_scaling_target(fallback_foreground=False).is_active)
