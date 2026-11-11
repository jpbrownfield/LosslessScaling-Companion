# LS Companion

A Windows companion for **Lossless Scaling (LS)** that activates executable profiles from the foreground window, verifies the exact target window before sending the synchronized LS hotkey, manages Lossless Scaling ReShade presets and add-ons, and never modifies game directories. An optional browser extension adds automatic browser-video scaling.

---

## ⚡ Key Features

1. **Foreground Application Auto-Scaling:**
   - Matches configured applications by executable path or process name.
   - Carries the selected PID and HWND through profile activation, refocuses that exact window after any Lossless Scaling restart, and refuses the hotkey if identity verification fails.
   - Confirms the resulting scaling state from Lossless Scaling's overlay instead of treating successful keyboard input as proof of activation.
   - Keeps an application scaled when focus changes. A configured game can explicitly take precedence over an actively scaled browser profile.
   - Remembers a manual scaling stop for that application process until it exits. Alt-tab, browser events and Process Lasso cannot automatically restart it; other applications can still auto-scale, and a manual start remains available.
   - Follows monitor moves (including Win+Shift+Left/Right) when the profile follows the application monitor. After the destination settles, Companion restarts scaling on that display while preserving any rendering-GPU selection.
   - Starts LS with a bundled, process-specific startup-window guard to suppress dashboard flashes without hiding its scaling overlay. The guard expires after startup so the dashboard can still be opened manually.
   - Offers DLSS and FSR manager add-ons in the profile's Scaling Type list. These choices save native NIS plus the matching add-on configuration together, disable competing upscalers, and remain distinct from LS's built-in FSR.
   - Refreshes GPU identities at startup and before routing. Profile GPU/display numbers are remapped using saved device identity, PCI model or an unambiguous friendly name; the inventory history is persisted. Unrecoverable legacy numeric-only overrides follow General Settings rather than binding to an arbitrary GPU. An unavailable explicit General Settings GPU is reported instead of silently switching to Auto. Native LS routing is updated through the normal safe profile-activation path, not by restarting scaling just to refresh the inventory.
   - Keeps readable per-profile JSON snapshots under `%LOCALAPPDATA%\LosslessScalingHelper\profile-settings\<profile-id>.json`, with friendly setting labels, GPU/display names, device identities and resolved IDs. Snapshots follow profile edits, renames and hardware remapping; unchanged files are not rewritten. The main `settings.json` remains authoritative, and revision checks prevent a stale snapshot from undoing newer settings. Missing or ambiguous legacy identity records fall back to General Settings. The per-profile add-on JSON remains separate.

2. **Native Win32 Input Injection (`ctypes` + `SendInput`):**
   - Synthesizes scan-code keyboard input through the Windows input API.
   - Ultra-low latency, non-blocking, and avoids focus traps or virtual key drops.

3. **Multi-Application Profile Engine:**
   - Create and customize profiles for specific executables (e.g. `Cyberpunk2077.exe`, `chrome.exe`) or web domains (e.g. `youtube.com`).
   - Automatically matches the active window when switching tasks.
   - Offers one per-profile **Automatically scale this application** toggle; there is no blur-triggered de-scaling.
   - A checked-by-default General Settings option enables autoscaling only for profiles newly created through the companion. Imported Lossless Scaling profiles start with helper autoscaling off, and changing the default never rewrites existing profiles.
   - Installation and initialization never rewrite Lossless Scaling's native profiles. Native hotkey and Auto Scale ownership begins only after the user changes an auto-saved General Setting.

4. **Lossless Scaling ReShade Preset Management:**
   - Sets ReShade's `PresetPath` and `StartupPresetPath` in `ReShade.ini` beside Lossless Scaling, with the legacy `CurrentPresetPath` kept synchronized.
   - Enables the selected shader techniques when a preset is first created, including all three built-in presets. Later deliberate technique toggles, ordering, shader arguments and preset preprocessor definitions are saved to that named shared preset and reused by every application profile selecting it.
   - Enables ReShade preset autosave and editable shader parameters. Companion captures runtime preset writes about once per second, on overlay close, at shutdown and before deployment changes. Deployment snapshots and capture receipts preserve newer shared edits; conflicting runtime values remain in the preset's `.runtime-capture-*.json` receipt. The general SDR-white/monitor and HDR-peak controls retain ownership of their luminance values. ReShade's menu/runtime configuration remains managed separately from shader presets.
   - Triggers ReShade reload hotkeys on profile activation.

5. **Lossless Scaling Add-ons:**
   - **Sunshine / Apollo:** A checkbox under Add-ons, enabled by default, temporarily matches the scaling GPU to the hardware encoder GPU during a connected streaming session. It reads the running host's log, keeps the selected output monitor, and restores normal profile/default GPU routing after disconnect. Missing logs, software encoders, and ambiguous GPU names leave normal routing in place.
   - Copies configured DLLs only into the Lossless Scaling directory.
   - Uses temporary transactional rollback data while applying changes, plus one persistent original backup and one preinsert backup for recognized legacy add-ons.
   - Game executable paths are used only for foreground profile detection.
   - ReShade profile settings offer separate **Display Commander** and **Inject NVIDIA Reflex** switches. Reflex enables Display Commander and ReShade even without a shader preset, and disables Special K Reflex for that profile. Download Display Commander from Add-ons first; ReShade Full Add-On 6.6.2 or later is required. Its foreground/background FPS limiters stay disabled. These controls affect LS, not the captured game's simulation or render queue. Display Commander updates are installed manually from its Add-ons card.
   - The profile editor lists supported image processors and display/input features with a master on/off switch. Internal reporters, automatic AMD backend routing and retired integrations do not appear as separate processors. Host startup flags and unrecognized saved values are retained in the read-only advanced view. Numeric tuning values are parameters of the parent feature, so they do not each have an enable switch. Overlay input is automatic while ReShade is enabled; upscaler selections stay synchronized with Scaling Type.
   - Managed Lilium SDR-to-HDR presets prepare captured SDR before inverse tone mapping: linear scRGB is normalized using its 80-nit reference, HDR10 is decoded to linear BT.709, and Lilium uses linear input without another gamma decode. Captured SDR white defaults to **400 nits**, configured once in **Add-ons ? ReShade ? HDR display & menu settings**. Each identified monitor can override it; blank monitor entries inherit the shared default. Overrides use the monitor device identity and apply to LS's selected output display at the next scaling start. Disconnected monitor settings are retained. This is separate from HDR peak brightness and is shared across application and shader profiles. Peak brightness remains separately controlled by the HDR display setting. Use an SDR source with Windows HDR and LS HDR support; native HDR, Windows Auto HDR, RTX HDR or Special K conversion must not feed a second SDR-to-HDR conversion. Imported or explicitly input-tuned presets retain their input path. Peak refresh preserves other shader edits.
   - Installs a bundled host built from pinned FriedSnowman sources, with unchanged upstream addons and a Companion headless/status bridge. Deployment is transactional; addon-host updates ship with Companion.
   - Saves one complete addon configuration per profile. Built-in controls edit inactive and active profiles; active changes apply through the safe stopped-process reload workflow.
   - Explicitly disables optional add-ons unless enabled for that profile. The editor includes a **Disable all processing** action; the **Overlay toggle** is enabled automatically for ReShade profiles when needed.
   - While the Overlay toggle is on, a yellow border and an **Overlay active** card identify the interaction mode. Lower-right hints show the actual ReShade menu key and the configured Companion shortcuts: **Delete** opens the current profile, **Insert** opens its Add-ons section, **=** toggles a live application FPS/frame-time graph, and **-** opens Special K when loaded. A custom Special K panel binding takes priority. Configure or disable these shortcuts in **Settings → Overlay**. The graph requires verified PresentMon from Performance & Benchmark and captures only the current application; it stops when the overlay is closed. The headless host does not create an add-on manager GUI.
   - Overlay text, cards, hotkey hints and the performance graph scale with the target monitor's resolution: 1080p uses the base size, 1440p uses 1.33× and 4K uses 2×.
   - Focusing an executable profile prepares it silently. Scaling notifications appear when a scaling request begins; each toast fades over seven seconds before the cards below it move up.
   - Add-on edits made during scaling are saved immediately and deployed after scaling stops. Unknown saved settings remain available in the editor and are preserved.
   - Keeps bounded runtime logs in `%LOCALAPPDATA%\LosslessScalingHelper\logs\companion.log` and includes the recent log tail in diagnostic downloads.
   - **Debug → Log** shows timestamped Info, Warning, and Error activity, including automatic scaling decisions, target verification, hotkeys, confirmation failures, GPU routing, deployments, updates, and diagnostics. Search or filter by level, pause the view, follow new entries, or download the shown entries. The live view retains up to 1,000 entries from the last 24 hours of the session. The plain-text file retains complete entries from the last 24 hours across restarts, capped at 10 MiB by dropping the oldest entries. Age cleanup runs at startup, while writing, and once per minute while idle.
   - Archives recognized LosslessProxy-era add-on folders before the first addon-host deployment and leaves unknown third-party folders untouched.

6. **Windows System Tray UI:**
   - Opens the Profiles dashboard or jumps directly to its Settings view.
   - Provides a compact **Quick Add Profile** menu for visible applications.
   - Keeps advanced configuration-folder access in the dashboard's debug tools.

7. **Process Lasso Performance Mode Trigger:**
   - Optionally tails new records in Process Lasso's CSV action log.
   - Activates scaling only when the triggering executable has an explicit helper profile and a focusable visible window.
   - With the override blank, discovers `/LogFolder=` on running Process Lasso components, the `LogFolder` registry setting, and then current `processlasso.log` or legacy `prolasso.log` locations.
   - Attaches at the end of the log so old launches are never replayed, and handles later log truncation or replacement.
   - Performance Mode ending does not de-scale the application.
   - Uses the application path for detection only; managed files remain confined to the Lossless Scaling directory.

8. **RTSS Static and Dynamic Frame Limiting:**
   - Creates reversible per-executable limiter profiles inside RTSS, never inside the game directory.
   - Static mode applies a configured source FPS cap. Dynamic mode starts at a configured maximum and lowers the game cap one FPS per qualified five-second gameplay window until `LosslessScaling.exe` reaches the configured minimum GPU-utilization target.
   - Samples Windows GPU Engine counters once per second and uses five-sample medians for both the scaled-window PID and Lossless Scaling PID.
   - Freezes indefinitely across dramatic game-GPU drops instead of learning a long-running menu as gameplay.
   - Treats game processes lasting more than ten minutes with representative GPU utilization above 50% as alternate-baseline candidates. A higher candidate is promoted only after the game exits and affects the next session.
   - Supports a five-minute manual representative-gameplay calibration. Completing it disables further automatic learning for that profile until reset; reset erases learned GPU baselines and identified limits and resumes learning.

---

## 📁 Project Structure

```text
.
├── extension/                       # Chrome Extension (Manifest V3)
│   ├── manifest.json
│   ├── background.js                # WebSocket client & event dispatcher
│   ├── content.js                   # Fullscreen & HTML5 video listeners
│   └── popup/                       # Extension popup interface
│
├── companion/                       # Python Companion Daemon
│   ├── config/                      # Generated settings & profiles
│   ├── core/
│   │   ├── models.py                # Pydantic schemas (Profiles, Config, Events)
│   │   ├── profile_manager.py       # JSON profile persistence & match engine
│   │   └── state.py                 # Central state machine
│   ├── services/
│   │   ├── input_simulator.py       # Hardware scan-code SendInput
│   │   ├── server.py                # Async WebSocket server (ws://127.0.0.1:24892)
│   │   ├── process_watcher.py       # Win32 foreground hook & process scanner
│   │   ├── reshade_manager.py       # ReShade.ini parser & preset swapper
│   │   └── dll_manager.py           # Legacy LS-only DLL backup/deployer
│   ├── ui/
│   │   └── tray.py                  # pystray system tray icon & menu
│   ├── main.py                      # Main daemon entry point
│   └── requirements.txt
├── run_companion.py                 # Root launcher script
└── README.md
```

---

## 🚀 Quick Start Guide

### 1. Install Python Dependencies

Open a PowerShell terminal in the project directory:

```powershell
pip install -r companion/requirements.txt
```

The desktop dashboard uses Python's pywebview with Microsoft Edge WebView2
Runtime. The Windows installer installs the Runtime when needed (internet is
required only if it is missing). For source runs, install the
[WebView2 Evergreen Runtime](https://developer.microsoft.com/microsoft-edge/webview2/)
if it is not already present.

### 2. Start the Python Companion

```powershell
python run_companion.py
```
* You will see the **LS Companion** icon appear in your Windows System Tray (near the clock).
* The companion automatically hosts a local WebSocket server at `ws://127.0.0.1:24892/ws`.

### Run without Lossless Scaling or administrator rights

Use the development simulation on a normal Windows desktop or Remote Desktop session:

```powershell
python run_simulation.py
```

The runner builds and starts an unprivileged `LosslessScaling.exe` simulator, a
visible `SimulationGame.exe` target, and the real companion UI on port `24893`.
It accepts companion toggle commands without desktop input injection, creates a
real topmost simulation overlay, and writes compatible scaling events for the normal inspector. Configuration, managed
assets, logs, and the fake installation stay under `.tmp/simulation`; real
Lossless Scaling, Task Scheduler, NVIDIA profiles, and RTSS profiles are not
modified. Source changes automatically reload the companion. Use
`python run_simulation.py --reset` to recreate the isolated settings.
Run `python run_simulation.py --self-test` for a short process, command, overlay,
and log-inspection check that exits automatically.

### 3. Load the Chrome Extension

Download the [current source archive](https://github.com/jpbrownfield/LosslessScaling-Companion/archive/refs/heads/main.zip), extract it, and use its `extension/` directory, or use that directory directly from a repository checkout.

1. Open Google Chrome and navigate to `chrome://extensions/`.
2. Enable **Developer mode** (toggle in the top-right corner).
3. Click **Load unpacked** and select the `extension/` directory in this workspace.
4. Click the extension puzzle icon and pin **Lossless Scaling Bridge**.
5. The badge will show **ON** (green) once connected to the companion!

---

## 🎮 Initializing Lossless Scaling: Methods Explained

### 1. Hardware ScanCode Simulation (Default & Recommended)
* **How it works:** Uses Win32 `SendInput` with direct hardware scan codes (e.g., `Ctrl + Alt + S`).
* **Why it's best:** Completely silent, instantaneous (takes under 10ms), does not require touching the Lossless Scaling GUI, and works seamlessly with fullscreen hooks.

### 2. Auto-Launch Support
* If `LosslessScaling.exe` is not running when the companion starts, the companion will automatically launch it in the background if configured in `companion/config/settings.json`.

---

## ⚙️ Configuration & Profiles

The configuration is saved in `%LOCALAPPDATA%\LosslessScalingHelper\settings.json`.
On first run, an existing legacy `companion/config/settings.json` is migrated. You
can open the settings folder from the red debug action at the bottom of the
dashboard's Settings view.

Lossless Scaling's configuration is normally read from
`%LOCALAPPDATA%\Lossless Scaling\Settings.xml`. LS Companion reads its activation
hotkey and can disable native Auto Scale when automatic profile scaling is enabled.
The optional hotkey override preserves an editable user-facing trigger, assigns
Lossless Scaling an unmodified F24 internally, and routes the trigger through the
matching foreground-window profile (or the Default profile when none matches).

Companion checks GPU index mappings at startup, then compares fresh DXGI adapter
order, identities, and duplicate counts about every three hours when idle. The
standalone DXGI helper runs at Windows idle process priority. Both checks wait
while scaling, editing, tests, or updates are active. A changed layout
or LS process triggers an idle LS adapter-list check. Refreshes wait for scaling,
profile editors, tests, and updates to finish; they never start scaling or request
game focus. Explicit profile GPU choices are remapped by saved hardware identity,
with Auto and uncertain matches preserved. Native XML changes are backed up and
rolled back if LS's order changes across the confirmation restart.

The native name-list call is restricted to the inspected LS UI build. Other builds
report that local validation is needed and retain saved selections. See the
[local GPU mapping validation guide](docs/idle-gpu-mapping-handoff.md) for the
remaining live LS checks, including Parsec/Vibeshine display changes.

Companion requires a separate copy of Lossless Scaling under
`%LOCALAPPDATA%\LosslessScalingHelper\runtime\lossless-scaling\installations\<generation>\app`.
The detected Steam installation is read only. Files are copied, never hard-linked.
LS updates create a new generation while preserving Companion's private settings.
The source path is remembered separately from the managed executable path.

Companion imports the existing XML once into
`runtime\lossless-scaling\data\LocalAppData\Lossless Scaling\Settings.xml`
under its configuration folder. It disables native auto-start/elevation in that
copy and creates `Settings.xml.bak` beside the private XML. Neither settings edits,
backup restoration, diagnostics nor add-on deployment may target the live XML or
source installation. Existing changes made by older builds are not restored automatically.

A mandatory .NET startup hook installs process-local Windows known-folder
redirection before LS enters Main, then verifies the same folder API LS uses.
Companion requires a nonce- and PID-matched receipt before accepting startup.
Missing bootstrap files, unsupported LS builds and unverified isolation stop
startup. Directly opening the cloned executable without Companion's launch
contract also stops before Main; launch the clone through Companion.

The tray opens one Python-owned WebView2 window, re-focusing the existing dashboard
instead of opening duplicates. Closing the window leaves the tray running; exiting
or crashing the companion also closes the dashboard host. Runtime changes are
pushed to the tray and dashboard without waiting for the process-monitor tick.
Disconnected or unresponsive backends show a stale-status warning and disable
dashboard controls until an authoritative state arrives. The window and tray share
the LS Companion lightning icon in packaged builds. Its collapsible settings
panels remember their expanded state.
By default, the dashboard follows the activation hotkey configured in Lossless
Scaling. The General Settings section's Scaling Behavior group controls automatic
profile scaling and the optional hotkey override. It also controls an elevated
`ONLOGON` Task Scheduler entry named `LosslessScalingHelper`. The companion
remains a tray process, and closing or minimizing the dashboard window does not
stop it.
The red **Revert All Lossless Scaling Settings and Remove Addon Files (Uninstall Companion)** action asks for confirmation, stops Lossless Scaling,
restores that untouched XML backup, rolls back only manifest-owned files in the
Lossless Scaling directory, and removes or restores every RTSS profile managed by
the helper. It also removes the helper's scheduled startup task, then disables
native-settings, Process Lasso, and RTSS enforcement so those changes are not
immediately reapplied. Downloaded assets and ordinary helper profiles are kept;
their add-on and RTSS selections are cleared after a successful rollback.
An opt-in monitor-isolation setting minimizes other eligible application windows
on the scaled target's monitor and restores only those windows when scaling stops.
Lossless Scaling, the target application's other windows, the companion, shell
windows, tool windows, and windows already minimized by the user are excluded.

On NVIDIA capture GPUs, the Companion Addon Host uses FriedSnowman's `DLSSNR02` implementation, including
NVIDIA Optical Flow, current-frame NR before LSFG, and SDR/scRGB composition.
Choose **Install bundled host** in Add-ons, import your own x64
`nvngx_dlssnr.dll`, and enable Neural Rendering in the desired profile. All
optional processing starts disabled. The supported configuration is LS 3.2.2.0
(the original DLL hash is checked), WGC, LSFG 3 or FG Off; FG Off requires Scaling
Off or NIS. Unknown LS builds and model failures can pause capture. Disable NR
and reload the profile or restart LS to recover.
Enabling the profile switch selects WGC, upgrades LSFG 1/2 to LSFG 3, and selects
NIS for an incompatible scaler when FG is off. These changes are saved together.
An unpinned profile attaches the most recently imported valid x64 NR model;
existing model pins and per-backend model paths are preserved. A missing model
is reported during deployment rather than silently omitted. The runtime status
shows capture/model failures and whether recent NR GPU timings were reported;
an enabled switch alone does not confirm that frames were enhanced.

AMD capture GPUs automatically use the separate experimental AMD NR addon.
Download **AMD Neural Rendering** in Add-ons, install Windows HIP 7, and use the
original NR 310.9.16 model matching the pinned runtime. The AMD model path can
differ from NVIDIA's patched model path. Compute flow and shared GPU textures
feed the asynchronous NR runtime; LS owns frame generation. AMD DLSS SR/RR is
not included: use FSR, XeSS or NIS. Adapter/model failures preserve the original
capture. Adapter-map-to-compute selection, hardware support, Ryzen iGPU test commands
and update notes are in [`native/amd_neuralrender/README.md`](native/amd_neuralrender/README.md).

The dashboard displays real host/addon status and the last completed GPU timings
through a bounded local pipe. There is no separate manager window in Companion
mode. Telemetry does not establish displayed FPS or prove scaling is active.
There is no migration from Echo settings. See the pinned source, build procedure,
protocol and future-upgrade checklist in
[`native/addon_manager/BRIDGING.md`](native/addon_manager/BRIDGING.md).

For DLSS upscaling, each profile's **DLSS Upscaler / DLAA** card exposes the
standard Auto/default/E/J/L/M presets. The separate **SM86 optimized Super
Resolution** dropdown offers **L-INT8** and **M-INT8** from sdli1995's backend.
Download its pinned package using **Add-ons → SM86 Super Resolution**, update the
bundled host, enable the DLSS upscaler, and select the desired INT8 mode. It pins
L or M and applies on a stopped profile reload. Off uses NVIDIA's ordinary
runtime. The optional SR files use a private directory; its FG features and
architecture spoofing are disabled. This targets RTX 30-series GPUs and does
not change DLSS 5 NR's model selection. Hardware activation, image quality and
speed must be checked on the selected GPU; the dashboard's proxy-loaded status
alone is not proof of INT8 inference. See the bridge notes for its log location.

### Graphics stack compatibility evaluation

After staging LSP-NeuralRender, ReShade, and Special K and configuring their
versions plus the DLSSNR runtime on a profile, exit LS Companion from the tray
and run the evaluator:

```powershell
python scripts/evaluate_graphics_stack.py
```

It selects the active profile by default (or asks which profile to use), performs
the preflight, asks for one explicit confirmation, and runs every viable
combination. To validate everything without changing the Lossless Scaling
installation, use:

```powershell
python scripts/evaluate_graphics_stack.py --preflight-only
```

The evaluator stops and restarts Lossless Scaling for each case, pauses while you
scale a representative application, records loaded modules and new Lossless
Scaling/LSP-NeuralRender/ReShade/Special K log content beneath
`diagnostics/graphics-stack`, and transactionally restores the managed deployment
that was active before the test. It prints a result summary when finished. Use
`--profile-id PROFILE_ID` to override the active profile,
`--cases lsp,lsp+reshade` for a subset, or `--capture-seconds 30` for timed
capture. It never deploys into a game directory. Logs establish load and runtime
health; visual quality still requires observing or capturing the scaled image.

### Performance benchmark

The performance benchmark is a separate permanent test path; it does not modify
or reuse the graphics-stack evaluator. Release builds embed the small,
project-owned `LSBenchmark.exe` workload directly in LS Companion. In the
dashboard's **Performance benchmark** panel, download and verify Intel's
PresentMon once to unlock the test. On startup, Companion also checks the managed
asset store, the user's Downloads folder, its own directory, common PresentMon
install directories, and `PATH`; a discovered binary is accepted only when its
x64 architecture and SHA-256 match an official GitHub Release. This detection is
silent and does not prompt or notify the user. While the test runs, Companion pauses its normal foreground automation
so the controlled test owns scaling. The dashboard test uses the Default
profile's existing Lossless Scaling, add-on, and RTSS limiter settings. For a
dynamic default it freezes the current learned limit, or the configured maximum
when no learned limit exists; it never calibrates or saves a different limit.
Raw CSV and a JSON summary are written under
`%LOCALAPPDATA%\LosslessScalingHelper\benchmarks` in a packaged release or
`diagnostics/performance-benchmark` in a source checkout.

The deterministic shader-heavy Direct3D 11 workload has adjustable GPU pressure
and a full-frame software-visible latency marker. A source checkout can also run
the Python command below after building `benchmark_app/ls_benchmark.cpp` into
`build/native/LSBenchmark.exe`.

```powershell
python scripts/benchmark_lossless_scaling.py `
  --width 1920 --height 1080 --shader-load 64 `
  --download-presentmon --use-default-profile --duration 90
```

When invoking the standalone Python command, exit the tray companion so it cannot
react to foreground changes. Use `--no-scale` for an unscaled baseline.
PresentMon latency fields are reported as software input/display observations,
not physical display click-to-photon measurements; the report does not assume
that source-process input is correlated to an LS-process output frame. With the
bundled workload, F15 additionally produces a known cyan/magenta frame and the
runner measures input injection to detection in Windows-composed screen output.
Use `--benchmark-exe` to substitute another windowed workload; the visual marker
measurement is unavailable for generic executables.

The **Lossless Scaling Add-ons** settings panel stages verified packages for
Companion Addon Host, its bundled add-on suite, ReShade, and Special K.
Staged packages are loaded only when selected in a profile's LS Graphics Stack.
The source and MIT notices for Companion's maintained ReShade bridge are under
`native/`; release builds compile and embed it rather than downloading the
upstream LSP-ReShade binary at runtime.

### Example Profile Configuration:

```json
{
  "id": "cyberpunk-cinematic",
  "name": "Cyberpunk 2077 UHD",
  "target_process": "Cyberpunk2077.exe",
  "auto_scale": true,
  "hotkey": {
    "modifiers": ["ctrl", "alt"],
    "key": "s",
    "hold_delay_ms": 50,
    "activation_delay_ms": 100
  },
  "reshade": {
    "enabled": true,
    "reshade_ini_path": null,
    "preset_path": "D:\\ReShade\\Presets\\Cinematic4K.ini",
    "reload_hotkey": "Home"
  },
  "dll_overrides": [
    {
      "enabled": true,
      "source_dll_path": "C:\\Mods\\OptiScaler\\dxgi.dll",
      "target_dll_name": "dxgi.dll",
      "deployment_mode": "copy",
      "deployment_target": "lossless_scaling"
    }
  ]
}
```

`deployment_target` must be `lossless_scaling`. When a profile changes that DLL
set, the companion stops Lossless Scaling, restores the previous profile's files,
deploys the new files, and relaunches it. Legacy `target_application` entries are
loaded only for compatibility and are refused at runtime. The companion never
deploys files into or injects code into the profiled game/application.

Run the companion at the same privilege level as Lossless Scaling and the target.
If either is elevated, run the companion as administrator so Windows permits
hotkey injection, window control, and process termination. A Lossless Scaling
instance relaunched by an elevated companion inherits that elevation.

---

## 📦 Building the Windows Executable

To compile the companion without requiring a Python installation:

```powershell
pip install pyinstaller
pyinstaller LosslessCompanion.spec
```
The compiled executable is `dist/LosslessCompanion/LosslessCompanion.exe`. Keep
the complete `LosslessCompanion` directory together when moving or distributing
it. The executable embeds a Windows `requireAdministrator` manifest, so Windows
shows a UAC elevation prompt whenever it starts.

Versioned GitHub Releases use a single `LosslessCompanion-Setup-x64.exe`
installer. The installer requests administrative elevation and installs the full
one-folder payload under Program Files by default, while allowing another install
location. Its shortcut page enables a Start Menu shortcut by default and offers
an unchecked desktop-shortcut option. The installed application retains its own
`requireAdministrator` manifest.

Packaged builds check this repository's stable GitHub Releases at dashboard
startup and once per day. You can also open **Settings → Updates** and
select **Check for updates** to bypass the cached result immediately. When a newer semantic version is available, Companion
offers to download `LosslessCompanion-Setup-x64.exe`, requires the matching
`LosslessCompanion-Setup-x64.exe.sha256` release asset, verifies the installer,
and then waits for explicit confirmation before launching it unless automatic
installation is enabled. Source and simulation runs never download or launch
production Companion updates. The checksum guards
against corruption and mismatched assets; releases are not currently code-signed.

**Notify me about Companion and add-on updates** is enabled by default.
**Automatically install updates** is opt-in and takes priority over update
notifications while preserving that preference. Automatic updates wait for
scaling, profile editors, diagnostics, and pending edits to finish. During the
update, the dashboard shows progress and blocks edits; hotkeys, foreground
automation, and server-side mutations are paused as well. A failed download or
verification releases the lock and can be retried manually; automatic retries
back off for an hour. Installed addon-host, ReShade, and Special K updates use their
existing verified release pipeline without enabling disabled integrations.
Checks use the versions selected by enabled profiles; merely downloading an
installer or staging a package does not mark those profiles as updated.
Automatic installs preserve newer and prerelease profile selections.
Community runtimes requiring a separate approval remain manual.

Automatic Companion updates download the stable GitHub release installer and
checksum, verify the staged installer again before launching, and use a silent
installation in the current app directory. Verified cached installers are reused,
including after a restart, without downloading them again. The WebView2 dashboard
helper exits before Companion releases its executable for replacement; closing
or disconnecting the dashboard cannot cancel an accepted installer handoff.
The installer waits for the old
process to exit before replacing files and reopens Companion on success, without
rebooting Windows. If setup aborts, it attempts to reopen the previous executable;
a persisted retry cooldown prevents repeated restart attempts. Installer logs remain under the configuration directory's
`updates/<version>/install.log`. Every Windows build now compiles and uploads
the updater installer and checksum; versioned releases publish those same assets
to GitHub Releases. Packaged updates replace the compiled application rather
than modifying source files inside a running Python/PyInstaller process.

Each available component update has its own dashboard notification row. Its
Download button stages that component directly, with percentage progress when
the download size is known and a byte counter otherwise. A shared server-side
lock prevents overlapping downloads/installs across dashboards and tray actions;
controls remain disabled through verification and installation. ReShade Setup
and Special K archives are extracted without running their installers. Their
profile updates wait until scaling stops before replacing active injector files.
The addon-host update flow may still restart LS when updating its host.

Opening Companion's Windows shortcut again opens or focuses the existing
dashboard instead of starting a second tray instance. Special K uses its private
component folder and `Global` configuration to hide its HUD and startup banner.
Its explicit injection mode selects D3D11 through the clone's `SpecialK.d3d11`
marker. Changes require restarting the managed LS process.

The addon host is pinned to FriedSnowman build-5 with Companion overlay revision 5.
The build verifies source/release archive digests, compiles the host, and retains
the release's unchanged NR/upscaler binaries, corresponding source and licences.
Run `python scripts/build_addon_manager.py` before packaging. Companion's controls
use the new NR schema directly; Echo-only processors are not included.

The existing host remains the engine integration. ReShade, Special K and custom
DLLs are explicitly loaded by that host from full paths beneath
`.companion/components/`, with identities and SHA-256 hashes declared in
`.companion/components.json`. The startup hook initializes selected components
before LS can create graphics devices. ReShade and Special K do not occupy
`dxgi.dll`, `d3d11.dll` or another Windows proxy filename. Conflicting loaded
copies and invalid component plans stop startup. Native host add-ons continue to
use their SDK manifests. Build the private-settings bootstrap with
`python scripts/build_runtime_isolation.py` before packaging (requires the .NET SDK).
DLL filenames are stable: `ReShade64.dll`, `SpecialK64.dll`, and custom slots
`LSC_CustomAddon1.dll`, `LSC_CustomAddon2.dll`, etc. Updating a binary changes its
manifest checksum without changing its slot name.

Host status comes from the loaded LS process and is checked against its PID,
creation time and a fresh sample. The host's capture diagnostics are advisory;
scaling confirmation continues to use native LS log/window evidence. The
headless host is pinned for the process lifetime and settings use stopped reloads.
GPU image quality and performance require live validation on supported hardware;
the synthetic smoke test validates startup, IPC and shutdown only.

Packaged launches of LS temporarily clear PyInstaller's private DLL search
directory so external programs resolve their own runtime dependencies. See
[PyInstaller's external-program guidance](https://pyinstaller.org/en/stable/common-issues-and-pitfalls.html#launching-external-programs-from-the-frozen-application).

The build intentionally uses PyInstaller's one-folder mode. PyInstaller advises
against granting administrator privileges to one-file bundles because they unpack
executable dependencies into a temporary directory before starting.

Every repository push also runs the **Build Windows executable** GitHub Actions
workflow. Its `LosslessCompanion-windows-x64-<commit>` artifact contains the
complete one-folder application and is retained with the workflow run for 30 days.
The workflow can also be started manually from the Actions tab. To publish a
versioned GitHub Release without manually creating a tag, include a semantic
version marker such as `@1.2.3` anywhere in a commit message pushed to `main`.
The workflow creates release tag `v1.2.3` and attaches the elevated
`LosslessCompanion-Setup-x64.exe` installer plus its SHA-256 checksum. The same
version is embedded in the packaged executable for update comparison.
Release versions are immutable through this shortcut, so reusing an existing
`@x.x.x` value fails instead of replacing its asset.

To stage all non-ignored changes, commit and push the next sequential release
from `main`, use the repository-root helper:

```powershell
python create_release.py --dry-run
python create_release.py --message "Describe this release"
```

The helper checks commit-message versions and local/remote release tags, then
increments with decimal rollover (`0.4.9` becomes `0.5.0`; `4.9.9` becomes
`5.0.0`). Running it stages everything, including deletions, commits and pushes
immediately without confirmation. Use `--dry-run` for a read-only preview.
It refuses an out-of-date/diverged `main`
and never force-pushes or creates release tags itself. If pushing fails after
the commit succeeds, it retains that commit and prints a manual push command;
retry that push rather than creating another release. A successful push starts
the existing CI release workflow; it does not guarantee the build will pass.

---

## Third-party attribution

The addon host is based on FriedSnowman's fork of Echo-Storm's manager, originally
LosslessProxy by FrankBarretta. The host is MIT; the replacement Neural Rendering
addon is GPL-3.0-only. Matching NR source and our patched host source ship with
the package. Other runtimes retain their own licences. See
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) and the
[upstream notice](https://github.com/FriedSnowman/ls-addon-manager/blob/build-5/NOTICE.md).

---

## Tests

```powershell
python -m unittest discover -v
node tests/check_javascript.js
```

For tray-app development, use the dependency-free reload launcher:

```powershell
python run_test_companion.py
```

It watches companion Python and dashboard files, gracefully stops the child it
launched, and relaunches it after a short debounce. Add `--include-extension` to
restart on Chrome extension JavaScript/CSS/HTML changes as well. Press Ctrl+C in
the watcher terminal to stop both the watcher and its companion child.

The implementation plan for hotkey ownership, native profile imports, the profile
editor, verified addon-host/ReShade/Special K acquisition and updates, asset
storage, and transactional deployment is documented in
[`docs/managed-profile-and-assets-roadmap.md`](docs/managed-profile-and-assets-roadmap.md).

The WebSocket server accepts native local clients, Chrome-extension origins, and
the companion-hosted dashboard origin. Ordinary web-page origins are rejected.

### Experimental cached runtime

The application uses `managed_runtime.py` for the mandatory installation copy
and private settings described above. The separate recipe cache below remains
experimental.

`companion/services/runtime_cache.py` is a staging prototype, not yet connected
to normal Lossless Scaling launches. It requires an explicit, verified pristine
file manifest rather than copying an already modified Steam installation.
Addon runtimes receive a private `Lossless.dll` manager and
`Lossless_original.dll` engine; vanilla runtimes retain the original
`Lossless.dll`. DLLs and per-profile INI/JSON files are real copies, not links.

Shader/texture directories can use Windows junctions to cache-owned asset
snapshots, with a copy fallback. These links never target Steam. Junctions are
not read-only: edits affect their shared cache target, and subsequent integrity
checks reject modified assets. Unchanged runtime recipes reuse their generation
without resetting edited configs; changed inputs create a separate generation.
Old generations are retained to avoid disrupting running processes. The prototype
rejects unexpected runtime files; handling legitimate new LS output files,
generation eviction, Steam launch behavior and executable-path-specific GPU rules
must be addressed before production integration.

Run the isolated tests (no real LS launch):

```powershell
python -m pytest tests/test_runtime_cache.py -q
```

### Experimental multi-instance probe

The older Windows-only probe supports read-only inventory. Its `Launch` and
`Full` modes are disabled because overriding environment variables alone does
not redirect the Windows known-folder API used by LS. Production launches use
Companion's mandatory managed runtime and verified startup hook.

Start with the non-mutating inventory:

```powershell
.\scripts\Test-LosslessScalingMultiInstance.ps1 `
  -LosslessScalingExe 'D:\SteamLibrary\steamapps\common\Lossless Scaling\LosslessScaling.exe' `
  -Mode Inventory
```

Native isolation and pre-Main component loading are tested by
`tests/test_managed_runtime.py`; build instructions are in `native/README.md`.
