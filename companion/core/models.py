"""
Data models and schemas for Lossless Companion.
"""

from typing import List, Optional, Dict, Any, Literal
from pathlib import Path
from pydantic import BaseModel, Field, field_validator, model_validator
import uuid


class HotkeyConfig(BaseModel):
    modifiers: List[str] = Field(default_factory=lambda: ["ctrl", "alt"])
    key: str = "s"
    hold_delay_ms: int = Field(default=50, ge=30, le=5000)
    activation_delay_ms: int = Field(default=350, ge=0, le=60000)

    @field_validator("modifiers")
    @classmethod
    def validate_modifiers(cls, values: List[str]) -> List[str]:
        aliases = {"control": "ctrl", "ctl": "ctrl", "windows": "win", "meta": "win"}
        allowed = ("ctrl", "alt", "shift", "win")
        normalized = []
        for raw in values:
            value = aliases.get(str(raw).strip().casefold(), str(raw).strip().casefold())
            if value not in allowed:
                raise ValueError(f"Unsupported hotkey modifier: {raw}")
            if value not in normalized:
                normalized.append(value)
        return [value for value in allowed if value in normalized]

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if not normalized or len(normalized) > 16 or not normalized.replace("_", "").isalnum():
            raise ValueError("hotkey key must be a short letter, number, or named key")
        return normalized


class ManagedPackageConfig(BaseModel):
    enabled: bool = False
    version: Optional[str] = None
    channel: Literal["stable", "prerelease"] = "stable"
    update_policy: Literal["pinned", "notify", "stage"] = "notify"


class NeuralRenderConfig(BaseModel):
    implementation: Literal["disabled", "lsp_neural_render", "ls_reshade_feeder"] = "disabled"
    package: ManagedPackageConfig = Field(default_factory=ManagedPackageConfig)
    runtime_asset_sha256: Optional[str] = None
    style: Literal["standard", "natural", "cinematic"] = "standard"
    intensity: float = Field(default=1.0, ge=0.0, le=1.0)
    local_structure: float = Field(default=1.0, ge=-10.0, le=10.0)
    local_tone: float = Field(default=1.0, ge=-10.0, le=10.0)
    skin_structure: float = Field(default=-1.0, ge=-10.0, le=10.0)
    auto_skin_mask: bool = True
    use_lsfg_optical_flow: bool = True
    working_scale: float = Field(default=0.35, ge=0.1, le=1.0)
    prioritize_ls_gpu_work: bool = True
    apply_strength: float = Field(default=1.0, ge=0.0, le=4.0)
    max_delta: float = Field(default=0.5, ge=0.0, le=1.0)
    protect_highlights_above: float = Field(default=0.85, ge=0.0, le=1.0)
    watchdog_ms: int = Field(default=80, ge=10, le=5000)


class GraphicsStackConfig(BaseModel):
    lossless_proxy: ManagedPackageConfig = Field(default_factory=ManagedPackageConfig)
    neural_render: NeuralRenderConfig = Field(default_factory=NeuralRenderConfig)
    reshade: ManagedPackageConfig = Field(default_factory=ManagedPackageConfig)
    special_k: ManagedPackageConfig = Field(default_factory=ManagedPackageConfig)


class ReshadeConfig(BaseModel):
    enabled: bool = False
    # Retained for legacy configuration compatibility. Runtime deployment always
    # targets ReShade.ini beside LosslessScaling.exe.
    reshade_ini_path: Optional[str] = None
    preset_path: Optional[str] = None
    reload_hotkey: Optional[str] = None  # e.g., "Home" or "F8"


class RtssLimiterConfig(BaseModel):
    enabled: bool = False
    limit_mode: Literal["inherit", "static", "dynamic"] = "inherit"
    framerate_limit: int = Field(default=60, ge=1, le=1000)
    gpu_target_percent: Optional[float] = Field(default=None, ge=1.0, le=100.0)
    minimum_framerate_limit: int = Field(default=30, ge=1, le=1000)
    maximum_framerate_limit: int = Field(default=240, ge=1, le=1000)
    limit_method: Literal[
        "async", "front_edge_sync", "back_edge_sync", "nvidia_reflex"
    ] = "async"
    learned_framerate_limit: Optional[int] = Field(default=None, ge=1, le=1000)
    game_gpu_baseline_percent: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    game_gpu_high_water_percent: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    # A completed manual calibration is authoritative until the user resets it.
    automatic_calibration_disabled: bool = False
    # Bookkeeping is persisted so disabling/deleting can safely undo only the
    # values this helper changed in RTSS.
    managed_profile_created: bool = False
    managed_target_process: Optional[str] = None
    managed_install_path: Optional[str] = None
    original_values: Dict[str, Optional[int]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_dynamic_range(self):
        if self.maximum_framerate_limit < self.minimum_framerate_limit:
            raise ValueError("maximum RTSS framerate must be at least the minimum")
        if self.learned_framerate_limit is not None and not (
            self.minimum_framerate_limit
            <= self.learned_framerate_limit
            <= self.maximum_framerate_limit
        ):
            raise ValueError("learned RTSS limit must be inside the configured range")
        return self


class DllOverrideConfig(BaseModel):
    enabled: bool = False
    source_dll_path: str
    target_dll_name: str = "dxgi.dll"  # e.g., dxgi.dll, d3d11.dll, dinput8.dll
    deployment_mode: Literal["copy", "symlink"] = "copy"
    # target_application remains parseable so old settings do not invalidate the
    # entire configuration, but automation refuses to deploy it.
    deployment_target: Literal["target_application", "lossless_scaling"] = "lossless_scaling"

    @field_validator("deployment_mode")
    @classmethod
    def production_copy_only(cls, value: str) -> str:
        # Legacy symlink profiles remain loadable, but elevated runtime assets are
        # snapshotted/copied so their contents cannot change behind the manifest.
        return "copy"

    @field_validator("target_dll_name")
    @classmethod
    def validate_target_dll_name(cls, value: str) -> str:
        if not value or value != Path(value).name or not value.casefold().endswith(".dll"):
            raise ValueError("target_dll_name must be a DLL filename without a path")
        return value


class Profile(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    target_process: Optional[str] = None  # e.g., "chrome.exe", "Cyberpunk2077.exe"
    target_executable_path: Optional[str] = None
    target_domain: Optional[str] = None   # e.g., "youtube.com", "twitch.tv"
    auto_scale: bool = False
    hotkey: HotkeyConfig = Field(default_factory=HotkeyConfig)
    lossless_profile_title: Optional[str] = None
    lossless_profile_path: Optional[str] = None
    last_imported_hash: Optional[str] = None
    native_scaling_settings: Dict[str, Any] = Field(default_factory=dict)
    graphics: GraphicsStackConfig = Field(default_factory=GraphicsStackConfig)
    rtss: RtssLimiterConfig = Field(default_factory=RtssLimiterConfig)
    reshade: Optional[ReshadeConfig] = None
    dll_overrides: List[DllOverrideConfig] = Field(default_factory=list)
    custom_notes: Optional[str] = None

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not value or len(value) > 128 or any(
            char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
            for char in value
        ):
            raise ValueError("profile id may contain only letters, numbers, dot, underscore, and dash")
        return value


class AppConfig(BaseModel):
    host: Literal["127.0.0.1", "localhost", "::1"] = "127.0.0.1"
    port: int = Field(default=24892, ge=1, le=65535)
    lossless_scaling_exe_path: Optional[str] = r"C:\Program Files (x86)\Steam\steamapps\common\Lossless Scaling\LosslessScaling.exe"
    auto_launch_lossless_scaling: bool = True
    disable_native_auto_scale: bool = True
    lossless_control_configured: bool = False
    hotkey_sync_mode: Literal["helper_controls_lossless", "follow_lossless", "warn_only"] = "helper_controls_lossless"
    lossless_settings_xml_path: Optional[str] = None
    asset_store_path: Optional[str] = None
    update_check_interval_hours: int = Field(default=24, ge=1, le=720)
    default_profile_auto_scale: bool = True
    run_at_startup: bool = False
    minimize_other_windows_on_scale: bool = False
    process_lasso_performance_mode_scaling: bool = False
    process_lasso_log_path: Optional[str] = None
    rtss_frame_limiting_enabled: bool = False
    rtss_install_path: Optional[str] = None
    rtss_default_limit_mode: Literal["static", "dynamic"] = "static"
    rtss_default_gpu_target_percent: float = Field(default=15.0, ge=1.0, le=100.0)
    global_hotkey: HotkeyConfig = Field(default_factory=HotkeyConfig)
    allowed_websocket_origins: List[str] = Field(
        default_factory=lambda: ["chrome-extension://"]
    )
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
