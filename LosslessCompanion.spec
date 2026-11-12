# PyInstaller build definition. Run: pyinstaller LosslessCompanion.spec

from PyInstaller.utils.hooks import collect_submodules, copy_metadata
from pathlib import Path
import json
import os

from companion.ui.icons import create_lightning_icon


hiddenimports = collect_submodules("pystray")
benchmark = Path("build/native/LSBenchmark.exe")
if not benchmark.is_file():
    raise SystemExit("Build build/native/LSBenchmark.exe before packaging LS Companion")
benchmark_binaries = [(str(benchmark), "tools")]
hdr_session = Path("build/native/LSHdrSession.exe")
if not hdr_session.is_file():
    raise SystemExit("Build native/hdr_session before packaging LS Companion")
benchmark_binaries.append((str(hdr_session), "tools"))
hdr_probe_test = Path("build/native/LSC_HDRProbeTest.exe")
if not hdr_probe_test.is_file():
    raise SystemExit("Build the HDR GPU fixture before packaging LS Companion")
benchmark_binaries.append((str(hdr_probe_test), "tools"))
startup_guard = Path("build/native/LSC_StartupGuard.dll")
if not startup_guard.is_file():
    raise SystemExit("Build the native startup guard before packaging LS Companion")
benchmark_binaries.append((str(startup_guard), "tools"))
for name in ("LSC_StartupHook.dll", "LSC_RuntimeIsolation.dll"):
    isolation = Path("build/native/runtime-isolation") / name
    if not isolation.is_file():
        raise SystemExit("Run python scripts/build_runtime_isolation.py before packaging LS Companion")
    benchmark_binaries.append((str(isolation), "tools/runtime-isolation"))
adapter_probe = Path("build/native/LSC-AdapterReport/LSC_AdapterProbe.exe")
if not adapter_probe.is_file():
    raise SystemExit("Build the native adapter probe before packaging LS Companion")
benchmark_binaries.append((str(adapter_probe), "tools"))
reshade_bridge = Path("build/native/LSP-ReShade/LSC_ReShadeBridge.dll")
reshade_bridge_manifest = Path("build/native/LSP-ReShade/addon.json")
if not reshade_bridge.is_file() or not reshade_bridge_manifest.is_file():
    raise SystemExit("Build the native ReShade bridge before packaging LS Companion")
native_binaries = benchmark_binaries + [(str(reshade_bridge), "addons/LSP-ReShade")]
native_data = [
    ("build/native/runtime-isolation/MinHook-LICENSE.txt", "licenses/MinHook"),
    ("THIRD_PARTY_NOTICES.md", "."),
    (str(reshade_bridge_manifest), "addons/LSP-ReShade"),
    ("native/reshade_bridge/LICENSE", "licenses/LSC-ReShadeBridge"),
    ("native/vendor/losslessproxy-sdk/LICENSE", "licenses/LosslessProxy-SDK"),
    ("native/README.md", "licenses/native-components"),
]
# Keep the archive reader's license and its dependency notices in the package.
native_data.extend(copy_metadata("py7zr", recursive=True))
native_data.extend(copy_metadata("pywebview", recursive=True))
addon_manager_bundle = Path("build/native/LSAddonManager-Companion.zip")
if not addon_manager_bundle.is_file():
    raise SystemExit("Run python scripts/build_addon_manager.py before packaging LS Companion")
native_data.append((str(addon_manager_bundle), "addons/ls-addon-manager"))
native_data.append(("native/amd_neuralrender/runtime.lock.json", "native/amd_neuralrender"))
amd_probe = Path("build/native/LSC-AMDNR/LSC_AMDNRProbe.exe")
if not amd_probe.is_file():
    raise SystemExit("Build the AMD NR addon and probe before packaging LS Companion")
native_binaries.append((str(amd_probe), "tools"))
amd_probe_fixture = Path("build/native/LSC-AMDNR/probe-fixture/dlss5nr_runtime.dll")
if not amd_probe_fixture.is_file():
    raise SystemExit("Build the AMD GPU bridge test fixture before packaging LS Companion")
native_binaries.append((str(amd_probe_fixture), "tools/probe-fixture"))
version_metadata = Path("build/package/version.json")
version_metadata.parent.mkdir(parents=True, exist_ok=True)
version_metadata.write_text(
    json.dumps({"version": os.environ.get("LS_COMPANION_VERSION", "0.0.0-dev")}),
    encoding="utf-8",
)
app_icon = Path("build/package/LosslessCompanion.ico")
create_lightning_icon(256, 256, state="open", pulse=1.0).save(
    app_icon,
    format="ICO",
    sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
)

a = Analysis(
    ["run_companion.py"],
    pathex=[],
    binaries=native_binaries,
    datas=[("companion/ui/dashboard.html", "companion/ui"), (str(version_metadata), "."), *native_data],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["PyQt5", "PyQt6", "PySide2", "PySide6", "webview.platforms.cef"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LosslessCompanion",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # Packed executables attract heuristic detections and obscure reproducibility.
    upx=False,
    console=False,
    icon=str(app_icon),
    manifest="installer/LosslessCompanion.exe.manifest",
    uac_admin=True,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="LosslessCompanion",
)
