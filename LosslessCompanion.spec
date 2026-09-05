# PyInstaller build definition. Run: pyinstaller LosslessCompanion.spec

from PyInstaller.utils.hooks import collect_submodules


hiddenimports = collect_submodules("pystray")

a = Analysis(
    ["run_companion.py"],
    pathex=[],
    binaries=[],
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
    upx=True,
    console=False,
    uac_admin=True,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    name="LosslessCompanion",
)
