# Lossless Scaling Automation Bridge & Companion

A high-performance bridge connecting Google Chrome with **Lossless Scaling (LS)** and a modular local Python companion daemon. It automatically triggers scaling when web videos enter fullscreen, de-scales on exit, manages process profiles, switches the Lossless Scaling ReShade preset, and deploys LS add-ons without modifying games.

---

## ⚡ Key Features

1. **Automatic Fullscreen Detection:**
   - Detects video fullscreen transitions across YouTube, Twitch, Netflix, custom players, etc.
   - Triggers Lossless Scaling after a stabilization delay (configurable in milliseconds).
   - Toggles scaling off automatically upon demaximizing/exiting fullscreen.

2. **Native Win32 Input Injection (`ctypes` + `SendInput`):**
   - Synthesizes scan-code keyboard input through the Windows input API.
   - Ultra-low latency, non-blocking, and avoids focus traps or virtual key drops.

3. **Multi-Application Profile Engine:**
   - Create and customize profiles for specific executables (e.g. `Cyberpunk2077.exe`, `chrome.exe`) or web domains (e.g. `youtube.com`).
   - Automatically matches the active window when switching tasks.
   - Optionally scales on focus and de-scales on blur.
   - Disables Lossless Scaling's native Auto Scale by default so profile activation, hotkeys, and DLL directory changes have one owner.

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
writing this file; the companion refuses live edits and creates a backup.

### Example Profile Configuration:

```json
{
  "id": "cyberpunk-cinematic",
  "name": "Cyberpunk 2077 UHD",
  "target_process": "Cyberpunk2077.exe",
  "auto_scale_on_fullscreen": false,
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

---

## Tests

```powershell
python -m unittest discover -v
node tests/check_javascript.js
```

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
