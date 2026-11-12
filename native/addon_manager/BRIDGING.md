# Companion bridge to FriedSnowman's addon manager

The source of truth is `upstream.lock.json`. Current baseline: **build-7**, commit
`d13c33ed5f3e094812f2258bb95c523d73047ccb`; Companion revision **1**. This replaces
Echo's integration. There is no conversion of Echo NR settings or package pins.

## Reproduce the package

From the repository root, with Python 3.11+, Visual Studio C++ tools, Windows SDK,
CMake and Git installed:

```powershell
python scripts/build_addon_manager.py
```

Both source and release downloads are SHA-256 pinned. The script builds the MIT
host from source, extracts the verified release using its documented `--extract`
mode, replaces only `Lossless.dll`, and emits
`build/native/LSAddonManager-Companion.zip`. The unchanged NR/upscaler binaries
and all accompanying source, runtimes and licences come from that release.
The package never includes `nvngx_dlssnr.dll` or user configuration.
The builder also builds and bundles `LSC-AdapterReport` and the separate MIT
`LSC-AMDNR` addon. Their sources/licences are in the host source archive.

The archive is bundled with Companion. The `ls-addon-manager` provider installs
this package locally through AssetStore and transactional deployment. It does
not replace our host with an unreviewed upstream release. New upstream versions
are incorporated by rebuilding and releasing Companion.

## Deliberate patch surface

Build 7 keeps the upstream FPS-counter placement hook in both managed startup
modes, after dispatch hooks and before addons initialize. The GUI still starts
hidden and responds to Companion's shortcuts. Managed mode suppresses upstream's
update-consent dialog and directs users to Companion for host updates; stock
unmanaged behavior is unchanged. Profile snapshots retain the native panel's
`LosslessScalingOverrides` settings without adding another basic editor control.
Build 7 migrates legacy negative NR `skinStructure` values to the saved local
structure strength once; the current skin slider is explicit and ranges from
0 to 2. Companion preserves older saved values for that native migration.

Revision 4 adds `component_loader.{h,cpp}` and the exported
`LSCInitializeComponents` entry point. The mandatory managed startup hook calls
it before LS Main and graphics initialization. The loader validates the complete
`.companion/components.json` plan before loading, including SHA-256, unique
identities and canonical paths beneath `.companion/components`. It uses
`LoadLibraryExW` with explicit paths, checks already loaded copies, pins selected
modules and publishes load/error evidence in host status. ReShade, Special K and
custom modules use this route; the existing SDK add-on loader is unchanged.
The host's `Lossless.dll` engine forwarding remains in use. Deployments target
only Companion's managed LS copy and private settings; see
`native/runtime_isolation` for the pre-Main folder redirection.

`prepare.py` applies exact, unique anchors and refuses upstream drift. It adds
the files `companion_bridge.{h,cpp}`, the host target's source/version
definition, and these edits to `manager/src/core/main.cpp`:

1. Select our managed worker when `global.companion.managed` is true, retaining
   `headless` for legacy configurations and smoke tests.
2. Pin the host for the process lifetime. Installation/settings changes require
   Companion's existing stopped-process reload.
3. Skip GPU/thread destruction during process termination under the loader lock.

The managed worker initializes components, SM86 routing and dispatch hooks before
starting the upstream GUI. The GUI owns addon loading and its shared ImGui context.
`global.ui.open_on_start` is false: the window starts hidden and blocks in
`WaitMessage` without drawing until opened. A `WM_SHOWWINDOW` patch keeps the GUI's
hidden state synchronized when Companion restores its HWND from an overlay action.
The status pipe remains read only and reports `managed: true, headless: false`;
legacy headless workers report `headless: true`. Both retain process identity and
freshness checks. The rendering pipeline,
model forwarder, shaders, SDK method order, and addon lifecycle are unchanged.

Revision 2 adds an optional preloader for **sdli1995/dlssg_for_sm86 0.4.1 SR**.
It is independent of FriedSnowman's renderer and can be disabled by choosing Off.
The source of truth for that dependency is `companion/services/sm86_sr.py`: release
archive SHA-256 plus individual hashes of the SR proxy, backend, runtime, kernel
pack and notices. It is downloaded on demand from upstream, not bundled into our
host ZIP. Updating FriedSnowman alone must not change this pin.

Companion deploys only these files into `companion-runtimes/SM86SR/`, with its own
`dlssg_sm86.ini`. The release's factory INI and FG/XeSS kernels/runtimes are never
deployed. Our INI sets `[General] Enabled=0` (FG off),
`[Compatibility] SpoofArchToGame=0`, and `[SuperResolution] Mode=L-INT8|M-INT8`.
The proxy is loaded by full path, on the host worker before addon/NGX initialization;
it never occupies a root proxy slot. A conflicting loaded SM86 proxy, incomplete
deployment or mismatched INI disables the DLSS upscaler and reports an error,
rather than silently presenting stock DLSS as optimized. Off/disabled profiles
do not load it and omit its files from the transactional deployment plan.

`DLSS4DLAA.dlaaPreset` remains upstream's numeric string (Auto=16, default=0,
E=5, J=10, L=12, M=13). `companionSm86Mode` is Companion-owned (Off/L-INT8/M-INT8).
INT8 pins the matching preset and disables upstream per-game overrides; Companion
already owns the per-profile setting. The NR addon `DLSSNR02.preset` is separate
and is never mapped to these SR presets. `sm86Sr.loaderReady` in bridge telemetry
proves only proxy loading. Before loading the upscaler, the worker loads the SR
snippet without creating/evaluating a feature, then reads at most 256 KiB of new
log bytes for up to 1.5 seconds. It requires this process's fresh `sr_gate` verdict;
a rejected or missing verdict disables the upscaler and reports the reason. This
prevents upstream's stock fallback on rejected devices from appearing as INT8.
`gateAllowed` confirms the device gate, not frame inference. Details are in the upstream
`sr_gate` logs under `companion-runtimes/SM86SR/dlssg_sm86/logs/`. No performance
or image-quality claim is inferred from successfully loading a DLL.

## Control and telemetry contract

The profile editor's presentation and vendor requirements live in
`companion/services/addon_schema.py`. NVIDIA-exclusive SR and NR flow/HDR/picture
groups require a detected NVIDIA GPU; AMD inference/model controls require AMD.
Use the full physical GPU inventory, allowing both on mixed systems. Logical NR,
FSR and XeSS remain shared. These editor gates do not change capture-adapter
routing or certify model/HIP compatibility.

`MANAGED_DEFAULTS` in that schema hides redundant processing/comparison switches,
automatic masking, AMD blocking/history/blending experiments, experimental FSR
frame generation and ReShade input housekeeping. `AddonConfigService.effective`
applies those safe values even to existing/imported configurations. NR processing
and result application stay on, automatic masking stays on, AMD stays asynchronous
without experimental history/blending, and LS owns frame generation. The actual
add-on `_enabled` flags remain profile-owned. Review this small defaults table and
vendor metadata when incorporating upstream settings changes; hidden keys remain
known to the editor so they cannot resurface as unknown saved settings.

Companion's profile editor owns `addons/config.json`. NR uses **DLSSNR02** and its
upstream string-valued settings. Image processors are explicitly opt-in. The
internal `LSC-AdapterReport` helper is enabled automatically when installed and
does not appear as an optional image processor in the editor. Saving an
active profile uses the established stop/deploy/restart workflow; changes are
not advertised as live until applied. Inactive profiles remain editable.

The local pipe `LSCompanionAddonStatus-<pid>` provides protocol/schema **1**.
It returns bounded JSON with process creation time, sampling time, host version,
addon load/errors/status, current published metrics, and existing capture-hook
diagnostics. The additive `reshade` object reports the **Overlay toggle** state
and its runtime menu binding; it contains no frame pixels or profile settings. The client checks server
PID, process instance, schema, size and freshness, and has a bounded read timeout.
The pipe accepts only a delivery acknowledgement; it has no command interface.
Overlay controls use this verified toggle state to show the yellow border,
persistent status card, and scoped configurable shortcuts. The native menu key
is never registered or swallowed by Companion. The add-on shortcut restores and
focuses the manager window belonging to the verified managed LS process. Its
WM_SHOWWINDOW handler resumes rendering; closing the panel hides it again.

Telemetry is advisory: missing captures, NR disabled, or a waiting/error status
must never be interpreted as proof that scaling is active or stopped. Scaling
confirmation retains the inspector's native LS log/window evidence.

The capture requirements remain upstream's: verified LS 3.2.2.0, WGC, LSFG 3 or
FG off, and Scaling Off/NIS when FG is off. NVIDIA rendering is unchanged.

## Revision 3: automatic AMD NR backend

`companion_real_frame.inc` replaces only HostImpl's real-frame registration,
requirement and invocation functions. It permits the reviewed pair
`DLSSNR02.dll`/`LSC_AMDNR.dll` to register together; unrelated transformers retain
exclusive ownership. Private host fields track each callback/module/requirement,
the observed adapter and the first capture's vendor. The IHost vtable, API 1.4
and renderer binaries are unchanged. ForgetCode removes only the unloading
module's slot and retains the other slot/count.

`real_frame_hook.cpp::Before` observes the dispatching device before the required
capture gate. A thread-local context/device cache and host pointer cache avoid
per-dispatch DXGI enumeration. InvokeRealFrame confirms the actual capture's
device using a thread-local adapter cache, selects NVIDIA 0x10de or AMD 0x1002, and rejects Intel/software rather
than trying HIP. A required NVIDIA callback cannot pause AMD captures. Vendor
switches after the first capture require restarting LS to avoid mixing CUDA
driver stacks. The verified LS capture/shader checks are retained.

For automatic NR, SM86 loading is deferred until a verified NVIDIA real capture,
before its transform and subsequent NIS/upscaler consumers. AMD never preloads
that CUDA proxy. Deferred gate errors block DLSS4DLAA dispatch hooks instead of
allowing a stock fallback. On AMD, native NVIDIA NR picture/upscaler dispatch
hooks are also skipped; FSR/XeSS remain available. Module identities are cached
when a device is selected, so normal dispatch filtering uses atomic comparisons.
The startup-only SR path remains unchanged when automatic NR is off. SM86 JSON
is mutex-protected because deferred initialization can overlap pipe snapshots.

Companion mirrors the logical DLSSNR02 opt-in to internal LSC-AMDNR and writes
its user-data cache path. That addon is hidden from the editor; AMD-only timing,
history, freshness and blend controls live on the common NR card. Optional
provider `amd-neural-render-runtime` stages a separate pinned download and never
enables profiles. Deployment is a verified private whitelist. The mutable cache
is seeded separately into user configuration, outside owned LS files/AssetStore.
`neuralBackend` identifies the selected vendor in telemetry; AMD inference is
confirmed by result count rather than runtime loading. Async inference timings
must not be added to compute-flow timing as if they were serial GPU stages.

The MIT addon reuses the pinned MIT Upscalers FlowEstimator, shared D3D11/D3D12
textures/fences and RedDukeDev's C ABI. It supplies estimated motion and no scene
depth. It uses the runtime's async residual composition, with an explicit
experimental same-frame option. This does not reuse LSFG optical flow or port
DLSS SR/RR. Details, hardware limits, controls and update checklist are in
[`native/amd_neuralrender/README.md`](../amd_neuralrender/README.md).
`native/amd_neuralrender/prepare_runtime.py` applies exact anchors to the separately
pinned MIT runtime source. It propagates the existing C API D3D12 device's LUID
into NetworkPaths, enumerates CUDA devices and selects the matching device token
before cuCtxCreate. The existing adapter map chooses LS's device; this translation
handles different DXGI/LS/CUDA enumeration orders, including non-primary AMD GPUs.
`companion_adapter.h` contains the shared selection function and its tests.
Private exports identify the patched runtime and report its selected LUID/ordinal;
the addon verifies that binding after startup. Public dlss5nr.h stays unchanged.
The host bundles the patched runtime under addons/LSC-AMDNR/runtime; optional
downloads supply the unchanged ZLUDA, NR kernels and cache. The upstream runtime
DLL from that download is not deployed. Host-memory-copy/runtime-translation
fallbacks are disabled. AMD's separate model path requires the original signed
model matching the shipped cache, independent of a patched NVIDIA model path.
Its SHA-256 is checked before loading the driver or model; a cache/runtime update
must also review this model pin. This digest does not redistribute the model.

Additional build targets are the standalone AMD flow/bridge probe and host
callback-routing test with two synthetic addon DLLs. These fixtures never enter
the LS addon/runtime deployment. The software bridge fixture reports zero model
results. Run the native tests alongside the original headless/SM86 smoke tests.

## Incorporating an upstream release

`.github/workflows/update-friedsnowman.yml` automates compatible stable updates.
It stages the lock and Python host version on a candidate branch and reuses the
full Windows release build before advancing main and publishing a new Companion
installer and addon-host ZIP. Exact patch anchors remain mandatory: upstream
drift fails the run and requires the manual review below. The addon host is never
replaced with the stock upstream DLL. See the README's release instructions for
permissions, recovery and hardware validation limits.

1. Review the release's host API, ImGui revision, NR schema/defaults, capture
   shader/runtime checks, licences and payload layout. Update the lock's commit,
   tag, `release_asset` filename and both archive digests. Increment
   `companion_revision` and `version`. Legacy locks without `release_asset` use
   the ZIP filename; current releases use the standalone EXE's `--extract` mode.
2. Run the builder. If an anchor fails, review that upstream change and update
   `prepare.py`; do not weaken the exact-match check. Inspect the generated host
   diff against a clean archive of the pinned commit.
3. Update `companion/services/addon_host.py`, `addon_schema.py`, and deployment
   expectations if the contract changed. Keep the lock/version consistency test.
4. Run the Python/JS tests and the real headless-host smoke test. Validate on
   NVIDIA hardware with the supported LS/model: SDR/scRGB, NR plus upscaling,
   ReShade, GPU reporter, profile reload, missing model, unsupported profiles,
   device/size changes, process restart, and recovery. Report hardware skips.
   For the optional SR backend, also run L-INT8/M-INT8 against the same standard
   L/M preset on RTX 30 hardware, check `sr_gate` activation and configuration,
   SDR/HDR, preset changes, missing/damaged assets, another SM86 proxy, and
   switching back to stock. Keep FG and architecture spoofing disabled.
5. Verify `NeuralRendering-source.zip`, all NR licence files, host source archive,
   this note and the lock survive deployment. Record any new patches here.

## Distribution and validation limits

The host is MIT; the NR addon is GPL-3.0-only. Its verified corresponding source
ZIP is retained unchanged. Our package also includes `CompanionHost-source.zip`
with the patched host, overlay, build script and exact ImGui/MinHook dependencies.
Other runtimes retain their own notices. Neither the proprietary NR model nor
Lossless Scaling's engine is redistributed.

A synthetic host test proves startup, pipe telemetry, no manager window and
shutdown without requiring a proprietary LS DLL or model. It does not prove NR
image quality, latency, compatibility across GPUs, or a performance advantage.
