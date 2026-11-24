"""Bounded telemetry and overlay input control for Companion's in-LS host."""
from __future__ import annotations
import ctypes
from ctypes import wintypes
import json
import math
import os
import re
import logging
import struct
import time

MANAGED_HOST_VERSION = "build-7-lsc.14"
MANAGED_HOST_ARCHIVE = "LSAddonManager-Companion.zip"
MAX_STATUS_BYTES = 262144
logger = logging.getLogger("LSCompanion.AddonHost")


def scaling_runtime_error(status):
    """Read explicit failures from cached telemetry; idle capture is not an error."""
    if not status.get("available"):
        return ""
    if status.get("startupError"):
        return str(status["startupError"])
    backend = status.get("neuralBackend")
    active_nr = "LSC-AMDNR" if backend == "AMD" else "DLSSNR02"
    neural_enabled = False
    for addon in status.get("addons", []):
        if not addon.get("enabled"):
            continue
        if addon.get("id") in {"DLSSNR02", "LSC-AMDNR"}:
            if addon.get("id") != active_nr:
                continue
            neural_enabled = True
        if addon.get("faulted") or addon.get("error") or addon.get("level", 0) >= 2:
            message = addon.get("error") or addon.get("status") or "Addon failed"
            return f"{addon.get('name') or addon.get('id')}: {str(message).splitlines()[0]}"
    if neural_enabled:
        capture = status.get("capture") or {}
        return str(capture.get("hookError") or capture.get("error") or "")
    return ""


def addon_version_key(value: object) -> tuple:
    """Numeric ordering within a release family; never equate build and semver."""
    text = str(value or "").strip()
    build = re.fullmatch(r"build-(\d+)(?:-lsc\.(\d+))?", text)
    if build:
        return (2, int(build[1]), int(build[2] or 0))
    if re.fullmatch(r"v?\d+(?:\.\d+)+", text):
        return (1, *(int(part) for part in text.removeprefix("v").split(".")))
    return (0, text)


def validate_status(payload, instance, *, now=None):
    """Do not accept stale telemetry or another incarnation of the same PID."""
    if not isinstance(payload, dict) or payload.get("schema") != 1 or payload.get("pid") != instance[0]:
        raise ValueError("Host telemetry has an unexpected identity or schema")
    created, sampled = float(payload.get("createdAt", 0)), float(payload.get("sampledAt", 0))
    current = time.time() if now is None else now
    if (not math.isfinite(created) or not math.isfinite(sampled) or abs(created - instance[1]) > 0.01
            or not -2 <= current - sampled <= 3):
        raise ValueError("Host telemetry is stale or belongs to another process instance")
    managed = payload.get("headless") is True or (payload.get("headless") is False and payload.get("managed") is True)
    if not managed or not re.fullmatch(r"build-\d+-lsc\.\d+", str(payload.get("hostVersion", ""))):
        raise ValueError("The running host does not implement Companion's bridge")
    if not isinstance(payload.get("addons"), list) or not isinstance(payload.get("metrics"), dict):
        raise ValueError("Host telemetry is malformed")
    return {**payload, "available": True}


def capture_trace_report(pid: int, *, expected_created_at: float | None = None) -> dict:
    import psutil

    if pid <= 0:
        raise ValueError("LS_PID must be a positive process ID")
    try:
        instance = pid, psutil.Process(pid).create_time()
    except psutil.Error as error:
        raise ValueError(f"Cannot inspect LS process {pid}: {error}") from error
    if expected_created_at is not None and abs(instance[1] - expected_created_at) > 0.01:
        raise ValueError("Lossless Scaling restarted before capture trace collection")
    snapshots = []
    for sample in range(3):
        if sample:
            time.sleep(1)
        try:
            status = validate_status(_read_pipe(pid), instance)
            snapshots.append({key: status.get(key) for key in (
                "available", "pid", "createdAt", "sampledAt", "hostVersion", "startupError",
                "neuralBackend", "capture", "cursor", "presentationPacing", "addons")})
            snapshots[-1]["metrics"] = {key: value for key, value in status["metrics"].items()
                                        if key in {"DLSSNR02", "LSC-AMDNR"}}
        except (OSError, ValueError, TypeError, RecursionError) as error:
            snapshots.append({"available": False, "error": str(error)})
    return {"kind": "nr-capture-trace", "pid": pid,
            "traceSupported": any(isinstance(sample.get("capture"), dict)
                                  and sample["capture"].get("traceVersion") == 1 for sample in snapshots),
            "samples": snapshots}


def _read_pipe(pid, timeout_ms=500, *, acknowledge=None):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)

    class Overlapped(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                    ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]

    declarations = {
        "CreateFileW": ([wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE], wintypes.HANDLE),
        "WaitNamedPipeW": ([wintypes.LPCWSTR, wintypes.DWORD], wintypes.BOOL),
        "GetNamedPipeServerProcessId": ([wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)], wintypes.BOOL),
        "CreateEventW": ([ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
        "ReadFile": ([wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(Overlapped)], wintypes.BOOL),
        "WriteFile": ([wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(Overlapped)], wintypes.BOOL),
        "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
        "GetOverlappedResult": ([wintypes.HANDLE, ctypes.POINTER(Overlapped), ctypes.POINTER(wintypes.DWORD), wintypes.BOOL], wintypes.BOOL),
        "CancelIoEx": ([wintypes.HANDLE, ctypes.POINTER(Overlapped)], wintypes.BOOL),
        "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
    }
    for name, (arguments, result) in declarations.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = arguments, result
    name = rf"\\.\pipe\LSCompanionAddonStatus-{pid}"
    pipe = kernel.CreateFileW(name, 0xC0000000, 0, None, 3, 0x40000000, None)
    if pipe == ctypes.c_void_p(-1).value and ctypes.get_last_error() == 231:
        kernel.WaitNamedPipeW(name, min(timeout_ms, 100))
        pipe = kernel.CreateFileW(name, 0xC0000000, 0, None, 3, 0x40000000, None)
    if pipe == ctypes.c_void_p(-1).value:
        raise OSError("The managed addon host is not running")

    def transfer(function, buffer, size):
        operation = Overlapped()
        operation.hEvent = kernel.CreateEventW(None, True, False, None)
        if not operation.hEvent:
            raise OSError("Could not create host telemetry event")
        count = wintypes.DWORD()
        try:
            if not function(pipe, buffer, size, ctypes.byref(count), ctypes.byref(operation)):
                if ctypes.get_last_error() != 997:
                    raise OSError("Host telemetry transfer failed or exceeded its size limit")
                if kernel.WaitForSingleObject(operation.hEvent, timeout_ms) != 0:
                    kernel.CancelIoEx(pipe, ctypes.byref(operation))
                    kernel.GetOverlappedResult(pipe, ctypes.byref(operation), ctypes.byref(count), True)
                    raise TimeoutError("Host telemetry timed out")
                if not kernel.GetOverlappedResult(pipe, ctypes.byref(operation), ctypes.byref(count), False):
                    raise OSError("Host telemetry transfer failed")
            return count.value
        finally:
            kernel.CloseHandle(operation.hEvent)
    try:
        server_pid = wintypes.ULONG()
        if not kernel.GetNamedPipeServerProcessId(pipe, ctypes.byref(server_pid)) or server_pid.value != pid:
            raise ValueError("Host telemetry pipe is owned by another process")
        buffer = ctypes.create_string_buffer(MAX_STATUS_BYTES + 1)
        size = transfer(kernel.ReadFile, buffer, MAX_STATUS_BYTES + 1)
        if not 0 < size <= MAX_STATUS_BYTES:
            raise ValueError("Host telemetry exceeded its size limit")
        payload = json.loads(buffer.raw[:size])
        receipt = acknowledge(payload) if acknowledge is not None else b"1"
        if receipt not in (b"1", b"E", b"D", b"C", b"c", b"P", b"p") and not (
                isinstance(receipt, bytes) and ((len(receipt) == 16 and receipt[:4] == b"E\0\0\0") or
                    (len(receipt) == 32 and receipt[:4] == b"B\0\0\0"))):
            raise ValueError("Unsupported host receipt")
        transfer(kernel.WriteFile, ctypes.create_string_buffer(receipt), len(receipt))
        return payload
    finally:
        kernel.CloseHandle(pipe)


def runtime_status(executable):
    from .adapter_report import configured_process
    unavailable = {"available": False, "reason": "Start Lossless Scaling with Companion's addon host to see runtime status."}
    if os.name != "nt":
        return unavailable
    instance = configured_process(executable)
    if not instance:
        return unavailable
    try:
        payload = _read_pipe(instance[0])
    except (OSError, ValueError, TypeError, RecursionError) as error:
        # Manual Companion controls still need to recognize the configured LS
        # presentation window when an older host has no telemetry pipe.
        current = configured_process(executable)
        return {"available": False, "reason": str(error),
                **({"pid": instance[0]} if current == instance else {})}
    if configured_process(executable) != instance:
        return {"available": False, "reason": "Lossless Scaling restarted while reading host telemetry"}
    try:
        return validate_status(payload, instance)
    except (OSError, ValueError, TypeError, RecursionError) as error:
        return {"available": False, "reason": str(error), "pid": instance[0]}


def set_cursor_recovery(executable, enabled: bool, *, timeout=1.5):
    """Apply the global cursor preference to the identified running host."""
    from .adapter_report import configured_process
    if type(enabled) is not bool:
        raise ValueError("Cursor recovery state must be a boolean")
    instance = configured_process(executable)
    if not instance:
        return {"available": False, "reason": "Lossless Scaling is not running"}

    def acknowledge(payload):
        status = validate_status(payload, instance)
        if (status.get("cursor") or {}).get("controlProtocol") != 1:
            raise ValueError("Restart LS with the updated Companion host to apply cursor recovery")
        return b"C" if enabled else b"c"

    _read_pipe(instance[0], acknowledge=acknowledge)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if configured_process(executable) != instance:
            raise ValueError("Lossless Scaling restarted during the cursor recovery request")
        try:
            status = validate_status(_read_pipe(instance[0]), instance)
        except OSError:
            time.sleep(0.025)
            continue
        if configured_process(executable) != instance:
            raise ValueError("Lossless Scaling restarted during the cursor recovery request")
        if (status.get("cursor") or {}).get("recoveryEnabled") is enabled:
            logger.info("Custom game cursor recovery confirmed: enabled=%s; host PID=%s", enabled, instance[0])
            return status
        time.sleep(0.025)
    raise ValueError("The LS host did not confirm the cursor recovery setting")


def set_passthrough(executable, enabled: bool, *, expected_pid=None, target_pid=None, target_hwnd=None, timeout=1.5):
    """Request an explicit state and return only after fresh telemetry confirms it."""
    from .adapter_report import configured_process
    if type(enabled) is not bool:
        raise ValueError("Passthrough state must be a boolean")
    if (target_pid is not None or target_hwnd is not None) and not (
            type(target_pid) is int and 0 < target_pid <= 0xFFFFFFFF
            and type(target_hwnd) is int and 0 < target_hwnd <= 0xFFFFFFFFFFFFFFFF):
        raise ValueError("Overlay input needs a valid scaling target process and window")
    instance = configured_process(executable)
    if not instance or expected_pid is not None and instance[0] != expected_pid:
        raise ValueError("Lossless Scaling changed before the overlay request")

    def acknowledge(payload):
        status = validate_status(payload, instance)
        # The pipe's server PID and this fresh snapshot's creation time already
        # bind the request to this incarnation. Do not enumerate every process
        # inside the host's short delivery-receipt deadline.
        if (status.get("reshade") or {}).get("controlAvailable") is not True:
            if (status.get("reshade") or {}).get("controlError"):
                raise ValueError(status["reshade"]["controlError"])
            raise ValueError("Restart Lossless Scaling with the updated Companion host to control passthrough")
        if enabled and addon_version_key(status["hostVersion"]) < addon_version_key("build-7-lsc.6"):
            raise ValueError("Update the Companion add-on manager to build-7-lsc.6 or newer and restart Lossless Scaling before opening overlay input")
        if enabled and target_pid is not None:
            if (status.get("reshade") or {}).get("controlProtocol", 1) < 2:
                raise ValueError("Update the Companion add-on manager and restart LS to enable overlay input reliably")
            return struct.pack("<c3xIQ", b"E", target_pid, target_hwnd)
        return b"E" if enabled else b"D"

    logger.info("Overlay input request: enabled=%s; host PID=%s; target PID=%s, HWND=%s",
                enabled, instance[0], target_pid, target_hwnd)
    _read_pipe(instance[0], acknowledge=acknowledge)
    deadline = time.monotonic() + timeout
    status = None
    while time.monotonic() < deadline:
        if configured_process(executable) != instance:
            raise ValueError("Lossless Scaling restarted during the overlay request")
        try:
            status = validate_status(_read_pipe(instance[0]), instance)
        except OSError:
            # The host retires each pipe after its receipt. A reader can arrive
            # between DisconnectNamedPipe and the next CreateNamedPipe.
            time.sleep(0.025)
            continue
        if configured_process(executable) != instance:
            raise ValueError("Lossless Scaling restarted during the overlay request")
        reshade = status.get("reshade") or {}
        requested = reshade.get("controlRequested", reshade.get("interactive"))
        if (reshade.get("controlAvailable") is True and reshade.get("controlPending") is not True
                and requested is enabled):
            logger.info("Overlay input confirmed: requested=%s; interactive=%s; host=%s",
                        requested, reshade.get("interactive"), status.get("hostVersion"))
            return status
        if reshade.get("controlPending") is not True and reshade.get("controlError"):
            logger.error("Overlay input rejected by host: %s", reshade)
            raise ValueError(reshade["controlError"])
        time.sleep(0.025)
    detail = (status or {}).get("reshade", {})
    logger.error("Overlay input confirmation timed out: enabled=%s; host PID=%s; target PID=%s, HWND=%s; last status=%s",
                 enabled, instance[0], target_pid, target_hwnd, detail)
    raise TimeoutError(f"The addon host did not confirm overlay input (host {(status or {}).get('hostVersion', 'unknown')}; "
                       f"requested={detail.get('controlRequested')}, pending={detail.get('controlPending')}, "
                       f"interactive={detail.get('interactive')}). See Debug for details.")


def presentation_status(executable, instance, *, binding=None, enabled=None):
    """One bounded pipe transaction; confirmation arrives in the next snapshot.

    Every request validates the process incarnation, including creation time. No
    retries/sleeps in the controller's polling path and no work on game threads.
    """
    from .adapter_report import configured_process
    if configured_process(executable) != instance:
        raise ValueError("Configured LS process changed")

    def receipt(payload):
        status = validate_status(payload, instance)
        pacing = status.get("presentationPacing") or {}
        if pacing.get("version") != 2 or pacing.get("controlProtocol") != 1:
            raise ValueError("Update the Companion add-on host and restart LS for adaptive limiting")
        if enabled is not None:
            if type(enabled) is not bool:
                raise ValueError("Monitoring state must be boolean")
            return b"P" if enabled else b"p"
        if binding is not None:
            pid, source, output, context = binding
            if not (0 < pid <= 0xFFFFFFFF and all(type(x) is int and 0 < x <= 0xFFFFFFFFFFFFFFFF for x in (source, output, context))):
                raise ValueError("Invalid presentation binding")
            return struct.pack("<c3xIQQQ", b"B", pid, source, output, context)
        return b"1"

    payload = _read_pipe(instance[0], timeout_ms=100, acknowledge=receipt)
    if configured_process(executable) != instance:
        raise ValueError("LS restarted during telemetry read")
    return validate_status(payload, instance)
