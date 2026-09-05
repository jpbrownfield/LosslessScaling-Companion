"""
Main entry point for Lossless Companion.
Runs the WebSocket server, process monitor, and system tray simultaneously.
"""

import asyncio
import threading
import time
import logging
from typing import Optional

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] (%(name)s) %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("LosslessCompanion")

from .core.profile_manager import ProfileManager
from .core.state import AppState
from .services.server import CompanionWebSocketServer
from .services.process_watcher import ProcessWatcher
from .services.ls_inspector import LosslessScalingInspector
from .services.automation import AutomationController
from .services.ls_settings import LosslessSettingsXml
from .services.asset_store import AssetStore
from .services.release_providers import ReleaseManager
from .ui.tray import CompanionTrayIcon


class CompanionApplication:
    def __init__(self):
        self.profile_manager = ProfileManager()
        self.state = AppState()
        self.ls_settings = LosslessSettingsXml(self.profile_manager.config.lossless_settings_xml_path)
        self.ls_inspector = LosslessScalingInspector(
            settings_xml_path=self.profile_manager.config.lossless_settings_xml_path
        )
        self.process_watcher = ProcessWatcher(
            self.profile_manager,
            self.state,
            lossless_settings=self.ls_settings,
        )
        self.asset_store = AssetStore(self.profile_manager.config.asset_store_path)
        self.release_manager = ReleaseManager(
            self.asset_store,
            check_interval_hours=self.profile_manager.config.update_check_interval_hours,
        )
        self.automation = AutomationController(
            self.profile_manager,
            self.state,
            self.process_watcher,
            asset_store=self.asset_store,
        )
        self.process_watcher.on_profile_changed = self.automation.activate_profile
        self.server = CompanionWebSocketServer(
            self.profile_manager,
            self.state,
            self.process_watcher,
            self.automation,
            self.ls_settings,
            self.asset_store,
            self.release_manager,
        )
        
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.server_thread: Optional[threading.Thread] = None
        self.monitor_thread: Optional[threading.Thread] = None
        self.running = False
        self._last_runtime_snapshot = None
        self._native_auto_scale_enforced = False
        self._next_native_auto_scale_check = 0.0
        self.tray = CompanionTrayIcon(
            self.profile_manager,
            self.state,
            self.process_watcher,
            self.ls_inspector,
            self.automation,
            on_exit_callback=self.stop
        )

    def _run_async_server(self):
        """Runs the asyncio WebSocket server in a dedicated thread."""
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        async def _main_loop():
            await self.server.start()
            try:
                while self.running:
                    await asyncio.sleep(1)
            finally:
                await self.server.stop()

        try:
            self.loop.run_until_complete(_main_loop())
        except Exception as e:
            logger.error(f"Async server loop error: {e}")
        finally:
            if not self.loop.is_closed():
                self.loop.close()

    def _run_process_monitor(self):
        """Background thread to poll foreground windows, inspect Lossless Scaling logs and overlay."""
        logger.info("Starting background process and window monitor...")
        while self.running:
            try:
                if (
                    not self._native_auto_scale_enforced
                    and time.monotonic() >= self._next_native_auto_scale_check
                ):
                    self._native_auto_scale_enforced = (
                        self.process_watcher.enforce_helper_scaling_control()
                    )
                    self._next_native_auto_scale_check = time.monotonic() + 5.0
                self.process_watcher.check_foreground_and_update()
                self.process_watcher.check_is_lossless_scaling_running()
                
                # Check live scaling target via log / overlay
                settings_title = (
                    self.state.current_active_profile.lossless_profile_title
                    if self.state.current_active_profile else None
                )
                target_info = self.ls_inspector.inspect_current_scaling_target(
                    settings_profile_title=settings_title
                )
                self.state.current_scaled_target = target_info.to_dict()
                if target_info.source != "unknown" and target_info.is_active != self.state.is_scaling_active:
                    self.state.is_scaling_active = target_info.is_active
                snapshot = (
                    self.state.is_scaling_active,
                    self.state.current_active_profile.id if self.state.current_active_profile else None,
                    self.state.current_foreground_process,
                    self.state.current_scaled_target.get("processName"),
                    tuple((p.id, p.name) for p in self.profile_manager.config.profiles),
                )
                if snapshot != self._last_runtime_snapshot:
                    self._last_runtime_snapshot = snapshot
                    self.tray.update_menu()
                    if self.loop and self.loop.is_running():
                        asyncio.run_coroutine_threadsafe(self.server.broadcast_state(), self.loop)
            except Exception as e:
                logger.debug(f"Process monitor tick error: {e}")
            time.sleep(1.0)

    def start(self):
        logger.info("Initializing Lossless Companion...")
        self.running = True

        # Check / Launch Lossless Scaling
        self._native_auto_scale_enforced = (
            self.process_watcher.enforce_helper_scaling_control()
        )
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
        finally:
            if self.running:
                self.stop()
            if self.server_thread:
                self.server_thread.join(timeout=2)

    def stop(self):
        if not self.running:
            return
        logger.info("Shutting down Lossless Companion...")
        self.running = False
        if self.monitor_thread and self.monitor_thread is not threading.current_thread():
            self.monitor_thread.join(timeout=2)
        self.automation.shutdown()

        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(lambda: None)



def main():
    app = CompanionApplication()
    app.start()


if __name__ == "__main__":
    main()
