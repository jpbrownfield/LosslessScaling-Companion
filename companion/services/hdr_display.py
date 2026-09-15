"""Read active HDR display luminance through DXGI without elevation."""

from __future__ import annotations

import ctypes
import os
import time
import uuid
from typing import Callable, Dict, List, Optional


DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020 = 12


class _Guid(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def from_string(cls, value: str) -> "_Guid":
        raw = uuid.UUID(value).bytes_le
        return cls.from_buffer_copy(raw)


class _Rect(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _DxgiOutputDesc1(ctypes.Structure):
    _fields_ = [
        ("DeviceName", ctypes.c_wchar * 32),
        ("DesktopCoordinates", _Rect),
        ("AttachedToDesktop", ctypes.c_int32),
        ("Rotation", ctypes.c_int32),
        ("Monitor", ctypes.c_void_p),
        ("BitsPerColor", ctypes.c_uint32),
        ("ColorSpace", ctypes.c_int32),
        ("RedPrimary", ctypes.c_float * 2),
        ("GreenPrimary", ctypes.c_float * 2),
        ("BluePrimary", ctypes.c_float * 2),
        ("WhitePoint", ctypes.c_float * 2),
        ("MinLuminance", ctypes.c_float),
        ("MaxLuminance", ctypes.c_float),
        ("MaxFullFrameLuminance", ctypes.c_float),
    ]


def _method(pointer: ctypes.c_void_p, index: int, result_type, *argument_types):
    table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    prototype = ctypes.WINFUNCTYPE(result_type, ctypes.c_void_p, *argument_types)
    return prototype(table[index])


def _release(pointer: ctypes.c_void_p) -> None:
    if pointer:
        _method(pointer, 2, ctypes.c_uint32)(pointer)


class HdrDisplayDetector:
    """Return the highest peak among active HDR outputs, with a short cache."""

    def __init__(
        self,
        output_enumerator: Optional[Callable[[], List[Dict]]] = None,
        cache_seconds: float = 30.0,
    ):
        self._output_enumerator = output_enumerator or self._enumerate_dxgi_outputs
        self._cache_seconds = cache_seconds
        self._cached_at = 0.0
        self._cached: Optional[Dict] = None

    def detect(self, *, force: bool = False) -> Dict:
        now = time.monotonic()
        if not force and self._cached is not None and now - self._cached_at < self._cache_seconds:
            return dict(self._cached)
        candidates = []
        try:
            outputs = self._output_enumerator()
        except (OSError, ValueError, ctypes.ArgumentError):
            outputs = []
        for output in outputs:
            peak = float(output.get("maxLuminanceNits") or 0)
            hdr_active = bool(output.get("hdrActive")) or int(output.get("colorSpace") or -1) == DXGI_COLOR_SPACE_RGB_FULL_G2084_NONE_P2020
            if hdr_active and 80 <= peak <= 10000:
                candidates.append({**output, "maxLuminanceNits": int(round(peak))})
        selected = max(candidates, key=lambda item: item["maxLuminanceNits"], default=None)
        result = {
            "available": selected is not None,
            "detectedNits": selected["maxLuminanceNits"] if selected else None,
            "displayName": str(selected.get("deviceName") or "") if selected else "",
            "activeHdrDisplays": len(candidates),
        }
        self._cached = result
        self._cached_at = now
        return dict(result)

    @staticmethod
    def _enumerate_dxgi_outputs() -> List[Dict]:
        if os.name != "nt":
            return []
        factory = ctypes.c_void_p()
        dxgi = ctypes.WinDLL("dxgi", use_last_error=True)
        create_factory = dxgi.CreateDXGIFactory1
        create_factory.argtypes = [ctypes.POINTER(_Guid), ctypes.POINTER(ctypes.c_void_p)]
        create_factory.restype = ctypes.c_long
        factory_iid = _Guid.from_string("770aae78-f26f-4dba-a829-253c83d1b387")
        if create_factory(ctypes.byref(factory_iid), ctypes.byref(factory)) < 0 or not factory:
            return []

        output6_iid = _Guid.from_string("068346e8-aaec-4b84-add7-137f513f77a1")
        results: List[Dict] = []
        try:
            adapter_index = 0
            while True:
                adapter = ctypes.c_void_p()
                if _method(factory, 12, ctypes.c_long, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p))(
                    factory, adapter_index, ctypes.byref(adapter)
                ) != 0:
                    break
                try:
                    output_index = 0
                    while True:
                        output = ctypes.c_void_p()
                        if _method(adapter, 7, ctypes.c_long, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p))(
                            adapter, output_index, ctypes.byref(output)
                        ) != 0:
                            break
                        try:
                            output6 = ctypes.c_void_p()
                            query = _method(
                                output, 0, ctypes.c_long,
                                ctypes.POINTER(_Guid), ctypes.POINTER(ctypes.c_void_p),
                            )
                            if query(output, ctypes.byref(output6_iid), ctypes.byref(output6)) == 0 and output6:
                                try:
                                    description = _DxgiOutputDesc1()
                                    get_desc = _method(
                                        output6, 27, ctypes.c_long,
                                        ctypes.POINTER(_DxgiOutputDesc1),
                                    )
                                    if get_desc(output6, ctypes.byref(description)) == 0 and description.AttachedToDesktop:
                                        results.append({
                                            "deviceName": description.DeviceName,
                                            "colorSpace": int(description.ColorSpace),
                                            "maxLuminanceNits": float(description.MaxLuminance),
                                            "maxFullFrameLuminanceNits": float(description.MaxFullFrameLuminance),
                                        })
                                finally:
                                    _release(output6)
                        finally:
                            _release(output)
                        output_index += 1
                finally:
                    _release(adapter)
                adapter_index += 1
        finally:
            _release(factory)
        return results
