"""Thread-owned Windows global hotkey used to proxy Lossless Scaling activation."""

from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import wintypes
from typing import Callable, Optional, Tuple

from ..core.profile_manager import ProfileManager
from ..core.state import AppState
from .input_simulator import InputSimulator

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
        self._ready_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._thread_id: Optional[int] = None
        self._registered = False
        self._signature: Optional[Tuple] = None
        self._callback_lock = threading.Lock()

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
                elif message.message == WM_REFRESH_HOTKEY:
                    self._sync_registration()
        finally:
            self._ready_event.set()
            if self._registered:
                ctypes.windll.user32.UnregisterHotKey(None, HOTKEY_ID)
                self._registered = False
            self._thread_id = None
