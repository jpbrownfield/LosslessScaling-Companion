"""
Global runtime state for Lossless Companion.
"""

from typing import Optional, Set, Dict, Any
import time
import threading
from .models import Profile


class AppState:
    def __init__(self):
        self.is_scaling_active: bool = False
        self.current_scaled_target: Dict[str, Any] = {}
        self.current_active_profile: Optional[Profile] = None
        self.current_foreground_process: Optional[str] = None
        self.current_foreground_window_title: Optional[str] = None
        self.last_scale_toggle_time: float = 0
        self.connected_clients: int = 0
        self.lossless_scaling_running: bool = False
        self.auto_scale_enabled: bool = True
        self.scaling_owner_profile_id: Optional[str] = None
        self.scaling_trigger: Optional[str] = None
        self.current_foreground_exe_path: Optional[str] = None
        self.current_foreground_hwnd: Optional[int] = None
        self.dynamic_limiter_status: Dict[str, Any] = {
            "state": "inactive",
            "message": "Dynamic limiter inactive",
        }
        self.scaling_control_status: Dict[str, Any] = {
            "state": "idle",
            "message": "No scaling command pending",
        }
        self._lock = threading.RLock()

    def mark_scaling_toggled(self, new_state: Optional[bool] = None) -> bool:
        with self._lock:
            if new_state is not None:
                self.is_scaling_active = new_state
            else:
                self.is_scaling_active = not self.is_scaling_active
            self.last_scale_toggle_time = time.time()
            return self.is_scaling_active
