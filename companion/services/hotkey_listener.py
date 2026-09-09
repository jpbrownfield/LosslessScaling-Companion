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

logger = logging.getLogger("LSCompanion.HotkeyListener")

WM_HOTKEY = 0x0312
PM_REMOVE = 0x0001
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
HOTKEY_ID = 0x4C53

_MODIFIERS = {
    "alt": MOD_ALT,
    "ctrl": MOD_CONTROL,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
}


class GlobalHotkeyListener:
    """Register and refresh the user-facing proxy hotkey on a message-loop thread."""

    def __init__(
        self,
        profile_manager: ProfileManager,
        state: AppState,
        callback: Callable[[], None],
    ):
        self.profile_manager = profile_manager
        self.state = state
        self.callback = callback
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._registered = False
        self._signature: Optional[Tuple] = None
        self._callback_lock = threading.Lock()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="GlobalHotkeyListener"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
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
            return

        def invoke() -> None:
            try:
                self.callback()
            except Exception:
                logger.exception("Override hotkey callback failed")
            finally:
                self._callback_lock.release()

        threading.Thread(target=invoke, daemon=True, name="HotkeyActivation").start()

    def _run(self) -> None:
        message = wintypes.MSG()
        try:
            while not self._stop_event.is_set():
                self._sync_registration()
                while ctypes.windll.user32.PeekMessageW(
                    ctypes.byref(message), None, 0, 0, PM_REMOVE
                ):
                    if message.message == WM_HOTKEY and message.wParam == HOTKEY_ID:
                        self._dispatch()
                time.sleep(0.05)
        finally:
            if self._registered:
                ctypes.windll.user32.UnregisterHotKey(None, HOTKEY_ID)
                self._registered = False
