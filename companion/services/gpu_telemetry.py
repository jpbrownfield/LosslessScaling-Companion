"""Low-frequency Windows GPU Engine telemetry using one persistent PDH query."""

from __future__ import annotations

import ctypes
import os
import re
import threading
from typing import Dict


PDH_FMT_DOUBLE = 0x00000200
PDH_MORE_DATA = 0x800007D2
ERROR_SUCCESS = 0


class _PdhValueUnion(ctypes.Union):
    _fields_ = [
        ("longValue", ctypes.c_long),
        ("doubleValue", ctypes.c_double),
        ("largeValue", ctypes.c_longlong),
        ("AnsiStringValue", ctypes.c_char_p),
        ("WideStringValue", ctypes.c_wchar_p),
    ]


class _PdhFormattedValue(ctypes.Structure):
    _anonymous_ = ("value",)
    _fields_ = [("CStatus", ctypes.c_ulong), ("value", _PdhValueUnion)]


class _PdhValueItem(ctypes.Structure):
    _fields_ = [("szName", ctypes.c_wchar_p), ("FmtValue", _PdhFormattedValue)]


class WindowsGpuTelemetry:
    """Return Task-Manager-style busiest-engine utilization grouped by PID."""

    COUNTER_PATH = r"\GPU Engine(*)\Utilization Percentage"
    _PID_PATTERN = re.compile(r"(?:^|_)pid_(\d+)(?:_|$)", re.IGNORECASE)

    def __init__(self):
        self._lock = threading.RLock()
        self._pdh = None
        self._query = ctypes.c_void_p()
        self._counter = ctypes.c_void_p()
        self._ready = False
        self._primed = False
        if os.name == "nt":
            self._open()

    @property
    def available(self) -> bool:
        return self._ready

    def _open(self) -> None:
        try:
            pdh = ctypes.WinDLL("pdh.dll")
            pdh.PdhOpenQueryW.argtypes = [ctypes.c_wchar_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_void_p)]
            pdh.PdhOpenQueryW.restype = ctypes.c_long
            pdh.PdhAddEnglishCounterW.argtypes = [
                ctypes.c_void_p,
                ctypes.c_wchar_p,
                ctypes.c_size_t,
                ctypes.POINTER(ctypes.c_void_p),
            ]
            pdh.PdhAddEnglishCounterW.restype = ctypes.c_long
            pdh.PdhCollectQueryData.argtypes = [ctypes.c_void_p]
            pdh.PdhCollectQueryData.restype = ctypes.c_long
            pdh.PdhGetFormattedCounterArrayW.argtypes = [
                ctypes.c_void_p,
                ctypes.c_ulong,
                ctypes.POINTER(ctypes.c_ulong),
                ctypes.POINTER(ctypes.c_ulong),
                ctypes.c_void_p,
            ]
            pdh.PdhGetFormattedCounterArrayW.restype = ctypes.c_long
            pdh.PdhCloseQuery.argtypes = [ctypes.c_void_p]
            pdh.PdhCloseQuery.restype = ctypes.c_long
            if self._status(pdh.PdhOpenQueryW(None, 0, ctypes.byref(self._query))) != ERROR_SUCCESS:
                return
            if self._status(
                pdh.PdhAddEnglishCounterW(
                    self._query, self.COUNTER_PATH, 0, ctypes.byref(self._counter)
                )
            ) != ERROR_SUCCESS:
                pdh.PdhCloseQuery(self._query)
                self._query = ctypes.c_void_p()
                return
            self._pdh = pdh
            self._ready = True
        except (AttributeError, OSError):
            self._ready = False

    def sample_by_pid(self) -> Dict[int, float]:
        with self._lock:
            if not self._ready or self._pdh is None:
                return {}
            if self._status(self._pdh.PdhCollectQueryData(self._query)) != ERROR_SUCCESS:
                return {}
            if not self._primed:
                self._primed = True
                return {}
            size = ctypes.c_ulong(0)
            count = ctypes.c_ulong(0)
            status = self._status(
                self._pdh.PdhGetFormattedCounterArrayW(
                    self._counter,
                    PDH_FMT_DOUBLE,
                    ctypes.byref(size),
                    ctypes.byref(count),
                    None,
                )
            )
            if status not in (PDH_MORE_DATA, ERROR_SUCCESS) or not size.value:
                return {}
            buffer = ctypes.create_string_buffer(size.value)
            status = self._status(
                self._pdh.PdhGetFormattedCounterArrayW(
                    self._counter,
                    PDH_FMT_DOUBLE,
                    ctypes.byref(size),
                    ctypes.byref(count),
                    buffer,
                )
            )
            if status != ERROR_SUCCESS:
                return {}
            items = ctypes.cast(buffer, ctypes.POINTER(_PdhValueItem))
            result: Dict[int, float] = {}
            for index in range(count.value):
                item = items[index]
                match = self._PID_PATTERN.search(item.szName or "")
                if not match or item.FmtValue.CStatus not in (ERROR_SUCCESS, 1):
                    continue
                value = max(0.0, min(100.0, float(item.FmtValue.doubleValue)))
                pid = int(match.group(1))
                result[pid] = max(result.get(pid, 0.0), value)
            return result

    def close(self) -> None:
        with self._lock:
            if self._pdh is not None and self._query:
                self._pdh.PdhCloseQuery(self._query)
            self._pdh = None
            self._query = ctypes.c_void_p()
            self._counter = ctypes.c_void_p()
            self._ready = False

    @staticmethod
    def _status(value: int) -> int:
        return int(value) & 0xFFFFFFFF


__all__ = ["WindowsGpuTelemetry"]
