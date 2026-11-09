"""Build the pinned Companion host and package the unmodified upstream addon suite.

The MIT host receives our overlay and separate AMD addon. The release's GPL NR
binaries, matching source, licences and upscaler runtimes are copied byte for byte.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
OVERLAY = ROOT / "native/addon_manager"


def digest(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def download(url, path, expected):
    if not path.is_file() or digest(path) != expected:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".download")
        with urllib.request.urlopen(url, timeout=60) as response, temporary.open("wb") as output:
            shutil.copyfileobj(response, output)
        if digest(temporary) != expected:
            raise RuntimeError(f"Publisher SHA-256 mismatch for {path.name}")
        temporary.replace(path)
    return path


def extract(archive, destination, *, strip_root=False):
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            parts = member.filename.replace("\\", "/").split("/")
            if strip_root:
                parts = parts[1:]
            if not parts or not any(parts):
                continue
            target = destination.joinpath(*parts).resolve()
            if destination.resolve() not in target.parents:
                raise RuntimeError(f"Unsafe source archive path: {member.filename}")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)


def cmake_executable():
    found = shutil.which("cmake")
    if found:
        return found
    matches = list(Path("C:/Program Files/Microsoft Visual Studio").glob(
        "*/*/Common7/IDE/CommonExtensions/Microsoft/CMake/CMake/bin/cmake.exe"))
    if not matches:
        raise RuntimeError("CMake and Visual Studio C++ tools are required")
    return str(sorted(matches)[-1])


def build(cache: Path):
    lock = json.loads((OVERLAY / "upstream.lock.json").read_text())
    repository, commit, tag = (lock[key] for key in ("repository", "commit", "tag"))
    source_zip = download(f"https://codeload.github.com/{repository}/zip/{commit}",
                          cache / "source.zip", lock["source_sha256"])
    release_zip = download(f"https://github.com/{repository}/releases/download/{tag}/LSAddonManager-{tag}-x64.zip",
                           cache / "release.zip", lock["release_sha256"])
    overlay_hash = hashlib.sha256(b"".join(path.read_bytes() for path in sorted(OVERLAY.iterdir())
        if path.suffix in {".cpp", ".h", ".inc", ".py", ".json"})).hexdigest()[:16]
    work = ROOT / "build/addon_manager" / overlay_hash
    source = work / "source"
    if not (work / "prepared").is_file():
        extract(source_zip, source, strip_root=True)
        subprocess.run([sys.executable, str(OVERLAY / "prepare.py"), str(source)], check=True)
        (work / "prepared").write_text(commit)
    amd_overlay = ROOT / "native/amd_neuralrender"
    amd_lock = json.loads((amd_overlay / "runtime.lock.json").read_text())
    amd_source_zip = download(f"https://codeload.github.com/{amd_lock['repository']}/zip/{amd_lock['commit']}",
        cache / "amd-runtime-source.zip", amd_lock["source_sha256"])
    amd_patch_hash = hashlib.sha256(b"".join((amd_overlay / name).read_bytes()
        for name in ("prepare_runtime.py", "companion_adapter.h", "runtime.lock.json"))).hexdigest()[:16]
    amd_runtime_source = ROOT / "build/amd_neuralrender/sources" / amd_patch_hash
    if not (amd_runtime_source / ".companion-prepared").is_file():
        extract(amd_source_zip, amd_runtime_source, strip_root=True)
        subprocess.run([sys.executable, str(amd_overlay / "prepare_runtime.py"), str(amd_runtime_source)], check=True)
        (amd_runtime_source / ".companion-prepared").write_text(amd_lock["commit"])
    cmake = cmake_executable()
    subprocess.run([cmake, "-S", str(ROOT / "native/adapter_report"),
                    "-B", str(ROOT / "build/adapter_report"), "-A", "x64"], check=True)
    subprocess.run([cmake, "--build", str(ROOT / "build/adapter_report"), "--config", "Release",
                    "--target", "LSC_AdapterReport"], check=True)
    subprocess.run([cmake, "-S", str(ROOT / "native/amd_neuralrender"), "-B", str(ROOT / "build/amd_neuralrender"),
                    "-A", "x64", f"-DLSC_UPSTREAM_SOURCE={source}", f"-DLSC_AMD_RUNTIME_SOURCE={amd_runtime_source}"], check=True)
    subprocess.run([cmake, "--build", str(ROOT / "build/amd_neuralrender"), "--config", "Release"], check=True)
    dependency_options = []
    dependencies = {}
    # Reuse existing pinned Git checkouts between overlay revisions; verify their
    # identity before overriding FetchContent. A clean machine fetches normally.
    for name, revision in (("imgui", "367b2c24f399988ddafc0bb4628da0106bcc09be"),
                           ("minhook", "c3fcafdc10146beb5919319d0683e44e3c30d537")):
        dependencies[name] = work / "host/_deps" / f"{name}-src"
        for candidate in sorted((ROOT / "build/addon_manager").glob(f"*/host/_deps/{name}-src")):
            result = subprocess.run(["git", "-C", str(candidate), "rev-parse", "HEAD"], capture_output=True, text=True)
            clean = subprocess.run(["git", "-C", str(candidate), "status", "--porcelain"], capture_output=True, text=True)
            if result.returncode == clean.returncode == 0 and result.stdout.strip() == revision and not clean.stdout.strip():
                dependency_options.append(f"-DFETCHCONTENT_SOURCE_DIR_{name.upper()}={candidate}")
                dependencies[name] = candidate
                break
    subprocess.run([cmake, "-S", str(source / "manager"), "-B", str(work / "host"), "-A", "x64",
        f'-DLSC_HOST_VERSION={lock["version"]}', *dependency_options], check=True)
    subprocess.run([cmake, "--build", str(work / "host"), "--config", "Release", "--target", "Lossless",
                    "lsc_headless_smoke", "lsc_fake_engine", "lsc_routing_test", "--parallel"], check=True)

    setup_dir = work / "setup"
    extract(release_zip, setup_dir)
    payload = work / "payload"
    if not (payload / "Lossless.dll").is_file():
        subprocess.run([str(setup_dir / "LSAddonManagerSetup.exe"), "--extract", str(payload),
            "--no-remember", "--log", str(work / "extract.log")], check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=180)
    shutil.copy2(work / "host/Release/Lossless.dll", payload / "Lossless.dll")
    helper = payload / "addons/LSC-AdapterReport"
    helper.mkdir(parents=True, exist_ok=True)
    for name in ("LSC_AdapterReport.dll", "addon.json"):
        shutil.copy2(ROOT / "build/native/LSC-AdapterReport" / name, helper / name)
    amd = payload / "addons/LSC-AMDNR"
    amd.mkdir(parents=True, exist_ok=True)
    for name in ("LSC_AMDNR.dll", "addon.json"):
        shutil.copy2(ROOT / "build/native/LSC-AMDNR" / name, amd / name)
    (amd / "runtime").mkdir(exist_ok=True)
    shutil.copy2(ROOT / "build/native/LSC-AMDNR/runtime/dlss5nr_runtime.dll", amd / "runtime/dlss5nr_runtime.dll")
    shutil.copy2(source / "LICENSE", amd / "FlowEstimator-LICENSE.txt")
    shutil.copy2(ROOT / "native/amd_neuralrender/README.md", amd / "README.md")
    shutil.copy2(ROOT / "native/amd_neuralrender/LICENSE", amd / "LICENSE")
    vendor = ROOT / "native/amd_neuralrender/vendor"
    shutil.copy2(vendor / "LICENSE", amd / "Runtime-API-LICENSE.txt")
    for name in ("ZLUDA-LICENSE-MIT", "ZLUDA-LICENSE-APACHE"):
        shutil.copy2(vendor / name, amd / name)
    for name in ("DLSSNR02.dll", "NeuralRendering_nvngx.dll", "NeuralRendering-source.zip", "LICENSE.txt", "NOTICE.md"):
        if not (payload / "addons/DLSSNR02" / name).is_file():
            raise RuntimeError(f"Upstream NR payload is incomplete: {name}")
    notices = payload / "licenses/LSAddonManager"
    notices.mkdir(parents=True, exist_ok=True)
    for name in ("LICENSE.txt", "NOTICE.md"):
        shutil.copy2(setup_dir / name, notices / name)
    shutil.copy2(OVERLAY / "upstream.lock.json", notices / "upstream.lock.json")
    shutil.copy2(OVERLAY / "BRIDGING.md", notices / "BRIDGING.md")
    shutil.copy2(ROOT / "native/vendor/losslessproxy-sdk/LICENSE", notices / "LosslessProxy-SDK-LICENSE.txt")

    # Host source plus the overlay and exact fetched dependencies. The NR's own
    # corresponding-source ZIP is retained from the verified upstream release.
    with zipfile.ZipFile(notices / "CompanionHost-source.zip", "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(source.rglob("*")):
            if path.is_file() and not any(part in {".git", "build"} for part in path.relative_to(source).parts):
                bundle.write(path, "upstream/" + path.relative_to(source).as_posix())
        for path in sorted(amd_runtime_source.rglob("*")):
            if path.is_file() and path.name != ".companion-prepared":
                bundle.write(path, "amd-runtime/" + path.relative_to(amd_runtime_source).as_posix())
        for dependency in ("imgui", "minhook"):
            folder = dependencies[dependency]
            for path in sorted(folder.rglob("*")):
                if path.is_file() and ".git" not in path.relative_to(folder).parts:
                    bundle.write(path, f"dependencies/{dependency}/" + path.relative_to(folder).as_posix())
        for path in sorted(OVERLAY.iterdir()):
            if path.is_file():
                bundle.write(path, "native/addon_manager/" + path.name)
        bundle.write(__file__, "scripts/build_addon_manager.py")
        bundle.write(ROOT / "companion/services/sm86_sr.py", "companion/services/sm86_sr.py")
        bundle.write(ROOT / "companion/services/amd_nr.py", "companion/services/amd_nr.py")
        for folder in (ROOT / "native/adapter_report", ROOT / "native/amd_neuralrender", ROOT / "native/vendor/losslessproxy-sdk"):
            for path in sorted(folder.rglob("*")):
                if path.is_file():
                    bundle.write(path, path.relative_to(ROOT).as_posix())
        bundle.writestr("BUILD.txt", "The upstream/ tree already has Companion's overlay applied.\n"
            f"Host version: {lock['version']}; upstream commit: {commit}\n"
            "cmake -S upstream/manager -B host -A x64 "
            f"-DLSC_HOST_VERSION={lock['version']} "
            "-DFETCHCONTENT_SOURCE_DIR_IMGUI=<absolute>/dependencies/imgui "
            "-DFETCHCONTENT_SOURCE_DIR_MINHOOK=<absolute>/dependencies/minhook\n"
            "cmake --build host --config Release --target Lossless\n"
            "The amd-runtime/ tree already has the adapter-selection patch applied.\n"
            "cmake -S native/amd_neuralrender -B amd -A x64 -DLSC_UPSTREAM_SOURCE=<absolute>/upstream -DLSC_AMD_RUNTIME_SOURCE=<absolute>/amd-runtime\n"
            "cmake --build amd --config Release\n")
    deployment = []
    for path in sorted(payload.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(payload).as_posix()
        if relative in {"addons/config.json", "lossless-scaling-deployment.json"}:
            continue
        if relative == "Lossless.dll" or relative.startswith(("addons/", "licenses/")) or relative.startswith("manager-icon."):
            deployment.append({"source": relative, "destination": relative,
                "role": "addon_manager_host" if relative == "Lossless.dll" else
                        "lossless_addon" if relative.startswith("addons/") else "addon_manager_support"})
    (payload / "lossless-scaling-deployment.json").write_text(json.dumps({"schema_version": 1,
        "version": lock["version"], "upstream_commit": commit, "bridge_protocol": lock["bridge_protocol"],
        "files": deployment}, indent=2), encoding="utf-8")
    destination = ROOT / "build/native/LSAddonManager-Companion.zip"
    destination.parent.mkdir(parents=True, exist_ok=True)
    smoke = destination.parent / "addon-host-smoke"
    smoke.mkdir(exist_ok=True)
    for name in ("lsc_headless_smoke.exe", "Lossless_original.dll"):
        shutil.copy2(work / "host/Release" / name, smoke / name)
    shutil.copy2(work / "host/Release/lsc_routing_test.exe", smoke / "lsc_routing_test.exe")
    shutil.copytree(work / "host/Release/routing-fixtures", smoke / "routing-fixtures", dirs_exist_ok=True)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(payload.rglob("*")):
            if path.is_file() and path.relative_to(payload).as_posix() != "addons/config.json":
                bundle.write(path, path.relative_to(payload).as_posix())
    print(f"Built {destination} ({lock['version']}); SHA-256 {digest(destination)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=ROOT / "build/addon_manager/downloads")
    build(parser.parse_args().cache.resolve())
