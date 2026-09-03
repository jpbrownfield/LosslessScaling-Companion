# Lossless Scaling Automation Bridge & Companion

A high-performance bridge connecting Google Chrome with **Lossless Scaling (LS)** and a modular local Python companion daemon. It automatically triggers scaling when web videos enter fullscreen, de-scales on exit, manages process profiles, switches ReShade presets, and deploys custom proxy DLLs.

---

## ⚡ Key Features

1. **Automatic Fullscreen Detection:**
   - Detects video fullscreen transitions across YouTube, Twitch, Netflix, custom players, etc.
   - Triggers Lossless Scaling after a stabilization delay (configurable in milliseconds).
   - Toggles scaling off automatically upon demaximizing/exiting fullscreen.

2. **Hardware-Level Input Injection (`ctypes` + Win32 `SendInput`):**
   - Synthesizes hardware scan codes directly at the kernel input level.
   - Ultra-low latency, non-blocking, and avoids focus traps or virtual key drops.

3. **Multi-Application Profile Engine:**
   - Create and customize profiles for specific executables (e.g. `Cyberpunk2077.exe`, `chrome.exe`) or web domains (e.g. `youtube.com`).
   - Automatically matches the active window when switching tasks.

4. **ReShade Preset Management:**
   - Programmatically swaps `CurrentPresetPath` in `ReShade.ini` per game or video player.
   - Triggers ReShade reload hotkeys on profile activation.

5. **DLL Overrides & Proxy Swapping:**
   - Automatically deploys or symlinks proxy DLLs (`dxgi.dll`, `d3d11.dll`, `dinput8.dll`, OptiScaler) into game folders.
   - Auto-creates `.orig.bak` backups and restores original files on exit.

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
│   │   └── dll_manager.py           # DLL backup, deployer & symlinker
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

The configuration is saved in `companion/config/settings.json`. You can open the settings folder directly via the system tray menu (`Open Settings Folder`).

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
    "reshade_ini_path": "D:\\Games\\Cyberpunk 2077\\bin\\x64\\ReShade.ini",
    "preset_path": "D:\\ReShade\\Presets\\Cinematic4K.ini",
    "reload_hotkey": "Home"
  },
  "dll_overrides": [
    {
      "enabled": true,
      "source_dll_path": "C:\\Mods\\OptiScaler\\dxgi.dll",
      "target_dll_name": "dxgi.dll",
      "deployment_mode": "copy"
    }
  ]
}
```

---

## 📦 Building a Standalone Executable (.exe)

To compile the companion into a standalone `.exe` without needing a Python installation:

```powershell
pip install pyinstaller
pyinstaller --noconsole --onefile --name "LosslessCompanion" run_companion.py
```
The compiled executable will be located in the `dist/` directory.
