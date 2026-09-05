"""
Process and Window monitoring service.
Monitors foreground window transitions and enumerates running applications for profiling.
"""

import ctypes
from ctypes import wintypes
import logging
import time
from typing import Callable, List, Dict, Optional, Tuple
import psutil
import subprocess
from pathlib import Path

from ..core.profile_manager import ProfileManager
from ..core.state import AppState
from .ls_settings import LosslessSettingsXml

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
    def __init__(
        self,
        profile_manager: ProfileManager,
        state: AppState,
        on_profile_changed: Optional[Callable] = None,
        lossless_settings: Optional[LosslessSettingsXml] = None,
    ):
        self.profile_manager = profile_manager
        self.state = state
        self._last_foreground_pid: Optional[int] = None
        self.on_profile_changed = on_profile_changed
        self.lossless_settings = lossless_settings

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
    def get_processes_with_visible_windows() -> Dict[int, str]:
        """
        Enumerates all top-level visible non-cloaked windows that have non-empty titles
        and standard window sizes, returning a map of PID -> Window Title.
        """
        pid_to_title: Dict[int, str] = {}

        # DWM Cloaked window attribute constant
        DWMWA_CLOAKED = 14
        dwmapi = None
        try:
            dwmapi = ctypes.windll.dwmapi
        except Exception:
            pass

        def enum_window_callback(hwnd, extra):
            # Check basic visibility
            if not user32.IsWindowVisible(hwnd):
                return True

            # Ignore tooltips, dialogs without taskbar presence or zero-sized windows
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            width = rect.right - rect.left
            height = rect.bottom - rect.top
            if width <= 100 or height <= 100:
                return True

            # Check DWM Cloaked state (virtual desktop / UWP suspended background apps)
            if dwmapi:
                cloaked = ctypes.c_int(0)
                res = dwmapi.DwmGetWindowAttribute(
                    hwnd,
                    ctypes.c_uint(DWMWA_CLOAKED),
                    ctypes.byref(cloaked),
                    ctypes.sizeof(cloaked)
                )
                if res == 0 and cloaked.value != 0:
                    return True

            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True

            buff = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buff, length + 1)
            title = buff.value.strip()

            if not title:
                return True

            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value and pid.value not in pid_to_title:
                pid_to_title[pid.value] = title

            return True

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows(WNDENUMPROC(enum_window_callback), 0)
        return pid_to_title

    @classmethod
    def list_running_executables(cls, visible_windows_only: bool = True, filter_system: bool = True) -> List[Dict]:
        """
        Lists interactive running processes for user profile selection.
        If visible_windows_only=True, only includes processes with real visible GUI windows.
        """
        results = []
        ignored_system = {
            'svchost.exe', 'system', 'registry', 'smss.exe', 'csrss.exe',
            'wininit.exe', 'services.exe', 'lsass.exe', 'winlogon.exe',
            'fontdrvhost.exe', 'dwm.exe', 'runtimebroker.exe', 'searchhost.exe',
            'taskhostw.exe', 'sihost.exe', 'ctfmon.exe', 'conhost.exe',
            'losslessscaling.exe', 'applicationframehost.exe', 'shellexperiencehost.exe'
        }

        # Step 1: Query visible top-level windows
        visible_window_map = cls.get_processes_with_visible_windows()

        seen_names = set()
        for p in psutil.process_iter(['pid', 'name', 'exe']):
            try:
                pid = p.info['pid']
                name = p.info['name']
                if not name:
                    continue
                name_lower = name.lower()

                if filter_system and name_lower in ignored_system:
                    continue

                # If filtering for active windows, require the PID to own a visible GUI window
                has_window = pid in visible_window_map
                if visible_windows_only and not has_window:
                    continue

                if name_lower in seen_names:
                    continue

                exe = p.info.get('exe') or ""
                window_title = visible_window_map.get(pid, "")

                seen_names.add(name_lower)
                results.append({
                    "pid": pid,
                    "name": name,
                    "exePath": exe,
                    "windowTitle": window_title,
                    "hasVisibleWindow": has_window
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        results.sort(key=lambda x: (not x.get('hasVisibleWindow', False), x['name'].lower()))
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

    def stop_lossless_scaling(self, timeout: float = 5.0) -> bool:
        """Stop every running Lossless Scaling process before changing its DLL set."""
        processes = []
        for process in psutil.process_iter(["name"]):
            try:
                if process.info["name"] and process.info["name"].casefold() == "losslessscaling.exe":
                    processes.append(process)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        if not processes:
            self.state.lossless_scaling_running = False
            return True

        logger.info("Stopping %d Lossless Scaling process(es) for DLL profile change", len(processes))
        stoppable = []
        access_denied = False
        for process in processes:
            try:
                process.terminate()
                stoppable.append(process)
            except psutil.NoSuchProcess:
                continue
            except psutil.AccessDenied as exc:
                access_denied = True
                logger.error(
                    "Cannot stop Lossless Scaling PID %s; run the companion at the same or higher privilege level: %s",
                    process.pid,
                    exc,
                )

        _, alive = psutil.wait_procs(stoppable, timeout=timeout)
        for process in alive:
            try:
                logger.warning("Lossless Scaling PID %s did not exit; forcing termination", process.pid)
                process.kill()
            except psutil.NoSuchProcess:
                continue
            except psutil.AccessDenied as exc:
                access_denied = True
                logger.error("Cannot force-stop Lossless Scaling PID %s: %s", process.pid, exc)
        _, alive = psutil.wait_procs(alive, timeout=timeout) if alive else ([], [])

        still_running = self.check_is_lossless_scaling_running()
        if access_denied or alive or still_running:
            logger.error("Lossless Scaling is still running; DLL profile change was aborted")
            return False
        self.state.is_scaling_active = False
        self.state.scaling_owner_profile_id = None
        self.state.scaling_trigger = None
        return True

    def launch_lossless_scaling(self, *, force: bool = False) -> bool:
        """Launch Lossless Scaling, optionally ignoring the auto-launch preference."""
        if self.check_is_lossless_scaling_running():
            return True

        cfg = self.profile_manager.config
        if (not force and not cfg.auto_launch_lossless_scaling) or not cfg.lossless_scaling_exe_path:
            return False

        if cfg.disable_native_auto_scale and self.lossless_settings:
            native_enabled = self.lossless_settings.native_auto_scale_enabled()
            if native_enabled and not self.lossless_settings.disable_native_auto_scale():
                logger.error("Lossless Scaling launch aborted: native Auto Scale could not be disabled")
                return False

        executable = Path(cfg.lossless_scaling_exe_path)
        if not executable.is_file():
            logger.error("Lossless Scaling executable not found: %s", executable)
            return False

        try:
            logger.info("Launching Lossless Scaling: %s", executable)
            subprocess.Popen([str(executable)], cwd=str(executable.parent), close_fds=True)
            self.state.lossless_scaling_running = True
            # Allow the process to create its window and register global hotkeys before
            # profile runtime actions are sent.
            time.sleep(1.0)
            return True
        except Exception as exc:
            logger.error("Failed to launch Lossless Scaling: %s", exc)
            self.state.lossless_scaling_running = False
            return False

    def enforce_helper_scaling_control(self) -> bool:
        """Synchronize hotkey/Auto Scale with at most one Lossless Scaling restart."""
        config = self.profile_manager.config
        if not self.lossless_settings:
            logger.warning("Lossless Scaling settings ownership cannot be enforced without Settings.xml access")
            return False

        desired_hotkey = config.global_hotkey
        native_hotkey = self.lossless_settings.read_hotkey()
        if config.hotkey_sync_mode == "follow_lossless":
            if native_hotkey is None:
                logger.warning("Native Lossless Scaling hotkey is unavailable")
                return False
            desired_hotkey = native_hotkey.model_copy(
                update={
                    "hold_delay_ms": config.global_hotkey.hold_delay_ms,
                    "activation_delay_ms": config.global_hotkey.activation_delay_ms,
                }
            )
            if desired_hotkey != config.global_hotkey:
                config.global_hotkey = desired_hotkey
                self.profile_manager.save_config()

        inspected = self.lossless_settings.control_changes_required(
            hotkey=desired_hotkey,
            disable_auto_scale=config.disable_native_auto_scale,
        )
        if inspected is None:
            logger.warning("Lossless Scaling control settings are unavailable")
            return False

        hotkey_mismatch = inspected["hotkey"]
        should_write_hotkey = config.hotkey_sync_mode == "helper_controls_lossless"
        if config.hotkey_sync_mode == "warn_only" and hotkey_mismatch:
            logger.warning("Helper and Lossless Scaling activation hotkeys differ")

        needs_write = inspected["auto_scale"] or (should_write_hotkey and hotkey_mismatch)
        if not needs_write:
            return True

        was_running = self.check_is_lossless_scaling_running()
        if was_running and not self.stop_lossless_scaling():
            return False

        updated = self.lossless_settings.update_control_settings(
            hotkey=desired_hotkey if should_write_hotkey else None,
            disable_auto_scale=config.disable_native_auto_scale,
        )
        restarted = True
        if was_running:
            restarted = self.launch_lossless_scaling(force=True)
        return updated is not None and restarted

    def launch_lossless_scaling_if_needed(self) -> bool:
        """Launches Lossless Scaling if auto-launch is configured and it's not running."""
        return self.launch_lossless_scaling()

    def check_foreground_and_update(self) -> Optional[ProcessInfo]:
        """Polls the foreground window, updates state, and detects profile switches."""
        info = self.get_foreground_window_info()
        if not info:
            return None

        if info.pid != self._last_foreground_pid:
            self._last_foreground_pid = info.pid
            self.state.current_foreground_process = info.name
            self.state.current_foreground_window_title = info.title
            self.state.current_foreground_exe_path = info.exe_path
            
            # Match profile if process changed
            matched = self.profile_manager.match_target_profile(
                process_name=info.name, executable_path=info.exe_path
            )
            if matched and matched != self.state.current_active_profile:
                logger.info(f"Switched active profile to: '{matched.name}' for {info.name}")
            if self.on_profile_changed:
                self.on_profile_changed(matched, info.exe_path)
            else:
                self.state.current_active_profile = matched

        return info
