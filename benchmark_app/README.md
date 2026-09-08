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
and `--vsync`.
