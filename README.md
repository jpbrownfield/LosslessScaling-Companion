# Lossless Scaling Automation Bridge & Companion

A Windows companion for **Lossless Scaling (LS)** that activates executable profiles from the foreground window, verifies the exact target window before sending the synchronized LS hotkey, manages Lossless Scaling ReShade presets and add-ons, and never modifies game directories. An optional browser extension adds automatic browser-video scaling.

---

## ⚡ Key Features

1. **Foreground Application Auto-Scaling:**
   - Matches configured applications by executable path or process name.
   - Carries the selected PID and HWND through profile activation, refocuses that exact window after any Lossless Scaling restart, and refuses the hotkey if identity verification fails.
   - Confirms the resulting scaling state from Lossless Scaling's overlay instead of treating successful keyboard input as proof of activation.
   - Keeps an application scaled when focus changes. A configured game can explicitly take precedence over an actively scaled browser profile.

2. **Native Win32 Input Injection (`ctypes` + `SendInput`):**
   - Synthesizes scan-code keyboard input through the Windows input API.
   - Ultra-low latency, non-blocking, and avoids focus traps or virtual key drops.

3. **Multi-Application Profile Engine:**
   - Create and customize profiles for specific executables (e.g. `Cyberpunk2077.exe`, `chrome.exe`) or web domains (e.g. `youtube.com`).
   - Automatically matches the active window when switching tasks.
   - Offers one per-profile **Automatically scale this application** toggle; there is no blur-triggered de-scaling.
   - A checked-by-default General Settings option enables autoscaling only for profiles newly created through the companion. Imported Lossless Scaling profiles start with helper autoscaling off, and changing the default never rewrites existing profiles.
   - Installation and initialization never rewrite Lossless Scaling's native profiles. Native hotkey and Auto Scale ownership begins only after the user explicitly saves General Settings.

4. **Lossless Scaling ReShade Preset Management:**
   - Programmatically swaps `CurrentPresetPath` in the `ReShade.ini` beside Lossless Scaling.
   - Triggers ReShade reload hotkeys on profile activation.

5. **Lossless Scaling Add-ons & Proxy Swapping:**
   - Copies configured DLLs only into the Lossless Scaling directory.
   - Keeps transaction-owned backups and restores original files when assets are unmanaged.
   - Game executable paths are used only for foreground profile detection.

6. **Windows System Tray UI:**
   - Shows live connection status and active profile.
   - Quick one-click "Create Profile From Running App" menu.
   - Quick toggle for manual scaling and auto-scaling mode.

7. **Process Lasso Performance Mode Trigger:**
   - Optionally tails new records in Process Lasso's CSV action log.
   - Activates scaling only when the triggering executable has an explicit helper profile and a focusable visible window.
   - With the override blank, discovers `/LogFolder=` on running Process Lasso components, the `LogFolder` registry setting, and then common `prolasso.log` locations.
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

### 2. Start the Python Companion

```powershell
python run_companion.py
```
* You will see the **Lossless Companion** icon appear in your Windows System Tray (near the clock).
* The companion automatically hosts a local WebSocket server at `ws://127.0.0.1:24892/ws`.

### 3. Load the Chrome Extension

Download the [current source archive](https://github.com/jpbrownfield/Lossless-Scaling-Helper/archive/refs/heads/main.zip), extract it, and use its `extension/` directory, or use that directory directly from a repository checkout.

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
can open the settings folder directly from the system tray.

Lossless Scaling's own profiles can be inspected and edited from the dashboard at
`http://127.0.0.1:24892/dashboard`. Its default configuration path is
`%LOCALAPPDATA%\Lossless Scaling\Settings.xml`. Close Lossless Scaling before
writing this file manually. Dashboard edits stop and restart Lossless Scaling as
needed and create a first-original backup.

On initialization, the companion creates an untouched, one-time
`Settings.xml.bak` beside `Settings.xml`. If the source XML is invalid or the
backup cannot be created, native settings writes fail closed. If Lossless Scaling
has not created `Settings.xml` yet, the companion retries the backup when the file
appears. No native hotkey or profile setting is changed until the user explicitly
saves General Settings.

The dashboard's General Settings section controls the global activation hotkey,
native Auto Scale ownership, and an elevated `ONLOGON` Task Scheduler entry named
`LosslessScalingHelper`. The companion has no native main window: it is always a
tray process, and closing or minimizing the browser dashboard does not stop it.
The red **Revert all changes & remove integrations** action stops Lossless Scaling,
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

There is currently no standalone official NVIDIA download for
`nvngx_dlssnr.dll`. The dashboard links to NVIDIA's official DLSS resources and
the LSP-NeuralRender guide, but the runtime itself must be imported from software
the user is licensed to use. The helper does not link to or download leaked or
community-redistributed NVIDIA binaries.

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

The build intentionally uses PyInstaller's one-folder mode. PyInstaller advises
against granting administrator privileges to one-file bundles because they unpack
executable dependencies into a temporary directory before starting.

Every repository push also runs the **Build Windows executable** GitHub Actions
workflow. Its `LosslessCompanion-windows-x64-<commit>` artifact contains the
complete one-folder application and is retained with the workflow run for 30 days.
The workflow can also be started manually from the Actions tab.

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
editor, verified LosslessProxy/LSP-NeuralRender/DLSS5-Feeder/ReShade/Special K
acquisition and updates, asset
storage, and transactional deployment is documented in
[`docs/managed-profile-and-assets-roadmap.md`](docs/managed-profile-and-assets-roadmap.md).

The WebSocket server accepts native local clients, Chrome-extension origins, and
the companion-hosted dashboard origin. Ordinary web-page origins are rejected.

### Experimental multi-instance probe

The Windows-only probe clones an installed Lossless Scaling directory into two
temporary layouts, gives each process a separate `LOCALAPPDATA` environment and
hotkey, launches both copies, and records process/window/settings evidence. It
does not modify the source installation. By default it stops only the processes
it launched and removes its temporary copies. The cloned settings disable native
Auto Scale and `StartAsAdmin` so those features do not race or relaunch the
controlled hotkey test; run the terminal elevated yourself if all tested targets
also run elevated.

Start with the non-mutating inventory:

```powershell
.\scripts\Test-LosslessScalingMultiInstance.ps1 `
  -LosslessScalingExe 'D:\SteamLibrary\steamapps\common\Lossless Scaling\LosslessScaling.exe' `
  -Mode Inventory
```

Test whether two cloned instances remain alive, retaining the report and clones:

```powershell
.\scripts\Test-LosslessScalingMultiInstance.ps1 `
  -LosslessScalingExe 'D:\SteamLibrary\steamapps\common\Lossless Scaling\LosslessScaling.exe' `
  -Mode Launch -KeepArtifacts
```

To test targeting, obtain the two application PIDs from Task Manager or
`Get-Process`, then use `Full` mode. The helper focuses and verifies each target
window before sending its assigned hotkey; if Windows refuses the focus change,
no input is sent.

```powershell
.\scripts\Test-LosslessScalingMultiInstance.ps1 `
  -LosslessScalingExe 'D:\SteamLibrary\steamapps\common\Lossless Scaling\LosslessScaling.exe' `
  -Mode Full -TargetAProcessId 1234 -TargetBProcessId 5678 `
  -HotkeyA 'ctrl+alt+f23' -HotkeyB 'ctrl+alt+f24' `
  -KeepArtifacts
```

Optional `-InstanceAOverlayDirectory` and `-InstanceBOverlayDirectory` values
overlay instance-specific DLL/ReShade files onto each cloned application folder.
The probe cannot guarantee that Lossless Scaling honors the overridden AppData
environment; compare the isolated settings evidence and the real settings hash
in the generated `test-results/ls-multi-instance-*.json` report before relying
on that isolation.
