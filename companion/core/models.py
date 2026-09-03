"""
Data models and schemas for Lossless Companion.
"""

from typing import List, Optional, Dict, Any, Literal
from pydantic import BaseModel, Field
import uuid


class HotkeyConfig(BaseModel):
    modifiers: List[str] = Field(default_factory=lambda: ["ctrl", "alt"])
    key: str = "s"
    hold_delay_ms: int = 50
    activation_delay_ms: int = 350  # Delay after fullscreen before pressing hotkey


class ReshadeConfig(BaseModel):
    enabled: bool = False
    reshade_ini_path: Optional[str] = None
    preset_path: Optional[str] = None
    reload_hotkey: Optional[str] = None  # e.g., "Home" or "F8"


class DllOverrideConfig(BaseModel):
    enabled: bool = False
    source_dll_path: str
    target_dll_name: str = "dxgi.dll"  # e.g., dxgi.dll, d3d11.dll, dinput8.dll
    deployment_mode: Literal["copy", "symlink"] = "copy"


class Profile(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    target_process: Optional[str] = None  # e.g., "chrome.exe", "Cyberpunk2077.exe"
    target_domain: Optional[str] = None   # e.g., "youtube.com", "twitch.tv"
    auto_scale_on_fullscreen: bool = True
    auto_scale_on_demaximize: bool = True
    hotkey: HotkeyConfig = Field(default_factory=HotkeyConfig)
    reshade: Optional[ReshadeConfig] = None
    dll_overrides: List[DllOverrideConfig] = Field(default_factory=list)
    custom_notes: Optional[str] = None


class AppConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 24892
    lossless_scaling_exe_path: Optional[str] = r"C:\Program Files (x86)\Steam\steamapps\common\Lossless Scaling\LosslessScaling.exe"
    auto_launch_lossless_scaling: bool = True
    global_hotkey: HotkeyConfig = Field(default_factory=HotkeyConfig)
    active_profile_id: Optional[str] = None
    profiles: List[Profile] = Field(default_factory=list)


class BrowserVideoMeta(BaseModel):
    videoWidth: int = 0
    videoHeight: int = 0
    duration: float = 0
    paused: bool = True
    src: str = ""


class BrowserFullscreenEvent(BaseModel):
    type: str = "BROWSER_FULLSCREEN_EVENT"
    event: Literal["FULLSCREEN_ENTER", "FULLSCREEN_EXIT"]
    domain: str
    url: str
    title: str
    isVideo: bool = True
    video: Optional[BrowserVideoMeta] = None
    processName: str = "chrome.exe"
    timestamp: int
