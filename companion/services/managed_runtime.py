"""Private, copy-once LS installations. The discovered installation is read-only."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import sys
import time
import uuid
import xml.etree.ElementTree as ET

from .asset_store import AssetStore

MARKER = ".companion-runtime.json"
BOOTSTRAP_FILES = ("LSC_StartupHook.dll", "LSC_RuntimeIsolation.dll")
PROXY_NAMES = {"dxgi.dll", "d3d11.dll", "d3d12.dll", "d3d9.dll", "dinput8.dll", "version.dll", "winmm.dll", "opengl32.dll"}


def native_settings_path() -> Path:
    # This is a source to import once, never a writable target.
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "Lossless Scaling/Settings.xml"


def assert_private_settings(path: Path) -> None:
    candidate = Path(path).resolve()
    if candidate == native_settings_path().resolve() or candidate.parent == native_settings_path().resolve().parent:
        raise RuntimeError("Companion cannot write the live Lossless Scaling settings; initialize the managed runtime")


def bootstrap_directory() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "tools/runtime-isolation"
    return Path(__file__).resolve().parents[2] / "build/native/runtime-isolation"


class ManagedRuntime:
    def __init__(self, config_dir: Path):
        self.root = Path(config_dir).resolve() / "runtime/lossless-scaling"
        self.local = self.root / "data/LocalAppData"
        self.roaming = self.root / "data/RoamingAppData"
        self.settings = self.local / "Lossless Scaling/Settings.xml"

    def prepare(self, source_exe: Path, store: AssetStore, *, seed_settings: Path | None = None,
                bootstrap: Path | None = None) -> Path:
        source_exe = Path(source_exe).resolve(strict=True)
        source = source_exe.parent
        if source_exe.name.casefold() != "losslessscaling.exe" or source.is_relative_to(self.root):
            raise RuntimeError("A real source LS installation outside the managed runtime is required")
        bootstrap = bootstrap or bootstrap_directory()
        for name in BOOTSTRAP_FILES:
            if not (bootstrap / name).is_file():
                raise RuntimeError(f"Runtime isolation component {name} is missing; rebuild or reinstall Companion")
        runtime_config = source / "LosslessScaling.runtimeconfig.json"
        if not runtime_config.is_file():
            raise RuntimeError("This LS build has no supported .NET startup-hook runtime configuration")
        options = json.loads(runtime_config.read_text(encoding="utf-8-sig")).get("runtimeOptions", {})
        if options.get("configProperties", {}).get("System.StartupHookProvider.IsSupported") is False:
            raise RuntimeError("This LS build disables the startup hook needed for private settings")

        # Recover stock bytes into the clone from existing ownership records,
        # including users whose source already contains our engine wrapper.
        from .deployment_manager import DeploymentManager
        previous = DeploymentManager(store)
        manifest = previous._read_manifest(str(source_exe))
        originals = {item["relative_path"].replace("\\", "/"): item.get("original_backup")
                     for item in manifest.get("files", [])}
        recorded_engine = originals.get("Lossless.dll")
        if recorded_engine and not Path(recorded_engine).is_file():
            raise RuntimeError("The recorded original LS engine backup is missing; verify the source installation before cloning")
        engine = Path(originals.get("Lossless.dll") or source / "Lossless_original.dll")
        if not engine.is_file():
            engine = source / "Lossless.dll"
        if not engine.is_file() or not (source / "LosslessScaling.dll").is_file():
            raise RuntimeError("The source LS installation is incomplete")
        identities = [source_exe, source / "LosslessScaling.dll", engine, runtime_config,
                      *(bootstrap / name for name in BOOTSTRAP_FILES)]
        fingerprint = hashlib.sha256("".join(AssetStore.sha256(path) for path in identities).encode()).hexdigest()[:24]
        directory = self.root / "installations" / fingerprint
        app = directory / "app"
        executable = app / "LosslessScaling.exe"
        if not executable.is_file():
            staging = self.root / "installations" / (".preparing-" + uuid.uuid4().hex)
            staging.mkdir(parents=True)
            clone = staging / "app"
            clone.mkdir()
            for item in source.rglob("*"):
                relative = item.relative_to(source)
                parts = [part.casefold() for part in relative.parts]
                if item.is_symlink() or getattr(item.lstat(), "st_file_attributes", 0) & 0x400:
                    raise RuntimeError(f"LS source contains a linked path: {relative}")
                if (parts[0] in {"addons", "logs", "reshade-shaders", "companion-runtimes", ".companion-special-k", ".companion"}
                        or relative.as_posix() in originals or parts[-1] == "lossless_original.dll"
                        or (len(parts) == 1 and (parts[0] in PROXY_NAMES or item.suffix.casefold() in {".log", ".ini", ".bak"}))):
                    continue
                target = clone / relative
                if item.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                elif item.is_file():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, target)
            shutil.copy2(engine, clone / "Lossless.dll")
            private = clone / ".companion/bootstrap"
            private.mkdir(parents=True)
            for name in BOOTSTRAP_FILES:
                shutil.copy2(bootstrap / name, private / name)
            # Force hook support only in our private copy of the runtime config.
            options.setdefault("configProperties", {})["System.StartupHookProvider.IsSupported"] = True
            options["configProperties"]["STARTUP_HOOKS"] = str(app / ".companion/bootstrap/LSC_StartupHook.dll")
            (clone / runtime_config.name).write_text(json.dumps({"runtimeOptions": options}, indent=2), encoding="utf-8")
            AssetStore._atomic_json(clone / MARKER, {
                "schema": 1, "root": str(self.root), "source": str(source_exe),
                "localAppData": str(self.local), "roamingAppData": str(self.roaming),
                "settings": str(self.settings), "fingerprint": fingerprint,
                "bootstrapHashes": {name: AssetStore.sha256(private / name) for name in BOOTSTRAP_FILES},
            })
            # Windows scanners can briefly hold newly copied executables open.
            for attempt in range(10):
                try:
                    staging.rename(directory)
                    break
                except PermissionError:
                    if attempt == 9:
                        raise
                    time.sleep(0.05 * (attempt + 1))
        self.local.mkdir(parents=True, exist_ok=True)
        self.roaming.mkdir(parents=True, exist_ok=True)
        if not self.settings.exists():
            seed = Path(seed_settings) if seed_settings else native_settings_path()
            self.settings.parent.mkdir(parents=True, exist_ok=True)
            if seed.is_file():
                tree = ET.parse(seed)
            else:
                tree = ET.ElementTree(ET.Element("Settings"))
            # A clone never elevates/relaunches itself outside our launch contract.
            for name, value in (("StartAsAdmin", "false"), ("AutoScale", "false")):
                for element in tree.getroot().iter(name):
                    element.text = value
            tree.write(self.settings, encoding="utf-8", xml_declaration=True)
        self.validate(executable)
        return executable

    def validate(self, executable: Path) -> dict:
        executable = Path(executable).resolve()
        if not executable.is_relative_to(self.root / "installations"):
            raise RuntimeError("LS operations are restricted to the Companion-managed clone")
        record = json.loads((executable.parent / MARKER).read_text(encoding="utf-8"))
        if (record.get("schema") != 1 or Path(record.get("root", "")).resolve() != self.root
                or Path(record.get("settings", "")).resolve() != self.settings.resolve()
                or Path(record.get("localAppData", "")).resolve() != self.local.resolve()
                or Path(record.get("roamingAppData", "")).resolve() != self.roaming.resolve()):
            raise RuntimeError("Managed LS runtime identity or private settings path is invalid")
        assert_private_settings(self.settings)
        properties = json.loads((executable.parent / "LosslessScaling.runtimeconfig.json").read_text(encoding="utf-8-sig")).get("runtimeOptions", {}).get("configProperties", {})
        hook = executable.parent / ".companion/bootstrap/LSC_StartupHook.dll"
        if properties.get("System.StartupHookProvider.IsSupported") is not True or properties.get("STARTUP_HOOKS") != str(hook):
            raise RuntimeError("The managed LS runtime configuration no longer requires private settings isolation")
        for name in BOOTSTRAP_FILES:
            path = executable.parent / ".companion/bootstrap" / name
            if not path.is_file() or AssetStore.sha256(path) != record.get("bootstrapHashes", {}).get(name):
                raise RuntimeError(f"Managed runtime isolation component was modified or is missing: {name}")
        return record

    def bind(self, profile_manager, executable: Path, store: AssetStore) -> None:
        self.validate(executable)
        profile_manager.managed_runtime = self
        profile_manager.config.lossless_scaling_exe_path = str(executable)
        profile_manager.config.lossless_settings_xml_path = str(self.settings)
        store.managed_runtime = self


def launch_environment(executable: Path) -> tuple[dict, Path | None, str | None]:
    """Child-only bootstrap. No mutation of the Companion process environment."""
    environment = os.environ.copy()
    # The clone's runtimeconfig owns its mandatory hook. Do not also supply the
    # same hook through the environment (.NET invokes both registrations).
    environment.pop("DOTNET_STARTUP_HOOKS", None)
    marker = Path(executable).resolve().parent / MARKER
    if not marker.is_file():
        if (Path(executable).parent / "LosslessScaling.runtimeconfig.json").is_file():
            raise RuntimeError("Launching the source LS installation without private settings isolation is prohibited")
        return environment, None, None  # Synthetic/native fixtures have no LS settings reader.
    record = json.loads(marker.read_text())
    runtime = ManagedRuntime(Path(record["root"]).parent.parent)
    runtime.validate(executable)
    token = secrets.token_hex(32)
    receipt = runtime.root / "receipts" / (token + ".json")
    receipt.parent.mkdir(parents=True, exist_ok=True)
    environment.update({
        "LOCALAPPDATA": str(runtime.local), "APPDATA": str(runtime.roaming),
        "LSC_PRIVATE_LOCALAPPDATA": str(runtime.local), "LSC_PRIVATE_APPDATA": str(runtime.roaming),
        "LSC_ISOLATION_RECEIPT": str(receipt), "LSC_ISOLATION_TOKEN": token,
        "LSC_ADAPTER_REPORT_DIR": str(runtime.root.parent.parent / "diagnostics"),
    })
    return environment, receipt, token


def assert_managed_executable(executable: Path) -> None:
    """Guard mutation routes that can be used outside the application bootstrap."""
    executable = Path(executable).resolve()
    marker = executable.parent / MARKER
    if marker.is_file():
        record = json.loads(marker.read_text())
        ManagedRuntime(Path(record["root"]).parent.parent).validate(executable)
    elif (executable.parent / "LosslessScaling.runtimeconfig.json").is_file():
        raise RuntimeError("LS operations require the Companion-managed clone")


def await_isolation(pid: int, receipt: Path | None, token: str | None, *, timeout=12.0) -> None:
    if receipt is None:
        return
    import psutil
    try:
        process = psutil.Process(pid)
        process.create_time()  # Cache the launched incarnation before waiting.
    except psutil.NoSuchProcess:
        process = None
    expected_settings = receipt.parent.parent / "data/LocalAppData/Lossless Scaling/Settings.xml"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            payload = json.loads(receipt.read_text())
            if (payload.get("pid") == pid and payload.get("token") == token and payload.get("verified") is True
                    and Path(payload.get("settings", "")).resolve() == expected_settings.resolve()):
                return
        except (OSError, ValueError):
            pass
        if process is None or not process.is_running():
            break
        time.sleep(0.05)
    # Kill only the child this launch created. Never fall back to the source exe.
    try:
        if process is not None and process.is_running():
            process.kill()
    except psutil.NoSuchProcess:
        pass
    raise RuntimeError("LS did not verify private settings isolation; its managed child was stopped")
