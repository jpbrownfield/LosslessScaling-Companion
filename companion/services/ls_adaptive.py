"""Bounded LS-only feedback policy. Pure, deterministic, and independent of IO.

Present submissions measure relative cadence, not displayed FPS or GPU time.
Absolute delivery remains unverified even when the relative cadence is stable.
All thresholds are conservative initial policy, recorded in benchmark reports.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
import statistics


@dataclass(frozen=True)
class Window:
    sequence: int
    started_ms: float
    ended_ms: float
    median_ms: float
    p95_ms: float
    intervals: int
    stalls: int
    intake_fps: float | None = None

    @property
    def score(self):
        return max(0., self.p95_ms / self.median_ms - 1.) + 2 * self.stalls / self.intervals

    @property
    def degraded(self):
        return self.p95_ms / self.median_ms >= 1.30 or self.stalls / self.intervals >= .02

    @property
    def stable(self):
        return self.p95_ms / self.median_ms <= 1.12 and self.stalls / self.intervals <= .005

    @classmethod
    def parse(cls, payload):
        keys = ("sequence", "startedMs", "endedMs", "medianMs", "p95Ms", "intervals", "relativeStalls")
        values = [payload[key] for key in keys]
        if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in values):
            raise ValueError("Non-finite or invalid timing window")
        sequence, start, end, median, p95, count, stalls = values
        if any(value != int(value) for value in (sequence, count, stalls)):
            raise ValueError("Timing counts must be integers")
        if not (sequence > 0 and 1800 <= end-start <= 2500 and 30 <= count <= 2048
                and 0 < median <= p95 <= end-start and 0 <= stalls <= count):
            raise ValueError("Incomplete or undersampled timing window")
        # Coverage: rejected/dropped presents must not turn a partial window into
        # apparently smooth evidence. Window span includes the closing interval.
        if count * median < .65 * (end-start):
            raise ValueError("Insufficient timing coverage")
        return cls(int(sequence), start, end, median, p95, int(count), int(stalls))


@dataclass(frozen=True)
class Decision:
    state: str
    reason: str
    limit: int | None = None
    benefit: bool | None = None
    cap_effect: str = "unverified"


@dataclass
class Trial:
    previous: int
    proposed: int
    before: list[Window]
    started_ms: float
    direction: str
    settling: int = 2


class AdaptivePolicy:
    VERSION = 2
    MAX_EXTRA_RESERVE = 10.
    RESERVE_STEP = 2.
    BASELINE_WINDOWS = 3
    EVALUATION_WINDOWS = 3
    RECOVERY_MS = 60000.
    COOLDOWN_MS = 30000.
    TRIAL_TIMEOUT_MS = 25000.

    def __init__(self, minimum, maximum, current, reserve, *, shadow_only=False):
        self.minimum, self.maximum, self.current = minimum, maximum, current
        self.reserve, self.extra = reserve, 0.
        self.shadow_only = shadow_only
        self.history = deque(maxlen=3)
        self.after = []
        self.trial: Trial | None = None
        self.last_end = 0.
        self.cooldown_until = 0.
        self.stable_since = 0.
        self.failures = 0
        self.valid_windows = 0
        self.confirmed_limit = current
        self.last_comparison = {}
        self.reference_median = None

    def interrupt(self, now_ms, reason):
        """Missing/focus/context/ownership evidence cancels an unproven trial."""
        restore = self.trial.previous if self.trial else None
        if restore is not None:
            self.current = restore
        self.trial = None
        self.after.clear(); self.history.clear(); self.stable_since = 0.
        self.reference_median = None
        self.last_end = 0.
        self.cooldown_until = now_ms + self.COOLDOWN_MS
        return Decision("unavailable", reason, restore)

    @staticmethod
    def _median(windows, attribute):
        return statistics.median(getattr(w, attribute) for w in windows)

    def observe(self, window, *, readback_verified):
        now = window.ended_ms
        if window.started_ms < self.last_end:
            return self.interrupt(now, "Overlapping timing windows")
        if self.last_end and window.started_ms-self.last_end > 250 and self.trial is None:
            return self.interrupt(now, "Gap between completed timing windows")
        self.last_end = now
        self.valid_windows += 1
        if self.trial:
            trial = self.trial
            if now - trial.started_ms > self.TRIAL_TIMEOUT_MS or not readback_verified:
                return self._rollback(now, "Cap write was not confirmed or the trial timed out")
            if trial.settling:
                trial.settling -= 1
                return Decision("settling", "Waiting for independent post-change LS windows")
            self.after.append(window)
            if len(self.after) < self.EVALUATION_WINDOWS:
                return Decision("trial_reduction" if trial.direction == "down" else "recovering", "Comparing fresh LS timing before and after the cap change")
            before, after = trial.before, self.after
            before_score = self._median(before, "score")
            after_score = self._median(after, "score")
            before_rate = [w.intake_fps for w in before if w.intake_fps is not None]
            after_rate = [w.intake_fps for w in after if w.intake_fps is not None]
            expected = abs(trial.proposed-trial.previous) / trial.previous
            rate_change = (statistics.median(after_rate) / statistics.median(before_rate)-1
                           if len(before_rate) == len(before) and len(after_rate) == len(after)
                           and statistics.median(before_rate) > 0 else None)
            effect = rate_change is not None and (rate_change <= -max(.01, expected*.4)
                        if trial.direction == "down" else rate_change >= max(.005, expected*.3))
            improved = (before_score-after_score >= max(.10, before_score*.20)
                        and self._median(after, "p95_ms") <= self._median(before, "p95_ms")*1.05
                        and self._median(after, "median_ms") <= self._median(before, "median_ms")*1.25)
            safe_recovery = (all(w.stable for w in after) and after_score <= before_score+.04)
            self.last_comparison = {"beforeScore": before_score, "afterScore": after_score,
                                    "previousLimit": trial.previous, "proposedLimit": trial.proposed,
                                    "beforeWindows": [[w.started_ms, w.ended_ms] for w in before],
                                    "afterWindows": [[w.started_ms, w.ended_ms] for w in after],
                                    "intakeRateChange": rate_change, "rateResponseObserved": effect,
                                    "readbackVerified": readback_verified, "direction": trial.direction,
                                    "relativeBenefit": improved, "displayDeliveryVerified": False}
            if not effect:
                return self._rollback(now, "No measured capture-intake response; RTSS enforcement remains unverified")
            if not (improved if trial.direction == "down" else safe_recovery):
                return self._rollback(now, "The cap change did not improve LS timing" if trial.direction == "down" else "Upward recovery degraded LS timing")
            direction = trial.direction
            self.extra = min(self.MAX_EXTRA_RESERVE, self.extra+self.RESERVE_STEP) if direction == "down" else max(0., self.extra-self.RESERVE_STEP)
            self.confirmed_limit = self.current
            self.reference_median = self._median(after, "median_ms")
            self.trial = None; self.after = []; self.history.clear()
            self.cooldown_until = now+self.COOLDOWN_MS
            self.stable_since = 0.
            return Decision("benefit_confirmed" if direction == "down" else "recovered", "Measured intake response and LS relative timing accepted", benefit=direction == "down", cap_effect="capture_intake_response")

        self.history.append(window)
        if len(self.history) < self.BASELINE_WINDOWS:
            return Decision("observing", "Warming up with disjoint LS timing windows")
        # A uniform output-rate collapse cannot certify its own lower baseline.
        if self.reference_median is None:
            self.reference_median = self._median(self.history, "median_ms")
        if window.median_ms > self.reference_median*1.15 and not window.degraded:
            self.stable_since = 0.
            return Decision("cadence_unverified", "Uniform cadence change needs independent output-rate context")
        if all(w.stable for w in self.history):
            if not self.stable_since:
                self.stable_since = now
            if now-self.stable_since >= self.RECOVERY_MS and now >= self.cooldown_until and self.current < self.maximum:
                return self._begin(now, "up")
            return Decision("stable", "Relative LS cadence is stable; absolute display delivery is unverified")
        self.stable_since = 0.
        if not all(w.degraded for w in self.history):
            return Decision("observing", "No persistent LS degradation across independent windows")
        if self.failures >= 2:
            return Decision("no_response", "Repeated unhelpful trials stopped adaptation for this session")
        if now < self.cooldown_until:
            return Decision("cooldown", "Holding cap during the minimum dwell interval")
        if self.current <= self.minimum:
            return Decision("minimum_reached", "Minimum FPS bound reached; LS degradation remains")
        if self.extra >= self.MAX_EXTRA_RESERVE:
            return Decision("reserve_exhausted", "Session reserve bound reached; LS degradation remains")
        return self._begin(now, "down")

    def _begin(self, now, direction):
        if not all(w.intake_fps is not None and w.intake_fps > 0 for w in self.history):
            return Decision("actuator_unverified", "Holding cap until source capture-intake response can be measured")
        step = max(1, min(5, math.ceil(self.current*.02)))
        proposed = max(self.minimum, self.current-step) if direction == "down" else min(self.maximum, self.current+step)
        if self.shadow_only:
            return Decision("shadow_trial", f"Would test {proposed} FPS; shadow mode keeps the cap fixed")
        self.trial = Trial(self.current, proposed, list(self.history), now, direction)
        self.current = proposed; self.after = []; self.stable_since = 0.
        return Decision("trial_reduction" if direction == "down" else "recovering", "Bounded trial; acceptance requires LS benefit and a measured cap response", proposed)

    def _rollback(self, now, reason):
        previous = self.trial.previous
        self.current = previous; self.trial = None
        self.after = []; self.history.clear(); self.stable_since = 0.
        self.failures += 1; self.cooldown_until = now+self.COOLDOWN_MS*2
        return Decision("no_response", reason+"; restoring the last useful cap", previous, False)
