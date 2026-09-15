"""Restricted native path pickers used by the local dashboard."""

from __future__ import annotations

from pathlib import Path
from typing import Optional


def _initial_directory(current_path: Optional[str]) -> Optional[str]:
    if not current_path:
        return None
    candidate = Path(current_path).expanduser()
    if candidate.is_file():
        return str(candidate.parent)
    if candidate.is_dir():
        return str(candidate)
    if candidate.parent.is_dir():
        return str(candidate.parent)
    return None


def choose_general_setting_path(kind: str, current_path: Optional[str] = None) -> Optional[str]:
    """Open one allowlisted picker and return the selected absolute path."""
    if kind not in {"process_lasso_log", "rtss_directory", "program_executable", "reshade_preset"}:
        raise ValueError("Unsupported settings path picker")

    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    root.update()
    initial = _initial_directory(current_path)
    try:
        if kind == "process_lasso_log":
            selected = filedialog.askopenfilename(
                parent=root,
                title="Select Process Lasso log",
                initialdir=initial,
                filetypes=(
                    ("Process Lasso logs", "*.log *.csv"),
                    ("Log files", "*.log"),
                    ("CSV files", "*.csv"),
                    ("All files", "*.*"),
                ),
            )
        elif kind == "program_executable":
            selected = filedialog.askopenfilename(
                parent=root,
                title="Select program executable",
                initialdir=initial,
                filetypes=(("Programs", "*.exe"), ("All files", "*.*")),
            )
        elif kind == "reshade_preset":
            selected = filedialog.askopenfilename(
                parent=root,
                title="Select ReShade preset",
                initialdir=initial,
                filetypes=(("ReShade presets", "*.ini"),),
            )
        else:
            selected = filedialog.askdirectory(
                parent=root,
                title="Select RTSS installation directory",
                initialdir=initial,
                mustexist=True,
            )
    finally:
        root.destroy()
    return str(Path(selected).resolve()) if selected else None


def choose_dlssnr_runtime(current_path: Optional[str] = None) -> Optional[str]:
    """Select the user-supplied NVIDIA DLSS neural-rendering runtime."""
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    root.update()
    try:
        selected = filedialog.askopenfilename(
            parent=root,
            title="Select NVIDIA DLSS 5 runtime (nvngx_dlssnr.dll)",
            initialdir=_initial_directory(current_path),
            filetypes=(("DLSS neural-rendering runtime", "nvngx_dlssnr.dll"),),
        )
    finally:
        root.destroy()
    return str(Path(selected).resolve()) if selected else None
