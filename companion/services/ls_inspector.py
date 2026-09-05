"""
Lossless Scaling Inspector & Log Parser.
Monitors Lossless Scaling's log files and Win32 DirectX overlay to determine:
1. Whether Lossless Scaling is actively scaling right now.
2. The exact executable, PID, window title, and resolution being scaled.
3. Live scaling metrics (LSFG frame generation, capture mode, errors).
"""

import os
import re
import glob
import logging
import ctypes
from ctypes import wintypes
from pathlib import Path
from typing import Optional, Dict, List, Tuple
import psutil
from .ls_settings import LosslessSettingsXml

logger = logging.getLogger("LosslessCompanion.LSInspector")

user32 = ctypes.windll.user32


class ScaledTargetInfo:
    def __init__(
        self,
        is_active: bool = False,
        process_name: Optional[str] = None,
        pid: Optional[int] = None,
        window_title: Optional[str] = None,
        exe_path: Optional[str] = None,
        capture_mode: Optional[str] = None,
        scale_factor: Optional[str] = None,
        source: str = "unknown"
    ):
        self.is_active = is_active
        self.process_name = process_name
        self.pid = pid
        self.window_title = window_title
        self.exe_path = exe_path
        self.capture_mode = capture_mode
        self.scale_factor = scale_factor
        self.source = source

    def to_dict(self) -> Dict:
        return {
            "isActive": self.is_active,
            "processName": self.process_name,
            "pid": self.pid,
            "windowTitle": self.window_title,
            "exePath": self.exe_path,
            "captureMode": self.capture_mode,
            "scaleFactor": self.scale_factor,
            "detectionSource": self.source
        }


class LosslessScalingInspector:
    def __init__(
        self,
        custom_log_path: Optional[str] = None,
        settings_xml_path: Optional[str] = None,
    ):
        self.custom_log_path = custom_log_path
        self.active_log_file: Optional[Path] = None
        self._last_log_pos: int = 0
        self.last_known_target = ScaledTargetInfo()
        self.settings_xml = LosslessSettingsXml(settings_xml_path)
        self._find_log_file()

    def _find_log_file(self) -> Optional[Path]:
        """Locates the active Lossless Scaling log file."""
        if self.custom_log_path and Path(self.custom_log_path).exists():
            self.active_log_file = Path(self.custom_log_path)
            return self.active_log_file

        candidate_patterns = [
            os.path.expandvars(r"%LOCALAPPDATA%\LosslessScaling\*.log"),
            os.path.expandvars(r"%LOCALAPPDATA%\LosslessScaling\logs\*.log"),
            os.path.expandvars(r"%LOCALAPPDATA%\LosslessScaling\log.txt"),
            os.path.expandvars(r"%APPDATA%\LosslessScaling\*.log"),
            r"C:\Program Files (x86)\Steam\steamapps\common\Lossless Scaling\*.log",
            r"C:\Program Files (x86)\Steam\steamapps\common\Lossless Scaling\log.txt"
        ]

        found_files = []
        for pattern in candidate_patterns:
            for f in glob.glob(pattern):
                p = Path(f)
                if p.is_file():
                    found_files.append((p, p.stat().st_mtime))

        if found_files:
            # Pick the most recently modified log file
            found_files.sort(key=lambda x: x[1], reverse=True)
            self.active_log_file = found_files[0][0]
            logger.info(f"Detected Lossless Scaling log file: {self.active_log_file}")
            return self.active_log_file

        return None

    def parse_log_updates(self) -> Optional[ScaledTargetInfo]:
        """Tails the Lossless Scaling log file for new scaling events."""
        if not self.active_log_file or not self.active_log_file.exists():
            self._find_log_file()
            if not self.active_log_file:
                return None

        try:
            current_size = self.active_log_file.stat().st_size
            if current_size < self._last_log_pos:
                # Log was rotated or cleared
                self._last_log_pos = 0

            if current_size == self._last_log_pos:
                return None

            with open(self.active_log_file, "r", encoding="utf-8", errors="ignore") as f:
                f.seek(self._last_log_pos)
                new_lines = f.readlines()
                self._last_log_pos = f.tell()

            # Parse lines for target window, process, and scaling state
            for line in new_lines:
                line_str = line.strip()
                # Pattern examples:
                # "Scaling started for window: [Title] (PID: 1234, Proc: chrome.exe)"
                # "Target HWND: 0x00010203 - Window: YouTube - Google Chrome"
                # "Capture method: WGC, Scaling mode: LS1"
                # "Scaling stopped"
                if re.search(r"scaling\s+(started|active|init)", line_str, re.IGNORECASE):
                    self.last_known_target.is_active = True
                    self.last_known_target.source = "log_file"

                    # Check for window title
                    win_match = re.search(r"(?:window|title)[:=]\s*['\"]?([^'\"\n\r]+)['\"]?", line_str, re.IGNORECASE)
                    if win_match:
                        self.last_known_target.window_title = win_match.group(1).strip()

                    # Check for process
                    proc_match = re.search(r"(?:proc|process|exe)[:=]\s*['\"]?([a-zA-Z0-9_\-\.]+\.exe)['\"]?", line_str, re.IGNORECASE)
                    if proc_match:
                        self.last_known_target.process_name = proc_match.group(1).strip()

                    # Check for PID
                    pid_match = re.search(r"PID[:=]\s*(\d+)", line_str, re.IGNORECASE)
                    if pid_match:
                        self.last_known_target.pid = int(pid_match.group(1))

                elif re.search(r"scaling\s+(stopped|terminated|closed|ended)", line_str, re.IGNORECASE):
                    self.last_known_target.is_active = False
                    self.last_known_target.source = "log_file"

                capture_match = re.search(r"capture(?:\s+(?:method|api))?[:=]\s*([A-Za-z0-9_-]+)", line_str, re.IGNORECASE)
                if capture_match:
                    self.last_known_target.capture_mode = capture_match.group(1)
                scale_match = re.search(r"scal(?:ing|e)(?:\s+(?:mode|factor))?[:=]\s*([A-Za-z0-9_.-]+)", line_str, re.IGNORECASE)
                if scale_match:
                    self.last_known_target.scale_factor = scale_match.group(1)

            return self.last_known_target

        except Exception as e:
            logger.debug(f"Error reading Lossless Scaling log: {e}")
            return None

    @staticmethod
    def detect_lossless_scaling_overlay_window() -> Tuple[bool, Optional[int]]:
        """
        Detects whether Lossless Scaling has instantiated its DirectX top-most
        presentation overlay window. Returns (is_overlay_active, overlay_hwnd).
        """
        overlay_found = False
        overlay_hwnd = None

        # Enum all top-level windows looking for LosslessScaling.exe's render overlay
        def enum_cb(hwnd, extra):
            nonlocal overlay_found, overlay_hwnd
            if not user32.IsWindowVisible(hwnd):
                return True

            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if not pid.value:
                return True

            try:
                proc = psutil.Process(pid.value)
                if proc.name().lower() == "losslessscaling.exe":
                    # Check window style: overlay windows typically have WS_POPUP (0x80000000) or WS_EX_TOPMOST (0x00000008)
                    style = user32.GetWindowLongW(hwnd, -16)  # GWL_STYLE
                    ex_style = user32.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE

                    # Exclude the main configuration GUI window if minimized/standard
                    length = user32.GetWindowTextLengthW(hwnd)
                    buff = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buff, length + 1)
                    title = buff.value

                    # The render overlay is borderless and covers monitor bounds
                    if (ex_style & 0x00000008) or (style & 0x80000000) or "Overlay" in title:
                        overlay_found = True
                        overlay_hwnd = hwnd
                        return False
            except Exception:
                pass
            return True

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows(WNDENUMPROC(enum_cb), 0)

        return overlay_found, overlay_hwnd

    def inspect_current_scaling_target(
        self,
        fallback_foreground: bool = True,
        settings_profile_title: Optional[str] = None,
    ) -> ScaledTargetInfo:
        """
        Combines log parsing + Win32 overlay checks + foreground tracking
        to accurately report what application is being scaled.
        """
        # 1. Check Win32 DirectX overlay
        is_overlay_present, _ = self.detect_lossless_scaling_overlay_window()
        
        # 2. Check logs for new events
        log_info = self.parse_log_updates()

        # 3. Formulate target report
        target = ScaledTargetInfo()
        target.is_active = is_overlay_present or (log_info.is_active if log_info else self.last_known_target.is_active)
        target.capture_mode = self.last_known_target.capture_mode
        target.scale_factor = self.last_known_target.scale_factor
        target.source = "overlay_window" if is_overlay_present else ("log_file" if log_info else "unknown")

        settings_profile = self.settings_xml.get_profile(settings_profile_title)
        if settings_profile:
            target.capture_mode = target.capture_mode or settings_profile.get("CaptureApi")
            target.scale_factor = target.scale_factor or settings_profile.get("ScaleFactor") or settings_profile.get("ScalingType")

        if self.last_known_target.process_name:
            target.process_name = self.last_known_target.process_name
            target.window_title = self.last_known_target.window_title
            target.pid = self.last_known_target.pid

        # If log didn't specify the PID/process, resolve from current/tracked foreground window
        if target.is_active and not target.process_name and fallback_foreground:
            hwnd = user32.GetForegroundWindow()
            if hwnd:
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value:
                    try:
                        proc = psutil.Process(pid.value)
                        target.pid = pid.value
                        target.process_name = proc.name()
                        target.exe_path = proc.exe()
                        
                        length = user32.GetWindowTextLengthW(hwnd)
                        buff = ctypes.create_unicode_buffer(length + 1)
                        user32.GetWindowTextW(hwnd, buff, length + 1)
                        target.window_title = buff.value
                        target.source = "win32_window_hook"
                    except Exception:
                        pass

        return target
