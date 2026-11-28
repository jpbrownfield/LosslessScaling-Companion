"""Check native startup failures and WARP readiness on an isolated desktop.

This validates startup, not hardware routing or RTSS performance. It never
switches the input desktop or sends input to any existing application.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes as w
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from companion.services.external_process import external_dll_search


def check(benchmark: Path, fixture: Path) -> None:
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.CreateDesktopW.argtypes = [w.LPCWSTR, w.LPCWSTR, ctypes.c_void_p,
                                   w.DWORD, w.DWORD, ctypes.c_void_p]
    user.CreateDesktopW.restype = w.HANDLE
    user.CloseDesktop.argtypes = [w.HANDLE]
    user.CloseDesktop.restype = w.BOOL
    desktop_name = "LSCBenchmarkStartup-" + uuid.uuid4().hex
    desktop = user.CreateDesktopW(desktop_name, None, None, 0, 0x01FF, None)
    if not desktop:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        with tempfile.TemporaryDirectory(prefix="lsc-benchmark-startup-") as directory:
            root = Path(directory)
            for label, executable, extra, expected in (
                ("missing-adapter", benchmark, ["--adapter-name", "LSC missing adapter " + uuid.uuid4().hex], 11),
                ("warp-ready", fixture, [], 0),
            ):
                receipt, adapter = root / (label + ".json"), root / (label + "-adapter.json")
                command = [str(executable.resolve()), "--width", "320", "--height", "240",
                           "--shader-load", "16", "--duration", "1", "--startup-report", str(receipt),
                           "--adapter-report", str(adapter), *extra]
                startup = subprocess.STARTUPINFO()
                startup.lpDesktop = desktop_name
                startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startup.wShowWindow = 0  # Reproduce a hidden parent's startup request.
                with (root / (label + ".log")).open("wb") as log:
                    with external_dll_search():
                        process = subprocess.Popen(command, cwd=executable.resolve().parent,
                                                   stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                                   startupinfo=startup)
                    try:
                        code = process.wait(timeout=30)
                    finally:
                        if process.poll() is None:
                            process.kill()
                            process.wait(timeout=5)
                data = json.loads(receipt.read_text()) if receipt.is_file() else {}
                assert code == expected, (label, code, data, (root / (label + ".log")).read_text(errors="replace"))
                assert data.get("exitCode") == expected, (label, data)
                if expected:
                    assert data.get("status") == "failed" and data.get("stage") == "select_adapter", data
                    assert data.get("hresult") == "0x887A0002", data
                    assert not adapter.exists(), "Explicit adapter failure silently fell back"
                else:
                    assert data.get("status") == "ready" and data.get("visible") is True, data
                    assert data.get("hwnd") and json.loads(adapter.read_text()).get("adapterLuid"), data
                print(f"{label}: native exit {code}, stage={data['stage']}, visible={data['visible']}")
    finally:
        user.CloseDesktop(desktop)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark", type=Path, required=True)
    parser.add_argument("--warp-fixture", type=Path, required=True)
    args = parser.parse_args()
    check(args.benchmark, args.warp_fixture)
