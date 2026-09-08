# PyInstaller build definition. Run: pyinstaller LosslessCompanion.spec

from PyInstaller.utils.hooks import collect_submodules
from pathlib import Path


hiddenimports = collect_submodules("pystray")
benchmark = Path("build/native/LSBenchmark.exe")
if not benchmark.is_file():
    raise SystemExit("Build build/native/LSBenchmark.exe before packaging LS Companion")
benchmark_binaries = [(str(benchmark), "tools")]

a = Analysis(
    ["run_companion.py"],
    pathex=[],
    binaries=benchmark_binaries,
    datas=[("companion/ui/dashboard.html", "companion/ui")],
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
