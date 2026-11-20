# Third-party benchmark tools

The optional performance benchmark can download the official PresentMon console
binary from `GameTechDev/PresentMon`. PresentMon is Copyright (C) 2017-2024 Intel
Corporation and is available under the MIT License:

https://github.com/GameTechDev/PresentMon/blob/main/LICENSE.txt

The helper does not download PresentMon during installation or ordinary startup.
Acquisition requires an explicit click in the dashboard's Performance benchmark
panel (or the standalone runner's `--download-presentmon` option). The download
is HTTPS-host allowlisted, content-addressed, and must match the SHA-256 digest
published by GitHub before the dashboard will enable the test.

At startup, LS Companion silently looks for the versioned x64 console executable
in its managed store, common install locations, `PATH`, and the user's Downloads
folder. It enables the test only if the file's SHA-256 matches official GitHub
Release metadata; detection alone never runs the executable.

The embedded `LSBenchmark.exe` workload is project-owned source built only from
Windows SDK and Direct3D system interfaces; it has no third-party engine or asset
dependency. It is compiled and included in the LS Companion distribution rather
than downloaded separately.
Diligent Asteroids remains a compatible optional external workload.
Diligent Engine and Diligent Samples are available under Apache License 2.0;
third-party components retain their own licenses:

https://github.com/DiligentGraphics/DiligentSamples

## Dynamic limiter benchmark

The Benchmark panel's **Run limiter benchmark** action runs the bundled workload
for five minutes at the foreground monitor's full pixel resolution. Choose the
GPU headroom reserve independently of the saved profile. The default profile's current
scaling/add-on configuration and minimum/maximum FPS bounds remain in use;
ordinary profile calibration is paused during this dedicated test.

The target is **spare capacity on the GPU adapters used by LS**, not LS's own
utilization. A 15% reserve means their busiest engines should remain at or below
85% combined utilization. Process activity is summed within each physical adapter
and engine; engine types and separate GPUs are never added together. If LS uses
multiple adapters, the least available reserve is used. Missing adapter telemetry
is unknown capacity, not free capacity. LS using 5% on a fast GPU can be healthy.

This is a conservative utilization-based headroom policy, not a measurement of
whether individual LS frames met their deadlines. PresentMon frame timings remain
available for pacing inspection; the headroom verdict does not certify frame
delivery or infer expected output FPS without an authoritative frame-rate target.

The live table and retained JSON record elapsed time, requested FPS limit, RTSS
profile readback, game GPU use, LS GPU use, adapter headroom, and controller state.
The final report includes limit changes, first reserve-hit time, settling statistics,
reserve occupancy, maximum shortfall, and failure reason. PresentMon captures retain actual frame
timings; RTSS profile readback alone does not prove its hook enforced every limit.

A pass requires a complete final 30-second settling window, at least 80% of the
expected telemetry samples, and adapter headroom at or above reserve minus two
percentage points for at least 90% of those samples. Missing telemetry, an
incomplete run, mismatched RTSS profile values, minimum-FPS exhaustion, or a
no-response controller result cannot pass. This does not guarantee an arbitrary
target is attainable with every GPU, workload, frame-generation mode, or FPS range.

RTSS must be installed and running, and PresentMon must be verified. The test
uses a temporary RTSS profile, restores previous limiter keys afterward, and
does not save calibration to the user's profiles. Raising resolution increases
shader work but does not itself prove GPU saturation; inspect workload qualification
and both processes' telemetry before generalizing the result to a game.

Standalone equivalent:

```powershell
.\.venv\Scripts\python.exe scripts/benchmark_lossless_scaling.py --dynamic-limiter --monitor-resolution --duration 300 --gpu-headroom 15 --input-probes 0 --yes
```
