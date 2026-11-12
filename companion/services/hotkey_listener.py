"""Thread-owned Windows global hotkey used to proxy Lossless Scaling activation."""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from ctypes import wintypes
from typing import Callable, Optional, Tuple

from ..core.profile_manager import ProfileManager
from ..core.state import AppState
from .input_simulator import InputSimulator
from .overlay_shortcuts import DEFAULT_OVERLAY_HOTKEYS, parse_shortcut

logger = logging.getLogger("LSCompanion.HotkeyListener")

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
WM_REFRESH_HOTKEY = 0x8001
PM_NOREMOVE = 0x0000
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
HOTKEY_ID = 0x4C53
OVERLAY_HOTKEY_BASE = 0x4D00

_MODIFIERS = {
    "alt": MOD_ALT,
    "ctrl": MOD_CONTROL,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
}


class _KeyboardEvent(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("extraInfo", ctypes.c_size_t)]


_KeyboardProc = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                                  wintypes.WPARAM, wintypes.LPARAM)


class GlobalHotkeyListener:
    """Register and refresh the user-facing proxy hotkey on a message-loop thread."""

    def __init__(
        self,
        profile_manager: ProfileManager,
        state: AppState,
        callback: Callable[[], None],
        native_stop_callback: Optional[Callable[[int, int], None]] = None,
    ):
        self.profile_manager = profile_manager
        self.state = state
        self.callback = callback
        self.native_stop_callback = native_stop_callback
        self._native_hook = None
        self._native_proc = None
        self._native_binding = None
        self._native_key_held = False
        self._stop_event = threading.Event()
        self._ready_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._thread_id: Optional[int] = None
        self._registered = False
        self._signature: Optional[Tuple] = None
        self._callback_lock = threading.Lock()
        self.overlay_registered = {}
        self._overlay_actions = {}
        self._overlay_signature = None
        self._overlay_pending = False
        self._overlay_retry_at = 0.0
        self._overlay_ids = {}
        self._overlay_locks = {name: threading.Lock() for name in DEFAULT_OVERLAY_HOTKEYS}

    def set_overlay_actions(self, actions: dict) -> None:
        if (actions != self._overlay_actions
                or self._overlay_pending and time.monotonic() >= self._overlay_retry_at):
            self._overlay_actions = dict(actions)
            self.refresh()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._ready_event.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="GlobalHotkeyListener"
        )
        self._thread.start()
        self._ready_event.wait(timeout=1)

    def refresh(self) -> None:
        """Wake the message loop to apply an updated hotkey configuration."""
        thread_id = self._thread_id
        if thread_id is not None:
            ctypes.windll.user32.PostThreadMessageW(
                thread_id, WM_REFRESH_HOTKEY, 0, 0
            )

    def stop(self) -> None:
        self._stop_event.set()
        thread_id = self._thread_id
        if thread_id is not None:
            ctypes.windll.user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0)
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)

    def _desired_signature(self) -> Tuple:
        config = self.profile_manager.config
        hotkey = config.override_hotkey
        return (
            bool(config.override_lossless_hotkey),
            tuple(hotkey.modifiers),
            hotkey.key,
        )

    def _sync_registration(self) -> None:
        self._sync_scaling_registration()
        self._sync_native_observer()
        actions = dict(self._overlay_actions)
        signature = tuple((name, item["binding"]) for name, item in sorted(actions.items()))
        changed = signature != self._overlay_signature
        if not changed and (not self._overlay_pending or time.monotonic() < self._overlay_retry_at):
            return
        if changed:
            for key_id in self._overlay_ids:
                ctypes.windll.user32.UnregisterHotKey(None, key_id)
            self.overlay_registered, self._overlay_ids = {}, {}
        registered, key_ids = dict(self.overlay_registered), dict(self._overlay_ids)
        pending = False
        for offset, name in enumerate(DEFAULT_OVERLAY_HOTKEYS):
            item = actions.get(name)
            if not item:
                continue
            if registered.get(name) == item["binding"]:
                continue
            try:
                parsed = parse_shortcut(item["binding"])
                if not parsed:
                    continue
                modifiers, vk, _label = parsed
                key_id = OVERLAY_HOTKEY_BASE + offset
                if ctypes.windll.user32.RegisterHotKey(None, key_id, modifiers | MOD_NOREPEAT, vk):
                    registered[name] = item["binding"]
                    key_ids[key_id] = name
                    logger.info("Registered overlay shortcut: %s (%s)", name, item["binding"])
                else:
                    pending = True
                    if changed:
                        logger.warning("Overlay shortcut unavailable; will retry: %s (%s)", name, item["binding"])
            except ValueError as error:
                logger.warning("Invalid overlay shortcut %s: %s", name, error)
        self.overlay_registered, self._overlay_ids = registered, key_ids
        self._overlay_signature = signature
        self._overlay_pending = pending
        self._overlay_retry_at = time.monotonic() + 1.0

    def _remove_native_observer(self) -> None:
        if self._native_hook:
            ctypes.windll.user32.UnhookWindowsHookEx(self._native_hook)
        self._native_hook = self._native_proc = self._native_binding = None
        self._native_key_held = False

    def _sync_native_observer(self) -> None:
        config = self.profile_manager.config
        if not self.native_stop_callback or config.override_lossless_hotkey:
            self._remove_native_observer()
            return
        hotkey = config.global_hotkey
        try:
            binding = parse_shortcut("+".join((*hotkey.modifiers, hotkey.key)))
        except ValueError:
            self._remove_native_observer()
            logger.warning("Cannot observe an invalid native scaling hotkey")
            return
        self._native_binding = binding[:2] if binding else None
        if not self._native_binding:
            self._remove_native_observer()
            return
        if self._native_hook:
            return
        u = ctypes.windll.user32
        u.SetWindowsHookExW.argtypes = [ctypes.c_int, _KeyboardProc, ctypes.c_void_p, wintypes.DWORD]
        u.SetWindowsHookExW.restype = ctypes.c_void_p
        u.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        u.CallNextHookEx.restype = ctypes.c_ssize_t
        u.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
        u.UnhookWindowsHookEx.restype = wintypes.BOOL
        u.GetAsyncKeyState.argtypes = [ctypes.c_int]
        u.GetAsyncKeyState.restype = ctypes.c_short

        def observe(code, message, pointer):
            try:
                if code == 0:
                    event = ctypes.cast(pointer, ctypes.POINTER(_KeyboardEvent)).contents
                    self._observe_native_key(message, event.vkCode, event.flags)
            except Exception:
                logger.exception("Could not observe the native scaling hotkey")
            # Always forward: LS keeps ownership of its native binding.
            return u.CallNextHookEx(None, code, message, pointer)

        self._native_proc = _KeyboardProc(observe)
        self._native_hook = u.SetWindowsHookExW(13, self._native_proc, None, 0)  # WH_KEYBOARD_LL
        if not self._native_hook:
            logger.warning("Could not observe the native Lossless Scaling hotkey")

    def _observe_native_key(self, message: int, vk: int, flags: int) -> None:
        # Ignore SendInput, including Companion's automatic scale/stop commands.
        if flags & 0x10 or not self._native_binding:
            return
        modifiers, key = self._native_binding
        if vk != key:
            return
        if message in (0x0101, 0x0105):  # WM_KEYUP / WM_SYSKEYUP
            self._native_key_held = False
            return
        if message not in (0x0100, 0x0104) or self._native_key_held:
            return
        self._native_key_held = True
        u = ctypes.windll.user32
        held = 0
        for mask, codes in ((MOD_CONTROL, (0x11,)), (MOD_ALT, (0x12,)),
                            (MOD_SHIFT, (0x10,)), (MOD_WIN, (0x5B, 0x5C))):
            if any(u.GetAsyncKeyState(code) & 0x8000 for code in codes):
                held |= mask
        if held == modifiers and self.state.is_scaling_active:
            # The callback only stores a candidate; the scaling observation
            # confirms the stop later. Keep this hook free of blocking work.
            self.native_stop_callback(self.state.scaling_target_pid, self.state.scaling_target_hwnd)

    def _sync_scaling_registration(self) -> None:
        signature = self._desired_signature()
        if signature == self._signature and (not signature[0] or self._registered):
            return
        if self._registered:
            ctypes.windll.user32.UnregisterHotKey(None, HOTKEY_ID)
            self._registered = False
        self._signature = signature
        enabled, modifiers, key = signature
        if not enabled:
            return
        modifier_mask = MOD_NOREPEAT
        for modifier in modifiers:
            modifier_mask |= _MODIFIERS[modifier]
        try:
            vk = InputSimulator.vk_from_string(key)
        except ValueError as error:
            logger.error("Cannot register override hotkey: %s", error)
            return
        self._registered = bool(
            ctypes.windll.user32.RegisterHotKey(None, HOTKEY_ID, modifier_mask, vk)
        )
        if self._registered:
            logger.info("Registered Lossless Scaling override hotkey: %s", "+".join((*modifiers, key)))
        else:
            error = ctypes.get_last_error()
            message = "The override hotkey is already in use by another application"
            self.state.scaling_control_status = {"state": "error", "message": message}
            logger.error("%s (Win32 error %s)", message, error)

    def _dispatch(self) -> None:
        if not self._callback_lock.acquire(blocking=False):
            logger.info("Override hotkey ignored while the previous activation is in progress")
            return
        logger.info("Override hotkey received; scheduling activation")
        # WM_HOTKEY arrives while the triggering keys may still be physically
        # held. Without a release window, the hidden LS hotkey can be emitted
        # as (for example) Alt+F24 instead of F24 and be ignored.
        release_delay = max(
            0,
            int(self.profile_manager.config.override_hotkey.activation_delay_ms),
        ) / 1000.0

        def invoke() -> None:
            try:
                if release_delay and self._stop_event.wait(release_delay):
                    return
                self.callback()
            except Exception:
                logger.exception("Override hotkey callback failed")
            finally:
                self._callback_lock.release()

        threading.Thread(target=invoke, daemon=True, name="HotkeyActivation").start()

    def _dispatch_overlay(self, name: str) -> None:
        item = self._overlay_actions.get(name)
        lock = self._overlay_locks[name]
        if not item or not lock.acquire(False):
            return

        def invoke():
            try:
                if not self._stop_event.wait(0.15) and self._overlay_actions.get(name) == item:
                    result = item["callback"]()
                    logger.info("Overlay shortcut received: %s (%s)", name,
                                "ignored by current session" if result is False else "handled")
            except Exception:
                logger.exception("Overlay shortcut callback failed: %s", name)
            finally:
                lock.release()

        threading.Thread(target=invoke, daemon=True, name="OverlayShortcut").start()

    def _run(self) -> None:
        message = wintypes.MSG()
        self._thread_id = int(ctypes.windll.kernel32.GetCurrentThreadId())
        # Force Windows to create this thread's message queue before start()
        # returns and another thread can request refresh or shutdown.
        ctypes.windll.user32.PeekMessageW(
            ctypes.byref(message), None, 0, 0, PM_NOREMOVE
        )
        try:
            self._sync_registration()
            self._ready_event.set()
            while not self._stop_event.is_set():
                result = ctypes.windll.user32.GetMessageW(
                    ctypes.byref(message), None, 0, 0
                )
                if result <= 0:
                    break
                if message.message == WM_HOTKEY and message.wParam == HOTKEY_ID:
                    self._dispatch()
                elif message.message == WM_HOTKEY and message.wParam in self._overlay_ids:
                    self._dispatch_overlay(self._overlay_ids[message.wParam])
                elif message.message == WM_REFRESH_HOTKEY:
                    self._sync_registration()
        finally:
            self._ready_event.set()
            self._remove_native_observer()
            if self._registered:
                ctypes.windll.user32.UnregisterHotKey(None, HOTKEY_ID)
                self._registered = False
            for key_id in self._overlay_ids:
                ctypes.windll.user32.UnregisterHotKey(None, key_id)
            self._overlay_ids.clear()
            self.overlay_registered = {}
            self._overlay_signature = None
            self._overlay_pending = False
            self._thread_id = None
