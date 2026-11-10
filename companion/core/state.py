"""
Global runtime state for LS Companion.
"""

from typing import Optional, Set, Dict, Any
import copy
import logging
import time
import threading
from .models import Profile


class AppState:
    def __init__(self):
        self._lock = threading.RLock()
        self._listeners = set()
        self._revision = 0
        self.simulation_mode: bool = False
        self.is_scaling_active: bool = False
        self.current_scaled_target: Dict[str, Any] = {}
        self.current_active_profile: Optional[Profile] = None
        self.current_foreground_process: Optional[str] = None
        self.current_foreground_window_title: Optional[str] = None
        self.last_scale_toggle_time: float = 0
        self.connected_clients: int = 0
        self.lossless_scaling_running: bool = False
        self.auto_scale_enabled: bool = True
        self.update_in_progress: bool = False
        self.gpu_mapping_in_progress: bool = False
        self.gpu_mapping_status: Dict[str, Any] = {"state": "pending", "message": "GPU index check pending"}
        self.scaling_owner_profile_id: Optional[str] = None
        self.scaling_trigger: Optional[str] = None
        self.current_foreground_exe_path: Optional[str] = None
        self.current_foreground_hwnd: Optional[int] = None
        self.scaling_target_hwnd: Optional[int] = None
        self.scaling_target_pid: Optional[int] = None
        self.scaling_target_verified_for_minimization: bool = False
        self.current_gpu_route_key: Optional[str] = None
        self.benchmark_mode_active: bool = False
        # Only this test window/profile may start while diagnostics own LS.
        self.diagnostic_scaling_target: Optional[tuple[int, int, str]] = None
        self.dynamic_limiter_status: Dict[str, Any] = {
            "state": "inactive",
            "message": "Dynamic limiter inactive",
        }
        self.scaling_control_status: Dict[str, Any] = {
            "state": "idle",
            "message": "No scaling command pending",
        }

    def __setattr__(self, name, value):
        if name.startswith("_") or "_lock" not in self.__dict__:
            object.__setattr__(self, name, value)
            return
        with self._lock:
            existed = name in self.__dict__
            previous = self.__dict__.get(name)
            object.__setattr__(self, name, value)
            changed = previous != value
            if existed and changed and name in {
                "lossless_scaling_running", "is_scaling_active", "auto_scale_enabled",
                "update_in_progress", "gpu_mapping_in_progress", "benchmark_mode_active",
            }:
                logging.getLogger("LSCompanion.State").info("%s: %s -> %s", name, previous, value)
            if changed and name != "connected_clients":
                self.publish_change()

    def subscribe(self, callback):
        """Observe changes from any worker; callbacks must only enqueue work."""
        with self._lock:
            self._listeners.add(callback)
        def unsubscribe():
            with self._lock:
                self._listeners.discard(callback)
        return unsubscribe

    def publish_change(self):
        """Also use after changing a nested object rather than replacing it."""
        with self._lock:
            self._revision += 1
            listeners = tuple(self._listeners)
        for callback in listeners:
            try:
                callback()
            except Exception:
                logging.getLogger("LSCompanion.State").exception("State subscriber failed")

    def snapshot(self):
        with self._lock:
            return self._revision, copy.deepcopy({
                name: value for name, value in self.__dict__.items()
                if not name.startswith("_")
            })

    def mark_scaling_toggled(self, new_state: Optional[bool] = None) -> bool:
        with self._lock:
            if new_state is not None:
                self.is_scaling_active = new_state
            else:
                self.is_scaling_active = not self.is_scaling_active
            self.last_scale_toggle_time = time.time()
            return self.is_scaling_active
