"""Load our early-window guard; no LS/Echo/ReShade DLL names are replaced."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
from pathlib import Path
import sys
import threading
from .external_process import external_dll_search
from .managed_runtime import launch_environment, await_isolation

logger = logging.getLogger(__name__)
_libraries: dict[Path, object] = {}
_lock = threading.Lock()


def guard_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "tools" / "LSC_StartupGuard.dll"
    built = Path(__file__).resolve().parents[2] / "build" / "native" / "LSC_StartupGuard.dll"
    if built.is_file():
        return built
    try:
        from .test_binaries import candidate as _test_binary

        cached = _test_binary("tools/LSC_StartupGuard.dll")
        if cached.is_file():
            return cached
    except Exception:
        pass
    return Path(__file__).resolve().parents[2] / "build" / "native" / "LSC_StartupGuard.dll"


def launch_hidden(executable: Path, *, duration_ms: int = 6000,
                  adapter_report_dir: Path | None = None,
                  probe_token: str | None = None) -> int | None:
    """Return the new PID, or None to use the existing hidden-launch fallback.

    Register a process-specific in-context event hook before resuming the child.
    Keep the library loaded in Companion until exit; the target pins its own
    module while its short-lived window subclass is installed.
    """
    path = guard_path()
    environment, receipt, token = launch_environment(executable)
    if not path.is_file():
        logger.warning("Startup guard DLL unavailable; using post-show hiding: %s", path)
        return None
    try:
        with _lock:
            library = _libraries.get(path)
            if library is None:
                library = ctypes.WinDLL(str(path), use_last_error=True)
                library.LSCLaunchHidden.argtypes = [
                    wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                    ctypes.POINTER(ctypes.c_void_p),
                ]
                library.LSCLaunchHidden.restype = wintypes.BOOL
                library.LSCReleaseStartupGuard.argtypes = [ctypes.c_void_p]
                library.LSCReleaseStartupGuard.restype = None
                _libraries[path] = library
        pid = wintypes.DWORD()
        session = ctypes.c_void_p()
        with external_dll_search():
            if receipt is not None:
                launch = library.LSCLaunchHiddenWithEnvironment
                launch.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPCWSTR, wintypes.DWORD,
                                   wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD),
                                   ctypes.POINTER(ctypes.c_void_p)]
                launch.restype = wintypes.BOOL
                block = "\0".join(f"{key}={value}" for key, value in sorted(environment.items(), key=lambda pair: pair[0].casefold())) + "\0\0"
                launched = launch(str(executable.resolve()), duration_ms, block, len(block),
                                  str(adapter_report_dir or environment["LSC_ADAPTER_REPORT_DIR"]), probe_token or "",
                                  ctypes.byref(pid), ctypes.byref(session))
            elif adapter_report_dir is not None:
                probe = library.LSCLaunchHiddenWithReport
                probe.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPCWSTR,
                                  wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD),
                                  ctypes.POINTER(ctypes.c_void_p)]
                probe.restype = wintypes.BOOL
                launched = probe(str(executable.resolve()), duration_ms,
                                 str(adapter_report_dir.resolve()), probe_token,
                                 ctypes.byref(pid), ctypes.byref(session))
            else:
                launched = library.LSCLaunchHidden(str(executable.resolve()), duration_ms,
                                                   ctypes.byref(pid), ctypes.byref(session))
            if not launched:
                raise ctypes.WinError(ctypes.get_last_error())

    except (OSError, AttributeError) as error:
        logger.warning("Early-window guard unavailable; using post-show hiding: %s", error)
        return None

    def release() -> None:
        library.LSCReleaseStartupGuard(session)

    try:
        await_isolation(pid.value, receipt, token)
    except Exception:
        release()
        raise

    try:
        timer = threading.Timer((duration_ms + 1000) / 1000, release)
        timer.name = f"LSStartupGuard-{pid.value}"
        timer.daemon = True
        timer.start()
    except RuntimeError:
        # The child has already resumed; never fall through and launch it twice.
        release()
        logger.exception("Could not schedule startup-hook cleanup")
    logger.info("Lossless Scaling PID %s started with pre-show DLL guard", pid.value)
    return pid.value
