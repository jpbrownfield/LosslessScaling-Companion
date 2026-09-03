"""
Main entry point for Lossless Companion.
Runs the WebSocket server, process monitor, and system tray simultaneously.
"""

import sys
import os
import asyncio
import threading
import time
import logging
from pathlib import Path
from typing import Optional, List, Dict

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] (%(name)s) %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("LosslessCompanion")

from .core.models import AppConfig
from .core.profile_manager import ProfileManager
from .core.state import AppState
from .services.server import CompanionWebSocketServer
from .services.process_watcher import ProcessWatcher
from .services.ls_inspector import LosslessScalingInspector
from .ui.tray import CompanionTrayIcon


class CompanionApplication:
    def __init__(self):
        self.profile_manager = ProfileManager()
        self.state = AppState()
        self.ls_inspector = LosslessScalingInspector()
        self.process_watcher = ProcessWatcher(self.profile_manager, self.state)
        self.server = CompanionWebSocketServer(self.profile_manager, self.state)
        
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.server_thread: Optional[threading.Thread] = None
        self.monitor_thread: Optional[threading.Thread] = None
        self.running = False
        self.tray = CompanionTrayIcon(
            self.profile_manager,
            self.state,
            self.process_watcher,
            self.ls_inspector,
            on_exit_callback=self.stop
        )

    def _run_async_server(self):
        """Runs the asyncio WebSocket server in a dedicated thread."""
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        async def _main_loop():
            await self.server.start()
            while self.running:
                await asyncio.sleep(1)

        try:
            self.loop.run_until_complete(_main_loop())
        except Exception as e:
            logger.error(f"Async server loop error: {e}")
        finally:
            if self.loop.is_running():
                self.loop.close()

    def _run_process_monitor(self):
        """Background thread to poll foreground windows, inspect Lossless Scaling logs and overlay."""
        logger.info("Starting background process and window monitor...")
        while self.running:
            try:
                self.process_watcher.check_foreground_and_update()
                self.process_watcher.check_is_lossless_scaling_running()
                
                # Check live scaling target via log / overlay
                target_info = self.ls_inspector.inspect_current_scaling_target()
                self.state.current_scaled_target = target_info.to_dict()
                if target_info.is_active != self.state.is_scaling_active:
                    self.state.is_scaling_active = target_info.is_active
            except Exception as e:
                logger.debug(f"Process monitor tick error: {e}")
            time.sleep(1.0)

    def start(self):
        logger.info("Initializing Lossless Companion...")
        self.running = True

        # Check / Launch Lossless Scaling
        self.process_watcher.launch_lossless_scaling_if_needed()

        # Start Server Thread
        self.server_thread = threading.Thread(target=self._run_async_server, daemon=True, name="WSServerThread")
        self.server_thread.start()

        # Start Process Monitor Thread
        self.monitor_thread = threading.Thread(target=self._run_process_monitor, daemon=True, name="MonitorThread")
        self.monitor_thread.start()

        logger.info("Lossless Companion is running. Check your Windows System Tray.")

        # Run Tray in Main Thread (Required for Windows Win32 message loop)
        try:
            self.tray.run()
        except KeyboardInterrupt:
            self.stop()

    def stop(self):
        if not self.running:
            return
        logger.info("Shutting down Lossless Companion...")
        self.running = False

        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self.server.stop(), self.loop)

        sys.exit(0)


def main():
    app = CompanionApplication()
    app.start()


if __name__ == "__main__":
    main()
