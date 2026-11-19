"""Opt-in acceptance check using the local LS archive, never the user's clone.

PowerShell: $env:LSC_TEST_CLEAN_INSTALL_LIVE='1'; python -m unittest tests.test_clean_install_live -v
"""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock
import zipfile

from companion.core.profile_manager import ProfileManager
from companion.core.state import AppState
from companion.services.asset_store import AssetStore
from companion.services.automation import AutomationController
from companion.services.clean_install import clean_install
from companion.services.managed_runtime import ManagedRuntime, await_isolation, launch_environment

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == 'nt' and os.environ.get('LSC_TEST_CLEAN_INSTALL_LIVE') == '1',
                     'Opt in to launching an isolated real LS from ls.zip')
class CleanInstallLiveTests(unittest.TestCase):
    def test_recloned_real_ls_starts_with_fresh_private_settings(self):
        with tempfile.TemporaryDirectory(prefix='clean-install-live-', dir=ROOT / 'build') as directory:
            root = Path(directory).resolve()
            self.assertTrue(root.is_relative_to(ROOT / 'build'))
            source = root / 'source'
            with zipfile.ZipFile(ROOT / 'ls.zip') as bundle:
                for info in bundle.infolist():
                    destination = (source / info.filename).resolve()
                    self.assertTrue(destination.is_relative_to(source))
                bundle.extractall(source)
            manager = ProfileManager(root / 'config')
            runtime = ManagedRuntime(manager.config_dir)
            store = AssetStore(root / 'assets')
            executable = runtime.prepare(source / 'LosslessScaling.exe', store, fresh_settings=True)
            runtime.bind(manager, executable, store)
            manager.config.lossless_source_exe_path = str(source / 'LosslessScaling.exe')
            manager.save_config()
            runtime.settings.write_text('<Settings><OldSetting>remove me</OldSetting></Settings>')
            (executable.parent / 'Lossless.dll').write_bytes(b'corrupted old engine')
            (executable.parent / 'old-addon.ini').write_text('old customization')
            watcher = Mock()
            watcher.check_is_lossless_scaling_running.return_value = False
            state = AppState()
            automation = AutomationController(manager, state, process_watcher=watcher, asset_store=store)
            result = clean_install(automation, Mock())
            self.assertTrue(result['ok'])
            executable = Path(manager.config.lossless_scaling_exe_path)
            self.assertFalse((executable.parent / 'old-addon.ini').exists())
            self.assertNotIn('OldSetting', runtime.settings.read_text())
            environment, receipt, token = launch_environment(executable)
            log_path = ROOT / 'build/clean-install-live-startup.log'
            with log_path.open('w', encoding='utf-8') as log:
                process = subprocess.Popen([str(executable)], cwd=executable.parent, env=environment,
                    stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
                try:
                    await_isolation(process.pid, receipt, token)
                    user32 = ctypes.WinDLL('user32', use_last_error=True)
                    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
                    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
                    user32.EnumWindows.restype = wintypes.BOOL
                    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
                    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
                    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
                    titles = []
                    @callback_type
                    def collect(hwnd, _):
                        pid = wintypes.DWORD()
                        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                        if pid.value == process.pid:
                            title = ctypes.create_unicode_buffer(256)
                            user32.GetWindowTextW(hwnd, title, len(title))
                            if title.value:
                                titles.append(title.value)
                        return True
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline and process.poll() is None:
                        user32.EnumWindows(collect, 0)
                        if any('lossless' in title.casefold() for title in titles):
                            break
                        time.sleep(.1)
                    self.assertIsNone(process.poll(), f'Fresh LS exited: see {log_path}')
                    self.assertTrue(any('lossless' in title.casefold() for title in titles),
                                    f'LS did not initialize its UI: {titles}; see {log_path}')
                    self.assertNotIn('OldSetting', runtime.settings.read_text())
                finally:
                    if process.poll() is None:
                        process.terminate()
                    process.wait(timeout=10)
