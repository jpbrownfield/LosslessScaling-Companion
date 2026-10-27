"""
System Tray interface for LS Companion using pystray and Pillow.
"""

import ctypes
from ctypes import wintypes
import logging
import math
import threading
import time
from typing import Callable, Optional
from pathlib import Path
from PIL import Image
import pystray
from pystray import MenuItem as item, Menu

from ..core.profile_manager import ProfileManager
from ..core.state import AppState
from ..services.process_watcher import ProcessWatcher
from ..services.ls_inspector import LosslessScalingInspector
from ..services.startup_manager import StartupTaskManager
from ..services.automation import AutomationController
from ..services.proxy_menu import AddonManagerMenuController
from .dashboard_window import close_dashboard_window, open_dashboard_window
from .icons import create_lightning_icon

logger = logging.getLogger("LSCompanion.Tray")


class _MonitorInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


class _MenuInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", wintypes.DWORD),
        ("dwStyle", wintypes.DWORD),
        ("cyMax", wintypes.UINT),
        ("hbrBack", wintypes.HBRUSH),
        ("dwContextHelpID", wintypes.DWORD),
        ("dwMenuData", ctypes.c_size_t),
    ]


class CompanionTrayIcon:
    WM_REFRESH_MENU = 0x8000 + 0x4C
    WM_ICON_FRAME = 0x8000 + 0x4D
    def __init__(
        self,
        profile_manager: ProfileManager,
        state: AppState,
        process_watcher: ProcessWatcher,
        ls_inspector: Optional[LosslessScalingInspector] = None,
        automation: Optional[AutomationController] = None,
        on_exit_callback: Optional[Callable] = None,
        startup_manager: Optional[StartupTaskManager] = None,
        on_check_updates_callback: Optional[Callable] = None,
        on_install_update_callback: Optional[Callable] = None,
        proxy_menu: Optional[AddonManagerMenuController] = None,
    ):
        self.profile_manager = profile_manager
        self.state = state
        self.process_watcher = process_watcher
        self.ls_inspector = ls_inspector
        self.automation = automation or AutomationController(profile_manager, state)
        self.on_exit_callback = on_exit_callback
        self.startup_manager = startup_manager
        self.on_check_updates_callback = on_check_updates_callback
        self.on_install_update_callback = on_install_update_callback
        self.proxy_menu = proxy_menu or getattr(process_watcher, "proxy_menu", None)
        self._update_status: Optional[dict] = None
        self._startup_enabled = bool(profile_manager.config.run_at_startup)
        if self.startup_manager:
            try:
                self._startup_enabled = self.startup_manager.is_enabled()
            except Exception:
                logger.exception("Could not query the startup task for the tray menu")
        self._startup_change_running = False
        self._update_check_running = False
        self._animation_stop = threading.Event()
        self._animation_thread: Optional[threading.Thread] = None
        self.icon: Optional[pystray.Icon] = None
        self._tray_dispatch_ready = False
        self._pending_icon_frame = None
        self._pending_menu_refresh = False

    def _visual_state(self) -> str:
        if not self.state.lossless_scaling_running:
            return "closed"
        if self.state.is_scaling_active:
            return "scaling"
        return "open"

    def _runtime_status_label(self) -> str:
        if self.state.lossless_scaling_running:
            return "LS Active"
        configured = self.profile_manager.config.lossless_scaling_exe_path
        if not configured or not Path(configured).is_file():
            return "LS Not Found"
        return "LS Disconnected"

    def _scaling_labels(self) -> tuple[str, str]:
        target = self.state.current_scaled_target or {}
        executable = target.get("processName") or Path(target.get("exePath") or "").name
        profile = self.state.current_active_profile
        return (
            f"Scaling: {executable or 'Unknown application'}",
            f"Profile: {profile.name if profile else 'Unknown profile'}",
        )

    def _tray_title(self) -> str:
        return "\n".join(self._scaling_labels()) if self.state.is_scaling_active else "LS Companion"

    @staticmethod
    def _pin_tray_menu_above_taskbar(native_icon) -> None:
        """Promote pystray's hidden popup owners before its menu is rendered."""
        user32 = ctypes.windll.user32
        hwnd_topmost = -1
        swp_nomove = 0x0002
        swp_nosize = 0x0001
        for attribute in ("_hwnd", "_menu_hwnd"):
            hwnd = getattr(native_icon, attribute, None)
            if hwnd:
                user32.SetWindowPos(
                    hwnd,
                    hwnd_topmost,
                    0,
                    0,
                    0,
                    0,
                    swp_nomove | swp_nosize,
                )

    @staticmethod
    def _tray_menu_placement(
        x: int, y: int, work: wintypes.RECT
    ) -> tuple[int, int, bool, bool, int]:
        """Choose an anchor and height wholly inside the monitor work area."""
        margin = 4
        left = int(work.left) + margin
        right = int(work.right) - margin
        top = int(work.top) + margin
        bottom = int(work.bottom) - margin
        if left >= right or top >= bottom:
            raise ValueError("Monitor work area is too small for a tray menu")
        x = max(left, min(int(x), right))
        y = max(top, min(int(y), bottom))
        align_right = x - left >= right - x
        align_bottom = y - top >= bottom - y
        available_height = (y - top) if align_bottom else (bottom - y)
        return x, y, align_right, align_bottom, max(1, available_height)

    @staticmethod
    def _work_area_at_cursor(point: wintypes.POINT) -> Optional[wintypes.RECT]:
        user32 = ctypes.windll.user32
        monitor_from_point = user32.MonitorFromPoint
        monitor_from_point.argtypes = (wintypes.POINT, wintypes.DWORD)
        monitor_from_point.restype = wintypes.HMONITOR
        monitor = monitor_from_point(point, 2)  # MONITOR_DEFAULTTONEAREST
        if not monitor:
            return None
        info = _MonitorInfo()
        info.cbSize = ctypes.sizeof(info)
        get_monitor_info = user32.GetMonitorInfoW
        get_monitor_info.argtypes = (wintypes.HMONITOR, ctypes.POINTER(_MonitorInfo))
        get_monitor_info.restype = wintypes.BOOL
        return info.rcWork if get_monitor_info(monitor, ctypes.byref(info)) else None

    def _show_tray_menu_in_work_area(self, native_icon, win32) -> bool:
        menu_handle = getattr(native_icon, "_menu_handle", None)
        if not menu_handle:
            return False
        point = wintypes.POINT()
        win32.GetCursorPos(ctypes.byref(point))
        work = self._work_area_at_cursor(point)
        if work is None:
            return False
        x, y, align_right, align_bottom, max_height = self._tray_menu_placement(
            point.x, point.y, work
        )
        hmenu, descriptors = menu_handle
        menu_info = _MenuInfo()
        menu_info.cbSize = ctypes.sizeof(menu_info)
        menu_info.fMask = 0x00000001  # MIM_MAXHEIGHT
        menu_info.cyMax = max_height
        set_menu_info = ctypes.windll.user32.SetMenuInfo
        set_menu_info.argtypes = (wintypes.HMENU, ctypes.POINTER(_MenuInfo))
        set_menu_info.restype = wintypes.BOOL
        if not set_menu_info(hmenu, ctypes.byref(menu_info)):
            return False
        win32.SetForegroundWindow(native_icon._hwnd)
        flags = win32.TPM_RETURNCMD
        flags |= win32.TPM_RIGHTALIGN if align_right else win32.TPM_LEFTALIGN
        flags |= win32.TPM_BOTTOMALIGN if align_bottom else win32.TPM_TOPALIGN
        index = win32.TrackPopupMenuEx(
            hmenu, flags, x, y, native_icon._menu_hwnd, None
        )
        if 0 < index <= len(descriptors):
            descriptors[index - 1](native_icon)
        return True

    def _install_topmost_menu_hook(self, native_icon) -> None:
        """Pin the native Windows tray popup above the taskbar on right-click."""
        handlers = getattr(native_icon, "_message_handlers", None)
        if not isinstance(handlers, dict):
            return
        try:
            from pystray._util import win32

            original = handlers.get(win32.WM_NOTIFY)
            if original is None:
                return

            def notify(wparam, lparam):
                if lparam == win32.WM_RBUTTONUP:
                    self._pin_tray_menu_above_taskbar(native_icon)
                    try:
                        if self._show_tray_menu_in_work_area(native_icon, win32):
                            return None
                    except Exception:
                        logger.exception("Could not place the tray menu within the monitor work area")
                return original(wparam, lparam)

            handlers[win32.WM_NOTIFY] = notify
            handlers[self.WM_REFRESH_MENU] = lambda _wp, _lp: self._update_menu_now()
            handlers[self.WM_ICON_FRAME] = lambda _wp, _lp: self._apply_icon_frame()
            self._tray_dispatch_ready = True
        except Exception:
            logger.exception("Could not enable topmost tray-menu behavior")

    def _create_icon_image(
        self, width: int = 64, height: int = 64, *, pulse: float = 1.0
    ) -> Image.Image:
        """Return the same artwork used by the dashboard window."""
        return create_lightning_icon(
            width, height, state=self._visual_state(), pulse=pulse
        )

    def _animate_icon(self) -> None:
        """Animate only the bolt while preserving the white outer ring."""
        while not self._animation_stop.is_set():
            visual_state = self._visual_state()
            if visual_state == "closed":
                pulse = 1.0
                delay = 0.25
            else:
                # Open LS has a deliberately slower blue breathing effect;
                # active scaling uses a more visible yellow pulse.
                period = 2.8 if visual_state == "open" else 1.2
                pulse = 0.5 + (0.5 * math.sin((time.monotonic() / period) * math.tau))
                delay = 0.10
            try:
                if self.icon:
                    image = self._create_icon_image(pulse=pulse)
                    self._pending_icon_frame = image
                    if not self._post_tray_update(self.WM_ICON_FRAME):
                        self._apply_icon_frame()
            except Exception:
                logger.debug("Could not update animated tray artwork", exc_info=True)
            self._animation_stop.wait(delay)

    def _on_add_profile_from_process(self, proc_name: str):
        def handler(icon, item):
            new_p = self.profile_manager.create_profile_from_process(proc_name)
            logger.info(f"Created new profile for: {proc_name} (ID: {new_p.id})")
            self.update_menu()
        return handler

    def _open_dashboard(self, icon, item):
        url = f"http://{self.profile_manager.config.host}:{self.profile_manager.config.port}/dashboard"
        open_dashboard_window(url)

    def _open_settings(self, icon, item):
        url = f"http://{self.profile_manager.config.host}:{self.profile_manager.config.port}/dashboard?view=settings"
        open_dashboard_window(url)

    def _open_proxy_menu(self, icon, menu_item):
        if not self.proxy_menu:
            self._notify("Add-on menu control is unavailable.")
            return
        result = self.proxy_menu.open()
        if not result.get("opened"):
            self._notify(str(result.get("reason") or "Could not open the add-on menu."))

    def _notify(self, message: str) -> None:
        try:
            if self.icon:
                self.icon.notify(message, "LS Companion")
        except Exception:
            logger.exception("Could not display a tray notification")

    def _on_toggle_startup(self, icon, menu_item):
        if not self.startup_manager or self._startup_change_running:
            return
        enabled = not self._startup_enabled
        self._startup_change_running = True
        self.update_menu()

        def apply() -> None:
            try:
                result = self.startup_manager.set_enabled(enabled)
                self._startup_enabled = bool(result.get("enabled"))
                self.profile_manager.config.run_at_startup = self._startup_enabled
                self.profile_manager.save_config()
                self._notify(
                    "Run at Windows Sign-In enabled."
                    if self._startup_enabled
                    else "Run at Windows Sign-In disabled."
                )
            except Exception as error:
                logger.exception("Could not change the startup task from the tray")
                self._notify(f"Could not change startup behavior: {error}")
            finally:
                self._startup_change_running = False
                self.update_menu()

        threading.Thread(
            target=apply, daemon=True, name="TrayStartupToggle"
        ).start()

    def _on_check_updates(self, icon, menu_item):
        if not self.on_check_updates_callback or self._update_check_running:
            return
        self._update_check_running = True
        self.update_menu()
        self._notify("Checking GitHub releases for an LS Companion update...")
        try:
            future = self.on_check_updates_callback()
        except Exception as error:
            self._update_check_running = False
            logger.exception("Could not start the tray update check")
            self._notify(f"Update check could not start: {error}")
            self.update_menu()
            return

        def completed(result_future) -> None:
            try:
                status = result_future.result()
                self._update_status = status
                if status.get("error"):
                    self._notify(f"Update check failed: {status['error']}")
                elif status.get("updateAvailable"):
                    self._notify(
                        f"LS Companion {status.get('latestVersion')} is available. Opening update controls."
                    )
                    self._open_settings(None, None)
                else:
                    self._notify(
                        f"LS Companion {status.get('currentVersion')} is up to date."
                    )
            except Exception as error:
                logger.exception("Tray update check failed")
                self._notify(f"Update check failed: {error}")
            finally:
                self._update_check_running = False
                self.update_menu()

        future.add_done_callback(completed)

    def _on_install_update(self, icon, menu_item):
        status = self._update_status or {}
        version = status.get("latestVersion")
        if (
            not version
            or not status.get("updateAvailable")
            or not self.on_install_update_callback
            or self._update_check_running
        ):
            return
        self._update_check_running = True
        self.update_menu()
        self._notify(f"Downloading, verifying, and installing LS Companion {version}...")
        try:
            future = self.on_install_update_callback(str(version))
        except Exception as error:
            self._update_check_running = False
            logger.exception("Could not start the tray update installation")
            self._notify(f"Update installation could not start: {error}")
            self.update_menu()
            return

        def completed(result_future) -> None:
            try:
                result_future.result()
                self._notify(f"LS Companion {version} installer launched.")
            except Exception as error:
                logger.exception("Tray update installation failed")
                self._notify(f"Update installation failed: {error}")
            finally:
                self._update_check_running = False
                self.update_menu()

        future.add_done_callback(completed)

    def _on_exit(self, icon, item):
        logger.info("Tray Exit clicked.")
        # pystray's Windows backend implements stop() by posting WM_STOP to
        # this same UI thread.  Keep this callback bounded so it can return to
        # the message pump and actually consume that message.
        try:
            (icon or self.icon).stop()
        except Exception:
            logger.exception("Could not stop the tray message loop")
        if self.on_exit_callback:
            try:
                self.on_exit_callback()
            except Exception:
                logger.exception("Could not signal application shutdown")
        try:
            close_dashboard_window()
        except Exception:
            logger.exception("Could not close the dashboard window")

    def _build_menu(self) -> Menu:
        running_procs = self.process_watcher.list_running_executables(visible_windows_only=True)[:15]
        proc_items = []
        for proc in running_procs:
            label = proc['name']
            if proc.get('windowTitle'):
                title = proc['windowTitle']
                if len(title) > 20:
                    title = title[:17] + "..."
                label = f"{proc['name']} ({title})"
            proc_items.append(
                item(f"Add: {label}", self._on_add_profile_from_process(proc['name']))
            )

        status_items = (
            [item(label, None, enabled=False) for label in self._scaling_labels()]
            if self.state.is_scaling_active
            else [item(self._runtime_status_label(), None, enabled=False)]
        )
        menu_items = [
            item("LS Companion", None, enabled=False),
            *status_items,
            Menu.SEPARATOR,
            item("Open Dashboard", self._open_dashboard, default=True),
            item("Quick Add Profile", Menu(*proc_items) if proc_items else Menu(item("No apps detected", lambda icon, item: None, enabled=False))),
            item("Open Settings", self._open_settings),
        ]
        if (
            self.proxy_menu
            and self.profile_manager.config.addon_manager_enabled
            and self.state.is_scaling_active
        ):
            menu_items.append(item("Show Add-on Menu", self._open_proxy_menu))
        menu_items.extend([
            Menu.SEPARATOR,
            item(
                "Run at Windows Sign-In",
                self._on_toggle_startup,
                checked=lambda menu_item: self._startup_enabled,
                enabled=lambda menu_item: bool(self.startup_manager) and not self._startup_change_running,
            ),
            item(
                (
                    "Install Update"
                    if self._update_status and self._update_status.get("downloaded")
                    else "Download and Install Update"
                )
                if self._update_status and self._update_status.get("updateAvailable")
                else "Check for Updates",
                self._on_install_update
                if self._update_status and self._update_status.get("updateAvailable")
                else self._on_check_updates,
                enabled=lambda menu_item: bool(
                    self.on_install_update_callback
                    if self._update_status and self._update_status.get("updateAvailable")
                    else self.on_check_updates_callback
                ) and not self._update_check_running,
            ),
            Menu.SEPARATOR,
            item("Exit", self._on_exit)
        ])
        return Menu(*menu_items)

    def run(self) -> None:
        """Runs the tray icon in the caller's thread."""
        img = self._create_icon_image()
        self.icon = pystray.Icon(
            "lossless_companion",
            img,
            self._tray_title(),
            menu=self._build_menu()
        )
        self._install_topmost_menu_hook(self.icon)
        self._animation_stop.clear()
        self._animation_thread = threading.Thread(
            target=self._animate_icon,
            daemon=True,
            name="TrayIconAnimation",
        )
        self._animation_thread.start()
        try:
            self.icon.run()
        finally:
            self._animation_stop.set()
            self._animation_thread.join(timeout=1.0)

    def update_menu(self) -> None:
        if getattr(self, "_pending_menu_refresh", False):
            return
        self._pending_menu_refresh = True
        if not self._post_tray_update(self.WM_REFRESH_MENU):
            self._update_menu_now()

    def _post_tray_update(self, message):
        hwnd = getattr(self.icon, "_hwnd", None) if self.icon else None
        if not getattr(self, "_tray_dispatch_ready", False) or not isinstance(hwnd, int) or not hwnd:
            return False
        post = ctypes.windll.user32.PostMessageW
        post.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        post.restype = wintypes.BOOL
        if not post(hwnd, message, 0, 0):
            self._pending_menu_refresh = False
            logger.debug("Tray update dropped while its window is closing")
        return True

    def _apply_icon_frame(self):
        image = self._pending_icon_frame
        self._pending_icon_frame = None
        if self.icon and image is not None:
            self.icon.icon = image

    def _update_menu_now(self):
        """Runs on the tray's message loop, not the monitor/animation thread."""
        self._pending_menu_refresh = False
        if self.icon:
            # Reflect state changes immediately instead of waiting for the next
            # animation frame.
            self.icon.icon = self._create_icon_image()
            self.icon.title = self._tray_title()
            self.icon.menu = self._build_menu()
            self.icon.update_menu()
