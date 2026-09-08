"""Conservative RTSS calibration from game and Lossless Scaling GPU activity."""

from __future__ import annotations

import logging
import statistics
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import psutil

from ..core.models import Profile
from ..core.profile_manager import ProfileManager
from ..core.state import AppState
from .gpu_telemetry import WindowsGpuTelemetry
from .rtss_manager import RtssProfileManager


logger = logging.getLogger("LSCompanion.DynamicLimiter")


@dataclass
class _Session:
    profile_id: str
    current_limit: int
    target_pid: int = 0
    started_at: float = 0.0
    samples: list[tuple[float, float]] = field(default_factory=list)
    windows_seen: int = 0
    recovery_windows: int = 0
    loading: bool = False
    paused_no_response: bool = False
    response_anchor: Optional[float] = None
    adjustments_since_anchor: int = 0
    valid_windows: int = 0
    observed_high_water: float = 0.0
    observed_baseline: float = 0.0


class DynamicLimiterController:
    WINDOW_SAMPLES = 5
    MANUAL_SECONDS = 5 * 60
    TARGET_DEADBAND = 0.0
    GAMEPLAY_MINIMUM_PERCENT = 20.0
    GAMEPLAY_RATIO = 0.75
    LOADING_RELATIVE_RATIO = 0.60
    LOADING_ABSOLUTE_DROP = 15.0
    ALTERNATE_BASELINE_SECONDS = 10 * 60
    ALTERNATE_BASELINE_MINIMUM_PERCENT = 50.0

    def __init__(
        self,
        profile_manager: ProfileManager,
        state: AppState,
        rtss_manager: RtssProfileManager,
        telemetry: Optional[WindowsGpuTelemetry] = None,
        time_fn: Callable[[], float] = time.monotonic,
        process_running_fn: Optional[Callable[[int], bool]] = None,
        process_age_fn: Optional[Callable[[int], float]] = None,
    ):
        self.profile_manager = profile_manager
        self.state = state
        self.rtss_manager = rtss_manager
        self.telemetry = telemetry or WindowsGpuTelemetry()
        self.time_fn = time_fn
        self.process_running_fn = process_running_fn or self._process_running
        self.process_age_fn = process_age_fn or self._process_age
        self._lock = threading.RLock()
        self._session: Optional[_Session] = None
        self._pending_sessions: list[_Session] = []
        self._manual_profile_id: Optional[str] = None
        self._manual_until = 0.0
        self._lossless_pid: Optional[int] = None

    @staticmethod
    def effective_mode(profile: Profile, global_mode: str) -> str:
        return global_mode if profile.rtss.limit_mode == "inherit" else profile.rtss.limit_mode

    def configured_limit(self, profile: Profile) -> int:
        if self.effective_mode(profile, self.profile_manager.config.rtss_default_limit_mode) == "static":
            return profile.rtss.framerate_limit
        return profile.rtss.learned_framerate_limit or profile.rtss.maximum_framerate_limit

    def reset(self, profile_id: str) -> dict:
        with self._lock:
            profile = self._dynamic_profile(profile_id)
            profile.rtss.learned_framerate_limit = None
            profile.rtss.game_gpu_baseline_percent = None
            profile.rtss.game_gpu_high_water_percent = None
            profile.rtss.automatic_calibration_disabled = False
            self.profile_manager.save_config()
            if self._session and self._session.profile_id == profile_id:
                self._session = _Session(
                    profile_id,
                    profile.rtss.maximum_framerate_limit,
                    target_pid=self._session.target_pid,
                    started_at=self._session.started_at,
                )
            self._pending_sessions = [
                session for session in self._pending_sessions
                if session.profile_id != profile_id
            ]
            if self._manual_profile_id == profile_id:
                self._manual_profile_id = None
                self._manual_until = 0.0
            if (
                self.profile_manager.config.rtss_frame_limiting_enabled
                and profile.rtss.enabled
                and profile.rtss.managed_target_process
            ):
                self.rtss_manager.apply_profile(
                    profile,
                    profile,
                    self.profile_manager.config.rtss_install_path,
                    framerate_limit=profile.rtss.maximum_framerate_limit,
                )
                self.profile_manager.save_config()
            return self._set_status(
                "waiting_for_gameplay",
                "Calibration reset; waiting for representative gameplay",
                profile=profile,
                current_limit=profile.rtss.maximum_framerate_limit,
            )

    def start_manual(self, profile_id: str) -> dict:
        with self._lock:
            profile = self._dynamic_profile(profile_id)
            if not self.profile_manager.config.rtss_frame_limiting_enabled:
                raise RuntimeError("Turn on RTSS Frame Limiting in General Settings first")
            active_id = self.state.current_active_profile.id if self.state.current_active_profile else None
            if active_id != profile_id or not self.state.is_scaling_active:
                raise RuntimeError(
                    "Manual calibration requires this profile to be active and currently scaling"
                )
            self._manual_profile_id = profile_id
            self._manual_until = self.time_fn() + self.MANUAL_SECONDS
            profile.rtss.automatic_calibration_disabled = False
            target_pid = int(self.state.current_scaled_target.get("pid") or 0)
            self._session = _Session(
                profile_id,
                profile.rtss.maximum_framerate_limit,
                target_pid=target_pid,
                started_at=self.time_fn() - self.process_age_fn(target_pid),
            )
            self.rtss_manager.apply_profile(
                profile,
                profile if profile.rtss.managed_target_process else None,
                self.profile_manager.config.rtss_install_path,
                framerate_limit=profile.rtss.maximum_framerate_limit,
            )
            self.profile_manager.save_config()
            return self._set_status(
                "manual_calibration",
                "Manual calibration active; play a representative section",
                profile=profile,
                current_limit=profile.rtss.maximum_framerate_limit,
                manual_remaining_seconds=self.MANUAL_SECONDS,
            )

    def poll(self) -> dict:
        with self._lock:
            self._finish_ended_session()
            profile = self._active_dynamic_profile()
            if profile is None:
                if self._session is not None:
                    return self._set_status(
                        "session_tracking",
                        "Scaling stopped; waiting for the game session to close",
                        profile=self.profile_manager.get_profile_by_id(self._session.profile_id),
                        current_limit=self._session.current_limit,
                    )
                return self._set_status("inactive", "Dynamic limiter inactive")
            target_pid = int(self.state.current_scaled_target.get("pid") or 0)
            if not target_pid:
                return self._set_status(
                    "telemetry_unavailable",
                    "Waiting for the scaled window process ID",
                    profile=profile,
                )
            if not self.process_running_fn(target_pid):
                return self._set_status(
                    "telemetry_unavailable",
                    "Waiting for scaling state to reconcile after the game exited",
                    profile=profile,
                )
            session = self._ensure_session(profile, target_pid)
            manual_remaining = self._manual_remaining(profile.id)
            if profile.rtss.automatic_calibration_disabled and manual_remaining == 0:
                return self._set_status(
                    "manual_calibration_locked",
                    "Manual calibration is saved; automatic learning is disabled until reset",
                    profile=profile,
                    current_limit=session.current_limit,
                )
            lossless_pid = self._find_lossless_pid()
            if not lossless_pid:
                return self._set_status(
                    "telemetry_unavailable",
                    "Waiting for LosslessScaling.exe GPU telemetry",
                    profile=profile,
                )
            utilization = self.telemetry.sample_by_pid()
            if target_pid not in utilization or lossless_pid not in utilization:
                return self._set_status(
                    "telemetry_unavailable",
                    "Waiting for valid game and Lossless Scaling GPU samples",
                    profile=profile,
                )
            session.samples.append((utilization[target_pid], utilization[lossless_pid]))
            if len(session.samples) < self.WINDOW_SAMPLES:
                return self._status_with_samples(
                    "sampling", "Collecting GPU samples", profile, session,
                    utilization[target_pid], utilization[lossless_pid], manual_remaining,
                )
            game_gpu = statistics.median(value[0] for value in session.samples)
            lossless_gpu = statistics.median(value[1] for value in session.samples)
            session.samples.clear()
            return self._evaluate(profile, session, game_gpu, lossless_gpu, manual_remaining)

    def close(self) -> None:
        # A companion shutdown is not proof that the game session ended. Keep
        # candidates volatile rather than promoting an incomplete session.
        self.telemetry.close()

    def _evaluate(
        self,
        profile: Profile,
        session: _Session,
        game_gpu: float,
        lossless_gpu: float,
        manual_remaining: int,
    ) -> dict:
        session.windows_seen += 1
        session.valid_windows += 1
        is_manual = manual_remaining > 0
        stored_high = 0.0 if is_manual else (profile.rtss.game_gpu_high_water_percent or 0.0)
        stored_baseline = 0.0 if is_manual else (profile.rtss.game_gpu_baseline_percent or 0.0)
        previous_high = session.observed_high_water
        reference = max(stored_baseline, stored_high, previous_high if not stored_high else 0.0)
        high_water = max(previous_high, game_gpu)
        session.observed_high_water = high_water
        dramatic_drop = (
            reference - game_gpu >= self.LOADING_ABSOLUTE_DROP
            and game_gpu <= reference * self.LOADING_RELATIVE_RATIO
        )
        if dramatic_drop:
            session.loading = True
            session.recovery_windows = 0
        elif session.loading:
            if reference <= 0 or game_gpu >= reference * self.GAMEPLAY_RATIO:
                session.recovery_windows += 1
                if session.recovery_windows >= 2:
                    session.loading = False
                    session.recovery_windows = 0
            else:
                session.recovery_windows = 0
        if session.loading:
            return self._status_with_samples(
                "loading",
                "Menu or loading activity detected; frame limit frozen",
                profile, session, game_gpu, lossless_gpu, manual_remaining,
            )

        qualified = is_manual or (
            session.windows_seen >= 3
            and high_water >= self.GAMEPLAY_MINIMUM_PERCENT
            and game_gpu >= high_water * self.GAMEPLAY_RATIO
        )
        if not qualified:
            return self._status_with_samples(
                "waiting_for_gameplay",
                "Waiting for representative gameplay GPU activity",
                profile, session, game_gpu, lossless_gpu, manual_remaining,
            )

        session.observed_baseline = max(session.observed_baseline, game_gpu)
        if is_manual:
            self._copy_metrics_to_profile(profile, session, replace=True)
        target = profile.rtss.gpu_target_percent or self.profile_manager.config.rtss_default_gpu_target_percent
        if lossless_gpu >= target - self.TARGET_DEADBAND:
            self._persist_calibration(profile, session)
            return self._status_with_samples(
                "calibrated" if not is_manual else "manual_calibration",
                "Lossless Scaling GPU target reached; frame limit latched",
                profile, session, game_gpu, lossless_gpu, manual_remaining,
                target=target,
            )
        if session.paused_no_response:
            return self._status_with_samples(
                "no_response",
                "Calibration paused because limit reductions did not improve Lossless Scaling GPU use",
                profile, session, game_gpu, lossless_gpu, manual_remaining,
                target=target,
            )
        if session.current_limit <= profile.rtss.minimum_framerate_limit:
            session.current_limit = profile.rtss.minimum_framerate_limit
            self._persist_calibration(profile, session)
            return self._status_with_samples(
                "minimum_reached",
                "Minimum FPS reached before the Lossless Scaling GPU target",
                profile, session, game_gpu, lossless_gpu, manual_remaining,
                target=target,
            )

        if session.response_anchor is None:
            session.response_anchor = lossless_gpu
        elif session.adjustments_since_anchor >= 3:
            if lossless_gpu < session.response_anchor + 0.5:
                session.paused_no_response = True
                return self._status_with_samples(
                    "no_response",
                    "Calibration paused because limit reductions did not improve Lossless Scaling GPU use",
                    profile, session, game_gpu, lossless_gpu, manual_remaining,
                    target=target,
                )
            session.response_anchor = lossless_gpu
            session.adjustments_since_anchor = 0

        session.current_limit -= 1
        session.adjustments_since_anchor += 1
        self.rtss_manager.apply_profile(
            profile,
            profile,
            self.profile_manager.config.rtss_install_path,
            framerate_limit=session.current_limit,
        )
        return self._status_with_samples(
            "manual_calibration" if is_manual else "calibrating",
            "Reducing the game limit to make room for Lossless Scaling",
            profile, session, game_gpu, lossless_gpu, manual_remaining,
            target=target,
        )

    def _manual_remaining(self, profile_id: str) -> int:
        if self._manual_profile_id != profile_id:
            return 0
        remaining = max(0, int(self._manual_until - self.time_fn()))
        if remaining == 0:
            if self._session and self._session.valid_windows:
                profile = self.profile_manager.get_profile_by_id(profile_id)
                if profile:
                    profile.rtss.learned_framerate_limit = self._session.current_limit
                    self._copy_metrics_to_profile(profile, self._session, replace=True)
                    profile.rtss.automatic_calibration_disabled = True
                    self.profile_manager.save_config()
            self._manual_profile_id = None
            self._manual_until = 0.0
        return remaining

    def _ensure_session(self, profile: Profile, target_pid: int) -> _Session:
        if (
            self._session is None
            or self._session.profile_id != profile.id
            or self._session.target_pid != target_pid
        ):
            if self._session:
                if self.process_running_fn(self._session.target_pid):
                    self._pending_sessions.append(self._session)
                else:
                    self._finalize_session(self._session)
            current = profile.rtss.learned_framerate_limit or profile.rtss.maximum_framerate_limit
            current = max(profile.rtss.minimum_framerate_limit, min(profile.rtss.maximum_framerate_limit, current))
            self._session = _Session(
                profile.id,
                current,
                target_pid=target_pid,
                started_at=self.time_fn() - self.process_age_fn(target_pid),
            )
            self.rtss_manager.apply_profile(
                profile,
                profile if profile.rtss.managed_target_process else None,
                self.profile_manager.config.rtss_install_path,
                framerate_limit=current,
            )
        return self._session

    def _persist_calibration(
        self, profile: Profile, session: _Session, *, force: bool = False
    ) -> None:
        before = (
            profile.rtss.learned_framerate_limit,
            profile.rtss.game_gpu_baseline_percent,
            profile.rtss.game_gpu_high_water_percent,
        )
        profile.rtss.learned_framerate_limit = session.current_limit
        if self._manual_profile_id == profile.id:
            self._copy_metrics_to_profile(profile, session, replace=True)
        after = (
            profile.rtss.learned_framerate_limit,
            profile.rtss.game_gpu_baseline_percent,
            profile.rtss.game_gpu_high_water_percent,
        )
        if force or after != before:
            self.profile_manager.save_config()

    def _finish_ended_session(self) -> None:
        still_running = []
        for session in self._pending_sessions:
            if self.process_running_fn(session.target_pid):
                still_running.append(session)
            else:
                self._finalize_session(session)
        self._pending_sessions = still_running
        if self._session and not self.process_running_fn(self._session.target_pid):
            self._finalize_session(self._session)
            self._session = None

    def _finalize_session(self, session: _Session) -> None:
        profile = self.profile_manager.get_profile_by_id(session.profile_id)
        if profile is None or profile.rtss.automatic_calibration_disabled:
            return
        duration = self.time_fn() - session.started_at
        candidate = session.observed_baseline
        if (
            duration <= self.ALTERNATE_BASELINE_SECONDS
            or candidate <= self.ALTERNATE_BASELINE_MINIMUM_PERCENT
            or candidate <= (profile.rtss.game_gpu_baseline_percent or 0.0)
        ):
            return
        profile.rtss.game_gpu_baseline_percent = candidate
        profile.rtss.game_gpu_high_water_percent = max(
            profile.rtss.game_gpu_high_water_percent or 0.0,
            session.observed_high_water,
        )
        self.profile_manager.save_config()
        logger.info(
            "Promoted %.1f%% as the alternate GPU baseline for %s after a %.1f-minute session",
            candidate,
            profile.name,
            duration / 60.0,
        )

    @staticmethod
    def _copy_metrics_to_profile(profile: Profile, session: _Session, *, replace: bool) -> None:
        if session.observed_baseline:
            profile.rtss.game_gpu_baseline_percent = (
                session.observed_baseline
                if replace
                else max(profile.rtss.game_gpu_baseline_percent or 0.0, session.observed_baseline)
            )
        if session.observed_high_water:
            profile.rtss.game_gpu_high_water_percent = (
                session.observed_high_water
                if replace
                else max(profile.rtss.game_gpu_high_water_percent or 0.0, session.observed_high_water)
            )

    def _active_dynamic_profile(self) -> Optional[Profile]:
        if not self.profile_manager.config.rtss_frame_limiting_enabled or not self.state.is_scaling_active:
            return None
        active = self.state.current_active_profile
        if active is None:
            return None
        profile = self.profile_manager.get_profile_by_id(active.id)
        if profile is None or not profile.rtss.enabled:
            return None
        if self.effective_mode(profile, self.profile_manager.config.rtss_default_limit_mode) != "dynamic":
            return None
        return profile

    def _dynamic_profile(self, profile_id: str) -> Profile:
        profile = self.profile_manager.get_profile_by_id(profile_id)
        if profile is None:
            raise ValueError("Unknown profile")
        if not profile.rtss.enabled:
            raise ValueError("RTSS limiting is not enabled for this profile")
        if self.effective_mode(profile, self.profile_manager.config.rtss_default_limit_mode) != "dynamic":
            raise ValueError("This profile is not using dynamic RTSS limiting")
        return profile

    def _find_lossless_pid(self) -> Optional[int]:
        if self._lossless_pid:
            try:
                if psutil.Process(self._lossless_pid).name().casefold() == "losslessscaling.exe":
                    return self._lossless_pid
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                self._lossless_pid = None
        for process in psutil.process_iter(["pid", "name"]):
            try:
                if (process.info.get("name") or "").casefold() == "losslessscaling.exe":
                    self._lossless_pid = int(process.info["pid"])
                    return self._lossless_pid
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return None

    @staticmethod
    def _process_running(pid: int) -> bool:
        if not pid:
            return False
        try:
            process = psutil.Process(pid)
            return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return False

    @staticmethod
    def _process_age(pid: int) -> float:
        if not pid:
            return 0.0
        try:
            return max(0.0, time.time() - psutil.Process(pid).create_time())
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return 0.0

    def _status_with_samples(
        self, state: str, message: str, profile: Profile, session: _Session,
        game_gpu: float, lossless_gpu: float, manual_remaining: int,
        target: Optional[float] = None,
    ) -> dict:
        return self._set_status(
            state,
            message,
            profile=profile,
            current_limit=session.current_limit,
            game_gpu_percent=round(game_gpu, 1),
            lossless_gpu_percent=round(lossless_gpu, 1),
            target_percent=target or profile.rtss.gpu_target_percent or self.profile_manager.config.rtss_default_gpu_target_percent,
            manual_remaining_seconds=manual_remaining,
        )

    def _set_status(self, state: str, message: str, profile: Optional[Profile] = None, **values) -> dict:
        payload = {
            "state": state,
            "message": message,
            "profileId": profile.id if profile else None,
            **values,
        }
        self.state.dynamic_limiter_status = payload
        return payload


__all__ = ["DynamicLimiterController"]
