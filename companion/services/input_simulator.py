"""
Hardware-level Windows Input Simulator using ctypes & SendInput.
Provides ultra-reliable, low-latency keystroke injection for Lossless Scaling.
"""

import ctypes
import time
from typing import List, Union
from ctypes import wintypes

# Win32 Constants
INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008

# Key mapping table: Name -> Virtual Key Code
VK_MAPPINGS = {
    'ctrl': 0x11,
    'control': 0x11,
    'lctrl': 0xA2,
    'rctrl': 0xA3,
    'alt': 0x12,
    'menu': 0x12,
    'lalt': 0xA4,
    'ralt': 0xA5,
    'shift': 0x10,
    'lshift': 0xA0,
    'rshift': 0xA1,
    'win': 0x5B,
    'lwin': 0x5B,
    'rwin': 0x5C,
    'f1': 0x70, 'f2': 0x71, 'f3': 0x72, 'f4': 0x73,
    'f5': 0x74, 'f6': 0x75, 'f7': 0x76, 'f8': 0x77,
    'f9': 0x78, 'f10': 0x79, 'f11': 0x7A, 'f12': 0x7B,
    'home': 0x24, 'end': 0x23, 'pageup': 0x21, 'pagedown': 0x22,
    'insert': 0x2D, 'delete': 0x2E,
    'space': 0x20, 'enter': 0x0D, 'tab': 0x09, 'escape': 0x1B,
}

# Add a-z and 0-9
for c in range(ord('a'), ord('z') + 1):
    VK_MAPPINGS[chr(c)] = ord(chr(c).upper())
for c in range(ord('0'), ord('9') + 1):
    VK_MAPPINGS[chr(c)] = c

# Ctypes Structs for Win32 SendInput
ULONG_PTR = ctypes.c_ulong if ctypes.sizeof(ctypes.c_void_p) == 4 else ctypes.c_ulonglong

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR)
    ]

class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD)
    ]

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR)
    ]

class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("ki", KEYBDINPUT),
        ("mi", MOUSEINPUT),
        ("hi", HARDWAREINPUT)
    ]

class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", wintypes.DWORD),
        ("union", _INPUT_UNION)
    ]

user32 = ctypes.windll.user32
user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT
user32.MapVirtualKeyW.argtypes = (wintypes.UINT, wintypes.UINT)
user32.MapVirtualKeyW.restype = wintypes.UINT


class InputSimulator:
    @staticmethod
    def vk_from_string(key_name: str) -> int:
        clean = key_name.lower().strip()
        if clean in VK_MAPPINGS:
            return VK_MAPPINGS[clean]
        if len(clean) == 1:
            return ord(clean.upper())
        raise ValueError(f"Unknown key name: {key_name}")

    @classmethod
    def send_scancode_event(cls, vk_code: int, keyup: bool = False, is_extended: bool = False) -> None:
        scan_code = user32.MapVirtualKeyW(vk_code, 0)
        flags = KEYEVENTF_SCANCODE
        if keyup:
            flags |= KEYEVENTF_KEYUP
        if is_extended or vk_code in [0x5B, 0x5C, 0x24, 0x23, 0x21, 0x22, 0x2D, 0x2E]:
            flags |= KEYEVENTF_EXTENDEDKEY

        inp = INPUT()
        inp.type = INPUT_KEYBOARD
        inp.union.ki.wVk = 0
        inp.union.ki.wScan = scan_code
        inp.union.ki.dwFlags = flags
        inp.union.ki.time = 0
        inp.union.ki.dwExtraInfo = 0

        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

    @classmethod
    def trigger_hotkey(cls, modifiers: List[str], key: str, hold_ms: int = 50, activation_delay_ms: int = 0) -> None:
        """
        Sends a hardware-level hotkey combination (e.g. Ctrl + Alt + S).
        """
        if activation_delay_ms > 0:
            time.sleep(activation_delay_ms / 1000.0)

        mod_vks = [cls.vk_from_string(m) for m in modifiers]
        target_vk = cls.vk_from_string(key)

        try:
            # 1. Press all modifiers down
            for vk in mod_vks:
                cls.send_scancode_event(vk, keyup=False)
                time.sleep(0.01)

            # 2. Press primary key down
            cls.send_scancode_event(target_vk, keyup=False)

            # 3. Hold
            time.sleep(max(hold_ms, 30) / 1000.0)

            # 4. Release primary key
            cls.send_scancode_event(target_vk, keyup=True)
            time.sleep(0.01)

            # 5. Release modifiers in reverse order
            for vk in reversed(mod_vks):
                cls.send_scancode_event(vk, keyup=True)
                time.sleep(0.01)

        except Exception as e:
            print(f"[InputSimulator] Error triggering hotkey: {e}")
            # Ensure keys are released on failure
            for vk in [target_vk] + mod_vks:
                try:
                    cls.send_scancode_event(vk, keyup=True)
                except Exception:
                    pass
