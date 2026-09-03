"""
System Tray interface for Lossless Companion using pystray and Pillow.
"""

import os
import subprocess
import threading
import logging
from typing import Callable, Optional
from PIL import Image, ImageDraw
import pystray
from pystray import MenuItem as item, Menu

from ..core.profile_manager import ProfileManager
from ..core.state import AppState
from ..services.input_simulator import InputSimulator
from ..services.process_watcher import ProcessWatcher
from ..services.ls_inspector import LosslessScalingInspector

logger = logging.getLogger("LosslessCompanion.Tray")


class CompanionTrayIcon:
    def __init__(
        self,
        profile_manager: ProfileManager,
        state: AppState,
        process_watcher: ProcessWatcher,
        ls_inspector: Optional[LosslessScalingInspector] = None,
        on_exit_callback: Optional[Callable] = None
    ):
        self.profile_manager = profile_manager
        self.state = state
        self.process_watcher = process_watcher
        self.ls_inspector = ls_inspector
        self.on_exit_callback = on_exit_callback
        self.icon: Optional[pystray.Icon] = None

    def _create_icon_image(self, width: int = 64, height: int = 64) -> Image.Image:
        """Generates a dynamic 64x64 icon."""
        image = Image.new("RGBA", (width, height), color=(0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        # Outer circle
        draw.ellipse((4, 4, width - 4, height - 4), fill=(15, 23, 42), outline=(14, 165, 233), width=4)
        # Inner lightning / scale glyph
        points = [
            (width * 0.55, height * 0.18),
            (width * 0.30, height * 0.52),
            (width * 0.48, height * 0.52),
            (width * 0.42, height * 0.82),
            (width * 0.70, height * 0.44),
            (width * 0.52, height * 0.44),
        ]
        draw.polygon(points, fill=(56, 189, 248))
        return image

    def _on_toggle_scale(self, icon, item):
        profile = self.state.current_active_profile or self.profile_manager.config.profiles[0]
        hotkey = profile.hotkey
        logger.info(f"Tray triggered scaling with profile '{profile.name}'")
        InputSimulator.trigger_hotkey(modifiers=hotkey.modifiers, key=hotkey.key, hold_ms=hotkey.hold_delay_ms)
        self.state.mark_scaling_toggled()

    def _on_toggle_auto_scale(self, icon, item):
        self.state.auto_scale_enabled = not self.state.auto_scale_enabled
        logger.info(f"Auto-scale set to: {self.state.auto_scale_enabled}")

    def _on_select_profile(self, profile_id: str):
        def handler(icon, item):
            self.profile_manager.config.active_profile_id = profile_id
            self.profile_manager.save_config()
            self.state.current_active_profile = self.profile_manager.get_profile_by_id(profile_id)
            logger.info(f"Active profile changed to: {self.state.current_active_profile.name}")
        return handler

    def _on_add_profile_from_process(self, proc_name: str):
        def handler(icon, item):
            new_p = self.profile_manager.create_profile_from_process(proc_name)
            logger.info(f"Created new profile for: {proc_name} (ID: {new_p.id})")
        return handler

    def _open_config_folder(self, icon, item):
        cfg_path = str(self.profile_manager.config_dir.resolve())
        os.startfile(cfg_path)

    def _on_exit(self, icon, item):
        logger.info("Tray Exit clicked.")
        if self.icon:
            self.icon.stop()
        if self.on_exit_callback:
            self.on_exit_callback()

    def _build_menu(self) -> Menu:
        profiles_items = []
        for p in self.profile_manager.config.profiles:
            is_active = (self.profile_manager.config.active_profile_id == p.id)
            profiles_items.append(
                item(
                    f"{'✓ ' if is_active else '   '}{p.name}",
                    self._on_select_profile(p.id)
                )
            )

        running_procs = self.process_watcher.list_running_executables()[:15]
        proc_items = []
        for proc in running_procs:
            proc_items.append(
                item(f"Add: {proc['name']}", self._on_add_profile_from_process(proc['name']))
            )

        target_proc = self.state.current_scaled_target.get("processName") or "None"
        scaled_title = self.state.current_scaled_target.get("windowTitle")
        if scaled_title and len(scaled_title) > 25:
            scaled_title = scaled_title[:22] + "..."

        status_text = f"Status: {'Scaling Active' if self.state.is_scaling_active else 'Idle'}"
        if self.state.is_scaling_active:
            target_text = f"Target: {target_proc}" + (f" ({scaled_title})" if scaled_title else "")
        else:
            target_text = f"Focused: {self.state.current_foreground_process or 'None'}"

        menu_items = [
            item(status_text, lambda icon, item: None, enabled=False),
            item(target_text, lambda icon, item: None, enabled=False),
            Menu.SEPARATOR,
            item("⚡ Toggle Scaling (HotKey)", self._on_toggle_scale),
            item(
                "Enable Auto-Scaling",
                self._on_toggle_auto_scale,
                checked=lambda item: self.state.auto_scale_enabled
            ),
            Menu.SEPARATOR,
            item("Profiles", Menu(*profiles_items)),
            item("Create Profile From App", Menu(*proc_items) if proc_items else Menu(item("No apps detected", lambda icon, item: None, enabled=False))),
            Menu.SEPARATOR,
            item("Open Settings Folder", self._open_config_folder),
            item("Exit", self._on_exit)
        ]
        return Menu(*menu_items)

    def run(self) -> None:
        """Runs the tray icon in the caller's thread."""
        img = self._create_icon_image()
        self.icon = pystray.Icon(
            "lossless_companion",
            img,
            "Lossless Companion",
            menu=self._build_menu
        )
        self.icon.run()

    def update_menu(self) -> None:
        if self.icon:
            self.icon.menu = self._build_menu()
