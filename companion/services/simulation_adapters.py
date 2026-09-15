"""No-side-effect adapters for operations that must never touch the host in simulation."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional
import os
import time

from ..core.models import Profile


class SimulationStartupTaskManager:
    TASK_NAME = "LosslessScalingHelper-Simulation (not installed)"

    def is_enabled(self) -> bool:
        return False

    def set_enabled(self, enabled: bool) -> Dict:
        return {"enabled": bool(enabled), "taskName": self.TASK_NAME, "simulated": True}


class SimulationHotkeyTrigger:
    """Signal the fake runtime without desktop input injection."""

    def __init__(self, command_path: Path):
        self.command_path = command_path

    def __call__(self, **kwargs) -> bool:
        try:
            self.command_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.command_path.with_suffix(".tmp")
            temporary.write_text(f"toggle {time.time_ns()}\n", encoding="utf-8")
            os.replace(temporary, self.command_path)
            return True
        except OSError:
            return False


class SimulationNvidiaProfileManager:
    def set_lossless_scaling_rtx_hdr(self, executable: str, enabled: bool) -> bool:
        return True

    def set_lossless_scaling_smooth_motion(self, executable: str, enabled: bool) -> bool:
        return True

    def restore_managed_changes(self) -> bool:
        return True


class SimulationRtssProfileManager:
    """Keep RTSS unavailable so a simulation cannot edit a real RTSS profile."""

    def __init__(self):
        self.configured_path: Optional[str] = None

    def status(self, configured_path: Optional[str] = None) -> Dict:
        return {
            "installed": False,
            "running": False,
            "path": None,
            "simulated": True,
        }

    @staticmethod
    def _target_process(profile: Profile) -> str:
        value = profile.target_process
        if not value and profile.target_executable_path:
            value = Path(profile.target_executable_path).name
        if not value or not value.casefold().endswith(".exe"):
            raise ValueError("RTSS limiting requires a valid target executable filename ending in .exe")
        return value

    @staticmethod
    def clear_metadata(profile: Profile) -> None:
        profile.rtss.managed_profile_created = False
        profile.rtss.managed_target_process = None
        profile.rtss.managed_install_path = None
        profile.rtss.original_values = {}

    def apply_profile(self, *args, **kwargs) -> bool:
        raise RuntimeError("RTSS writes are disabled in simulation mode")

    def remove_profile(self, *args, **kwargs) -> bool:
        raise RuntimeError("RTSS writes are disabled in simulation mode")
