# RTSS Dynamic Limiting Notes

Updated: 2026-11-20. Local implementation: policy 2, host build-7-lsc.11.

## Objective and implementation status

The source application's RTSS cap is an actuator for improving **Lossless Scaling's
relative presentation cadence**. Game GPU utilization and game smoothness do not
grade LS health. Low LS utilization never requests a reduction. GPU headroom is
supporting resource-pressure evidence, not proof of LS performance or a minimum
LS utilization target.

The native observer, shadow evaluator, reversible adaptive controller, dashboard,
and acceptance benchmark are connected. Automatic actions require trusted native
identity and complete independent windows. This is a local source/build result;
it has not been deployed to a user's running LS installation or published.

| Component | Implementation |
| --- | --- |
| Native timing and identity | `native/addon_manager/hdr_probe.cpp`, `pacing_probe.h` |
| Deterministic LS-only policy | `companion/services/ls_adaptive.py` |
| Process, capture, GPU and RTSS integration | `companion/services/dynamic_limiter.py` |
| Independent acceptance measurements | `companion/services/adaptive_benchmark.py` |
| Existing benchmark runner | `scripts/benchmark_lossless_scaling.py` |

## Identity and session boundaries

The controller uses the configured LS executable's PID **and creation time**.
Each status/control transaction validates that incarnation; it never chooses the
first process with a matching name. The source PID, creation time, executable and
HWND are bound separately. Multiple live instances sharing an RTSS executable
basename are ambiguous and prevent adaptation, since RTSS profiles use basenames.

The host validates HWND ownership and the actual `LosslessScaling` render output,
excluding WPF/ImGui helpers. Its device token and adapter LUID come from that
swapchain. Metadata is cached, with a bounded recheck every two seconds before
completing a window to catch device recreation/pointer reuse. Resize/color-space
hooks invalidate history. Unbound legacy profiling remains measurement-only.

Process/profile/settings changes and scaling stop/start reset policy history.
Native generations also change on output/device/swapchain changes, foreground
changes, minimized/hidden source/output windows, source/output size changes, and
monitor/refresh/resolution changes. A changed generation cannot reuse old windows.

Capture and presentation LUIDs are retained separately: cross-adapter operation
is allowed, but both must join the physical adapters observed in LS's GPU Engine
counters. GPU enumeration order and vendor ID are never used as unique identity.
Missing association freezes adaptation. Actual capture-frame adapter evidence is
distinguished from a device-creation observation; stopped capture is unavailable.

## Native measurements and render-path cost

Version 2 `presentationPacing` reports completed, non-overlapping two-second
windows with sequence/generation, QPC start/end, age, source/output identity,
swapchain/device, presentation LUID, sample count, drops, overflow and statistics.
Readers cannot create new evidence by repeatedly reading the same sequence.

Each window retains up to 2,048 intervals. Overflow, fewer than 30 intervals,
inadequate coverage, stale age over three seconds, missed windows, focus loss or
mixed context prevent control. The controller polls at the existing one-second
worker cadence and samples GPU Engine counters at most once per two seconds.

The steady Present path uses cached QPC frequency, timestamps, fixed storage and
a try-lock. A contended observer skips collection, with drops assigned to the
completed window. It never waits for a telemetry reader. Snapshot copies release
the mutex before sorting. There are no GPU queries/readbacks/waits, per-frame
allocations, disk writes or telemetry logging in this observer. COM/USER32 metadata
checks occur on chain selection and the bounded metadata refresh, not every frame.
The existing opt-in HDR pixel sampler is separate and unchanged.

Ephemeral P/p host controls bypass timing collection for the benchmark's off arms.
The CPU collection microbenchmark is an implementation check, **not** a substitute
for live off/on frametime evidence. The native release builder runs it together
with duration-window, stall, reset, overflow and invalid-input checks.

## Policy 2: bounded LS-performance trials

Initial thresholds are explicit policy constants pending broader hardware evidence:

- Warm-up and persistence: three independent valid windows. One isolated stall
  cannot request a reduction.
- Degradation: p95/median at least 1.30, or relative stalls at least 2% of intervals.
  A relative stall is an interval greater than 1.5 times that window's median.
- Relative stability: p95/median at most 1.12 and relative stalls at most 0.5%.
- A uniform median slowdown above 15% of the retained baseline stays unverified;
  it cannot certify a new healthy baseline by waiting out the rolling history.
- A downward trial is 2% of the cap, rounded up and bounded to 1?5 FPS and the
  configured minimum. It requires measurable capture intake before starting.
- Two fresh windows settle the write; three more evaluate it. Trial timeout is
  25 seconds. Missing evidence cancels an unproven trial rather than reducing more.
- Acceptance requires stored RTSS readback, a measured intake-rate response, and
  an LS jitter-score improvement of at least 0.10 and 20%. LS p95 cannot worsen by
  over 5%, and median interval cannot worsen by over 25% during a downward trial.
- A useful reduction adds two percentage points of session reserve, bounded to
  ten extra points. Higher reserve alone never counts as success.
- No-response/ineffective trials restore the previous useful cap. Two failed
  trials stop adaptation for that session. External RTSS edits are not overwritten.
- A confirmed change has a 30-second dwell; failed trials wait 60 seconds. Sixty
  seconds of sustained relative stability permit a bounded upward test. Measured
  intake must respond, and LS timing must remain stable; regression rolls back.

The score combines p95/median excess with relative-stall fraction. It describes
relative submission cadence, not GPU execution time, generated-frame quality or
confirmed display delivery. Capture intake is **supporting actuator-response
evidence**, not a production PresentMon measurement of game FPS. The benchmark
independently validates actual source-rate response and LS timing with ETW.

The profile's **Measure only** option exposes shadow proposals while keeping the
initial cap fixed. Automatic trials are blocked by manual locks, missing/old hosts,
unknown timing/adapter identity, benchmark ownership, FPS bounds and reserve bounds.

## Persistence and cleanup

Policy 2 discards incompatible automatic learned caps and retired game-GPU
baselines. Explicit manual locks/caps survive policy upgrades until the user resets
them. User FPS bounds and RTSS original-value rollback metadata are preserved.
Extra reserve and automatic results remain session-local; they are not promoted
into a future session's learned cap.

Manual calibration lasts five minutes. Saving requires fresh stable or confirmed
useful LS evidence, then locks the confirmed cap until reset. Missing/stale or
inconclusive data does not silently save a calibration.

Unfinished trials are restored on interruption/stop/shutdown where profile
ownership is still verified. Benchmark takeover clears production trials under
the controller lock before creating a temporary RTSS profile. A snapshot of the
original target protects rollback when an edited profile changes executables.
The existing RTSS manager restores/removes only its managed limiter keys.

## Acceptance benchmark and reports

The last **Debug ? Application tests** item is **Adaptive RTSS: LS benefit and
monitoring overhead (about 7 minutes)**. The existing **Run limiter benchmark**
button uses the same runner. Both require RTSS installed/running, verified
PresentMon, the bundled workload and a current Companion host.

The run includes:

1. Four 20-second off/on/on/off monitoring arms at a fixed RTSS cap. Policy and
   GPU/process polling also run in shadow mode in the on arms. Each arm uses its
   own PresentMon session and verifies the bound LS swapchain, cap and coverage.
2. A separate 15-second controlled source-cap step, restored immediately, to
   check actuator response even when LS is already smooth.
3. Five minutes of actual bounded adaptive control using the current LS settings
   and a temporary RTSS profile. Original presets and learned values are untouched.

The ABBA gate checks p95 overhead against max(0.10 ms, 2%) and p99 against
max(0.20 ms, 3%). Source/LS cadence drift, missing coverage or identity changes
cannot produce an overhead pass. Displayed timing is also checked when available.
These are initial acceptance budgets, not universal guarantees for every machine.

Adaptive captures use PresentMon `--qpc_time_ms` to align ETW frames with native
window boundaries, rather than file timestamps or an assumed FG multiplier.
Reports separate telemetry validity, cap enforcement, LS benefit, descriptive
headroom and monitoring overhead. Native benefit alone cannot pass the independent
LS-benefit check: aligned source/LS swapchain evidence and displayed timing are
required. A smooth workload with no beneficial pressure trial is inconclusive.

Report JSON, per-window decisions/reasons, context, RTSS readbacks and PresentMon
CSVs are preserved. The application diagnostic includes a nested evidence ZIP.
Exceptions/interruptions restore the monitoring state and temporary RTSS profile;
cleanup failures remain visible and prevent success.

```powershell
.\.venv\Scripts\python.exe scripts/benchmark_lossless_scaling.py --dynamic-limiter --adaptive-acceptance --monitor-resolution --duration 300 --gpu-headroom 15 --input-probes 0 --yes
```

## Validation and remaining hardware acceptance

Deterministic Python/native checks cover low LS utilization, isolated/persistent
stalls, useful/ineffective trials, shadow mode, uniform slowdown, cap response,
minimum/reserve bounds, slow recovery/regression, duplicate/stale/overlapping
windows, capture pause, restarts, cross-adapter identity, manual locks, write
failures, external edits and cleanup. The real-engine headless integration checks
host protocol controls and rejection of invalid presentation bindings.

RTSS is not installed/running in this workspace, so **live RTSS convergence and
frametime overhead have not been measured here**. The acceptance test must run on
the target machine. It cannot claim dual-NVIDIA, 3090/5050 routing, HDR/SDR,
LSFG on/off, capture/scaler variants or real-game performance from unit tests.
Each run retains its observed settings/GPU context; run the test under each
relevant combination. No absolute expected output FPS is inferred from cap,
median or an assumed frame-generation multiplier.

The existing read-only `nr_capture_current` diagnostic still preserves a failing
session without restarting LS. A benchmark reproduction is separate evidence;
missing NR callbacks alone do not prove scaling is inactive.

Focused checks:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_ls_adaptive tests.test_dynamic_limiter tests.test_adaptive_benchmark tests.test_rtss_manager tests.test_benchmark_metrics
```
