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
