"""Build the private-settings bootstrap; use the same pinned MinHook as the host."""
from pathlib import Path
import subprocess
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]


def build():
    from scripts.build_addon_manager import cmake_executable
    cmake = cmake_executable()
    options = []
    for candidate in sorted((ROOT / "build/addon_manager").glob("*/host/_deps/minhook-src")):
        revision = subprocess.run(["git", "-C", str(candidate), "rev-parse", "HEAD"], capture_output=True, text=True)
        dirty = subprocess.run(["git", "-C", str(candidate), "status", "--porcelain"], capture_output=True, text=True)
        if revision.returncode == dirty.returncode == 0 and revision.stdout.strip() == "c3fcafdc10146beb5919319d0683e44e3c30d537" and not dirty.stdout.strip():
            options.append(f"-DFETCHCONTENT_SOURCE_DIR_MINHOOK={candidate}")
            break
    subprocess.run([cmake, "-S", str(ROOT / "native/runtime_isolation"), "-B", str(ROOT / "build/runtime_isolation"), "-A", "x64", *options], check=True)
    subprocess.run([cmake, "--build", str(ROOT / "build/runtime_isolation"), "--config", "Release", "--parallel", "2"], check=True)
    dotnet = shutil.which("dotnet") or "C:/Program Files/dotnet/dotnet.exe"
    subprocess.run([dotnet, "build", str(ROOT / "native/runtime_isolation/StartupHook/StartupHook.csproj"), "-c", "Release"], check=True)
    license_file = ROOT / "build/native/runtime-isolation/MinHook-LICENSE.txt"
    sources = options or [str(p) for p in (ROOT / "build/runtime_isolation/_deps").glob("minhook-src")]
    if not sources:
        raise RuntimeError("The pinned MinHook license is unavailable")
    dependency = Path(sources[0].split("=", 1)[-1])
    shutil.copy2(dependency / "LICENSE.txt", license_file)


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    build()
