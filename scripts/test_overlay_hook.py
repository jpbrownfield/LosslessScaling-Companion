"""Opt-in desktop integration test for the packaged host and a D3D11 workload.

Build tests/native/overlay_input first. This test opens windows and takes focus.
Supply --ls-source for a real, isolated LS clone; omit it for the native window
fixture (no engine/capture emulation). No native focus bypass is used.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes as wt
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from companion.services.addon_host import _read_pipe, set_passthrough, validate_status
from companion.services.input_simulator import InputSimulator
from companion.services.performance_benchmark import find_largest_window, focus_window
import psutil

u = ctypes.WinDLL("user32", use_last_error=True)
for name, arguments, result in (
    ("GetForegroundWindow", [], wt.HWND),
    ("IsWindow", [wt.HWND], wt.BOOL),
    ("GetCursorPos", [ctypes.POINTER(wt.POINT)], wt.BOOL),
    ("GetWindowLongW", [wt.HWND, ctypes.c_int], ctypes.c_long),
    ("GetWindow", [wt.HWND, wt.UINT], wt.HWND),
    ("GetWindowThreadProcessId", [wt.HWND, ctypes.POINTER(wt.DWORD)], wt.DWORD),
    ("SetWindowPos", [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT], wt.BOOL),
    ("PostMessageW", [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM], wt.BOOL),
    ("SendMessageTimeoutW", [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM, wt.UINT, wt.UINT, ctypes.POINTER(ctypes.c_size_t)], ctypes.c_size_t),
):
    function = getattr(u, name)
    function.argtypes, function.restype = arguments, result


def message(hwnd, msg, wp=0, lp=0):
    result = ctypes.c_size_t()
    if not u.SendMessageTimeoutW(hwnd, msg, wp, lp, 2, 1000, ctypes.byref(result)):
        raise RuntimeError(f"Window {hwnd} did not answer message {msg}")
    return ctypes.c_ssize_t(result.value).value


def wait_for(label, predicate, timeout=10):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except (OSError, ValueError) as error:
            last_error = error
        time.sleep(.05)
    raise RuntimeError(f"Timed out: {label}; last error: {last_error}")


def window_snapshot(pid):
    windows = []
    def visit(hwnd, _):
        owner = wt.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            name, title, rect = ctypes.create_unicode_buffer(256), ctypes.create_unicode_buffer(256), wt.RECT()
            u.GetClassNameW(wt.HWND(hwnd), name, 256)
            u.GetWindowTextW(wt.HWND(hwnd), title, 256)
            u.GetWindowRect(wt.HWND(hwnd), ctypes.byref(rect))
            windows.append({"hwnd": hwnd, "class": name.value, "title": title.value,
                            "visible": bool(u.IsWindowVisible(wt.HWND(hwnd))),
                            "style": hex(u.GetWindowLongW(hwnd, -16) & 0xffffffff),
                            "exstyle": hex(u.GetWindowLongW(hwnd, -20) & 0xffffffff),
                            "size": [rect.right-rect.left, rect.bottom-rect.top]})
        return True
    callback = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)(visit)
    u.EnumWindows(callback, 0)
    return windows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ls-source", type=Path)
    parser.add_argument("--host-archive", type=Path, default=ROOT / "build/native/LSAddonManager-Companion.zip")
    parser.add_argument("--reshade-dll", type=Path, help="Load this ReShade DLL through the real component manifest")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(output / "test.log"), logging.StreamHandler()])
    if args.reshade_dll and not args.ls_source:
        parser.error("--reshade-dll requires --ls-source")
    if args.reshade_dll:
        import pefile
        with pefile.PE(str(args.reshade_dll), fast_load=True) as image:
            image.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_EXPORT"]])
            exports = {entry.name for entry in getattr(image, "DIRECTORY_ENTRY_EXPORT", ()).symbols} if hasattr(image, "DIRECTORY_ENTRY_EXPORT") else set()
            if b"ReShadeVersion" not in exports:
                parser.error("--reshade-dll does not export ReShadeVersion; supply the actual ReShade injector")
    report = {"mode": "real-ls" if args.ls_source else "native-window-fixture", "checks": [], "passed": False}
    children, handles, streams = [], [], []
    foreground_before = u.GetForegroundWindow()
    fixtures = ROOT / "build/overlay-input-fixture/Release"
    app = output / "app"
    host = None

    def check(label, condition=True, **evidence):
        if not condition:
            raise AssertionError(label)
        report["checks"].append({"check": label, **evidence})
        logging.info("PASS %s %s", label, json.dumps(evidence) if evidence else "")

    def launch(command, cwd, env=None):
        path = output / f"child-{len(children)}.log"
        stream = path.open("w")
        streams.append(stream)
        child = subprocess.Popen([str(x) for x in command], cwd=cwd, env=env,
                                 stdout=stream, stderr=subprocess.STDOUT,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        children.append(child)
        return child

    try:
        environment = {**os.environ, "LOCALAPPDATA": str(output / "local"), "APPDATA": str(output / "roaming")}
        if args.ls_source:
            from companion.services.asset_store import AssetStore
            from companion.services.managed_runtime import ManagedRuntime, launch_environment
            runtime = ManagedRuntime(output / "companion")
            executable = runtime.prepare(args.ls_source, AssetStore(str(output / "assets")), seed_settings=ROOT / "Settings.xml")
            app = executable.parent
            # Only the isolated test copy is changed. Keep this input test cheap
            # and independent of DLSS, HDR, frame generation and selected GPUs.
            import xml.etree.ElementTree as ET
            tree = ET.parse(runtime.settings)
            root = tree.getroot()
            for key, value in {"StartAsAdmin": "false", "Hotkey": "F23", "HotkeyModifierKeys": "Control Alt Shift",
                               "GpuPreference": "0", "GpuPreferenceChangeCount": "3"}.items():
                node = root.find(key)
                if node is None:
                    node = ET.SubElement(root, key)
                node.text = value
            for profile in root.findall("./GameProfiles/Profile"):
                for key, value in {"FrameGeneration": "Off", "ScalingType": "Off", "CaptureApi": "WGC", "PreferredGpuId": "0", "HdrSupport": "false", "ClipCursor": "false"}.items():
                    node = profile.find(key)
                    if node is None:
                        node = ET.SubElement(profile, key)
                    node.text = value
            tree.write(runtime.settings, encoding="utf-8", xml_declaration=True)
            environment, receipt, token = launch_environment(executable)
        else:
            app.mkdir()
            executable = app / "LosslessScaling.exe"
            shutil.copy2(fixtures / "LSC_OverlayInputFixture.exe", executable)
            shutil.copy2(ROOT / "build/native/addon-host-smoke/Lossless_original.dll", app / "Lossless_original.dll")

        if args.ls_source:
            shutil.copy2(app / "Lossless.dll", app / "Lossless_original.dll")
        with zipfile.ZipFile(args.host_archive) as bundle:
            for name in bundle.namelist():
                if name == "Lossless.dll" or name.startswith("addons/LSC-AdapterReport/"):
                    bundle.extract(name, app)
        (app / "addons").mkdir(exist_ok=True)
        (app / "addons/config.json").write_text(json.dumps({
            "global": {"auto_load": True, "companion": {"headless": not bool(args.ls_source), "managed": True},
                       "ui": {"open_on_start": False, "hotkey_enabled": False}},
            "addons": {"LSC-AdapterReport": {"_enabled": True},
                       "ReShadePassthrough": {"_enabled": True, "companion_control": "1"}}
        }), encoding="utf-8")
        if args.reshade_dll:
            relative = ".companion/components/reshade/ReShade64.dll"
            component = app / relative
            component.parent.mkdir(parents=True)
            shutil.copy2(args.reshade_dll, component)
            (app / ".companion/components.json").write_text(json.dumps({"schema": 1, "components": [
                {"id": "reshade", "path": relative, "sha256": AssetStore.sha256(component)}
            ]}), encoding="utf-8")
            (app / "ReShade.ini").write_text("[GENERAL]\nEffectSearchPaths=\nTextureSearchPaths=\nPresetPath=\n[INPUT]\nKeyOverlay=36,0,0,0\n[OVERLAY]\nTutorialProgress=4\n", encoding="utf-8")

        host = launch([executable, *([] if args.ls_source else ["host"])], app, environment)
        report["host_pid"] = host.pid
        report["executable"] = str(executable)
        if args.ls_source:
            from companion.services.managed_runtime import await_isolation
            await_isolation(host.pid, receipt, token)
            check("Real LS verified private settings isolation")
        instance = host.pid, psutil.Process(host.pid).create_time()

        def status():
            return validate_status(_read_pipe(host.pid), instance)

        initial = wait_for("add-on startup", lambda: (s if (s := status())["reshade"]["controlAvailable"] else None), 20)
        report["startup"] = initial
        check("Packaged add-on loaded and real pipe is available", version=initial["hostVersion"], capture=initial["capture"])
        check("Host initialized without startup errors", not initial["startupError"])
        if args.reshade_dll:
            check("Actual ReShade loaded through Companion's component loader", any(
                item["id"] == "reshade" and item["loaded"] for item in initial["components"]), components=initial["components"])
        if args.ls_source:
            check("Add-on verified the real LS engine", initial["capture"]["runtimeVerified"])
            native_log = (app / "logs/LSAddonManager.log").read_text(encoding="utf-8", errors="replace")
            check("D3D11 hooks installed in the actual LS process", "Hooked 5 of 5 Dispatch implementations" in native_log)
            wait_for("LS settings window initialized", lambda: any(
                window["class"].startswith("HwndWrapper[") and window["title"] == "Lossless Scaling"
                for window in window_snapshot(host.pid)), 20)
            time.sleep(.5)
        benchmark = launch([fixtures / "LSC_BenchmarkInputFixture.exe", "--width", "640", "--height", "360", "--shader-load", "8", "--vsync", "--duration", "120"], fixtures)
        game_window = find_largest_window(benchmark.pid)
        handles.append(game_window)
        check("D3D11 test scaling window initialized", pid=benchmark.pid, hwnd=game_window)
        focused = focus_window(game_window)
        if not focused:
            point = wt.POINT()
            ctypes.set_last_error(0)
            cursor_available = bool(u.GetCursorPos(ctypes.byref(point)))
            report["desktop"] = {"foreground": u.GetForegroundWindow(), "cursor_available": cursor_available,
                                 "cursor_error": ctypes.get_last_error()}
            # Still exercise the actual command parser and rejection path. Do
            # not defeat native foreground checks to manufacture a positive.
            check("Add-on disable command confirmed", not set_passthrough(str(executable), False, expected_pid=host.pid, timeout=5)["reshade"]["interactive"])
            cases = [("Native host explains why input cannot open", game_window,
                      ("LS presentation window was not found", "Focus changed"))]
            if not args.ls_source:
                cases.append(("Native host rejects an invalid target window", 1, ("changed or closed",)))
            for label, hwnd, expected in cases:
                try:
                    set_passthrough(str(executable), True, expected_pid=host.pid,
                                    target_pid=benchmark.pid, target_hwnd=hwnd, timeout=5)
                except ValueError as error:
                    check(label, any(text in str(error) for text in expected), error=str(error))
                else:
                    raise AssertionError(f"{label}: request unexpectedly succeeded")
            rejected = status()
            report["after_rejected_requests"] = rejected
            check("Rejected requests leave input disabled", not rejected["reshade"]["interactive"] and not rejected["reshade"]["controlRequested"])
            report["blocked"] = "Windows denied foreground ownership. Interactive scaling, Home delivery and Alt-Tab could not be verified."
            logging.warning("%s Desktop evidence: %s", report["blocked"], report["desktop"])
            return 2
        check("Test scaling window received real foreground focus")
        if args.ls_source:
            from companion.services.ls_inspector import LosslessScalingInspector
            check("Scaling hotkey injected", InputSimulator.trigger_hotkey(["ctrl", "alt", "shift"], "f23"))
            def scaling_window():
                active, window = LosslessScalingInspector.detect_lossless_scaling_overlay_window(str(executable))
                return window if active else None
            try:
                presentation = wait_for("real LS scaling presentation", scaling_window, 20)
            finally:
                report["after_scale_request"] = status()
        else:
            presentation = wait_for("native presentation fixture", lambda: next(
                (w["hwnd"] for w in window_snapshot(host.pid) if w["class"] == "LosslessScaling" and w["visible"]), None))
        handles.append(presentation)
        check("Presentation window exists", hwnd=presentation)

        obstruction = launch([fixtures / "LSC_OverlayInputFixture.exe", "obstruction"], fixtures)
        obstruction_window = find_largest_window(obstruction.pid)
        handles.append(obstruction_window)
        away = launch([fixtures / "LSC_OverlayInputFixture.exe", "away"], fixtures)
        away_window = find_largest_window(away.pid)
        handles.append(away_window)
        check("Workload focused before enabling", wait_for("workload foreground before enable",
              lambda: focus_window(game_window, timeout=.25), 5))
        if args.ls_source:
            # LS can replace its startup presentation while WGC initializes.
            # Resolve after foreground restoration, not from the first window
            # observed immediately after the scaling hotkey.
            time.sleep(1)
            presentation = wait_for("current LS presentation", scaling_window)
            handles.append(presentation)
            report["presentation_before_enable"] = presentation
        check("Border fixture becomes topmost", bool(u.SetWindowPos(obstruction_window, wt.HWND(-1), 0, 0, 0, 0, 0x213)))
        check("Presentation raised without activation", bool(u.SetWindowPos(presentation, wt.HWND(-1), 0, 0, 0, 0, 0x213)))
        check("Border fixture positioned behind presentation", bool(u.SetWindowPos(obstruction_window, presentation, 0, 0, 0, 0, 0x213)))
        chain = []
        cursor = u.GetWindow(presentation, 2)
        while cursor and len(chain) < 1000:
            chain.append(cursor)
            cursor = u.GetWindow(cursor, 2)
        report["z_order_below_presentation"] = chain
        check("Foreign border window is between presentation and target",
              obstruction_window in chain and game_window in chain and chain.index(obstruction_window) < chain.index(game_window),
              presentation=presentation, obstruction=obstruction_window, target=game_window)
        original_style = u.GetWindowLongW(presentation, -20)
        report["windows_before_enable"] = window_snapshot(host.pid)
        if not args.ls_source:
            check("Presentation initially unhooked and click-through", message(presentation, 0x8029) == 0 and message(presentation, 0x84, lp=(100 << 16) | 100) == -1)
            check("LS-style foreground stop listener registered", message(presentation, 0x802c) == 1)
            configuration = next(w["hwnd"] for w in window_snapshot(host.pid) if w["class"].startswith("HwndWrapper["))
            check("Larger LS settings fixture initially unhooked", message(configuration, 0x8029) == 0)
            event_count = message(presentation, 0x802d)

        def control(enabled):
            return set_passthrough(str(executable), enabled, expected_pid=host.pid,
                                   target_pid=benchmark.pid if enabled else None,
                                   target_hwnd=game_window if enabled else None, timeout=5)

        enabled = control(True)
        check("Actual pipe command enabled interactive passthrough", enabled["reshade"]["interactive"], status=enabled["reshade"])
        check("Presentation handle remains live after enable", bool(u.IsWindow(presentation)))
        check("Transparent and noactivate styles removed", not u.GetWindowLongW(presentation, -20) & (0x20 | 0x8000000))
        if not args.ls_source:
            check("Native input subclass installed", message(presentation, 0x8029) == 1)
            check("LS settings window is excluded from input hooks", message(configuration, 0x8029) == 0)
            check("Real hit test is interactive", message(presentation, 0x84, lp=(100 << 16) | 100) == 1)
            check("Presentation can receive keyboard focus", focus_window(presentation))
            before = message(presentation, 0x802a)
            check("Home key injected", InputSimulator.trigger_hotkey([], "home"))
            wait_for("Home key delivery", lambda: message(presentation, 0x802a) > before)
            check("Home key reached the hooked presentation")
        elif args.reshade_dll:
            check("Actual LS presentation can receive keyboard focus", focus_window(presentation))
            check("Home key injected into actual LS presentation", InputSimulator.trigger_hotkey([], "home"))
            report["reshade_menu_visibility"] = "Not asserted: injector may skip the software renderer; inspect ReShade.log."
        check("Focus moved to another application", focus_window(away_window))
        suspended = wait_for("passthrough suspension", lambda: (s if not (s := status())["reshade"]["interactive"] and not s["reshade"]["controlPending"] else None))
        check("Alt-Tab suspension preserves requested state", suspended["reshade"]["controlRequested"])
        check("Alt-Tab removes topmost without stealing focus", not u.GetWindowLongW(presentation, -20) & 8 and u.GetForegroundWindow() == away_window)
        if not args.ls_source:
            check("Alt-Tab restored original window procedure", message(presentation, 0x8029) == 0)
            wait_for("external foreground events forwarded", lambda: message(presentation, 0x802d) > event_count)
            check("External foreground callbacks remain active")
            replacement = message(presentation, 0x802e)
            check("LS-style presentation replacement created", bool(replacement) and bool(u.IsWindow(replacement)))
            presentation = replacement
            handles.append(presentation)
            original_style = u.GetWindowLongW(presentation, -20)
            suspended = wait_for("replacement stays suspended", lambda: (s if not (s := status())["reshade"]["interactive"] and not s["reshade"]["controlPending"] else None))
            check("Presentation replacement preserves requested input state", suspended["reshade"]["controlRequested"])
        returned = focus_window(game_window)
        # The watcher may already have handed focus to the interactive
        # presentation by the time Python samples the foreground HWND.
        returned = returned or (u.GetForegroundWindow() == presentation and status()["reshade"]["interactive"])
        check("Game or LS owns focus after returning to workload", returned)
        wait_for("passthrough resumed", lambda: (s := status())["reshade"]["interactive"] and not s["reshade"]["controlPending"])
        if args.ls_source:
            presentation = wait_for("LS presentation after resume", scaling_window)
            handles.append(presentation)
            report["presentation_after_resume"] = presentation
        check("Returning to the workload restores interactive topmost", bool(u.GetWindowLongW(presentation, -20) & 8))
        disabled = control(False)
        check("Disable confirmed by actual add-on", not disabled["reshade"]["interactive"] and not disabled["reshade"]["controlRequested"])
        check("Original extended styles restored", u.GetWindowLongW(presentation, -20) == original_style)
        if not args.ls_source:
            check("Disable removed native subclass", message(presentation, 0x8029) == 0)
        check("Workload focused for close test", focus_window(game_window))
        control(True)
        u.PostMessageW(game_window, 0x10, 0, 0)
        benchmark.wait(timeout=5)
        wait_for("closed workload recovery", lambda: not status()["reshade"]["controlRequested"])
        check("Closing the scaled workload clears passthrough automatically")
        if not args.ls_source:
            check("Closing workload removes native subclass", message(presentation, 0x8029) == 0)
        report["passed"] = True
    except Exception as error:
        report["error"] = str(error)
        report["foreground_on_failure"] = u.GetForegroundWindow()
        if host is not None and host.poll() is None:
            report["windows_on_failure"] = window_snapshot(host.pid)
            try:
                report["status_on_failure"] = status()
            except (OSError, ValueError):
                pass
        logging.exception("Integration test failed")
    finally:
        # Stop only processes this run launched. The source installation and
        # user's running applications are never stopped or overwritten.
        test_owned_foreground = u.GetForegroundWindow() in handles
        report["child_exit_codes_before_cleanup"] = {str(child.pid): child.poll() for child in children}
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
        for stream in streams:
            stream.close()
        if test_owned_foreground and foreground_before:
            focus_window(foreground_before)
        if app.is_dir():
            for path in (app / "logs").glob("*.log"):
                shutil.copy2(path, output / path.name)
            for path in app.glob("ReShade*.log"):
                shutil.copy2(path, output / path.name)
        (output / "results.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
