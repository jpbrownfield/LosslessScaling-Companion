"""LS cadence feedback with reversible, bounded RTSS trials.

Runs on Companion's one-second worker, never the presentation thread. GPU engine
counters describe pressure only; game utilization never grades LS health.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import math
import os
import threading
import time

import psutil

from .gpu_telemetry import WindowsGpuTelemetry
from .ls_adaptive import AdaptivePolicy, Window

logger = logging.getLogger("LSCompanion.DynamicLimiter")


@dataclass
class _Session:
    profile_id: str
    current_limit: int
    target_pid: int
    key: tuple
    policy: AdaptivePolicy
    binding: tuple
    source_identity: tuple
    ls_instance: tuple
    profile_snapshot: object
    pending_restore: int | None = None
    generation: tuple | None = None
    sequence: int = 0
    dropped: int | None = None
    intake: tuple | None = None
    last_decision: str = ""
    last_valid_at: float = 0.
    last_gpu_at: float = -1e9
    utilization: dict | None = None
    telemetry_context: dict | None = None


class DynamicLimiterController:
    CALIBRATION_POLICY_VERSION = AdaptivePolicy.VERSION
    MANUAL_SECONDS = 300

    def __init__(self, profile_manager, state, rtss_manager, telemetry=None,
                 time_fn=time.monotonic, process_running_fn=None, process_age_fn=None,
                 *, identity_fn=None, ls_instance_fn=None, presentation_fn=None,
                 output_window_fn=None, shadow_only=False):
        self.profile_manager, self.state, self.rtss_manager = profile_manager, state, rtss_manager
        self.telemetry = telemetry or WindowsGpuTelemetry()
        self.time_fn = time_fn
        self.process_running_fn = process_running_fn or self._process_running
        self.process_age_fn = process_age_fn
        self.identity_fn = identity_fn or self._source_identity
        if ls_instance_fn is None:
            from .adapter_report import configured_process
            ls_instance_fn = configured_process
        if presentation_fn is None:
            from .addon_host import presentation_status
            presentation_fn = presentation_status
        if output_window_fn is None:
            from .ls_inspector import LosslessScalingInspector
            output_window_fn = lambda exe: LosslessScalingInspector.detect_lossless_scaling_overlay_window(exe)[1]
        self.ls_instance_fn, self.presentation_fn, self.output_window_fn = ls_instance_fn, presentation_fn, output_window_fn
        self.shadow_only = shadow_only
        self._lock = threading.RLock()
        self._session = None
        self._pending_sessions = []  # no adaptive observations are persisted after exit
        self._manual_profile_id, self._manual_until = None, 0.
        self._lossless_pid = None

    @staticmethod
    def effective_mode(profile, global_mode):
        return global_mode if profile.rtss.limit_mode == "inherit" else profile.rtss.limit_mode

    def configured_limit(self, profile):
        if self.effective_mode(profile, self.profile_manager.config.rtss_default_limit_mode) == "static":
            return profile.rtss.framerate_limit
        # Explicit manual locks survive policy upgrades; automatic headroom-only
        # results are incompatible with LS-performance feedback.
        if profile.rtss.automatic_calibration_disabled and profile.rtss.learned_framerate_limit:
            return profile.rtss.learned_framerate_limit
        return profile.rtss.maximum_framerate_limit

    def reset(self, profile_id):
        with self._lock:
            profile = self._dynamic_profile(profile_id)
            self._discard("Calibration reset")
            profile.rtss.learned_framerate_limit = None
            profile.rtss.game_gpu_baseline_percent = None
            profile.rtss.game_gpu_high_water_percent = None
            profile.rtss.automatic_calibration_disabled = False
            profile.rtss.calibration_policy_version = self.CALIBRATION_POLICY_VERSION
            self._manual_profile_id, self._manual_until = None, 0.
            self.profile_manager.save_config()
            if self.profile_manager.config.rtss_frame_limiting_enabled and profile.rtss.managed_target_process:
                self._write(profile, profile.rtss.maximum_framerate_limit)
            return self._status("observing", "Calibration reset; waiting for verified LS timing", profile)

    def start_manual(self, profile_id):
        with self._lock:
            profile = self._dynamic_profile(profile_id)
            active = self.state.current_active_profile
            if not (self.profile_manager.config.rtss_frame_limiting_enabled and self.state.is_scaling_active and active and active.id == profile_id):
                raise RuntimeError("Manual calibration requires this profile to be active and currently scaling")
            self._discard("Manual calibration started")
            self._manual_profile_id, self._manual_until = profile_id, self.time_fn()+self.MANUAL_SECONDS
            profile.rtss.automatic_calibration_disabled = False
            profile.rtss.learned_framerate_limit = None
            profile.rtss.calibration_policy_version = self.CALIBRATION_POLICY_VERSION
            self.profile_manager.save_config()
            return self._status("manual_calibration", "Collecting LS evidence for five minutes", profile)

    def poll(self):
        with self._lock:
            try:
                return self._poll()
            except (OSError, ValueError, RuntimeError, KeyError, TypeError, psutil.Error) as error:
                return self._unavailable(str(error))

    def _poll(self):
        if self.state.update_in_progress or self.state.benchmark_mode_active:
            # Ownership handoff must never write into the benchmark's temporary
            # profile. Forget policy history; its owner restores its own RTSS state.
            self._session = None
            return self._status("inactive", "Maintenance or dedicated benchmark owns scaling")
        profile = self._active_dynamic_profile()
        if profile is None:
            self._discard("Scaling/profile stopped")
            return self._status("inactive", "Dynamic limiter inactive")
        if not self.profile_manager.config.addon_manager_enabled:
            return self._unavailable("Enable the Companion add-on host for verified LS timing", profile)
        target = self.state.current_scaled_target
        pid = int(target.get("pid") or self.state.scaling_target_pid or 0)
        source = int(target.get("hwnd") or self.state.scaling_target_hwnd or 0)
        if not pid or not source or not self.process_running_fn(pid):
            return self._unavailable("Waiting for a live scaling source process and window", profile)
        identity = self.identity_fn(pid, profile)
        exe = self.profile_manager.config.lossless_scaling_exe_path
        instance = self.ls_instance_fn(exe)
        if not instance:
            return self._unavailable("Configured managed LS process is unavailable", profile)
        self._lossless_pid = instance[0]
        output = int(self.output_window_fn(exe) or 0)
        if not output:
            return self._unavailable("Verified LS output window is unavailable", profile)
        settings = {"graphics": profile.graphics.model_dump(mode="json"),
                    "native": profile.native_scaling_settings,
                    "addons": profile.addon_config,
                    "reshade": profile.reshade.model_dump(mode="json") if profile.reshade else None,
                    "host": self.profile_manager.config.addon_manager.model_dump(mode="json"),
                    "hostEnabled": self.profile_manager.config.addon_manager_enabled}
        key = (profile.id, identity, instance, source, output, self.state.last_scale_toggle_time,
               json.dumps(settings, sort_keys=True), profile.rtss.minimum_framerate_limit,
               profile.rtss.maximum_framerate_limit, profile.rtss.gpu_target_percent,
               self.profile_manager.config.rtss_default_gpu_target_percent, profile.rtss.limit_method,
               profile.rtss.automatic_calibration_disabled, profile.rtss.adaptive_shadow_only)
        if self._session is None or self._session.key != key:
            if self._session and self._session.target_pid == pid and self._session.source_identity != identity:
                # The new incarnation has just been verified against this profile.
                # Its initial cap replaces the stale incarnation's unfinished trial.
                self._session = None
            try:
                self._discard("Scaling process, settings, or session identity changed")
            except (OSError, ValueError, RuntimeError, psutil.Error):
                # A newly verified session applies its own configured initial
                # cap. A prior target's externally edited entry is left alone.
                logger.warning("Previous trial could not be restored during session change", exc_info=True)
                self._session = None
            self._migrate(profile)
            current = max(profile.rtss.minimum_framerate_limit, min(profile.rtss.maximum_framerate_limit, self.configured_limit(profile)))
            reserve = profile.rtss.gpu_target_percent or self.profile_manager.config.rtss_default_gpu_target_percent
            context = int.from_bytes(os.urandom(8), "little") or 1
            policy = AdaptivePolicy(profile.rtss.minimum_framerate_limit, profile.rtss.maximum_framerate_limit, current, reserve, shadow_only=self.shadow_only or profile.rtss.adaptive_shadow_only)
            self._write(profile, current)
            self._session = _Session(profile.id, current, pid, key, policy, (pid, source, output, context), identity, instance, profile.model_copy(deep=True))
            self.profile_manager.save_config()
        session = self._session
        manual_remaining = self._manual_remaining(profile)
        if profile.rtss.automatic_calibration_disabled and not manual_remaining:
            return self._status("manual_calibration_locked", "Saved manual cap is locked until reset", profile)
        host = self.presentation_fn(exe, instance, binding=session.binding)
        pacing = host.get("presentationPacing") or {}
        if not pacing.get("enabled"):
            return self._unavailable("Presentation monitoring is disabled", profile)
        if not pacing.get("identityVerified") or not pacing.get("focused") or any(pacing.get(k) != v for k,v in zip(("sourcePid", "sourceWindow", "outputWindow", "context"), session.binding)):
            return self._unavailable("Waiting for focused, bound LS presentation identity", profile)
        capture = host.get("capture") or {}
        session.telemetry_context = {
            "lsPid": instance[0], "lsCreatedAt": instance[1], "sourcePid": pid,
            "sourceCreatedAt": identity[1],
            "captureLuid": capture.get("adapterLuid"), "captureAdapterSource": capture.get("adapterSource"),
            "presentationLuid": pacing.get("presentationLuid"),
            **{k:capture.get(k) for k in ("captureApi", "frameGeneration", "scaler", "hdr", "width", "height")},
            **{k:pacing.get(k) for k in ("monitor", "refreshHz", "monitorWidth", "monitorHeight")},
        }
        generation = (pacing.get("generation"), pacing.get("swapchain"), pacing.get("device"),
                      pacing.get("presentationLuid"), capture.get("adapterLuid"), capture.get("captureApi"),
                      capture.get("frameGeneration"), capture.get("scaler"), capture.get("hdr"),
                      capture.get("width"), capture.get("height"), capture.get("settingsAppliedTick"))
        if not generation[0] or not generation[1] or not generation[2] or not generation[3]:
            return self._unavailable("Presentation device identity is missing", profile)
        if session.generation != generation:
            self._interrupt(session, profile, "Presentation/capture context changed")
            session.generation, session.sequence, session.dropped, session.intake = generation, 0, None, None
            # New context starts with warm-up, not the old context's cooldown.
            session.policy.cooldown_until = 0
        age = pacing.get("ageMs", float("inf"))
        if not isinstance(age, (float, int)) or not math.isfinite(age) or not 0 <= age <= 3000:
            return self._unavailable("LS timing is stale or paused", profile)
        window = Window.parse(pacing)
        if window.sequence == session.sequence:
            if self.time_fn()-session.last_valid_at > 5:
                return self._unavailable("No new completed LS timing window", profile)
            if self._manual_profile_id == profile.id and self.time_fn() >= self._manual_until:
                progressing = session.intake and capture.get("captures",0) > session.intake[1]
                self._manual_remaining(profile, verified=True, allow_save=bool(progressing and self.rtss_manager.read_limit(profile)==session.current_limit))
            return self._status("sampling", "Waiting for the next disjoint LS timing window", profile, pacing=pacing)
        if session.sequence and window.sequence != session.sequence+1:
            self._interrupt(session, profile, "Timing windows were missed")
        session.sequence = window.sequence
        dropped = pacing.get("dropped")
        if type(dropped) is not int or dropped != 0 or pacing.get("overflow"):
            session.dropped = dropped
            return self._unavailable("Dropped or overflowed timing observations", profile)
        session.dropped = dropped
        # Capture intake is supporting actuator-response evidence, never game
        # smoothness or proof of displayed/generated frames.
        stamp, captures = host.get("sampledAt"), capture.get("captures")
        intake = None
        if (isinstance(stamp, (int,float)) and isinstance(captures, int)
                and capture.get("adapterSource") == "capture_frame"):
            if session.intake:
                old_stamp, old_count = session.intake
                if captures <= old_count:
                    return self._unavailable("Source capture is paused or restarted; waiting for fresh intake", profile)
                if 1 <= stamp-old_stamp <= 4 and captures > old_count:
                    intake = (captures-old_count)/(stamp-old_stamp)
            session.intake = stamp, captures
        from dataclasses import replace
        window = replace(window, intake_fps=intake)
        if self.time_fn()-session.last_gpu_at >= 2:
            session.utilization = self.telemetry.sample_for_scaling(pid, instance[0]) or {}
            session.last_gpu_at = self.time_fn()
        adapters = session.utilization.get("gpu_adapters", [])
        known = {self._luid(x) for x in adapters}
        expected = {self._luid(pacing["presentationLuid"]), self._luid(capture.get("adapterLuid", ""))} - {None}
        if not adapters or not expected.issubset(known):
            return self._unavailable("GPU headroom cannot be joined to capture/presentation adapter LUIDs", profile)
        verified = self.rtss_manager.read_limit(profile) == session.current_limit
        if not verified:
            return self._unavailable("RTSS stored cap changed or readback is unavailable", profile)
        decision = session.policy.observe(window, readback_verified=verified)
        session.last_valid_at = self.time_fn()
        session.last_decision = decision.state
        self._manual_remaining(profile, verified=True)
        if decision.limit is not None:
            session.current_limit = decision.limit
            if session.policy.trial is None:
                session.pending_restore = decision.limit
            self._write(profile, decision.limit)
            session.pending_restore = None
        return self._status(decision.state, decision.reason, profile, pacing=pacing,
                            benefit_confirmed=decision.benefit, cap_effect=decision.cap_effect,
                            rtss_profile_verified=verified, intake_fps=intake)

    @staticmethod
    def _luid(value):
        try:
            parts = value.lower().replace("0x", "").replace("luid_", "").replace(":", "_").split("_")
            return int(parts[0],16), int(parts[1],16)
        except (AttributeError, IndexError, ValueError):
            return None

    def _write(self, profile, limit):
        self.rtss_manager.apply_profile(profile, profile if profile.rtss.managed_target_process else None,
                                        self.profile_manager.config.rtss_install_path, framerate_limit=limit)
        if self.rtss_manager.read_limit(profile) != limit:
            raise RuntimeError("RTSS profile readback did not match the requested limit")

    def _interrupt(self, session, profile, reason):
        decision = session.policy.interrupt(self.time_fn()*1000, reason)
        if decision.limit is not None:
            session.pending_restore = decision.limit
        if session.pending_restore is not None:
            original = session.profile_snapshot
            readback = self.rtss_manager.read_limit(original)
            if readback == session.pending_restore:
                session.current_limit = session.pending_restore
                session.pending_restore = None
                return
            if readback != session.current_limit:
                raise RuntimeError("RTSS cap was edited externally; refusing to overwrite it during rollback")
            if self.process_running_fn(session.target_pid) and self.identity_fn(session.target_pid, original) != session.source_identity:
                raise RuntimeError("Source PID was reused; rollback awaits a verified target")
            self._write(original, session.pending_restore)
            session.current_limit = session.pending_restore
            session.pending_restore = None
        session.intake = None

    def _unavailable(self, reason, profile=None):
        session = self._session
        if session:
            profile = profile or self.profile_manager.get_profile_by_id(session.profile_id)
            if profile:
                try:
                    self._interrupt(session, profile, reason)
                except (OSError, ValueError, RuntimeError, psutil.Error) as error:
                    reason += "; rollback unavailable: "+str(error)
            session.last_valid_at = 0
        if profile and self._manual_profile_id == profile.id and self.time_fn() >= self._manual_until:
            self._manual_remaining(profile, verified=True, allow_save=False)
            reason += "; manual calibration ended without fresh verified evidence"
        return self._status("unavailable", reason, profile)

    def _discard(self, reason):
        session = self._session
        if session:
            profile = self.profile_manager.get_profile_by_id(session.profile_id)
            if profile:
                self._interrupt(session, profile, reason)
        self._session = None

    def _migrate(self, profile):
        if profile.rtss.calibration_policy_version != self.CALIBRATION_POLICY_VERSION:
            if not profile.rtss.automatic_calibration_disabled:
                profile.rtss.learned_framerate_limit = None
            profile.rtss.game_gpu_baseline_percent = None
            profile.rtss.game_gpu_high_water_percent = None
            profile.rtss.calibration_policy_version = self.CALIBRATION_POLICY_VERSION
            self.profile_manager.save_config()

    def _manual_remaining(self, profile, *, verified=False, allow_save=True):
        if self._manual_profile_id != profile.id:
            return 0
        remaining = max(0, int(self._manual_until-self.time_fn()))
        if not remaining and verified:
            session = self._session
            if allow_save and session and session.policy.valid_windows >= 3 and self.time_fn()-session.last_valid_at <= 5 and session.last_decision in {"stable", "benefit_confirmed", "recovered"}:
                self._interrupt(session, profile, "Manual calibration complete")
                profile.rtss.learned_framerate_limit = session.policy.confirmed_limit
                profile.rtss.automatic_calibration_disabled = True
                self.profile_manager.save_config()
            self._manual_profile_id, self._manual_until = None, 0.
        return remaining

    def clear_sessions(self):
        with self._lock:
            self._discard("Sessions reset")
            self._pending_sessions.clear()
            self._manual_profile_id, self._manual_until = None, 0.
            self._lossless_pid = None

    def close(self):
        with self._lock:
            try:
                self._discard("Companion stopped")
            except (OSError, ValueError, RuntimeError, psutil.Error):
                logger.exception("Could not restore the unfinished RTSS trial on shutdown")
            finally:
                self.telemetry.close()

    def _active_dynamic_profile(self):
        if not self.profile_manager.config.rtss_frame_limiting_enabled or not self.state.is_scaling_active:
            return None
        active = self.state.current_active_profile
        profile = self.profile_manager.get_profile_by_id(active.id) if active else None
        return profile if profile and profile.rtss.enabled and self.effective_mode(profile, self.profile_manager.config.rtss_default_limit_mode) == "dynamic" else None

    def _dynamic_profile(self, profile_id):
        profile = self.profile_manager.get_profile_by_id(profile_id)
        if profile is None or not profile.rtss.enabled or self.effective_mode(profile, self.profile_manager.config.rtss_default_limit_mode) != "dynamic":
            raise ValueError("Profile is not using dynamic RTSS limiting")
        return profile

    @staticmethod
    def _process_running(pid):
        try:
            return psutil.Process(pid).is_running()
        except psutil.Error:
            return False

    @staticmethod
    def _source_identity(pid, profile):
        process = psutil.Process(pid)
        path = process.exe()
        target = profile.target_executable_path
        targets = [x for x in [target, *profile.target_executable_paths] if x]
        if targets and os.path.normcase(os.path.abspath(path)) not in {os.path.normcase(os.path.abspath(x)) for x in targets}:
            raise ValueError("Scaled process does not match the RTSS executable")
        name = os.path.basename(path)
        expected = profile.target_process or os.path.basename(target or "")
        names = {x.casefold() for x in [expected, *profile.target_processes] if x}
        if names and name.casefold() not in names:
            raise ValueError("Scaled process name does not match the RTSS target")
        # RTSS selects by executable basename. Refuse ambiguous multiple copies.
        for other in psutil.process_iter(["pid", "name"]):
            if other.info["pid"] != pid and (other.info.get("name") or "").casefold() == name.casefold():
                raise ValueError("Multiple instances share this RTSS executable profile")
        return pid, process.create_time(), os.path.normcase(path)

    def _status(self, state, message, profile=None, **values):
        session = self._session
        policy = session.policy if session else None
        utilization = (session.utilization or {}) if session else {}
        reserve = policy.reserve if policy else (profile.rtss.gpu_target_percent if profile else None)
        previous = self.state.dynamic_limiter_status or {}
        payload = {"state": state, "message": message, "profileId": profile.id if profile else None,
                   "current_limit": session.current_limit if session else None,
                   **utilization, "target_percent": reserve,
                   "extra_reserve_percent": policy.extra if policy else 0,
                   "effective_reserve_percent": min(100., reserve+policy.extra) if policy else reserve,
                   "target_scope": "LS relative Present cadence; capture intake verifies actuator response only",
                   "absolute_delivery_verified": False,
                   "manual_remaining_seconds": max(0, int(self._manual_until-self.time_fn())) if self._manual_profile_id else 0,
                   "comparison": policy.last_comparison if policy else {},
                   "telemetry_context": session.telemetry_context if session else None,
                   "shadow_only": policy.shadow_only if policy else self.shadow_only,
                   "pressure_suspected": bool(policy and utilization.get("gpu_headroom_percent") is not None and utilization["gpu_headroom_percent"] < min(100.,policy.reserve+policy.extra)), **values}
        if (previous.get("state"), previous.get("message"), previous.get("profileId")) != (state, message, payload["profileId"]):
            routine = state in {"sampling", "observing", "stable", "settling"}
            logger.log(logging.DEBUG if routine else logging.INFO, "RTSS limiter [%s] %s (profile=%s)", state, message, payload["profileId"])
        self.state.dynamic_limiter_status = payload
        return payload
