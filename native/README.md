The production addon host is built from pinned FriedSnowman sources by
`python scripts/build_addon_manager.py`. See
[`addon_manager/BRIDGING.md`](addon_manager/BRIDGING.md) for the exact overlay,
licences, protocol, validation limits and upstream upgrade procedure. The older
`lsp_neuralrender` tree is retained for historical experiments and is no longer
built or bundled by the Windows application workflow.

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

adapter_report is a diagnostics add-on for the same host ABI. On load it records the physical GPU the host renders with (via GetD3D11Device), both DXGI adapter enumerations, the rendering adapter's attached outputs, and the saved numeric routes from Settings.xml, then writes ls-adapter-report-<pid>.json under the companion diagnostics directory. Raw DXGI indexes are search evidence; LS's own adapter-name list remains the authority for its dropdown indexes.

Version 1.1.0 adds an on-demand runtime report. After each confirmed scaling activation, Companion signals the already-loaded DLL through a PID-specific event and shared-memory request mailbox. A random nonce, configured-process identity, and process birth time prevent an earlier report from passing as current proof. The reporter retains the existing D3D device while reading its adapter; it creates no rendering device, benchmark workload, scaling hotkey, or per-frame callback. Its worker sleeps between requests after the bounded startup retries.

A matching physical GPU skips PresentMon and automatic calibration. A confirmed mismatch displays a GPU-testing overlay and stops scaling the current application. Companion restarts LS with candidate GPU indexes, preserves the output-display index, and asks the DLL for fresh evidence after each confirmed activation. The first index selecting the requested GPU stays active; no other GPUs are actively calibrated. Both the initial incorrect index and every subsequent proven index are saved in `ls_gpu_index_observations`, with their actual GPU identities and request provenance, and exported by the live GPU diagnostic. A failed or cancelled search leaves scaling off and restores the original native profile indexes while LS is stopped. Missing initial DLL evidence never starts testing. Install/load the updated adapter-report add-on in the existing compatible LosslessProxy/Echo host to enable this path; Companion does not inject it into another process.

```powershell
cmake -S native/adapter_report -B build/adapter_report -A x64
cmake --build build/adapter_report --config Release
ctest --test-dir build/adapter_report -C Release --output-on-failure
```

The off-LS fixture (LSC_AdapterReportHost.exe) drives the real DLL entry point against a real D3D11 device: `.\LSC_AdapterReportHost.exe <adapter-index>`. Python coverage lives in tests/test_adapter_report.py (reader) and tests/test_adapter_report_host.py (fixture, skips when the DLL was not built).

The separate [AMD Neural Rendering addon](amd_neuralrender/README.md) is built
with the Companion host by `python scripts/build_addon_manager.py`. It routes
verified captures automatically from the actual GPU, reuses MIT compute flow,
and calls the optional pinned AMD runtime's D3D12 C API. Its standalone probe
tests compute flow and D3D11/D3D12 sharing without loading a neural model.

`runtime_isolation` supplies the mandatory private-settings bootstrap. Its
netstandard2.1 startup hook runs before LS Main, installs the pinned MinHook
helper for process-local LocalAppData/RoamingAppData known-folder APIs, verifies
the managed folder lookup, preloads selected host components and writes the
launch receipt. Build with a .NET SDK and x64 MSVC tools:

```powershell
python scripts/build_runtime_isolation.py
dotnet build native/runtime_isolation/Fixture/Fixture.csproj -c Release
$env:LSC_TEST_RUNTIME_ISOLATION = '1'
python -m unittest tests.test_managed_runtime -v
```

The fixture checks real managed/native folder lookups, writes only after the
private path matches, and verifies a bare cloned executable fails before Main.
