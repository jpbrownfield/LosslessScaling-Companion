# Bundled native components

`startup_guard` is Companion's own small Windows x64 DLL. Companion registers a
PID-filtered, in-context `SetWinEventHook` before resuming a new LS process. On
creation/show of LS's unowned, captioned WPF dashboard it installs a temporary
subclass that prevents subsequent show requests, and DWM-cloaks the window as a
second layer against first-paint flashes. It does not patch code, attach a
debugger, replace a proxy DLL, or install desktop-wide hooks. All UI threads of
that one new LS PID are covered. Borderless scaling overlays, Echo's panel,
owned dialogs, and non-WPF errors are excluded.

The window is temporarily non-activating as well. After six seconds the dashboard
is hidden, uncloaked and unsubclassed, with its activation style restored; later
manual opens work normally. The DLL is pinned in LS until that process exits
to keep queued callbacks safe. The hook-installing worker unhooks itself and
exits after startup. Existing running LS instances and manual Steam launches
are not modified. Restricted security policies can prevent in-process hook
delivery; Companion retains its old post-show hide watcher as a fallback.

Build and run the off-screen native integration fixture (not the installed LS):

```powershell
cmake -S native/startup_guard -B build/startup_guard -A x64
cmake --build build/startup_guard --config Release
$env:LSC_TEST_NATIVE_STARTUP_GUARD = "1"
python -m unittest tests.test_startup_guard -v
```

The tests include both an off-screen Win32 fixture and a real .NET Framework
WPF window/dispatcher. They check repeated show requests, overlay/Echo/dialog
exclusions, expiry and later manual opening. The WPF fixture uses the framework
compiler shipped with Windows; neither test executable is packaged.

The result is `build/native/LSC_StartupGuard.dll`, bundled under Companion's
`tools` directory. Windows maps that exact DLL into LS; no copy into the LS
installation or additional add-on load ordering is necessary. Simply dropping
the DLL beside LS does **not** activate it; Companion's guarded launch does.

`reshade_bridge` is LS Companion's maintained fork of the MIT-licensed
LSP-ReShade input-passthrough add-on. It targets the LosslessProxy 0.3.x add-on
ABI vendored under `vendor/losslessproxy-sdk`.

The fork is derived from FrankBarretta/LSP-ReShade commit
`15902acd5d532ceba97345b3e318ce60d44effbc`. The SDK snapshot is derived from
FrankBarretta/LosslessProxy commit
`6ca17e1f66a37f0e0f58ee21bc1b7b622f556c23`. Both upstream projects are MIT
licensed; their notices are retained beside the source.

The bridge deliberately has no ImGui dependency. Companion owns its defaults,
and the add-on uses the stable LosslessProxy host configuration interface.

Build from an x64 MSVC developer shell:

```powershell
cmake -S native/reshade_bridge -B build/reshade_bridge -A x64
cmake --build build/reshade_bridge --config Release
```

The DLL and manifest are copied to `build/native/LSP-ReShade/`. Packaged builds
embed both files; the graphics resolver deploys them only when a profile enables
both LosslessProxy and ReShade.

`lsp_neuralrender` is an imported snapshot of andreiday/LSP-NeuralRender at
commit `750847223656d002e3a0f7090842defc2bae4077`. Its upstream MIT license and
documentation are retained in that directory. The NVIDIA NGX headers and import
library needed to build it are local-only dependencies excluded by the imported
`.gitignore`; NVIDIA runtime DLLs are not part of the source import.

Build it from an x64 MSVC developer shell after supplying the SDK files described
in `native/lsp_neuralrender/external/ngx/README.md`:

```powershell
cmake -S native/lsp_neuralrender -B build/lsp_neuralrender -A x64
cmake --build build/lsp_neuralrender --config Release
```

The Windows GitHub Actions build performs those steps without requiring a local
Visual Studio installation. It sparsely checks out the required files from the
official `NVIDIA/DLSS` repository at a pinned commit, keeps them outside release
artifacts, and uploads a separate `LSP-NeuralRender-windows-x64-*` artifact. That
artifact does not contain `nvngx_dlssnr.dll` or any neural model/runtime.
