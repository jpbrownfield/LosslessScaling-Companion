"""Toast lifetimes and smoothly collapsing layout, independent of Win32."""
from __future__ import annotations

from dataclasses import dataclass

TOAST_WIDTH = 420
TOAST_HEIGHT = 52
TOAST_GAP = 8
TOAST_RADIUS = 12
TOAST_SECONDS = 7.0
TOAST_MOVE_SECONDS = 0.24
MAX_TOASTS = 5


@dataclass
class Toast:
    key: object
    line: dict
    created_at: float
    expires_at: float
    source_y: float
    target_y: float
    move_at: float

    def y(self, now: float) -> float:
        progress = min(1.0, max(0.0, (now - self.move_at) / TOAST_MOVE_SECONDS))
        eased = 1 - (1 - progress) ** 3
        return self.source_y + (self.target_y - self.source_y) * eased

    def alpha(self, now: float) -> int:
        if self.line.get("persistent"):
            return 255
        duration = max(0.001, self.expires_at - self.created_at)
        remaining = (self.expires_at - now) / duration
        return round(255 * min(1.0, max(0.0, remaining)))


class ToastStack:
    def __init__(self):
        self.toasts: list[Toast] = []

    def clear(self):
        self.toasts.clear()

    def sync(self, lines: list[dict], now: float):
        existing = {toast.key: toast for toast in self.toasts}
        updated = []
        for index, line in enumerate(lines[-MAX_TOASTS:]):
            key = line.get("id", (index, line.get("text", "")))
            toast = existing.get(key)
            if toast is None:
                created = float(line.get("created_at", now))
                expires = (float("inf") if line.get("persistent")
                           else float(line.get("expires_at", created + TOAST_SECONDS)))
                target = len(updated) * (TOAST_HEIGHT + TOAST_GAP)
                toast = Toast(key, dict(line), created, expires,
                              target + 8, target, now)
            if now < toast.expires_at:
                updated.append(toast)
        self.toasts = updated
        return self.frames(now)

    def frames(self, now: float) -> list[Toast]:
        self.toasts = [toast for toast in self.toasts if now < toast.expires_at]
        for index, toast in enumerate(self.toasts):
            target = index * (TOAST_HEIGHT + TOAST_GAP)
            if target != toast.target_y:
                toast.source_y = toast.y(now)
                toast.target_y = target
                toast.move_at = now
        return list(self.toasts)
