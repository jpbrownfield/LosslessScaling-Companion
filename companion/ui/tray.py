"""
System Tray interface for LS Companion using pystray and Pillow.
"""

import logging
from typing import Callable, Optional
from PIL import Image
import pystray
from pystray import MenuItem as item, Menu

from ..core.profile_manager import ProfileManager
from ..core.state import AppState
from ..services.process_watcher import ProcessWatcher
from ..services.ls_inspector import LosslessScalingInspector
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
        on_exit_callback: Optional[Callable] = None
    ):
        self.profile_manager = profile_manager
        self.state = state
        self.process_watcher = process_watcher
        self.ls_inspector = ls_inspector
        self.automation = automation or AutomationController(profile_manager, state)
        self.on_exit_callback = on_exit_callback
        self.icon: Optional[pystray.Icon] = None

    def _create_icon_image(self, width: int = 64, height: int = 64) -> Image.Image:
        """Return the same artwork used by the dashboard window."""
        return create_lightning_icon(width, height)

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

    def _on_exit(self, icon, item):
        logger.info("Tray Exit clicked.")
        if self.icon:
            self.icon.stop()
        try:
            close_dashboard_window()
        except Exception:
            logger.exception("Could not close the dashboard window")
        if self.on_exit_callback:
            self.on_exit_callback()

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
        self.icon.run()

    def update_menu(self) -> None:
        if self.icon:
            self.icon.menu = self._build_menu()
            self.icon.update_menu()
