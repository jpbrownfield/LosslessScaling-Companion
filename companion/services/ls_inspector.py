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

logger = logging.getLogger("LSCompanion.LSInspector")

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
    # Window titles/classes that are part of LS itself or OS helpers and must
    # never count as the scaling presentation overlay. A bare WS_POPUP /
    # WS_EX_TOPMOST check matches tooltips, IME windows and 1x1 GDI+
    # helpers, which caused single-frame "detected" flicker.
    _OVERLAY_EXCLUDED_TITLE_SUBSTRINGS = (
        "lossless scaling",
        "gdi+ window",
        "cicero",
        "msctfime",
        "default ime",
        "mediacontext",
        "systemresourcenotify",
        "wpfui_th_",
    )
    # Minimum size for a real fullscreen presentation overlay. Anything
    # smaller is a tooltip, menu, or helper window.
    _OVERLAY_MIN_WIDTH = 800
    _OVERLAY_MIN_HEIGHT = 600
    # Log basenames that are never LS scaling-event logs (Special K DXGI /
    # module dumps, ReShade logs, hook/proxy diagnostics). Picking one of
    # these as "the LS log" poisons parse state, so they are skipped during
    # discovery.
    _LOG_EXCLUDED_NAME_SUBSTRINGS = (
        "dxgi",
        "modules",
        "reshade",
        "specialk",
        "special k",
        "hook",
        "proxy",
        "presentmon",
        "rtss",
    )

    def __init__(
        self,
        custom_log_path: Optional[str] = None,
        settings_xml_path: Optional[str] = None,
        lossless_exe_path: Optional[str] = None,
    ):
        self.custom_log_path = custom_log_path
        self.lossless_exe_path = lossless_exe_path
        self.active_log_file: Optional[Path] = None
        self._last_log_pos: int = 0
        self.last_known_target = ScaledTargetInfo()
        self.settings_xml = LosslessSettingsXml(settings_xml_path)
        self._overlay_true_streak = 0
        self._overlay_false_streak = 0
        self._overlay_stable_present = False
        self._find_log_file()

    @staticmethod
    def _lossless_exe_directories(configured_exe: Optional[str] = None) -> List[Path]:
        """Collect candidate LS install dirs: configured path + running process."""
        directories: List[Path] = []
        if configured_exe:
            try:
                parent = Path(os.path.expandvars(configured_exe)).parent
                if parent and str(parent) not in {str(p) for p in directories}:
                    directories.append(parent)
            except Exception:
                pass
        try:
            for process in psutil.process_iter(["name", "exe"]):
                try:
                    if (process.info.get("name") or "").casefold() == "losslessscaling.exe":
                        exe = process.info.get("exe")
                        if not exe:
                            try:
                                exe = process.exe()
                            except Exception:
                                exe = None
                        if exe:
                            parent = Path(exe).parent
                            if str(parent) not in {str(p) for p in directories}:
                                directories.append(parent)
                            break
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except Exception:
            pass
        return directories

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
        ]
        # Resolve the real install dir instead of a hardcoded C:\ Steam path
        # (the user's install may live on another drive, e.g. J:\Steam).
        for directory in self._lossless_exe_directories(self.lossless_exe_path):
            candidate_patterns.append(str(directory / "*.log"))
            candidate_patterns.append(str(directory / "log.txt"))
            candidate_patterns.append(str(directory / "logs" / "*.log"))

        found_files = []
        for pattern in candidate_patterns:
            for f in glob.glob(pattern):
                p = Path(f)
                name_lower = p.name.casefold()
                if any(key in name_lower for key in self._LOG_EXCLUDED_NAME_SUBSTRINGS):
                    continue
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
            saw_state_transition = False
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
                    saw_state_transition = True

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
                    saw_state_transition = True

                capture_match = re.search(r"capture(?:\s+(?:method|api))?[:=]\s*([A-Za-z0-9_-]+)", line_str, re.IGNORECASE)
                if capture_match:
                    self.last_known_target.capture_mode = capture_match.group(1)
                scale_match = re.search(r"scal(?:ing|e)(?:\s+(?:mode|factor))?[:=]\s*([A-Za-z0-9_.-]+)", line_str, re.IGNORECASE)
                if scale_match:
                    self.last_known_target.scale_factor = scale_match.group(1)

            # Only report a log observation when new scaling state actually
            # appeared. Reporting latched state on every no-op poll is what
            # kept "unknown" sources from ever clearing.
            if saw_state_transition:
                return self.last_known_target
            return None

        except Exception as e:
            logger.debug(f"Error reading Lossless Scaling log: {e}")
            return None

    @classmethod
    def _window_title(cls, hwnd) -> str:
        try:
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return ""
            buff = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buff, length + 1)
            return buff.value or ""
        except Exception:
            return ""

    @classmethod
    def detect_lossless_scaling_overlay_window(cls) -> Tuple[bool, Optional[int]]:
        """
        Detects whether Lossless Scaling has instantiated its DirectX top-most
        presentation overlay window. Returns (is_overlay_active, overlay_hwnd).

        The overlay must be a large borderless/topmost window owned by
        LosslessScaling.exe. Small helper windows (tooltips, IME, 1x1 GDI+),
        the "Lossless Scaling" config GUI, and invisible non-presentation
        windows are excluded so a transient popup can't flash a
        single-frame "detected".
        """
        overlay_found = False
        overlay_hwnd = None

        # Enum all top-level windows looking for LosslessScaling.exe's render overlay
        def enum_cb(hwnd, extra):
            nonlocal overlay_found, overlay_hwnd
            # The HWND must be a live window that is actually visible right
            # now. Zombie handles from a torn-down overlay (GetWindowRect /
            # style reads returning zeros) previously matched on rect + style
            # alone and flipped detection for a single poll.
            if not hwnd or not user32.IsWindow(hwnd) or not user32.IsWindowVisible(hwnd):
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if not pid.value:
                return True

            try:
                proc = psutil.Process(pid.value)
                if (proc.name() or "").casefold() != "losslessscaling.exe":
                    return True
            except Exception:
                return True

            try:
                style = user32.GetWindowLongW(hwnd, -16)  # GWL_STYLE
                ex_style = user32.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
                if not style and not ex_style:
                    # Stale/torn-down handle: real windows always report style bits.
                    return True
                title = cls._window_title(hwnd)
                title_lower = title.casefold()
                if any(key in title_lower for key in cls._OVERLAY_EXCLUDED_TITLE_SUBSTRINGS):
                    return True

                rect = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                width = rect.right - rect.left
                height = rect.bottom - rect.top
                if width < cls._OVERLAY_MIN_WIDTH or height < cls._OVERLAY_MIN_HEIGHT:
                    return True

                # Check window style: overlay windows typically have WS_POPUP (0x80000000) or WS_EX_TOPMOST (0x00000008)
                style = user32.GetWindowLongW(hwnd, -16)  # GWL_STYLE
                ex_style = user32.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE

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

    # Consecutive agreeing polls required before a raw observation flips
    # the reported state. One stray HWND match no longer flashes "active"
    # for a single monitor tick.
    _OVERLAY_ACTIVATE_STREAK = 2
    _OVERLAY_DEACTIVATE_STREAK = 3

    def _stable_overlay_present(self, raw_present: bool) -> bool:
        if raw_present:
            self._overlay_true_streak += 1
            self._overlay_false_streak = 0
        else:
            self._overlay_false_streak += 1
            self._overlay_true_streak = 0
        if self._overlay_stable_present:
            if self._overlay_false_streak >= self._OVERLAY_DEACTIVATE_STREAK:
                self._overlay_stable_present = False
        elif self._overlay_true_streak >= self._OVERLAY_ACTIVATE_STREAK:
            self._overlay_stable_present = True
        return self._overlay_stable_present

    def inspect_current_scaling_target(
        self,
        fallback_foreground: bool = True,
        settings_profile_title: Optional[str] = None,
    ) -> ScaledTargetInfo:
        """
        Combines log parsing + Win32 overlay checks + foreground tracking
        to accurately report what application is being scaled.
        """
        # 1. Check Win32 DirectX overlay (debounced: a single-frame match
        # must not flip the reported state)
        raw_overlay_present, _ = self.detect_lossless_scaling_overlay_window()
        is_overlay_present = self._stable_overlay_present(raw_overlay_present)

        # 2. Check logs for new events
        log_info = self.parse_log_updates()

        # 3. Formulate target report. Log state is only trusted when the log
        # actually produced a fresh observation; otherwise the overlay
        # decides. Never latch a stale log "active" across polls, which is
        # what kept the UI flickering between detected and idle.
        log_active = log_info.is_active if log_info is not None else None
        if log_active is not None:
            target_active = log_active or is_overlay_present
        else:
            target_active = is_overlay_present
        target = ScaledTargetInfo()
        target.is_active = target_active
        target.capture_mode = self.last_known_target.capture_mode
        target.scale_factor = self.last_known_target.scale_factor
        if is_overlay_present:
            target.source = "overlay_window"
        elif log_info is not None:
            target.source = "log_file"
        else:
            target.source = "unknown"

        settings_profile = self.settings_xml.get_profile(settings_profile_title)
        if settings_profile:
            target.capture_mode = target.capture_mode or settings_profile.get("CaptureApi")
            target.scale_factor = target.scale_factor or settings_profile.get("ScaleFactor") or settings_profile.get("ScalingType")

        if self.last_known_target.process_name:
            target.process_name = self.last_known_target.process_name
            target.window_title = self.last_known_target.window_title
            target.pid = self.last_known_target.pid

        # If log didn't specify the PID/process, resolve from current/tracked foreground window
        # Only for authoritative sources (overlay / fresh log event). Falling
        # back to "whatever is focused" on an unknown/idle poll is what
        # stamped VS Code (or any foreground app) as the scaling target and
        # flipped the UI to detected for a frame.
        if (
            target.is_active
            and target.source in ("overlay_window", "log_file")
            and (not target.process_name or not target.pid)
            and fallback_foreground
        ):
            hwnd = user32.GetForegroundWindow()
            if hwnd:
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value:
                    try:
                        proc = psutil.Process(pid.value)
                        if target.process_name and proc.name().casefold() != target.process_name.casefold():
                            return target
                        target.pid = pid.value
                        target.process_name = target.process_name or proc.name()
                        target.exe_path = proc.exe()
                        
                        length = user32.GetWindowTextLengthW(hwnd)
                        buff = ctypes.create_unicode_buffer(length + 1)
                        user32.GetWindowTextW(hwnd, buff, length + 1)
                        target.window_title = buff.value
                        target.source = "win32_window_hook"
                    except Exception:
                        pass

        return target
