"""Experimental cached-runtime builder. Not connected to production LS launches.

Callers supply a verified, pristine baseline manifest (not a blind copy of an
already modified Steam folder). This module never launches or patches processes.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import stat
import struct
import threading
import uuid

from .asset_store import AssetStore, UnsafeAssetError


class RuntimeCacheConflict(RuntimeError):
    pass


def _reparse(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def create_directory_junction(link: Path, target: Path):
    """Create a local Windows junction using kernel32, without a command shell."""
    if os.name != "nt":
        raise OSError("Windows directory junctions are unavailable")
    target = target.resolve(strict=True)
    if not target.is_dir() or str(target).startswith("\\\\"):
        raise OSError("Junction target must be a local directory")
    substitute = ("\\??\\" + str(target)).encode("utf-16-le")
    printable = str(target).encode("utf-16-le")
    names = substitute + b"\0\0" + printable + b"\0\0"
    buffer = struct.pack("<IHHHHHH", 0xA0000003, 8 + len(names), 0,
                         0, len(substitute), len(substitute) + 2, len(printable)) + names
    if len(buffer) > 16384:
        raise OSError("Junction target is too long")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p,
                                      wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                      ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    kernel.DeviceIoControl.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    link.mkdir()  # Never replace an existing file/directory/link.
    handle = kernel.CreateFileW(str(link), 0x40000000, 7, None, 3, 0x02200000, None)
    try:
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        raw = ctypes.create_string_buffer(buffer)
        returned = wintypes.DWORD()
        if not kernel.DeviceIoControl(handle, 0x900A4, raw, len(buffer), None, 0, ctypes.byref(returned), None):
            raise ctypes.WinError(ctypes.get_last_error())
    except BaseException:
        if handle != ctypes.c_void_p(-1).value:
            kernel.CloseHandle(handle)
            handle = ctypes.c_void_p(-1).value
        link.rmdir()  # Only this newly created, still-empty directory.
        raise
    finally:
        if handle != ctypes.c_void_p(-1).value:
            kernel.CloseHandle(handle)


@dataclass(frozen=True)
class CachedRuntime:
    root: Path
    executable: Path
    variant: str
    reused: bool
    linked_assets: tuple[str, ...]


class RuntimeCache:
    OWNER = "LSCompanion.RuntimeCache"
    MANIFEST = ".companion-runtime.json"
    SHARED_ASSETS = {"reshade-shaders/shaders", "reshade-shaders/textures"}
    RESERVED = {"losslessscaling.exe", "lossless.dll", "lossless_original.dll", MANIFEST}
    ACTIVE_EXTENSIONS = {".dll", ".exe", ".ini", ".json", ".log", ".bat", ".cmd", ".ps1"}

    def __init__(self, root: Path, *, junction_creator=create_directory_junction):
        # No filesystem mutations until source/cache separation is checked.
        self.root = Path(root).absolute()
        self.junction_creator = junction_creator
        self._lock = threading.RLock()

    @staticmethod
    def _relative(value):
        raw = str(value).replace("\\", "/")
        win = PureWindowsPath(raw)
        if (not raw or win.drive or raw.startswith("/")
                or any(part in {"", ".", ".."} or re.search(r'[<>:"|?*\x00-\x1f]', part)
                       or part.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
                       or part.endswith((".", " "))
                       for part in raw.split("/"))):
            raise UnsafeAssetError(f"Unsafe runtime path: {value}")
        return raw

    @staticmethod
    def _plain(path, root):
        current = path
        while True:
            if _reparse(current):
                raise RuntimeCacheConflict(f"Unexpected filesystem link: {current}")
            if current == root:
                break
            if root not in current.parents:
                raise RuntimeCacheConflict("Runtime path escaped its owned directory")
            current = current.parent

    def _check_source(self, path):
        path = Path(path).absolute()
        # Do not follow source symlinks/junctions into an unverified tree.
        for current in (path, *path.parents):
            if _reparse(current):
                raise UnsafeAssetError(f"Runtime sources must be real files/directories: {path}")
        source = path.resolve(strict=True)
        cache = self.root.resolve()
        if source == cache or source in cache.parents or cache in source.parents:
            raise UnsafeAssetError("Runtime cache and source trees must be separate")
        return source

    def _discard_build(self, build):
        parent = self.root / ".building"
        if (build.parent != parent or not re.fullmatch(r"build-[0-9a-f]{32}", build.name)
                or _reparse(build) or build.resolve().parent != parent.resolve()):
            raise RuntimeCacheConflict("Refusing unsafe runtime-build cleanup")
        # Python 3.12's Windows rmtree removes junctions without traversing them.
        shutil.rmtree(build)

    def _publish(self, destination, document, populate):
        self._plain(destination, self.root)
        if destination.exists() or _reparse(destination):
            return
        building = self.root / ".building"
        self._plain(building, self.root)
        building.mkdir(parents=True, exist_ok=True)
        build = building / ("build-" + uuid.uuid4().hex)
        build.mkdir()
        try:
            populate(build)
            (build / self.MANIFEST).write_text(json.dumps(document, sort_keys=True, indent=2), encoding="utf-8")
            self._validate(build, document)
            self._plain(destination.parent, self.root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                build.rename(destination)
            except FileExistsError:
                pass  # Another builder published this immutable generation.
        finally:
            if build.exists():
                self._discard_build(build)

    def _validate(self, root, expected):
        self._plain(root, self.root)
        try:
            manifest = root / self.MANIFEST
            self._plain(manifest, root)
            actual = json.loads(manifest.read_text(encoding="utf-8"))
            if actual != expected:
                raise RuntimeCacheConflict("Runtime manifest does not match its recipe")
            allowed = {name.casefold() for name in expected["files"]} | {self.MANIFEST}
            assets = {name.casefold() for name in expected.get("assets", {})}
            for directory, directories, filenames in os.walk(root, followlinks=False):
                for name in list(directories):
                    child = Path(directory) / name
                    relative = child.relative_to(root).as_posix().casefold()
                    if relative in assets:
                        directories.remove(name)
                    else:
                        self._plain(child, root)
                for name in filenames:
                    if (Path(directory) / name).relative_to(root).as_posix().casefold() not in allowed:
                        raise RuntimeCacheConflict(f"Unexpected cached runtime file: {name}")
            for relative, entry in expected["files"].items():
                path = root / relative
                self._plain(path, root)
                if not path.is_file() or path.stat().st_nlink != 1:
                    raise RuntimeCacheConflict(f"Runtime file missing or hard-linked: {relative}")
                if not entry.get("mutable") and AssetStore.sha256(path) != entry["sha256"]:
                    raise RuntimeCacheConflict(f"Runtime file was modified: {relative}")
        except (OSError, ValueError) as error:
            raise RuntimeCacheConflict(f"Invalid cached runtime: {root}") from error

    def prepare(self, profile_id, baseline_files, *, echo_host=None, addon_files=None,
                reshade_files=None, config_files=None, shared_assets=None):
        """Build/reuse a private runtime from explicit verified file inputs.

        A baseline manifest must include LosslessScaling.exe and the genuine
        original engine as Lossless.dll. It may include other required Steam
        files. Large asset directories are copied into cache-owned snapshots
        before linking; no runtime link ever points back into Steam.
        """
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", profile_id):
            raise UnsafeAssetError("Invalid cached-runtime profile ID")
        files, configs, assets = {}, {}, {}

        def add(relative, source, *, reserved=False):
            relative = self._relative(relative)
            folded = relative.casefold()
            if folded in files or (folded in self.RESERVED and not reserved):
                raise UnsafeAssetError(f"Reserved or duplicate runtime file: {relative}")
            source = self._check_source(source)
            if not source.is_file():
                raise UnsafeAssetError(f"Runtime source is not a file: {source}")
            files[folded] = (relative, source)

        for relative, source in baseline_files.items():
            if str(relative).casefold() == "lossless_original.dll":
                raise UnsafeAssetError("Baseline must identify the original engine as Lossless.dll")
            add(relative, source, reserved=str(relative).casefold() in {"losslessscaling.exe", "lossless.dll"})
        if not {"losslessscaling.exe", "lossless.dll"} <= files.keys():
            raise UnsafeAssetError("A pristine executable and original Lossless.dll are required")
        if files["losslessscaling.exe"][1].parent in self.root.resolve().parents:
            raise UnsafeAssetError("Never build the runtime cache inside the Steam installation")
        if echo_host:
            original = files.pop("lossless.dll")[1]
            add("Lossless_original.dll", original, reserved=True)
            add("Lossless.dll", echo_host, reserved=True)
        elif addon_files:
            raise UnsafeAssetError("Add-on files require an Echo host")
        for mapping in (addon_files or {}, reshade_files or {}):
            for relative, source in mapping.items():
                add(relative, source)
        for relative, value in (config_files or {}).items():
            relative = self._relative(relative)
            if (relative.casefold() in files or relative.casefold() in self.RESERVED
                    or Path(relative).suffix.lower() not in {".json", ".ini"}):
                raise UnsafeAssetError("Runtime configuration must be a private INI/JSON file")
            if relative.casefold() in {key.casefold() for key in configs}:
                raise UnsafeAssetError("Duplicate runtime configuration")
            configs[relative] = value.encode("utf-8") if isinstance(value, str) else bytes(value)
        for relative, source in (shared_assets or {}).items():
            relative = self._relative(relative)
            if relative.casefold() not in self.SHARED_ASSETS:
                raise UnsafeAssetError("Only shader/texture asset directories may be linked")
            if relative.casefold() in {key.casefold() for key in assets}:
                raise UnsafeAssetError("Duplicate shared asset directory")
            source = self._check_source(source)
            if not source.is_dir():
                raise UnsafeAssetError("Shared asset source must be a directory")
            contents = {}
            for item in source.rglob("*"):
                self._plain(item, source)
                if item.is_file():
                    name = self._relative(item.relative_to(source).as_posix())
                    if item.suffix.casefold() in self.ACTIVE_EXTENSIONS:
                        raise UnsafeAssetError("DLLs, configs, logs and scripts cannot be shared assets")
                    contents[name] = dict(sha256=AssetStore.sha256(item), mutable=False)
            assets[relative] = (source, contents)
        entries = {relative: dict(sha256=AssetStore.sha256(source), mutable=False)
                   for relative, source in files.values()}
        entries.update({name: dict(sha256=hashlib.sha256(data).hexdigest(), mutable=True) for name, data in configs.items()})
        destinations = list(entries) + list(assets)
        for index, left in enumerate(destinations):
            for right in destinations[index + 1:]:
                a, b = left.casefold(), right.casefold()
                if a == b or a.startswith(b + "/") or b.startswith(a + "/"):
                    raise UnsafeAssetError("Runtime files and linked directories overlap")
        variant = ("echo" if echo_host else "vanilla") + ("-reshade" if reshade_files else "")
        document = dict(owner=self.OWNER, schema=1, profile=profile_id, variant=variant,
                        files=entries, assets={name: contents for name, (_, contents) in assets.items()})
        digest = hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()
        destination = self.root / "runtimes" / profile_id / digest
        with self._lock:
            for current in (self.root, *self.root.parents):
                if _reparse(current):
                    raise RuntimeCacheConflict("Runtime cache cannot reside beneath a filesystem link")
            self.root.mkdir(parents=True, exist_ok=True)
            snapshots = {}
            for name, (source, contents) in assets.items():
                asset_document = dict(owner=self.OWNER, schema=1, files=contents)
                key = hashlib.sha256(json.dumps(asset_document, sort_keys=True).encode()).hexdigest()
                snapshot = self.root / "assets" / key

                def populate_asset(build, source=source, contents=contents):
                    for relative in contents:
                        path = build / relative
                        path.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source / relative, path)

                self._publish(snapshot, asset_document, populate_asset)
                self._validate(snapshot, asset_document)
                snapshots[name] = snapshot
            reused = destination.exists()

            def populate(build):
                for relative, source in files.values():
                    target = build / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                for relative, data in configs.items():
                    target = build / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                for relative, snapshot in snapshots.items():
                    link = build / relative
                    link.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        self.junction_creator(link, snapshot)
                    except OSError:
                        if link.exists() or _reparse(link):
                            raise RuntimeCacheConflict("Incomplete directory-link creation")
                        shutil.copytree(snapshot, link)

            self._publish(destination, document, populate)
            self._validate(destination, document)
            linked = []
            for relative, snapshot in snapshots.items():
                path = destination / relative
                self._plain(path.parent, destination)
                if _reparse(path):
                    if path.resolve(strict=True) != snapshot.resolve(strict=True):
                        raise RuntimeCacheConflict("Shared assets point at an unexpected target")
                    linked.append(relative)
                else:
                    self._validate(path, dict(owner=self.OWNER, schema=1, files=assets[relative][1]))
            return CachedRuntime(destination, destination / files["losslessscaling.exe"][0], variant, reused, tuple(linked))
