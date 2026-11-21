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

## Adaptive limiter acceptance benchmark

**Run limiter benchmark**, and the last **Debug ? Application tests** item, use
the existing bundled workload and PresentMon runner. Allow about seven minutes:
four fixed-cap 20-second monitoring off/on/on/off arms, a separate 15-second
source-cap response check, then five minutes of actual adaptive control.
Ordinary profile calibration is paused and a temporary RTSS profile is restored
in `finally`; no automatic results are saved into the original presets.

The controller grades LS relative presentation cadence, with headroom as
supporting resource evidence. LS consuming only 5% GPU can be healthy. Game GPU
activity is descriptive, never the LS performance target. Capture/presentation
LUIDs identify actual devices independently of GPU numbering.

Reports have separate telemetry-validity, cap-enforcement, LS-benefit and monitoring
cost verdicts. A high reserve or successful profile write cannot pass the LS
benefit test. Adaptive ETW captures use `--qpc_time_ms` to align source and bound-LS
swapchain frames with native before/after windows. Displayed timing must be
available for an independent benefit pass. A smooth workload without a beneficial
pressure case is inconclusive rather than a success.

ABBA keeps the cap and workload fixed. Acceptance budgets are max(0.10 ms, 2%)
for p95 and max(0.20 ms, 3%) for p99; drift, missing frames, changed context or a
failed monitoring control prevents a pass. These are initial hardware acceptance
budgets. The CPU-only native microbenchmark does not establish real frametime cost.

The native observer uses fixed storage, no GPU readbacks/waits, no per-frame
logging, a try-lock and off-render-thread sorting. The on arms also run controller
inspection/GPU sampling in shadow mode. All arms retain PresentMon, so this is an
incremental monitoring comparison, not a measurement of PresentMon's own cost.

PresentMon 2.x `CPUStartTime` is milliseconds; legacy `TimeInSeconds` is seconds.
The parser handles these separately. See the [official column documentation](https://github.com/GameTechDev/PresentMon/blob/main/README-ConsoleApplication.md#csv-columns).

RTSS must be installed/running and PresentMon verified before the run. The test
retains report JSON, native observations, cap readbacks and CSVs. The application
diagnostic includes them in a nested evidence ZIP. Live RTSS convergence and
monitoring cost still require a run on the target GPU/display configuration.

```powershell
.\.venv\Scripts\python.exe scripts/benchmark_lossless_scaling.py --dynamic-limiter --adaptive-acceptance --monitor-resolution --duration 300 --gpu-headroom 15 --input-probes 0 --yes
```

See [policy and validation details](../rtss%20dynamic%20limiting%20notes.md).
