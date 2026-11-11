"""Five-second toast cards plus a scaling status border.

Each action gets its own fading card; surviving cards slide up as older
ones expire. A separate
click-through frame around the target window pulses blue while scaling is
pending (hotkey received or game detected) and maroon while scaling is
active. No injection, no hooks; every failure degrades to silent no-op.
"""
from __future__ import annotations

import os
import threading
import time as _time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional
from .toast_stack import TOAST_SECONDS

LINE_TTL_SECONDS = TOAST_SECONDS
MAX_LINES = 5
ERROR_TTL_SECONDS = LINE_TTL_SECONDS
STABLE_SECONDS = 3.0
MIN_GAME_AREA = 640 * 480
# Re-present at least this often while content is visible so the cards and
# border re-anchor if the game window moves (e.g. across monitors). The
# presenter re-reads the live window rect on every present.
REFOLLOW_SECONDS = 0.5

_ACCENTS = {
    "info": (200, 200, 200),
    "armed": (0, 120, 255),
    "hotkey": (255, 185, 0),
    "ok": (16, 192, 16),
    "error": (255, 80, 80),
}

_SPLASH_RE = None


def _splash_pattern():
    global _SPLASH_RE
    if _SPLASH_RE is None:
        import re
        _SPLASH_RE = re.compile(
            r"splash|loading|launcher|installer|patcher|patching|"
            r"patch notes|(^|\s)setup(\s|$| -)|"
            r"updat(er|ing)|crash|error report|configuration|settings",
            re.IGNORECASE)
    return _SPLASH_RE


@dataclass
class _Line:
    id: int
    text: str
    accent: tuple
    created_at: float
    expires_at: float


@dataclass
class _BorderRequest:
    state: Optional[str] = None  # "pending" | "active" | None
    anchor_hwnd: Optional[int] = None
    first_seen: float = 0.0
    ready_at: float = 0.0


def is_game_like_window(width: int, height: int, title: str) -> bool:
    """Splash screens and dialogs never earn the border.

    The autoscale trigger itself is unchanged (process-name matching); this
    only gates the attention-grabbing frame so launchers and splash art do
    not get outlined while the real game window settles.
    """
    try:
        area = int(width) * int(height)
    except (TypeError, ValueError):
        return False
    if area < MIN_GAME_AREA:
        return False
    if title and _splash_pattern().search(title):
        return False
    return True


def describe_window_default(hwnd: int):
    """(width, height, title) for a window, or None when unavailable."""
    try:
        from . import overlay_window as impl
        return impl.describe_window(hwnd)
    except Exception:
        return None


class OverlayState:
    """Pure state: timed toast messages plus one border request."""

    def __init__(self) -> None:
        self._lines: List[_Line] = []
        self._sequence = 0
        self._border = _BorderRequest()

    @property
    def current(self) -> List[_Line]:
        return list(self._lines)

    @property
    def border(self) -> _BorderRequest:
        return self._border

    def log(self, text: str, kind: str = "info",
            hold_seconds: Optional[float] = None,
            now: Optional[float] = None) -> None:
        moment = _time.monotonic() if now is None else now
        if hold_seconds is None:
            hold_seconds = (ERROR_TTL_SECONDS if kind == "error"
                            else LINE_TTL_SECONDS)
        accent = _ACCENTS.get(kind, _ACCENTS["info"])
        self._sequence += 1
        self._lines.append(_Line(id=self._sequence, text=text[:90], accent=accent,
                                 created_at=moment,
                                 expires_at=moment + hold_seconds))
        del self._lines[:-MAX_LINES]

    def clear(self) -> None:
        self._lines.clear()
        self._border = _BorderRequest()

    def set_border(self, state: Optional[str],
                   anchor_hwnd: Optional[int] = None,
                   now: Optional[float] = None) -> None:
        moment = _time.monotonic() if now is None else now
        if self._border.anchor_hwnd != anchor_hwnd:
            self._border = _BorderRequest(state=state, anchor_hwnd=anchor_hwnd,
                                          first_seen=moment)
        else:
            self._border.state = state

    def tick(self, now: Optional[float] = None) -> None:
        moment = _time.monotonic() if now is None else now
        self._lines = [line for line in self._lines
                       if moment < line.expires_at]


Presenter = Callable[[Optional[Dict]], None]


def null_presenter() -> Presenter:
    def present(_payload: Optional[Dict]) -> None:
        return None
    return present


def recording_presenter(path: str, inner: Presenter) -> Presenter:
    """Tap that journals every presented frame to JSONL, then forwards."""
    import json

    lock = threading.RLock()

    def present(payload: Optional[Dict]) -> None:
        try:
            with lock:
                with open(path, "a", encoding="utf-8") as stream:
                    stream.write(json.dumps({
                        "t": _time.time(),
                        "payload": payload,
                    }) + "\n")
        except Exception:
            pass
        try:
            inner(payload)
        except Exception:
            pass

    def close() -> None:
        closer = getattr(inner, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:
                pass

    present.close = close  # type: ignore[attr-defined]
    return present


def win32_presenter() -> Presenter:
    try:
        from . import overlay_window as overlay_window_module
        return overlay_window_module.Win32OverlayPresenter()
    except Exception:
        return null_presenter()


def production_indicator(profile_manager) -> "OverlayIndicator":
    """Overlay wiring for the real app: Win32 presenter, live pump.

    Unit tests construct AutomationController directly and get the safe
    default (no presenter, no pump thread); production entry points
    (main/tray/server) pass this explicitly so the on-screen indicator
    actually animates and follows the game window.
    """
    return OverlayIndicator(
        enabled=lambda: bool(profile_manager.config.overlay_indicator_enabled),
        pump=True,
    )


class OverlayIndicator:
    """Toast cards plus the scaling status border; never raises."""

    def __init__(self, presenter: Optional[Presenter] = None,
                 enabled: Optional[Callable[[], bool]] = None,
                 window_info: Optional[Callable[[int], object]] = None,
                 stable_seconds: float = STABLE_SECONDS,
                 pump: bool = False,
                 pump_interval: float = 0.25) -> None:
        self._state = OverlayState()
        self._line_anchor_hwnd: Optional[int] = None
        self._reshade_context: Optional[Dict] = None
        self._performance_graph: Optional[Dict] = None
        self._lock = threading.RLock()
        # NOTE: the Win32 presenter is created lazily on first real show so
        # unit tests (and headless runs) never create HWNDs or threads.
        self._presenter = presenter
        self._enabled = enabled or (lambda: True)
        self._window_info = window_info or describe_window_default
        self._stable_seconds = stable_seconds
        self._pump_enabled = pump
        self._pump_interval = max(0.05, pump_interval)
        self._pump_running = False
        self._pump_thread: Optional[threading.Thread] = None
        self._last_signature: Optional[tuple] = None
        self._last_present_at = 0.0

    def _get_presenter(self) -> Optional[Presenter]:
        """Return the presenter, creating the Win32 one on first use."""
        with self._lock:
            if self._presenter is None:
                if not self._pump_enabled and not getattr(
                        self, "_allow_lazy_presenter", False):
                    # Unit-test default: never create real windows or
                    # threads. Production passes pump=True; explicit
                    # presenters (including null_presenter) bypass this.
                    return None
                try:
                    created = win32_presenter()
                except Exception:
                    created = None
                if created is not None:
                    tap_path = os.environ.get("LS_OVERLAY_RECORD")
                    if tap_path and not getattr(created, "_tapped", False):
                        try:
                            created = recording_presenter(tap_path, created)
                            created._tapped = True  # type: ignore[attr-defined]
                        except Exception:
                            pass
                self._presenter = created
            return self._presenter

    # -- public API ------------------------------------------------------
    def set_performance_graph(self, data: Optional[Dict]) -> None:
        with self._lock:
            self._performance_graph = dict(data) if data is not None else None
        self.tick()

    def set_reshade_context(self, active: bool, anchor_hwnd: Optional[int] = None,
                            hints: Optional[List[Dict]] = None) -> None:
        with self._lock:
            previous = self._reshade_context
            if not active and previous is None:
                return
            since = previous["since"] if previous and previous["anchor"] == anchor_hwnd else _time.monotonic()
            self._reshade_context = ({"anchor": anchor_hwnd, "hints": hints or [], "since": since}
                                     if active else None)
        if active:
            self._ensure_pump()
        self.tick()

    def log(self, text: str, kind: str = "info",
            anchor_hwnd: Optional[int] = None,
            now: Optional[float] = None) -> None:
        try:
            with self._lock:
                if not self._enabled():
                    return
                if anchor_hwnd is not None:
                    self._line_anchor_hwnd = anchor_hwnd
                self._state.log(text, kind, now=now)
            self._ensure_pump()
            self._render(anchor_hwnd, now=now)
        except Exception:
            pass

    def set_border(self, state: Optional[str],
                   anchor_hwnd: Optional[int] = None,
                   now: Optional[float] = None,
                   immediate: bool = False) -> None:
        """Request the status frame; pending waits out splash screens.

        ``immediate`` skips the stability wait for user-initiated actions
        (hotkey) and confirmed scaling, where the target is already known
        good. Automatic pre-confirmation borders always wait.
        """
        try:
            with self._lock:
                if not self._enabled():
                    state = None
                moment = _time.monotonic() if now is None else now
                if (self._state.border.anchor_hwnd != anchor_hwnd
                        or self._state.border.state != state):
                    self._state.set_border(state, anchor_hwnd, now=moment)
                if state is None:
                    self._state.border.ready_at = moment
                elif immediate or state == "active":
                    self._state.border.ready_at = moment
                elif self._state.border.ready_at <= self._state.border.first_seen:
                    self._state.border.ready_at = (
                        self._state.border.first_seen + self._stable_seconds)
            if state is not None:
                self._ensure_pump()
            self._render(anchor_hwnd, now=now)
        except Exception:
            pass

    def hide(self) -> None:
        try:
            with self._lock:
                self._state.clear()
                self._reshade_context = None
                self._performance_graph = None
                self._line_anchor_hwnd = None
            self.tick()
        except Exception:
            pass

    def stop(self) -> None:
        try:
            with self._lock:
                self._pump_running = False
                pump_thread = self._pump_thread
                self._pump_thread = None
            self.hide()
            closer = getattr(self._presenter, "close", None)
            if callable(closer):
                closer()
            if (pump_thread is not None and pump_thread.is_alive()
                    and pump_thread is not threading.current_thread()):
                pump_thread.join(timeout=2.0)
        except Exception:
            pass

    def _ensure_pump(self) -> None:
        if not self._pump_enabled:
            return
        with self._lock:
            if self._pump_running:
                return
            self._pump_running = True
        thread = threading.Thread(target=self._pump_loop, daemon=True,
                                  name="LSOverlayPump")
        with self._lock:
            self._pump_thread = thread
        thread.start()

    def _pump_loop(self) -> None:
        idle_ticks = 0
        try:
            while True:
                _time.sleep(self._pump_interval)
                with self._lock:
                    if not self._pump_running:
                        return
                    active = (bool(self._state.current)
                              or self._state.border.state is not None
                              or self._reshade_context is not None)
                if not active:
                    idle_ticks += 1
                    if idle_ticks >= 8:
                        with self._lock:
                            self._pump_running = False
                        return
                    continue
                idle_ticks = 0
                try:
                    self.tick()
                except Exception:
                    return
        except Exception:
            try:
                with self._lock:
                    self._pump_running = False
            except Exception:
                pass

    # -- internals --------------------------------------------------------
    def _border_allowed(self, now: Optional[float] = None) -> Optional[int]:
        """Anchor hwnd when the border may show, else None."""
        request = self._state.border
        if request.state is None or not request.anchor_hwnd:
            return None
        moment = _time.monotonic() if now is None else now
        try:
            info = (self._window_info(int(request.anchor_hwnd))
                    if self._window_info else None)
        except Exception:
            return None
        if not info:
            return None
        width, height, title = info
        if not is_game_like_window(width, height, title or ""):
            return None
        if moment < request.ready_at:
            return None
        return int(request.anchor_hwnd)

    def tick(self, anchor_hwnd: Optional[int] = None,
             now: Optional[float] = None) -> None:
        try:
            with self._lock:
                self._state.tick(now=now)
                lines = self._state.current
                if not lines:
                    self._line_anchor_hwnd = None
                if anchor_hwnd is None:
                    anchor_hwnd = self._line_anchor_hwnd
                border_anchor = self._border_allowed(now=now)
                border_state = None
                if border_anchor is not None:
                    border_state = self._state.border.state
                context = self._reshade_context
                if context and context["anchor"]:
                    info = self._window_info(context["anchor"])
                    if info and is_game_like_window(*info):
                        border_anchor = context["anchor"]
                        border_state = "reshade"
            moment = _time.monotonic() if now is None else now
            pulse = bool(border_state) and (int(moment * 4) % 2 == 0)
            self._present(lines, border_state, border_anchor, anchor_hwnd,
                          pulse, now=moment, context=context)
        except Exception:
            pass

    def _render(self, anchor_hwnd: Optional[int] = None,
                now: Optional[float] = None) -> None:
        self.tick(anchor_hwnd=anchor_hwnd, now=now)

    def _present(self, lines: List[_Line], border_state: Optional[str],
                 border_anchor: Optional[int],
                 fallback_anchor: Optional[int],
                 pulse: bool = False,
                 now: Optional[float] = None,
                 context: Optional[Dict] = None) -> None:
        try:
            rendered_lines = [{"id": line.id, "text": line.text, "accent": line.accent,
                               "created_at": line.created_at, "expires_at": line.expires_at}
                              for line in lines]
            if context and border_state == "reshade":
                rendered_lines = [{"id": "reshade-status", "text": "Overlay active",
                                   "accent": (235, 183, 42), "persistent": True,
                                   "created_at": context["since"]}, *rendered_lines[-(MAX_LINES - 1):]]
            if not lines and border_state is None:
                payload = None
            else:
                payload = {
                    "lines": rendered_lines,
                    "border": border_state,
                    "border_anchor_hwnd": border_anchor,
                    "anchor_hwnd": (border_anchor if border_anchor is not None
                                    else fallback_anchor),
                    "pulse": pulse,
                    "hotkey_hints": context["hints"] if context and border_state == "reshade" else [],
                    "performance_graph": self._performance_graph if context and border_state == "reshade" else None,
                }
            signature = (tuple(line.id for line in lines),
                         border_state, border_anchor, fallback_anchor, pulse,
                         tuple((hint["key"], hint["label"]) for hint in (payload or {}).get("hotkey_hints", [])),
                         repr((payload or {}).get("performance_graph")))
            moment = _time.monotonic() if now is None else now
            with self._lock:
                changed = (signature != self._last_signature
                           or payload is None)
                due = (payload is not None and not changed
                       and moment - self._last_present_at >= REFOLLOW_SECONDS)
                if not (changed or due):
                    return
                if changed:
                    self._last_signature = signature
                enabled = self._enabled()
            if not enabled:
                payload = None
                with self._lock:
                    self._last_signature = ((), None, None, None)
            if payload is None and self._presenter is None:
                return  # never shown; don't create a window just to hide it
            presenter = self._get_presenter()
            if presenter is not None:
                presenter(payload)
                with self._lock:
                    self._last_present_at = moment
        except Exception:
            pass
