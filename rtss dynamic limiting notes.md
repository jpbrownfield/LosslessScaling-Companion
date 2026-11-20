# RTSS Dynamic Limiting Notes

Updated: 2026-11-20

## Goal and Current Status

Protect Lossless Scaling (LS) by adjusting the source application's RTSS FPS cap
when doing so measurably improves LS's own performance. The control objective is
LS's relative performance, not maximizing game utilization or grading game frame
pacing. The source application is the actuator target, not the performance target.

A fast GPU may need only 5-10% utilization for LS. Low LS utilization is not a
problem and must never, by itself, cause a cap reduction. A configured 15% reserve
means spare GPU capacity, not a requirement for LS to consume 15%.

There are currently two separate systems:

| System | Implemented Behavior | Changes the FPS Cap? |
| --- | --- | --- |
| Dynamic limiter | Uses adapter headroom to calibrate a per-application RTSS cap | Yes |
| LS pacing profiler | Measures recent successful LS Present-call timing | No |

**The pacing profiler is not yet connected to the limiter.** The current limiter
also retains game-GPU qualification/loading guards from its original design. It
does not yet implement the agreed LS-only feedback policy.

These notes describe local source changes, not a guarantee that the latest
published installer contains them. No release or deployed-host replacement was
performed as part of this work.

## Current Dynamic Limiter

Implementation: [companion/services/dynamic_limiter.py](companion/services/dynamic_limiter.py).

### Headroom Measurement

[companion/services/gpu_telemetry.py](companion/services/gpu_telemetry.py) reads
Windows GPU Engine counters. It:

- Identifies engines by adapter LUID, physical-adapter index, and engine index.
- Sums process activity on the same adapter/engine.
- Uses the busiest engine's utilization for an adapter, clamped to 0-100%.
- Computes headroom as 100% minus that utilization.
- Considers adapters on which LS has positive observed activity; if several are
  active, uses the least available headroom.
- Does not add different engines or separate GPUs together.
- Treats missing process samples or an unknown LS adapter association as
  unavailable telemetry, not free capacity.

This estimates spare busiest-engine capacity. It is not a measurement of GPU
queue delay, VRAM residency, LS execution time, or available frame-deadline budget.

### Calibration Behavior

- Requires RTSS limiting to be enabled globally and for the active dynamic
  profile, with LS reported as scaling.
- Starts from a compatible learned cap, otherwise the profile's maximum, within
  the user's minimum/maximum bounds.
- Collects five samples per decision window. The intended sampling cadence is
  once per second; decisions use median game utilization, LS utilization, and
  adapter headroom.
- Normally waits for three decision windows before qualifying gameplay. Manual
  calibration bypasses this qualification.
- Reduces the source cap by one FPS per qualified window while the reserve is
  insufficient. It does not currently perform upward cap recovery.
- Saves/latches the cap when the reserve is met, while continuing to evaluate
  later windows.
- Pauses reductions if three adjustments fail to improve headroom by at least
  0.5 percentage points relative to the response anchor.
- Reports minimum-FPS exhaustion rather than declaring success.
- Suspends ordinary calibration during maintenance or a dedicated benchmark.

The legacy gameplay/loading logic still depends on game GPU activity. A dramatic
drop is at least 15 percentage points and at most 60% of the reference activity;
this freezes learning until two windows recover to at least 75% of the reference.
Long-running sessions can promote higher game-activity baselines after exit.
These are existing safeguards, not the intended final LS-performance objective.

Manual calibration runs for five minutes. Completion with valid windows saves
the cap and disables automatic learning for that profile until reset. Do not let
a future adaptive controller silently bypass this explicit user lock.

### Persistence and RTSS Safety

Configuration lives in [companion/core/models.py](companion/core/models.py).
The existing `gpu_target_percent` and `rtss_default_gpu_target_percent` fields now
mean headroom reserve; their names remain for compatibility.

Calibration policy version 1 invalidates older utilization-floor learned caps,
game baselines, and manual locks on the next dynamic session. User FPS bounds and
RTSS rollback values are preserved. Future policy changes need similarly explicit
migration rules rather than reusing incompatible learned results.

[companion/services/rtss_manager.py](companion/services/rtss_manager.py) owns
reversible RTSS profile writes. A successful profile write/readback verifies the
stored configuration, not that RTSS actually enforced the cap on every frame.

## How the Current LS Profiler Works

The profiler reuses the add-on manager host's existing DXGI `Present` and
`Present1` hooks in
[native/addon_manager/hdr_probe.cpp](native/addon_manager/hdr_probe.cpp).
Despite that file's name, pacing collection is independent of enabling HDR pixel
sampling and does not add GPU readbacks.

### Sampling and Filtering

- Records high-resolution CPU timestamps around successful, non-test Present
  calls in the host process.
- Excludes `DXGI_PRESENT_TEST`, non-`S_OK` results, hidden/minimized/tiny windows,
  and known WPF/ImGui helper classes.
- Avoids double-counting nested Present/Present1 calls.
- Tracks one active swapchain at a time. A different chain can take over after
  two seconds of inactivity; a gap longer than two seconds resets the baseline.
- Uses a bounded 512-interval ring in
  [native/addon_manager/pacing_probe.h](native/addon_manager/pacing_probe.h).
- Adds no per-frame disk writes or GPU-completion waits. The sampling path uses
  a try-lock, so contention can omit observations rather than block rendering.

This is a fixed-count recent history, not a fixed-duration window. At different
FPS rates, 512 intervals cover different amounts of time. Repeated reads overlap;
they must not be counted as independent evidence by a future controller.

### Exported Measurements

`presentationPacing` is an additive object in the existing process-validated
status pipe, published by
[native/addon_manager/companion_bridge.cpp](native/addon_manager/companion_bridge.cpp).
It is also preserved by the read-only capture diagnostic collector in
[companion/services/addon_host.py](companion/services/addon_host.py).

| Field | Meaning |
| --- | --- |
| `version` | Pacing payload version, currently 1 |
| `intervals` | Number of retained successful-Present intervals, at most 512 |
| `medianMs`, `p95Ms`, `maximumMs` | Recent intervals between successful Present call entries |
| `relativeStalls` | Intervals greater than 1.5 times the recent median; counted only with at least 30 intervals |
| `presentP95Ms` | p95 CPU duration inside the observed Present path |
| `observedPresents` | Observed outermost non-test Present calls |
| `successfulPresents` | Observed calls returning `S_OK` |
| `eligiblePresents` | Successful calls passing window eligibility checks |
| `measurementOnly` | Currently true; no reserve/cap changes are driven by this data |

Snapshots without recent activity return no interval samples after two seconds.
Coverage counters remain available for diagnosis; zero samples must be treated
as unknown, not as perfect pacing.

### What It Does Not Prove

- Present calls are submissions, not proof that distinct frames reached the
  display. There is no displayed-frame deadline or generated-frame quality verdict.
- CPU time inside Present is not LS GPU execution time. Intentional pacing,
  synchronization, driver work, and compositor behavior can affect it.
- A relative stall is not a diagnosis of resource starvation or its cause.
- A stable lower frame rate eventually establishes a lower baseline and can have
  zero relative stalls. The profiler alone cannot detect every sustained slowdown.
- Swapchain selection is currently heuristic. Bind it to verified LS rendering
  identity before using these observations to change settings.
- There is no game-process timing input to this profiler.

### Local Evidence

The native regression covers steady cadence, an injected stall, bounded recovery,
a stable rate change, and invalid samples. It is wired into the native release
builder as `lsc_pacing_probe_test`.

A real-engine local check uses LSFG 3 + WGC + HDR + scaling Off and a temporary
animated source window. One Intel-machine run recorded 459 successful Presents,
458 intervals, median 12.1999 ms, p95 19.5930 ms, maximum 91.7359 ms, and 24 relative
stalls. Present-call duration p95 was 0.3228 ms. These are fixture observations,
not an accepted smoothness threshold or a representative game benchmark.

Focus loss initially stopped fixture output. The opt-in fixture now keeps its
own source foreground and waits for that child process to exit. Missing output
fails the collection test rather than passing as healthy pacing.

## Next Implementation Steps

### 1. Establish Reliable Identity and Session Boundaries

Bind telemetry and RTSS actions to the configured managed LS process, including
creation time, rather than whichever `LosslessScaling.exe` is found first.
Verify the render swapchain/device association and explicitly exclude the manager
panel and other auxiliary chains.

Join pacing, capture, and adapter-headroom observations by process identity and
adapter LUID. Reset observation history on process restart, profile/settings
changes, swapchain/device changes, monitor/refresh changes, and scaling stop/start.
Do not treat GPU 0 enumeration order or a vendor ID as unique GPU identity.

The host now exposes `capture.adapterLuid` and `capture.adapterSource` to distinguish
a device-creation observation from an actual capture-frame adapter. The pacing
payload still needs an explicit association with that identity.

### 2. Add Fresh, Non-overlapping LS Observation Windows

Add a monotonic measurement sequence, window start/end timestamps, sample age,
and sufficient coverage information. The controller must reject stale, duplicate,
undersampled, or mixed-session observations.

Use a shadow/measurement-only evaluator first. It should log the proposed action
and reason without touching RTSS. Establish warm-up, disjoint observation windows,
and baseline-reset rules before choosing control thresholds.

Validate the Present hook against PresentMon observations of LS, including actual
displayed-frame information where available. Do not substitute game pacing for
LS pacing. If adding GPU timing or queue-pressure probes, use asynchronous,
bounded sampling without flushing or waiting for the GPU on the render thread.

### 3. Define an LS-only Health Objective

Use LS's own cadence stability and, when independently available, its processing
budget or display-delivery measurements. Track source-rate/refresh changes as
context so intentional cadence changes do not look like LS regressions; do not
grade source/game performance as the control objective.

Do not derive an expected output FPS solely from the current median, RTSS profile
value, or an assumed LSFG multiplier. Reliable external timing/target information
is needed to distinguish a uniformly slower LS pipeline from a healthy rate change.

Replace the current game-GPU gameplay/loading qualification as an input to the
adaptive decision. Keep source PID identity only where required to apply the cap.
Adapter headroom can remain a supporting pressure signal, but not a mandatory LS
utilization floor or a substitute for observed LS health.

### 4. Implement a Bounded Adaptive Reserve Controller

Proposed states: observing, stable, suspected pressure, trial reduction, settling,
recovering, unavailable, and no response. These are a design proposal, not current
controller states.

1. Begin with the user's configured reserve and FPS bounds.
2. Require persistent LS degradation across independent valid windows before a
   trial. A single stall or missing sample must not trigger a reduction.
3. Increase the session's reserve in a small bounded step and reduce the source cap
   through the existing RTSS manager. Exact step sizes need benchmark evidence.
4. Wait through a settling period, then compare LS health before/after the change
   with adequate fresh samples. Confirm both the profile write and actual cap effect
   where measurable; a successful write alone is insufficient.
5. Continue only when LS measurably benefits. Stop and consider restoring the last
   useful cap when reductions do not help, rather than sacrificing FPS indefinitely.
6. After sustained stability, cautiously relax the extra reserve and test an upward
   cap step. Use hysteresis, cooldowns, minimum dwell time, and a rollback rule.
7. Respect manual locks, the minimum/maximum FPS bounds, a maximum extra-reserve
   bound, stop/pause requests, benchmark ownership, and process-exit cleanup.

Initial rollout should keep the extra reserve session-local. Define compatibility
keys and policy-version migration before persisting learned adaptive results.
Increasing a numeric reserve is not itself a successful outcome: LS must improve.

### 5. Extend the Existing Benchmark and Dashboard

Reuse [scripts/benchmark_lossless_scaling.py](scripts/benchmark_lossless_scaling.py)
and the existing benchmark UI rather than introducing a separate harness.

The current five-minute monitor-resolution benchmark verifies headroom attainment,
RTSS profile readback, and a complete settling tail. It does not yet verify the
adaptive pacing objective. Its current pass gate requires the final 30 seconds,
at least 80% expected samples, and reserve-minus-two percentage points in at least
90% of samples. See
[docs/third-party-benchmark-tools.md](docs/third-party-benchmark-tools.md).

Extend the trace with LS timing-window identity, coverage, pacing statistics,
configured/effective reserve, trial/settling state, before/after benefit, cap
enforcement evidence, and a reason for every action or refusal. Keep separate
verdicts for telemetry validity, headroom attainment, and LS pacing improvement.
The dashboard should distinguish resource-pressure suspicion from a confirmed
benefit and show why adaptation is frozen or unavailable.

### 6. Required Validation Before Automatic Control

- Steady LS cadence with low LS utilization never triggers a reduction.
- An isolated injected stall does not trigger a reduction; persistent degradation
  can trigger a bounded trial, with no change driven by game timing.
- A uniform rate reduction is not falsely certified healthy solely because its
  relative-stall count is zero.
- No-response, saturation, minimum-cap, and cap-not-enforced cases terminate safely.
- Long-term stability permits slow recovery without reserve/cap oscillation.
- Missing/stale/overlapping samples, occlusion, focus loss, and warm-up do not
  masquerade as healthy pacing or automatically consume more reserve.
- Restart, profile changes, multiple LS instances, device/swapchain changes, and
  monitor/refresh changes invalidate the appropriate history.
- Separate game/LS GPUs, two NVIDIA GPUs, cross-adapter operation, LSFG on/off,
  HDR/SDR, capture variants, and supported scaling modes are tested explicitly.
- RTSS state restoration, saved user settings, manual locks, and benchmark
  ownership remain intact after success, timeout, exceptions, and cancellation.

## Open Issues and Reproduction Commands

The reported false scaling confirmation has a local regression fix: fresh,
debounced presentation transitions can confirm a command when existing logs stay
silent, without accepting an unchanged pre-command window. That failure could
otherwise pause autoscaling. Do not use missing NR callbacks as proof that LS is
not scaling.

The reported 3090 Ti/5050 NR failure remains unconfirmed. A first-device or GPU 0
label mismatch alone does not establish a routing bug: NVIDIA NR initializes its
D3D12 device from the captured D3D11 frame's LUID. Preserve a diagnostic from the
actual failing session before restarting LS or replacing it with a benchmark.

The local Intel machine cannot validate dual-NVIDIA behavior. The dedicated RTSS
headroom benchmark was previously blocked by missing RTSS and an unresolved
bundled-workload path; the separate native capture fixture does not establish
live RTSS control-loop convergence.

Focused Python checks:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_dynamic_limiter.py tests/test_benchmark_metrics.py tests/test_rtss_manager.py -q
```

Opt-in real-LS pacing collection, using the locally built validation probe:

```powershell
$env:LSC_TEST_CAPTURE_PROBE = (Resolve-Path .\build\nr-validation-host\Release\eam_captureprobe.exe).Path
.\.venv\Scripts\python.exe -m pytest tests/test_addon_host.py -k ls_relative_pacing -q
```

This requires a locally supplied original engine at
`build/overlay-input-ls/Lossless.dll`; it does not redistribute LS or test NVIDIA NR.

Read-only collection while a failing managed LS process is still active:

```powershell
.\.venv\Scripts\python.exe run_live_diagnostics.py --capture-trace <LS_PID>
```

Replace `<LS_PID>` with the actual managed LS PID. The diagnostic validates process
identity and retains partial failures; it does not restart LS or press its hotkey.

**Next actionable milestone:** verified LS process/swapchain identity plus fresh,
non-overlapping pacing windows consumed by a measurement-only shadow evaluator.
Do not enable adaptive reserve writes until that evidence is trustworthy.