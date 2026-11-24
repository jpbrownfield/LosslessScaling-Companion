# Companion bridge to FriedSnowman's addon manager

The source of truth is `upstream.lock.json`. Current baseline: **build-7**, commit
`d13c33ed5f3e094812f2258bb95c523d73047ccb`; Companion revision **13**. This replaces
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

Revision 9 adds capture progress diagnostics to the existing host status pipe.
`capture.traceVersion=1`, `capture.progress` and `capture.shaderSamples` distinguish
dispatches without a callback, profile waits, missing shader identities, shader
matches, unsupported textures, skipped capture dispatches, handoff attempts,
actual callback entry, and accepted/declined handoffs. Driver and DXGI format
are included. The trace retains only the first eight SHA-256/group-shape samples
per settings application; it stores no shader bytecode or frame pixels. Counters
are best-effort concurrent observations, not an atomic frame transaction. There
are no new graphics hooks, GPU readbacks or per-frame log writes.

In the Debug menu's Application tests, run **DLSS5 NR benchmark reproduction
(LSFG 3 + HDR)** (`nr_capture_trace`). Confirm the restart prompt. It launches
the bundled benchmark, deploys one temporary DLSSNR02-enabled recipe, and applies
LSFG 3, WGC, HDR and Scaling Off to the private native settings. It waits up to
30 seconds for NR, then captures three snapshots before stopping scaling, even
when processed-frame checks fail. No unrelated add-on recipes are run.
The host package and imported x64 `nvngx_dlssnr.dll` runtime must already be in
the asset store; it does not download proprietary runtimes or disable NR as a
fallback. Actual inference requires a supported NVIDIA capture adapter.

The existing live deployment workflow stops the benchmark and restores temporary
settings/deployment and in-memory flags in cleanup, including failed attempts.
Normal diagnostic folder reset still runs afterward. The downloadable archive
contains `nr_capture_trace.log`, with capture snapshots under
`details.testedProfiles[].captureTrace`, plus module, log and processed-frame
checks. A successful reproduction requires processed NR frames and complete
capture telemetry; merely loading the addon or collecting snapshots cannot pass.

For an already failing session, run **DLSS5 NR: inspect current application
without restarting** (`nr_capture_current`) in Application tests. This read-only
test preserves the application, settings, deployment and LS process. It exports
the active preset, observed native settings, actual capture adapter and three
capture-progress/NR-metric snapshots. Loading the addon or retaining historical
processed-frame counts cannot produce a processing pass: the frame counts must
advance during collection. A known requested GPU mismatch remains a warning.

The benchmark reproduction now uses the actual capture adapter from telemetry
when available and requires a requested GPU to be positively identified. A 3090
run cannot pass a benchmark requested on the 5050 just because frames processed.

The same read-only collector is also available from the command line:

```powershell
.venv/Scripts/python.exe run_live_diagnostics.py --capture-trace <LS_PID> > nr-capture-trace.json
```

This mode reads three fresh, PID/creation-time-validated snapshots one second
apart, without elevation, taking Companion's single-instance guard, changing
profiles, or stopping scaling. Transient read errors remain in the report.
`traceSupported=false` means the added progress fields were not observed; check
the host version and read errors. Normal addon diagnostic bundles also retain
the full capture telemetry. Rebuild and deploy the host package for these native
fields; changing Companion's Python source alone cannot instrument an older DLL.

Compare successive snapshots while the problem is present:

- Growing `dispatchesWithoutCallback`: no real-frame callback is registered;
   inspect addon enabled/loaded/faulted/error fields.
- Growing `profileWaits`: inspect capture API, FG, scaler and support text.
- Dispatches with a callback but no shader matches: inspect shader samples and
   missing-identity count. Unmatched passes are normal alongside successful matches.
- Matches but `unsupportedPauses`: inspect capture error, dimensions and format.
- Handoff attempts and declined results but no callback entry: inspect vendor
   routing, backend selection and addon status.
- Handoff attempts without callback entry or a result across repeated snapshots:
   processing may be blocked before invocation (for example during backend setup).
- Callback entry without a result across repeated snapshots: the callback may be
   blocked initializing or processing; compare the NR metrics and status.
- Accepted handoffs increasing: callbacks are returning accepted results. Check
   NR-specific processed-frame metrics before concluding that inference ran;
   an identity/pass-through callback also accepts frames. A persistent waiting
   label may need investigation in the addon/status presentation.

`lsc_capture_trace_test` checks concurrency, sampling bounds and reset behavior;
the live capture matrix additionally checks callback entry and accepted results.

Revision 8 recognizes all four verified NIS capture consumers: SDR/HDR with
128-thread groups (LS resources 245/246) and 256-thread groups (243/244).
FG Off transforms their mip-zero input before dispatch, just as for Scaling Off's
copy pass (249). LSFG 3 continues to transform capture output (254) before
history/pyramid/interpolation consumers. The copy and capture passes are shared
by SDR and scRGB; unrelated scalers and generated-frame shaders are not added.
Exact runtime SHA-256, WGC/profile gates and texture-format checks remain in force.
DXGI capture is still unsupported. This revision requires a rebuilt host package;
the published revision 7 binary does not acquire these changes automatically.

The opt-in live matrix uses the supplied original LS 3.2.2.0 engine with an
identity callback, without loading NR or modifying installed LS/settings:

```powershell
cmake --build <host-build> --config Release --target eam_captureprobe
$env:LSC_TEST_CAPTURE_PROBE = (Resolve-Path '<host-build>/Release/eam_captureprobe.exe').Path
.venv/Scripts/python.exe -m pytest tests/test_addon_host.py::NativeCaptureVariantTests -q
```

Place a copy of the original engine at `build/overlay-input-ls/Lossless.dll` first.
The test opens a temporary animated source and exercises all eight combinations
of FG Off/LSFG 3, Scaling Off/NIS and SDR/HDR. On the local Intel driver, both
FG-Off NIS cases previously stayed Waiting (resources 243/244) and now capture
frames. LSFG 3 + HDR + Scaling Off already captured scRGB frames before this
patch; a waiting NR addon in that setup needs further investigation beyond
shader recognition. Identity capture proves neither NR inference nor NVIDIA
backend initialization or image quality.

Revision 7 protects the shared system cursor used by LS's cursor watcher.
The supplied, verified LS 3.2.2.0 engine loads OCR_NORMAL with LR_SHARED and
passes that same handle to SetSystemCursor while the pointer is outside the
capture. SetSystemCursor consumes its input; shared cursor handles must not be
passed to it. The guard replaces only the forwarded engine's SetSystemCursor
import. A replacement of a system cursor with its own current shared handle
becomes a successful no-op. Private replacements and errors still reach the
original API with their ownership semantics. Other modules' imports, system
cursor schemes, visibility counts and game cursor settings are unchanged.
The import hook remains pinned for the process lifetime, independently of
ReShade and whether passthrough is enabled. `cursor` telemetry reports guard
installation, Windows errors and suppressed self-replacement count.

Companion also requests WM_SETCURSOR when the verified scaled game regains
focus, with at most three attempts on subsequent process-monitor ticks. The
recipient must remain in the target process and its foreground window family,
with the pointer inside the game's client area. A bounded SendMessageTimeout
lets the game select its own shape (including intentionally no cursor). This
does not inject mouse movement, force an arrow, change ShowCursor counts,
reload a system cursor theme or take focus. Stop, target/session changes and
focus loss cancel pending requests. Cursor snapshots and message outcomes are
logged for diagnosis. Software-drawn cursors or engine-side capture failures
may still require a game-specific fix; full Alt-Tab acceptance needs a desktop.

`lsc_cursor_test` checks private/error forwarding with a spy (no global cursor
changes), actual PE import interception, repeated LS-style shared-handle calls
and a real off-screen game window restoring a custom cursor via WM_SETCURSOR.
The real-engine headless test additionally verifies guard installation and
native status with the supplied LS engine. Neither claims live Alt-Tab coverage.
See Microsoft's [SetSystemCursor ownership contract](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setsystemcursor)
and [WM_SETCURSOR handling](https://learn.microsoft.com/en-us/windows/win32/menurc/wm-setcursor).

Revision 10 additionally recovers the engine's custom cursor image cache while
keeping Multi-display mode enabled. The supplied LS 3.2.2.0 engine publishes the
HCURSOR before converting it into a renderable image, and an unchanged HCURSOR
skips conversion. Its foreground callback now advances a cache generation; at
the next eligible cursor-image update, the render thread clears only the cached
identity so LS releases and rebuilds its image. This also refreshes custom cursor
pixels changed by a game without a new handle. A missing image can retry at
bounded intervals, with at most four build attempts per shape/focus generation.
Stable valid images are retained. Hidden cursors, nonforeground games and other
GetCursorInfo callers are unchanged; no synthetic activation, pointer movement,
visibility override or replacement arrow is used.

Private cache access requires exact PE identity and instruction signatures for
the cursor update, foreground callback, target window and Multi-display setting.
An unknown engine disables this recovery and reports ERROR_REVISION_MISMATCH,
while retaining the existing shared-handle guard. Native `cursor` telemetry
includes `cacheRecovery`, `cacheRecoveryError`, `cacheInvalidations`,
`cacheMissingImageRetries` and `cacheFocusEvents`. Native policy tests exercise
same-handle recovery, intentional hiding, unrelated focus, successful cache
retention and bounded missing-image retries; the supplied-engine test verifies
installation. Interactive game Alt-Tab validation remains necessary.

Settings → Display & scaling → Window behavior exposes **Restore custom game
cursors after Alt+Tab**, enabled by default for existing and new configurations.
The global preference overrides stale per-profile host snapshots and controls
both the native cache refresh and the bounded WM_SETCURSOR requests. Host pipe
receipts `C`/`c` enable/disable recovery immediately and persist it under
`global.companion.cursor_cache_recovery`; `cursor.controlProtocol=1` advertises
support and `cursor.recoveryEnabled` confirms the requested state. Disabling
recovery retains the shared-handle ownership guard and never changes native
Multi-display mode, cursor visibility settings or window focus.

Revision 6 keeps LS capture running when Companion activates the presentation
for overlay keyboard input. The managed app and native engine both subscribe
to foreground WinEvents; a process-local guard wraps those LS subscriptions and
omits only foreground notifications for the protected presentation. ReShade,
other add-on callbacks, and foreground changes to other applications remain
unchanged. The guard and callback registry stay pinned with the host, and
successful UnhookWinEvent calls retire their registry entries. A failed guard
installation disables managed input control and publishes the native error.
Settings/manager windows are excluded from presentation selection and subclassing.

LS can destroy and recreate its presentation during focus suspension. The watcher
releases the old input hooks, preserves the requested state while the same game
window remains alive, and attaches to the replacement. Target closure or an
explicit disable clears the preference. Control stays pending through the initial
window changes; unexpected presentation loss during enable is logged explicitly.
The desktop integration test loads the packaged DLL, registers an LS-style
foreground-stop listener, and tests real keyboard input and presentation replacement.

Revision 5 added the 16-byte `E` receipt (`<c3xIQ`: command, target PID, full-width
HWND) under `reshade.controlProtocol=2`. The target is verified directly instead
of inferred from windows between LS and the game in Z order. `D` is still one byte.

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
The status pipe reports `managed: true, headless: false`;
legacy headless workers report `headless: true`. Both retain process identity and
freshness checks. The rendering pipeline,
model forwarder, shaders, SDK method order, and addon lifecycle are unchanged.

Build 7 Companion revision 2 accepts two one-byte delivery receipts in addition
to the existing `1` acknowledgement: `E` enables overlay input passthrough and
`D` disables it. No configuration or paths are accepted. The Python client checks
the pipe server PID, process creation time, telemetry freshness and control
capability before issuing either request. The existing native passthrough
watcher applies it; `controlPending` remains true until window changes finish.
Companion waits for confirmed native state before updating the overlay. Managed
profiles set `ReShadePassthrough.companion_control=1`, so the ReShade menu key
does not also toggle passthrough or trigger an automatic click/key replay.
Focus loss restores window styles without taking focus back. The native
`eam_featurestest companion` test checks explicit, repeated enable/disable and
window-procedure restoration using an off-screen window without moving focus.

Build 7 Companion revision 3 separates `controlRequested` (the remembered
overlay preference) from `interactive` (effective passthrough). Focus loss
suspends input and demotes the presentation with `HWND_NOTOPMOST` and
`SWP_NOACTIVATE`. Focus returning to the remembered game process or LS restores
passthrough and topmost. Explicit close, target/session loss and shutdown cancel
the preference. Window restoration explicitly restores Z order, preserving an
originally topmost presentation when closing while focused. The native off-screen
test simulates focus transitions and verifies resume, cancellation, topmost
restoration and unchanged foreground focus.

Revision 4 renders the manager panel with a separate WARP software D3D11 device.
Loading the system DLLs alone cannot bypass ReShade's process-wide detours;
ReShade explicitly excludes WARP devices and their swap chains. LS's rendering
and add-on devices are unchanged. The panel keeps its existing hidden/minimized
render-loop suspension. `lsc_panel_test` checks the software adapter, absence of
ReShade device/swap-chain proxies, exact pixel colors, resize and presentation.
Pass an absolute path to an official ReShade DLL to exercise its hooks; create
an isolated `ReShade.ini` beside the test executable first. The hardware positive
control is skipped if only Microsoft's Basic Render Driver is available, which
ReShade also excludes. See ReShade's [D3D11 device creation](https://github.com/crosire/reshade/blob/v6.8.0/source/d3d11/d3d11.cpp#L128)
and [swap-chain wrapping](https://github.com/crosire/reshade/blob/v6.8.0/source/dxgi/dxgi.cpp#L316).
The Add-ons card reads the bundled version from `lossless-scaling-deployment.json`
inside the archive; `MANAGED_HOST_VERSION` remains the expected native contract.

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

Confirmation snapshots the presentation HWND before a command. An unchanged
pre-existing window cannot bypass a log freshness requirement. If no new log
event arrives, a new presentation HWND observed on two consecutive polls can
confirm activation; disappearance of a pre-existing presentation on three polls
can confirm deactivation. These transitions invalidate older latched log state,
so a silent host log cannot permanently pause autoscaling after a successful
hotkey. A fresh explicit log event remains authoritative.

On multi-GPU systems, `capture.adapterSource` distinguishes `device_creation`
from `capture_frame`, and `capture.adapterLuid` identifies the observed adapter.
The status line labels a creation-only observation as capture-unverified rather
than an active capture GPU. LS can create devices on more than one GPU. GPU 0
load/VRAM telemetry is sampled independently and must not be used to select NR's
adapter. NVIDIA NR initializes its D3D12 device from the real frame's D3D11 device
LUID. A first-device name mismatch while callbacks are absent does not by itself
prove NR selected the wrong adapter; collect the failing capture trace before
restarting LS or replacing the failing session with a benchmark.

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

## LS-only pacing observer, protocol 2

Host build-7-lsc.11 publishes `presentationPacing.version=2` and
`controlProtocol=1`. The existing validated status pipe accepts these ephemeral
receipts in addition to the cursor and ReShade controls:

- `B` plus three zero bytes, uint32 source PID, uint64 source HWND, uint64 LS
  output HWND, uint64 session-context key (32 bytes, little-endian).
- `P` enables timing collection; `p` bypasses it for benchmark off arms.

Binding validates HWND ownership and the actual `LosslessScaling` output title.
Unbound snapshots cannot authorize adaptive writes. Output device token and LUID
come from that swapchain, with metadata refreshed at most once per two seconds.
Resize/color-space changes, focus, window state/size and monitor/refresh changes
invalidate the generation. Capture and presentation adapter LUIDs remain separate.

The payload retains median/p95/max interval, p95 time inside Present, relative
stalls and coverage counters, and adds `sequence`, `generation`, `context`,
`startedMs`, `endedMs`, `ageMs`, `sourcePid`, `sourceWindow`, `outputWindow`,
`swapchain`, `device`, `presentationLuid`, `monitor`, `refreshHz`, `monitorWidth`,
`monitorHeight`, `enabled`, `identityVerified`, `focused`, `dropped`, `totalDropped`
and `overflow`. Start/end use QPC milliseconds. `dropped` belongs to that completed
window; `totalDropped` is cumulative. Completed windows are disjoint two-second
intervals with at most 2,048 retained observations, not a rolling evidence window.

Present/Present1 test calls, failed returns and nested calls are excluded. Fixed
buffers and a try-lock keep collection bounded; contention drops data instead of
blocking rendering. Sorting occurs on the telemetry reader after copying/unlocking.
No GPU waits/readbacks, per-frame allocations or disk logging are added. A disabled
observer bypasses QPC/timing collection. Unbound legacy profiling remains available
for the existing capture fixture and never establishes adaptive identity.

Python policy 2 consumes fresh bound windows once, and handles source-cap trials.
Present submissions and capture-intake response do not prove display deadlines or
GPU processing time. The acceptance benchmark joins QPC-aligned PresentMon frames
and separately measures fixed-cap off/on overhead. See
[`rtss dynamic limiting notes.md`](../../rtss%20dynamic%20limiting%20notes.md).

The release builder runs `lsc_pacing_probe_test`, including duration windows,
repeated readers, overflow and a CPU collection microbenchmark. The latter is not
a live GPU frametime verdict. Existing opt-in real-engine capture fixtures remain
available via `LSC_TEST_CAPTURE_PROBE` and `EAM_PROBE_LS_PACING=1`.

## Standalone HDR probe build (revision 12)

The bridge passes the cursor focus-event epoch into `PresentationPacingSnapshot`.
This preserves focus-change invalidation in the host while allowing the standalone
HDR probe to link without the cursor hook implementation. Standalone probes use
an epoch of zero. Validate with the complete `scripts/build_addon_manager.py`
build, including the final HDR fixture build, rather than selected host targets.

## ReShade context identity and cursor resume (revision 13)

The November 23 live Warframe trace has a loaded NR addon but zero observed
dispatches. ReShade was loaded; the successful November 20 benchmark did not load
it. ReShade wraps the immediate context, while the runtime Dispatch detour receives
the underlying context. Registering only the wrapper caused the identity filter to
reject that work before capture diagnostics ran. Device attachment now resolves
ReShade's owned `IID_UnwrappedObject` interface and verifies its D3D11 context
interface once. The host retains the public wrapper; the render hook compares the
native identity. There is no additional per-dispatch query, lock, allocation or
readback. See ReShade v6.8.0's `source/com_utils.hpp` and
`source/d3d11/d3d11_device_context.cpp`.

`lsc_routing_test` validates the unwrapping contract, software-device dispatch and
shader identity, exactly-once callbacks and shutdown. An optional official ReShade
DLL argument exercises its real hardware proxy, requiring a supported GPU.
The HDR DLSS5 NR benchmark now enables the latest installed ReShade runtime too.
It requires ReShade to be installed, verifies its deployed DLL and runtime
initialization alongside NR frame processing, and restores the previous settings
and deployment afterward. Its existing capture trace includes cursor counters.

The same trace reports enabled cursor recovery and 56 focus events but no cache
invalidations. It cannot establish which cursor gate prevented a refresh. New
`cacheQueries`, `cacheImageQueries`, `cacheHiddenSkips`, `cacheUnfocusedSkips` and
`cacheSettlingRetries` counters distinguish those paths. Counters use relaxed
atomics and do not add logging or GPU work to stable frames. A same-handle focus
return now permits three delayed rechecks even if the cached image pointer is
non-null: a pointer does not prove its pixels survived resume. The four-attempt
limit, foreground and visibility requirements remain; Multi-display stays enabled.
An unhandled WM_SETCURSOR request is reported as `unhandled`, rather than evidence
of cursor restoration. Interactive Prey and Warframe validation remains required.

## Distribution and validation limits

The host is MIT; the NR addon is GPL-3.0-only. Its verified corresponding source
ZIP is retained unchanged. Our package also includes `CompanionHost-source.zip`
with the patched host, overlay, build script and exact ImGui/MinHook dependencies.
Other runtimes retain their own notices. Neither the proprietary NR model nor
Lossless Scaling's engine is redistributed.

A synthetic host test proves startup, pipe telemetry, no manager window and
shutdown without requiring a proprietary LS DLL or model. It does not prove NR
image quality, latency, compatibility across GPUs, or a performance advantage.
