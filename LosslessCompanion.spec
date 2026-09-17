# PyInstaller build definition. Run: pyinstaller LosslessCompanion.spec

from PyInstaller.utils.hooks import collect_submodules
from pathlib import Path
import json
import os

from companion.ui.icons import create_lightning_icon


hiddenimports = collect_submodules("pystray")
benchmark = Path("build/native/LSBenchmark.exe")
if not benchmark.is_file():
    raise SystemExit("Build build/native/LSBenchmark.exe before packaging LS Companion")
benchmark_binaries = [(str(benchmark), "tools")]
reshade_bridge = Path("build/native/LSP-ReShade/LSC_ReShadeBridge.dll")
reshade_bridge_manifest = Path("build/native/LSP-ReShade/addon.json")
if not reshade_bridge.is_file() or not reshade_bridge_manifest.is_file():
    raise SystemExit("Build the native ReShade bridge before packaging LS Companion")
native_binaries = benchmark_binaries + [(str(reshade_bridge), "addons/LSP-ReShade")]
native_data = [
    (str(reshade_bridge_manifest), "addons/LSP-ReShade"),
    ("native/reshade_bridge/LICENSE", "licenses/LSC-ReShadeBridge"),
    ("native/vendor/losslessproxy-sdk/LICENSE", "licenses/LosslessProxy-SDK"),
    ("native/README.md", "licenses/native-components"),
]
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
    excludes=[],
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
