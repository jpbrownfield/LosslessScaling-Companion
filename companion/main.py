"""
Main entry point for LS Companion.
Runs the WebSocket server, process monitor, and system tray simultaneously.
"""

import asyncio
import os
import sys
import threading
import time
import logging
from pathlib import Path
from typing import Optional

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] (%(name)s) %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("LSCompanion")

from .core.profile_manager import ProfileManager
from .core.state import AppState
from .services.server import CompanionWebSocketServer
from .services.process_watcher import ProcessWatcher
from .services.ls_inspector import LosslessScalingInspector
from .services.automation import AutomationController
from .services.ls_settings import LosslessSettingsXml
from .services.asset_store import AssetStore
from .services.release_providers import ReleaseManager
from .services.process_lasso_monitor import ProcessLassoScalingMonitor
from .services.rtss_manager import RtssProfileManager
from .services.dynamic_limiter import DynamicLimiterController
from .services.hotkey_listener import GlobalHotkeyListener
from .services.single_instance import SingleInstanceGuard
from .ui.tray import CompanionTrayIcon
from .services.simulation_adapters import (
    SimulationNvidiaProfileManager,
    SimulationHotkeyTrigger,
    SimulationRtssProfileManager,
    SimulationStartupTaskManager,
)


def simulation_mode_enabled() -> bool:
    """Allow simulation only from source; production executables must stay live."""
    return (
        not bool(getattr(sys, "frozen", False))
        and os.environ.get("LOSSLESS_COMPANION_SIMULATION") == "1"
    )


class CompanionApplication:
    def __init__(self, config_dir: Optional[Path] = None):
        self.simulation_mode = simulation_mode_enabled()
        self.profile_manager = ProfileManager(config_dir)
        self.state = AppState()
        self.state.simulation_mode = self.simulation_mode
        self.state.auto_scale_enabled = self.profile_manager.config.disable_native_auto_scale
        self.ls_settings = LosslessSettingsXml(self.profile_manager.config.lossless_settings_xml_path)
        self._settings_backup_ready = bool(self.ls_settings.ensure_initial_backup())
        self.profile_manager.ensure_lossless_default_profile(self.ls_settings.get_profile())
        self._next_settings_backup_check = 0.0
        self.ls_inspector = LosslessScalingInspector(
            settings_xml_path=self.profile_manager.config.lossless_settings_xml_path,
            lossless_exe_path=self.profile_manager.config.lossless_scaling_exe_path,
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
            lossless_settings=self.ls_settings,
            nvidia_profile_manager=(
                SimulationNvidiaProfileManager() if self.simulation_mode else None
            ),
            hotkey_trigger=(
                SimulationHotkeyTrigger(
                    Path(self.profile_manager.config.lossless_scaling_exe_path).parent
                    / "simulation.command"
                )
                if self.simulation_mode
                and self.profile_manager.config.lossless_scaling_exe_path
                else None
            ),
        )
        self.automation.scaling_state_probe = (
            lambda: self.ls_inspector.detect_lossless_scaling_overlay_window()[0]
        )
        self.process_watcher.on_profile_changed = self.automation.activate_profile
        self.process_lasso_monitor = ProcessLassoScalingMonitor(
            self.profile_manager, self.state, self.process_watcher, self.automation
        )
        self.rtss_manager = (
            SimulationRtssProfileManager()
            if self.simulation_mode
            else RtssProfileManager(self.profile_manager.config.rtss_install_path)
        )
        self.dynamic_limiter = DynamicLimiterController(
            self.profile_manager, self.state, self.rtss_manager
        )
        self.hotkey_listener = GlobalHotkeyListener(
            self.profile_manager, self.state, self.automation.handle_override_hotkey
        )
        self.server = CompanionWebSocketServer(
            self.profile_manager,
            self.state,
            self.process_watcher,
            self.automation,
            self.ls_settings,
            self.asset_store,
            self.release_manager,
            rtss_manager=self.rtss_manager,
            dynamic_limiter=self.dynamic_limiter,
            hotkey_listener=self.hotkey_listener,
            startup_manager=(
                SimulationStartupTaskManager() if self.simulation_mode else None
            ),
        )
        
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.server_thread: Optional[threading.Thread] = None
        self.monitor_thread: Optional[threading.Thread] = None
        self.running = False
        self._instance_guard: Optional[SingleInstanceGuard] = None
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
                    not self._settings_backup_ready
                    and time.monotonic() >= self._next_settings_backup_check
                ):
                    self._settings_backup_ready = bool(
                        self.ls_settings.ensure_initial_backup()
                    )
                    if self._settings_backup_ready:
                        self.profile_manager.ensure_lossless_default_profile(
                            self.ls_settings.get_profile()
                        )
                    self._next_settings_backup_check = time.monotonic() + 5.0
                if (
                    self.profile_manager.config.lossless_control_configured
                    and not self._native_auto_scale_enforced
                    and time.monotonic() >= self._next_native_auto_scale_check
                ):
                    self._native_auto_scale_enforced = (
                        self.process_watcher.enforce_helper_scaling_control()
                    )
                    self._next_native_auto_scale_check = time.monotonic() + 5.0
                if not self.state.benchmark_mode_active:
                    self.process_watcher.check_foreground_and_update()
                    self.process_lasso_monitor.poll()
                lossless_running = self.process_watcher.check_is_lossless_scaling_running()
                
                # Check live scaling target via log / overlay
                if not self.state.benchmark_mode_active:
                    settings_title = (
                        self.state.current_active_profile.lossless_profile_title
                        if self.state.current_active_profile else None
                    )
                    target_info = self.ls_inspector.inspect_current_scaling_target(
                        settings_profile_title=settings_title
                    )
                    self.state.current_scaled_target = target_info.to_dict()
                    if target_info.source != "unknown" and target_info.is_active != self.state.is_scaling_active:
                        self.automation.reconcile_observed_scaling_state(
                            target_info.is_active,
                            self.state.current_scaled_target,
                        )
                    self.automation.reconcile_gpu_route()
                    self.dynamic_limiter.poll()
                snapshot = (
                    self.state.is_scaling_active,
                    lossless_running,
                    self.state.current_active_profile.id if self.state.current_active_profile else None,
                    self.state.current_foreground_process,
                    self.state.current_scaled_target.get("processName"),
                    tuple((p.id, p.name) for p in self.profile_manager.config.profiles),
                    tuple(sorted(self.state.dynamic_limiter_status.items())),
                    tuple(sorted(self.state.scaling_control_status.items())),
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
        logger.info("Initializing LS Companion...")
        self._instance_guard = SingleInstanceGuard(
            "simulation" if self.simulation_mode else "companion"
        )
        if not self._instance_guard.acquire():
            holder = self._instance_guard.holder_pid
            detail = f" (PID {holder})" if holder else ""
            logger.error(
                "Another LS Companion instance is already running%s; exiting",
                detail,
            )
            print(
                f"Another LS Companion instance is already running{detail}; exiting.",
                flush=True,
            )
            return
        self.running = True

        # Check / Launch Lossless Scaling
        self._native_auto_scale_enforced = bool(
            self.profile_manager.config.lossless_control_configured
            and self.process_watcher.enforce_helper_scaling_control()
        )
        self.process_watcher.launch_lossless_scaling_if_needed()
        self.hotkey_listener.start()

        # Start Server Thread
        self.server_thread = threading.Thread(target=self._run_async_server, daemon=True, name="WSServerThread")
        self.server_thread.start()

        # Start Process Monitor Thread
        self.monitor_thread = threading.Thread(target=self._run_process_monitor, daemon=True, name="MonitorThread")
        self.monitor_thread.start()

        logger.info("LS Companion is running. Check your Windows System Tray.")

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
            guard = getattr(self, "_instance_guard", None)
            if guard is not None:
                guard.release()
                self._instance_guard = None
            return
        logger.info("Shutting down LS Companion...")
        self.running = False
        self.hotkey_listener.stop()
        if self.monitor_thread and self.monitor_thread is not threading.current_thread():
            self.monitor_thread.join(timeout=2)
        self.automation.shutdown()
        self.dynamic_limiter.close()

        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(lambda: None)
        guard = getattr(self, "_instance_guard", None)
        if guard is not None:
            guard.release()
            self._instance_guard = None



def main():
    app = CompanionApplication()
    app.start()


if __name__ == "__main__":
    main()
