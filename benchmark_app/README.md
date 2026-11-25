# LS Helper GPU Benchmark

This bundled Win32/Direct3D 11 workload renders a deterministic, shader-heavy
animated scene. `--shader-load` controls GPU pressure. F15 displays a 120 ms
full-frame cyan/magenta marker used by the companion's software-visible latency
probe.

It uses only Windows SDK and Direct3D system libraries. Build with the x64 MSVC
developer environment:

```powershell
cl /nologo /std:c++17 /O2 /EHsc /DUNICODE /D_UNICODE `
  /Fe:LSBenchmark.exe ls_benchmark.cpp `
  d3d11.lib dxgi.lib d3dcompiler.lib user32.lib shell32.lib
```

Options: `--width`, `--height`, `--shader-load`, `--duration`, `--event-log`,
`--vsync`, `--adapter-name` (exact DXGI name), and `--adapter-report` (JSON path).
An explicit adapter request must match exactly one hardware adapter; it fails
instead of falling back. The report contains the actual device name and LUID.

The last Debug / Application tests item in Companion runs adaptive RTSS acceptance
on the 3090 with NR off. It checks workload/capture/presentation adapter identity,
fixed-cap monitoring overhead, independent cap response and bounded adaptation,
then restores the temporary settings and includes the CSV/JSON evidence in the
exported diagnostic ZIP. RTSS must already be installed and running.
