"""Read-only, bounded telemetry from Companion's in-LS FriedSnowman host."""
from __future__ import annotations
import ctypes
from ctypes import wintypes
import json
import math
import os
import re
import time

MANAGED_HOST_VERSION = "build-5-lsc.3"
MANAGED_HOST_ARCHIVE = "LSAddonManager-Companion.zip"
MAX_STATUS_BYTES = 262144


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
    if payload.get("headless") is not True or not re.fullmatch(r"build-\d+-lsc\.\d+", str(payload.get("hostVersion", ""))):
        raise ValueError("The running host does not implement Companion's bridge")
    if not isinstance(payload.get("addons"), list) or not isinstance(payload.get("metrics"), dict):
        raise ValueError("Host telemetry is malformed")
    return {**payload, "available": True}


def _read_pipe(pid, timeout_ms=500):
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
        raise OSError("The headless addon host is not running")

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
        transfer(kernel.WriteFile, ctypes.create_string_buffer(b"1"), 1)
        return json.loads(buffer.raw[:size])
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
        if configured_process(executable) != instance:
            raise ValueError("Lossless Scaling restarted while reading host telemetry")
        return validate_status(payload, instance)
    except (OSError, ValueError, TypeError, RecursionError) as error:
        return {"available": False, "reason": str(error)}
