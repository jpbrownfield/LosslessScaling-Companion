"""Temporary, verified single-GPU setup for adaptive RTSS acceptance."""
from __future__ import annotations

import uuid
from pathlib import Path

from companion.core.models import Profile
from .managed_runtime import assert_private_settings


def select_gpu(inventory, requested):
    matches = [gpu for gpu in inventory.get("gpus", [])
               if requested.casefold() in str(gpu.get("name") or "").casefold()]
    exact = [gpu for gpu in matches if str(gpu["name"]).casefold() == requested.casefold()]
    matches = exact or matches
    if len(matches) != 1 or not matches[0].get("deviceId") or not matches[0].get("lsGpuId"):
        raise RuntimeError(f"The single-GPU test needs one uniquely mapped {requested} adapter; found {len(matches)}. Run GPU calibration if needed.")
    return matches[0]


def verify_gpu_context(source, host, expected_name):
    """Use device LUIDs, never GPU enumeration order, to certify the route."""
    from .dynamic_limiter import DynamicLimiterController
    luid = DynamicLimiterController._luid
    capture, pacing = host.get("capture") or {}, host.get("presentationPacing") or {}
    expected = luid(source.get("adapterLuid"))
    if (not expected or str(source.get("adapter") or "").casefold() != expected_name.casefold()
            or capture.get("adapterSource") != "capture_frame"
            or capture.get("observationVersion", 0) < 2 or capture.get("captures", 0) <= 0
            or luid(capture.get("adapterLuid")) != expected
            or luid(pacing.get("presentationLuid")) != expected):
        raise RuntimeError("Single-GPU routing was not verified: workload, LS capture and LS presentation must all use the requested GPU")
    enabled_nr = [item.get("id") for item in host.get("addons", [])
                  if item.get("id") in {"DLSSNR02", "LSC-AMDNR"} and item.get("enabled")]
    if enabled_nr:
        raise RuntimeError("The independent-capture acceptance test requires NR disabled")
    return {"verified": True, "adapter": source["adapter"], "adapterLuid": source["adapterLuid"],
            "nr_disabled": True, "capture": capture, "presentation": pacing}


class SingleGpuSetup:
    def __init__(self, manager, automation, watcher, requested, directory):
        self.manager, self.automation, self.watcher = manager, automation, watcher
        self.directory = Path(directory)
        self.gpu = select_gpu(automation.gpu_router.detect(), requested)
        default = next((p for p in manager.config.profiles if p.is_default), None)
        self.previous = manager.get_profile_by_id(manager.config.active_profile_id) or default
        self.profile = Profile(id=f"rtss-acceptance-{uuid.uuid4().hex}", name="Temporary single-GPU RTSS acceptance",
                               lossless_profile_title=default.lossless_profile_title if default else None,
                               preferred_gpu_device_id=self.gpu["deviceId"], preferred_gpu_name=self.gpu["name"],
                               addon_config={"addons": {"DLSSNR02": {"_enabled": False}, "LSC-AMDNR": {"_enabled": False}}},
                               native_scaling_settings={"CaptureApi": "WGC", "FrameGeneration": "LSFG3",
                                                        "ScalingType": "Off", "HdrSupport": "false",
                                                        "PreferredGpuId": str(self.gpu["lsGpuId"])})
        self.settings_path = automation.lossless_settings.path
        assert_private_settings(self.settings_path)
        self.settings = self.settings_path.read_bytes()
        self.restore_plan = automation.resolve_deployment_files(self.previous)
        self.was_running = watcher.check_is_lossless_scaling_running()
        self.changed = False

    def apply(self, hwnd, pid, executable):
        if self.watcher.check_is_lossless_scaling_running():
            self.watcher.stop_lossless_scaling()
        if self.watcher.check_is_lossless_scaling_running():
            raise RuntimeError("Could not stop LS before temporary single-GPU setup")
        # Also preserve the exact private XML in the evidence if the application
        # is interrupted before normal restoration. Saved profiles are untouched.
        (self.directory / "original-private-settings.xml").write_bytes(self.settings)
        self.changed = True
        native = self.automation.lossless_settings.get_profile(self.profile.lossless_profile_title)
        if not native or not self.automation.lossless_settings.update_profile(
                native["Title"], self.profile.native_scaling_settings, backup=False):
            raise RuntimeError("Could not apply the temporary WGC/LSFG3/NR-off settings")
        self.automation.activate_profile(self.profile, str(executable), force=True, target_pid=pid,
                                        target_hwnd=hwnd, suppress_auto_scale=True, for_scaling=True)
        if self.automation.state.current_active_profile is not self.profile:
            raise RuntimeError("The temporary single-GPU profile could not be activated")

    def restore(self):
        if not self.changed:
            return
        if self.watcher.check_is_lossless_scaling_running():
            self.watcher.stop_lossless_scaling()
        if self.watcher.check_is_lossless_scaling_running():
            raise RuntimeError("LS is still running; temporary settings/deployment restoration could not proceed")
        errors = []
        try:
            self.automation.deployment_manager.apply(profile_id=self.previous.id if self.previous else None,
                lossless_scaling_exe=self.manager.config.lossless_scaling_exe_path, files=self.restore_plan)
        except (OSError, ValueError, RuntimeError) as error:
            errors.append(f"deployment: {error}")
        try:
            self.settings_path.write_bytes(self.settings)
        except OSError as error:
            errors.append(f"private settings: {error}")
        if errors:
            raise RuntimeError("Single-GPU test restoration failed: "+"; ".join(errors))
        self.automation.state.current_active_profile = self.previous
        if self.was_running and not self.watcher.launch_lossless_scaling(force=True):
            raise RuntimeError("Settings restored, but the prior idle LS instance could not be restarted")
        self.changed = False
