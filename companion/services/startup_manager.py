"""Manage the companion's fixed Windows Task Scheduler startup entry."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Dict, List


class StartupTaskError(RuntimeError):
    pass


class StartupTaskManager:
    TASK_NAME = "LosslessScalingHelper"

    @staticmethod
    def _creation_flags() -> int:
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)

    @staticmethod
    def launch_arguments() -> List[str]:
        if getattr(sys, "frozen", False):
            return [str(Path(sys.executable).resolve())]
        launcher = Path(__file__).parents[2] / "run_companion.py"
        return [str(Path(sys.executable).resolve()), str(launcher.resolve())]

    def is_enabled(self) -> bool:
        result = subprocess.run(
            ["schtasks.exe", "/Query", "/TN", self.TASK_NAME],
            capture_output=True,
            text=True,
            creationflags=self._creation_flags(),
            check=False,
        )
        return result.returncode == 0

    def set_enabled(self, enabled: bool) -> Dict:
        if enabled:
            task_command = subprocess.list2cmdline(self.launch_arguments())
            command = [
                "schtasks.exe", "/Create", "/TN", self.TASK_NAME,
                "/SC", "ONLOGON", "/TR", task_command,
                "/RL", "HIGHEST", "/IT", "/F",
            ]
        else:
            if not self.is_enabled():
                return {"enabled": False, "taskName": self.TASK_NAME}
            command = ["schtasks.exe", "/Delete", "/TN", self.TASK_NAME, "/F"]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            creationflags=self._creation_flags(),
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown Task Scheduler error").strip()
            raise StartupTaskError(detail)
        return {"enabled": enabled, "taskName": self.TASK_NAME}
