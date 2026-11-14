"""Exercise the real bundled DLSS5 addon; report hardware inference separately.

Run after scripts/build_addon_manager.py. No downloads, user settings changes,
vendor spoofing or substitute model/backend. Exit 2 means hardware acceptance
is blocked, even when every available contract/negative test passes.
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
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.build_addon_manager import cmake_executable


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", type=Path, help="Your own nvngx_dlssnr.dll; never downloaded by this test")
    parser.add_argument("--adapter", type=int, help="DXGI NVIDIA adapter index; defaults to the first hardware NVIDIA adapter")
    parser.add_argument("--ls-source", type=Path, help="Also test the real LS capture hook using this installation's original engine")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    report = {"schema": 1, "availableTestsPassed": False, "inferenceVerified": False,
              "captureHookVerified": False, "checks": [], "blocked": []}
    failed = False

    def run(name, command, *, cwd=output, timeout=120, env=None):
        log = output / (name + ".log")
        print(name, flush=True)
        with log.open("w", encoding="utf-8") as stream:
            import psutil
            process = subprocess.Popen([str(p) for p in command], cwd=cwd, env=env,
                                       stdout=stream, stderr=subprocess.STDOUT,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
            descendants = {}
            deadline = time.monotonic() + timeout
            try:
                while process.poll() is None:
                    try:
                        for child in psutil.Process(process.pid).children(recursive=True):
                            descendants[child.pid] = child.create_time()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                    if time.monotonic() >= deadline:
                        stream.write("\nTEST PROCESS TIMEOUT\n")
                        process.kill()
                        process.wait(timeout=10)
                        code = -1
                        break
                    time.sleep(.1)
                else:
                    code = process.returncode
            finally:
                # A failed capture probe can leave its source window running.
                # Only children observed beneath this test process are eligible.
                for pid, created in descendants.items():
                    try:
                        child = psutil.Process(pid)
                        if child.create_time() == created:
                            child.kill()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
        check = {"name": name, "passed": code == 0, "exitCode": code, "log": str(log)}
        report["checks"].append(check)
        if code:
            raise RuntimeError(f"{name} failed ({code}); see {log}")
        return log.read_text(encoding="utf-8", errors="replace")

    try:
        if os.name != "nt":
            raise RuntimeError("These native addon tests require Windows x64")
        overlay = ROOT / "native/addon_manager"
        lock = json.loads((overlay / "upstream.lock.json").read_text())
        key = hashlib.sha256(b"".join(p.read_bytes() for p in sorted(overlay.iterdir())
                            if p.suffix in {".cpp", ".h", ".inc", ".py", ".json"})).hexdigest()[:16]
        work = ROOT / "build/addon_manager" / key
        source = work / "source"
        archive = ROOT / "build/native/LSAddonManager-Companion.zip"
        if not (work / "prepared").is_file() or not archive.is_file():
            raise RuntimeError("Build the current bundled addon host first: python scripts/build_addon_manager.py")
        if (work / "prepared").read_text().strip() != lock["commit"]:
            raise RuntimeError("Prepared host source does not match the reviewed upstream commit")
        cache = (work / "host/CMakeCache.txt").read_text()
        imgui = next((line.split("=", 1)[1] for line in cache.splitlines()
                      if line.startswith("FETCHCONTENT_SOURCE_DIR_IMGUI:PATH=") and line.split("=", 1)[1]),
                     str(work / "host/_deps/imgui-src"))
        revision = subprocess.check_output(["git", "-C", imgui, "rev-parse", "HEAD"], text=True).strip()
        dirty = subprocess.check_output(["git", "-C", imgui, "status", "--porcelain"], text=True).strip()
        if revision != "367b2c24f399988ddafc0bb4628da0106bcc09be" or dirty:
            raise RuntimeError("The test must use the host's unmodified, pinned ImGui ABI")
        report["hostVersion"] = lock["version"]
        with archive.open("rb") as stream:
            report["archiveSha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
        app = output / "app"
        app.mkdir()
        with zipfile.ZipFile(archive) as bundle:
            manifest = json.loads(bundle.read("lossless-scaling-deployment.json"))
            if manifest["version"] != lock["version"]:
                raise RuntimeError("Bundled package version differs from the current source")
            report["addonFiles"] = {}
            for name in ("DLSSNR02.dll", "NeuralRendering_nvngx.dll", "addon.json"):
                data = bundle.read("addons/DLSSNR02/" + name)
                (app / name).write_bytes(data)
                report["addonFiles"][name] = hashlib.sha256(data).hexdigest()
        cmake = cmake_executable()
        native = ROOT / "build/neural-render-probe"
        run("configure-probe", [cmake, "-S", ROOT / "tests/native/neural_render", "-B", native,
                                "-A", "x64", f"-DLSC_UPSTREAM_SOURCE={source}", f"-DLSC_IMGUI_SOURCE={imgui}"])
        run("build-probe", [cmake, "--build", native, "--config", "Release", "--parallel", "4"], timeout=300)
        probe = native / "Release/LSC_NRProbe.exe"
        report["adapters"] = json.loads(run("adapters", [probe, "--list"]))["adapters"]
        environment = {**os.environ, "LOCALAPPDATA": str(output / "local-app-data")}
        for mode in ("invalid", "unsupported"):
            receipt = output / (mode + ".json")
            run(mode, [probe, "--addon", app / "DLSSNR02.dll", "--mode", mode, "--result", receipt],
                env=environment, timeout=60)
            evidence = json.loads(receipt.read_text())
            if not evidence["passed"] or evidence["inferenceVerified"]:
                raise RuntimeError(f"Invalid negative-test receipt: {receipt}")
        # Existing native tests exercise the real host's vendor router, then the
        # actual D3D12 flow and cross-API bridge. The latter uses a copy fixture.
        smoke = ROOT / "build/native/addon-host-smoke"
        run("vendor-routing", [smoke / "lsc_routing_test.exe"], cwd=smoke)
        amd_probe = ROOT / "build/native/LSC-AMDNR/LSC_AMDNRProbe.exe"
        run("software-compute-flow", [amd_probe, "--warp", "--flow-only"])
        run("software-frame-bridge", [amd_probe, "--warp", "--bridge-only"])
        report["availableTestsPassed"] = True
        nvidia = [a for a in report["adapters"] if a["vendorId"] == 0x10de and not a["software"]]
        if not nvidia:
            report["blocked"].append("No hardware NVIDIA adapter is exposed to this Windows session")
        if not args.model:
            report["blocked"].append("Supply your NR model with --model to test real inference")
        if nvidia and args.model:
            index = args.adapter if args.adapter is not None else nvidia[0]["index"]
            receipt = output / "inference.json"
            run("inference", [probe, "--addon", app / "DLSSNR02.dll", "--mode", "inference",
                              "--model", args.model.resolve(), "--adapter", index, "--result", receipt],
                env=environment, timeout=240)
            evidence = json.loads(receipt.read_text())
            if not evidence["passed"] or not evidence["inferenceVerified"]:
                raise RuntimeError("Model inference did not produce a successful native receipt")
            report["inferenceVerified"] = True
        if args.ls_source:
            # Upstream's probe invokes the real native engine against its own
            # animated window, checking capture provenance and downstream LSFG
            # consumers. It does not launch LS's managed UI or read its settings.
            engine = args.ls_source.resolve().parent / "Lossless.dll"
            import re
            support_header = (source / "manager/sdk/include/eam/real_frame_status.h").read_text()
            expected = re.search(r'EamSupportedLsSha256\[\]\s*=\s*"([a-f0-9]{64})"', support_header)
            if not expected or not engine.is_file():
                raise RuntimeError("Cannot locate the supported stock LS engine beside --ls-source")
            with engine.open("rb") as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
            if actual != expected[1]:
                raise RuntimeError("Capture test requires the verified original Lossless.dll, not a proxy/replacement")
            report["engineSha256"] = actual
            run("build-capture-probe", [cmake, "--build", work / "host", "--config", "Release",
                                        "--target", "eam_captureprobe", "--parallel", "4"], timeout=300)
            capture = app / "eam_captureprobe.exe"
            shutil.copy2(work / "host/Release/eam_captureprobe.exe", capture)
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            point = wintypes.POINT()
            cursor_available = bool(user32.GetCursorPos(ctypes.byref(point)))
            cursor_error = 0 if cursor_available else ctypes.get_last_error()
            report["desktop"] = {"cursorAvailable": cursor_available, "cursorError": cursor_error,
                                 "foregroundAvailable": bool(user32.GetForegroundWindow())}
            if not cursor_available:
                report["blocked"].append(f"Windows input desktop is unavailable (error {cursor_error}); real LS window capture was not tested")
            if report["inferenceVerified"] and cursor_available:
                capture_environment = {**environment, "EAM_PROBE_SECONDS": "12"}
                text = run("real-ls-capture", [capture, engine, "1", "1", "0", "0", "0", "0",
                                              app / "DLSSNR02.dll", args.model.resolve(), "100", "1"],
                           env=capture_environment, timeout=120)
                accepted = re.search(r"NR accepted (\d+) edited (\d+)", text)
                totals = re.search(r"Total passes (\d+) real frames (\d+) verified (\d+) failures (\d+)", text)
                if not accepted or not totals or int(accepted[1]) <= 30 or not int(accepted[2]) or not int(totals[3]) or int(totals[4]):
                    raise RuntimeError("The LS probe did not prove processed/edited frames and capture provenance")
                report["captureHookVerified"] = True
                report["captureEvidence"] = {"accepted": int(accepted[1]), "edited": int(accepted[2]),
                                             "passes": int(totals[1]), "realFrames": int(totals[2]),
                                             "verified": int(totals[3]), "engineSha256": actual}
        elif not args.ls_source:
            report["blocked"].append("Supply --ls-source to also verify the real LS capture hook")
    except Exception as error:
        report["error"] = str(error)
        failed = True
    finally:
        (output / "results.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2), flush=True)
    return 1 if failed else 2 if report["blocked"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
