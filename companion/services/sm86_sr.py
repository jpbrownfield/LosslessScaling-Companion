"""Pinned, optional sdli1995 Super Resolution backend; never deploy its FG path."""
from __future__ import annotations

from pathlib import Path

from .asset_store import AssetStore

PROVIDER = "sm86-sr"
VERSION = "0.4.1"
ARCHIVE = "dlssg-release-0.4.1-x64.zip"
ARCHIVE_SHA256 = "7230131637617ede9dc45c552244ddb814ae70098406e102fc254d52bcdb6e14"
URL = f"https://github.com/sdli1995/dlssg_for_sm86/releases/download/{VERSION}/{ARCHIVE}"
ROOT = "companion-runtimes/SM86SR"
MODE_KEY = "companionSm86Mode"
MODES = {"L-INT8": "12", "M-INT8": "13"}
# Hashes from the verified 0.4.1 archive, also checked against its manifest.
FILES = {
    "version.dll": "275ffa781b2d244ad269d1d65d4e5b68a12a3f89ff2baaec58d100eb5a1ff411",
    "dlssg_sm86/backend/sm86_backend.dll": "d1b56f6ee9a58ba6284804303cf903535c7acbf4fdce3320f6fd21f50569d357",
    "dlssg_sm86/sr/nvngx_dlss.dll": "858ccff96360c3098db9264cd6f13d04a027e09b6ce7ad5bbc297570e824b4f2",
    "dlssg_sm86/sr/sr_kernels.dll": "b8d5d4cac3142647630b72a4a29271de35679646d48bdd2b70a71876da0fae78",
    "LICENSE.md": "7537220f50c6430d953eda8bff01f0817014e604cb3607f3b62c786d070d8570",
    "THIRD_PARTY_NOTICES.txt": "7b713d4449f50ff6dac8a26ff9af1d88600814dc5c35bfcf61416b9ec089614b",
    "README.en.md": "e98bc3810f3bd26f28278173ed7ed81761da6feafb68f4e8c8307159b0f21147",
}


def selection(config):
    addons = config.get("addons", {})
    section = addons.get("DLSS4DLAA", {}) if isinstance(addons, dict) else {}
    if not isinstance(section, dict) or section.get("_enabled") is not True:
        return "Off"
    mode = section.get(MODE_KEY) or "Off"
    if not isinstance(mode, str) or (mode != "Off" and mode not in MODES):
        raise ValueError("SM86 Super Resolution mode must be Off, L-INT8 or M-INT8")
    return mode


def installed(packages):
    return next((package for package in packages
                 if package.get("provider") == PROVIDER and package.get("version") == VERSION
                 and package.get("archive_sha256") == ARCHIVE_SHA256), None)


def deployment_files(store, profile, *, host_version):
    from .addon_host import addon_version_key

    mode = selection(profile.addon_config)
    if mode == "Off":
        return []
    if addon_version_key(host_version) < addon_version_key("build-5-lsc.2"):
        raise ValueError("SM86 Super Resolution needs the current bundled Companion host; update it in Add-ons")
    package = installed(store.list_packages())
    if package is None:
        raise ValueError("Install the SM86 Super Resolution backend in Add-ons before applying L-INT8 or M-INT8")
    payload = Path(package["payload_path"]).resolve(strict=True)
    result = []
    for relative, expected in FILES.items():
        try:
            source = (payload / relative).resolve(strict=True)
            valid = payload in source.parents and source.is_file() and AssetStore.sha256(source) == expected
        except OSError:
            valid = False
        if not valid:
            raise ValueError(f"SM86 backend file failed verification: {relative}; reinstall the backend")
        result.append({"relative_path": f"{ROOT}/{relative}", "source_path": str(source),
                       "role": "sm86_sr_runtime", "source_package": f"{PROVIDER}/{VERSION}/{ARCHIVE_SHA256}"})
    # This INI belongs to Companion's private copy. The downloaded factory INI
    # enables FG and must never enter our deployment plan.
    ini = store.write_generated_text(profile.id, "sm86-sr.ini",
        "[General]\nEnabled=0\n\n[Compatibility]\nSpoofArchToGame=0\n\n"
        f"[SuperResolution]\nMode={mode}\n\n[Runtime]\nSRMode=Bundled\n\n"
        "[Logging]\nLevel=1\nDirectory=dlssg_sm86\\logs\n")
    result.append({"relative_path": f"{ROOT}/dlssg_sm86.ini", "source_path": str(ini),
                   "role": "addon_manager_config", "source_package": f"generated/sm86-sr/{profile.id}"})
    return result
