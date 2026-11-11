"""WebView2 GUI entry point. Its stdin pipe is owned only by the companion."""

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import queue
import sys
import threading
from urllib.parse import parse_qs, urlsplit


def _pipe_stream(stream, handle_id, mode):
    if stream is not None:
        return stream
    # Windowed Python sets sys.std* to None even when Popen supplied redirected
    # Windows handles. Reopen those handles for the packaged executable too.
    import msvcrt
    kernel32 = ctypes.windll.kernel32
    kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel32.GetStdHandle.restype = wintypes.HANDLE
    handle = kernel32.GetStdHandle(handle_id & 0xFFFFFFFF)
    if not handle or handle == ctypes.c_void_p(-1).value:
        raise RuntimeError("Dashboard requires a companion control pipe")
    flags = os.O_RDONLY if mode == "r" else os.O_WRONLY
    return os.fdopen(msvcrt.open_osfhandle(handle, flags), mode, encoding="utf-8", buffering=1)


def _read_commands(stream, commands, exit_process=os._exit):
    try:
        for line in stream:
            commands.put(json.loads(line))
    finally:
        # EOF must close the host even while the GUI is importing or hung.
        exit_process(0)


def _dashboard_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.path != "/dashboard" or parsed.username or parsed.password):
        raise ValueError("Dashboard host accepts only the companion's local dashboard")
    return parsed


def run():
    control = _pipe_stream(sys.stdin, -10, "r")
    output = _pipe_stream(sys.stdout, -11, "w")
    commands = queue.Queue()
    threading.Thread(target=_read_commands, args=(control, commands), daemon=True,
                     name="DashboardLifetime").start()
    first = commands.get()
    url = first["url"]
    origin = _dashboard_url(url)

    def emit(event):
        output.write(json.dumps(event) + "\n")
        output.flush()

    try:
        import webview
        webview.settings["ALLOW_DOWNLOADS"] = True
        webview.settings["ALLOW_FILE_URLS"] = False
        window = webview.create_window("LS Companion", url, width=1180, height=820,
                                       min_size=(800, 600), background_color="#141820",
                                       text_select=True)

        def shown():
            emit({"event": "ready", "hwnd": int(window.native.Handle.ToInt64())})

        window.events.shown += shown

        def dispatch():
            while True:
                command = commands.get()
                parsed = _dashboard_url(command["url"])
                if parsed.netloc != origin.netloc:
                    continue
                window.restore()
                window.show()
                profile_id = parse_qs(parsed.query).get("editProfile", [None])[0]
                if profile_id:
                    detail = {"profileId": profile_id,
                              "section": parse_qs(parsed.query).get("profileSection", [None])[0]}
                    window.evaluate_js("window.dispatchEvent(new CustomEvent('companion-edit-profile', "
                                       + "{detail: " + json.dumps(detail) + "}))")
                    continue
                # Focus without reloading the page or losing unsaved editors.
                requested_view = parse_qs(parsed.query).get("view", ["profiles"])[0]
                view = requested_view if requested_view in {"settings", "addons"} else "profiles"
                window.evaluate_js("window.dispatchEvent(new CustomEvent('companion-navigate', "
                                   + "{detail: " + json.dumps(view) + "}))")

        local_root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
        storage = local_root / "LosslessScalingHelper" / "DashboardWebView"
        # Each GUI library gets its own main thread. Force WebView2 rather than
        # silently falling back to MSHTML, which cannot render this dashboard.
        webview.start(dispatch, gui="edgechromium", private_mode=False, storage_path=str(storage))
        return 0
    except Exception as error:
        emit({"event": "error", "message":
              "Could not open the dashboard. Install the app's Python dependencies and "
              "Microsoft Edge WebView2 Runtime, then try again.\n\n" + str(error)})
        return 1
