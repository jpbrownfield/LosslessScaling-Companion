"""
System Tray interface for LS Companion using pystray and Pillow.
"""

import logging
import math
import threading
import time
from typing import Callable, Optional
from PIL import Image
import pystray
from pystray import MenuItem as item, Menu

from ..core.profile_manager import ProfileManager
from ..core.state import AppState
from ..services.process_watcher import ProcessWatcher
from ..services.ls_inspector import LosslessScalingInspector
from ..services.startup_manager import StartupTaskManager
from ..services.automation import AutomationController
from .dashboard_window import close_dashboard_window, open_dashboard_window
from .icons import create_lightning_icon

logger = logging.getLogger("LSCompanion.Tray")


class CompanionTrayIcon:
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

    def _visual_state(self) -> str:
        if not self.state.lossless_scaling_running:
            return "closed"
        if self.state.is_scaling_active:
            return "scaling"
        return "open"

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
                    self.icon.icon = self._create_icon_image(pulse=pulse)
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

        menu_items = [
            item("LS Companion", None, enabled=False),
            Menu.SEPARATOR,
            item("Open Dashboard", self._open_dashboard, default=True),
            item("Quick Add Profile", Menu(*proc_items) if proc_items else Menu(item("No apps detected", lambda icon, item: None, enabled=False))),
            item("Open Settings", self._open_settings),
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
        ]
        return Menu(*menu_items)

    def run(self) -> None:
        """Runs the tray icon in the caller's thread."""
        img = self._create_icon_image()
        self.icon = pystray.Icon(
            "lossless_companion",
            img,
            "LS Companion",
            menu=self._build_menu()
        )
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
        if self.icon:
            # Reflect state changes immediately instead of waiting for the next
            # animation frame.
            self.icon.icon = self._create_icon_image()
            self.icon.menu = self._build_menu()
            self.icon.update_menu()
