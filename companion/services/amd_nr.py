"""Optional pinned AMD NR runtime; no game proxy, FG or NVIDIA model deployment."""
from __future__ import annotations
import json
import os
from pathlib import Path
import shutil
import sys
import threading

from .asset_store import AssetStore

_base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
LOCK = json.loads((_base / "native/amd_neuralrender/runtime.lock.json").read_text(encoding="utf-8"))
PROVIDER = "amd-neural-render-runtime"
VERSION = LOCK["tag"]
ARCHIVE = LOCK["archive"]
ARCHIVE_SHA256 = LOCK["archive_sha256"]
URL = f"https://github.com/{LOCK['repository']}/releases/download/{VERSION}/{ARCHIVE}"
ROOT = f"companion-runtimes/AMDNR/{VERSION}"
FILES = LOCK["files"]
_cache_lock = threading.Lock()


def enabled(config):
    addons = config.get("addons", {})
    nr = addons.get("DLSSNR02", {}) if isinstance(addons, dict) else {}
    return isinstance(nr, dict) and nr.get("_enabled") is True


def cache_path(config_dir):
    return Path(config_dir).resolve() / "runtime-cache" / "AMDNR" / VERSION


def installed(packages):
    return next((p for p in packages if p.get("provider") == PROVIDER
                 and p.get("version") == VERSION and p.get("archive_sha256") == ARCHIVE_SHA256), None)


def verified_files(package):
    payload = Path(package["payload_path"]).resolve(strict=True)
    result = {}
    for name, info in FILES.items():
        try:
            path = (payload / LOCK["prefix"] / name).resolve(strict=True)
            valid = payload in path.parents and path.is_file() and path.stat().st_size == info["size"] and AssetStore.sha256(path) == info["sha256"]
        except OSError:
            valid = False
        if not valid:
            raise ValueError(f"AMD NR runtime file failed verification: {name}; reinstall the runtime")
        result[name] = path
    return result


def prepare_cache(store, config_dir):
    """Seed once into writable user data. Never edit the immutable package DB."""
    package = installed(store.list_packages())
    if package is None:
        return
    destination = cache_path(config_dir)
    with _cache_lock:
        marker = destination / ".companion-seed"
        if (marker.is_file() and marker.read_text(encoding="ascii") == ARCHIVE_SHA256
                and all((destination / name.removeprefix("cache/")).is_file()
                        for name in FILES if name.startswith("cache/"))):
            return
        paths = verified_files(package)
        for name, source in paths.items():
            if not name.startswith("cache/"):
                continue
            target = destination / name.removeprefix("cache/")
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_name(target.name + ".seed-partial")
            shutil.copyfile(source, temp)
            os.replace(temp, target)
        destination.mkdir(parents=True, exist_ok=True)
        temporary = marker.with_suffix(".tmp")
        temporary.write_text(ARCHIVE_SHA256, encoding="ascii")
        os.replace(temporary, marker)


def deployment_files(store, profile, *, host_version):
    from .addon_host import addon_version_key
    if not enabled(profile.addon_config):
        return []
    package = installed(store.list_packages())
    # NVIDIA profiles do not need the optional AMD download. The native AMD
    # addon reports a missing runtime when an AMD capture actually arrives.
    if package is None:
        return []
    if addon_version_key(host_version) < addon_version_key("build-5-lsc.3"):
        raise ValueError("AMD NR needs the current bundled Companion host; update it in Add-ons")
    return [{"relative_path": f"{ROOT}/{name}", "source_path": str(path),
             "role": "amd_nr_runtime", "source_package": f"{PROVIDER}/{VERSION}/{ARCHIVE_SHA256}"}
            for name, path in verified_files(package).items()
            if not name.startswith("cache/") and name != "dlss5nr_runtime.dll"]
