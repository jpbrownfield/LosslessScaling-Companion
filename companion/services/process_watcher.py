"""
Process and Window monitoring service.
Monitors foreground window transitions and enumerates running applications for profiling.
"""

import os
import ctypes
from ctypes import wintypes
import logging
from typing import List, Dict, Optional, Tuple
import psutil
import subprocess

from ..core.profile_manager import ProfileManager
from ..core.state import AppState

logger = logging.getLogger("LosslessCompanion.ProcessWatcher")

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

class ProcessInfo:
    def __init__(self, pid: int, name: str, exe_path: str, title: str):
        self.pid = pid
        self.name = name
        self.exe_path = exe_path
        self.title = title

    def to_dict(self) -> Dict:
        return {
            "pid": self.pid,
            "name": self.name,
            "exePath": self.exe_path,
            "title": self.title
        }


class ProcessWatcher:
    def __init__(self, profile_manager: ProfileManager, state: AppState):
        self.profile_manager = profile_manager
        self.state = state
        self._last_foreground_pid: Optional[int] = None

    @staticmethod
    def get_foreground_window_info() -> Optional[ProcessInfo]:
        """Gets info for the currently focused foreground window."""
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None

        length = user32.GetWindowTextLengthW(hwnd)
        buff = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buff, length + 1)
        title = buff.value

        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return None

        try:
            proc = psutil.Process(pid.value)
            exe_path = proc.exe()
            name = proc.name()
            return ProcessInfo(pid=pid.value, name=name, exe_path=exe_path, title=title)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return None

    @staticmethod
    def list_running_executables(filter_system: bool = True) -> List[Dict]:
        """
        Lists interactive running processes for user profile selection.
        """
        results = []
        ignored_system = {
            'svchost.exe', 'system', 'registry', 'smss.exe', 'csrss.exe',
            'wininit.exe', 'services.exe', 'lsass.exe', 'winlogon.exe',
            'fontdrvhost.exe', 'dwm.exe', 'runtimebroker.exe', 'searchhost.exe',
            'taskhostw.exe', 'sihost.exe', 'ctfmon.exe', 'conhost.exe'
        }

        seen_names = set()
        for p in psutil.process_iter(['pid', 'name', 'exe']):
            try:
                name = p.info['name']
                if not name:
                    continue
                name_lower = name.lower()
                if filter_system and name_lower in ignored_system:
                    continue
                if name_lower in seen_names:
                    continue

                exe = p.info.get('exe') or ""
                seen_names.add(name_lower)
                results.append({
                    "pid": p.info['pid'],
                    "name": name,
                    "exePath": exe
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        results.sort(key=lambda x: x['name'].lower())
        return results

    def check_is_lossless_scaling_running(self) -> bool:
        """Checks if LosslessScaling.exe is currently active."""
        for p in psutil.process_iter(['name']):
            try:
                if p.info['name'] and p.info['name'].lower() == 'losslessscaling.exe':
                    self.state.lossless_scaling_running = True
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        self.state.lossless_scaling_running = False
        return False

    def launch_lossless_scaling_if_needed(self) -> bool:
        """Launches Lossless Scaling if auto-launch is configured and it's not running."""
        if self.check_is_lossless_scaling_running():
            return True

        cfg = self.profile_manager.config
        if not cfg.auto_launch_lossless_scaling or not cfg.lossless_scaling_exe_path:
            return False

        if os.path.exists(cfg.lossless_scaling_exe_path):
            try:
                logger.info(f"Auto-launching Lossless Scaling: {cfg.lossless_scaling_exe_path}")
                subprocess.Popen([cfg.lossless_scaling_exe_path], close_fds=True)
                self.state.lossless_scaling_running = True
                return True
            except Exception as e:
                logger.error(f"Failed to launch Lossless Scaling: {e}")
        return False

    def check_foreground_and_update(self) -> Optional[ProcessInfo]:
        """Polls the foreground window, updates state, and detects profile switches."""
        info = self.get_foreground_window_info()
        if not info:
            return None

        if info.pid != self._last_foreground_pid:
            self._last_foreground_pid = info.pid
            self.state.current_foreground_process = info.name
            self.state.current_foreground_window_title = info.title
            
            # Match profile if process changed
            matched = self.profile_manager.match_profile(process_name=info.name)
            if matched and matched != self.state.current_active_profile:
                self.state.current_active_profile = matched
                logger.info(f"Switched active profile to: '{matched.name}' for {info.name}")

        return info
