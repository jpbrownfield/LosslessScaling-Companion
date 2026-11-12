"""Connect verified ReShade interaction telemetry to Companion's overlay."""
from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes
from .input_simulator import InputSimulator
from .overlay_shortcuts import (DEFAULT_OVERLAY_HOTKEYS, ACTION_LABELS, parse_shortcut,
                                special_k_menu_hotkey)


def menu_hotkey_label(status: dict, fallback: str) -> str:
    vk = status.get("hotkeyVk")
    if type(vk) is not int or not 1 <= vk <= 255:
        return str(fallback).upper()
    names = {8: "Backspace", 9: "Tab", 13: "Enter", 19: "Pause", 20: "Caps Lock",
             27: "Esc", 32: "Space", 33: "Page Up", 34: "Page Down", 35: "End",
             36: "Home", 37: "Left", 38: "Up", 39: "Right", 40: "Down",
             45: "Insert", 46: "Delete"}
    key = (chr(vk) if 48 <= vk <= 57 or 65 <= vk <= 90 else f"F{vk - 111}" if 112 <= vk <= 135
           else names.get(vk, f"VK {vk}"))
    return "+".join([label for flag, label in (("ctrl", "Ctrl"), ("alt", "Alt"), ("shift", "Shift"))
                     if status.get(flag) is True] + [key])


class ReShadeOverlayController:
    def __init__(self, manager, state, indicator, listener=None, open_profile=None, foreground_check=None,
                 performance_graph=None, open_addon_panel=None):
        self.manager, self.state, self.indicator = manager, state, indicator
        self.listener, self.open_profile = listener, open_profile
        self.performance_graph = performance_graph
        self.open_addon_panel = open_addon_panel
        self.foreground_check = foreground_check or self._foreground_allowed
        self.active = False
        self.profile_id = None
        self.host_pid = None
        self._session = None
        self._manual_active = None
        self._native_active = False
        self._last_status = {}
        self._lock = threading.RLock()
        self._special_k_loaded = False
        self._special_k_binding = "ctrl+shift+backspace"
        self._special_k_custom = False
        self._callbacks = {name: (lambda name=name: self.dispatch_action(name)) for name in DEFAULT_OVERLAY_HOTKEYS}

    def _current_profile(self):
        return self.state.current_active_profile or next(
            (profile for profile in self.manager.config.profiles if profile.is_default), None)

    def eligible(self) -> bool:
        profile, config = self._current_profile(), self.manager.config
        return bool(profile and self.state.is_scaling_active
                    and config.overlay_indicator_enabled and not self.state.benchmark_mode_active
                    and not self.state.update_in_progress and not self.manager.repair_session)

    def update(self, status: dict) -> None:
        with self._lock:
            self._update(status)

    def _update(self, status: dict) -> None:
        self._last_status = status
        eligible = self.eligible()
        profile = self._current_profile()
        session = ((profile.id, self.state.scaling_target_pid, self.state.scaling_target_hwnd,
                    self.state.last_scale_toggle_time) if eligible else None)
        if session != self._session:
            self._session = session
            self._manual_active = None
            self._native_active = False
        reshade = status.get("reshade") or {}
        if not isinstance(reshade, dict):
            reshade = {}
        uses_reshade = bool(profile and (profile.graphics.reshade.enabled or (profile.reshade and profile.reshade.enabled)))
        native_active = bool(eligible and uses_reshade and status.get("available") is True
                             and reshade.get("interactive") is True)
        if native_active != self._native_active:
            self._manual_active = None
        self._native_active = native_active
        active = bool(eligible and (native_active if self._manual_active is None else self._manual_active))
        target = self.state.current_scaled_target or {}
        anchor = (reshade.get("presentationHwnd") or self.state.scaling_target_hwnd or target.get("hwnd"))
        if type(anchor) is not int or anchor <= 0:
            active = False
        was_active = self.active
        self.active = active
        self.profile_id = profile.id if eligible else None
        self.host_pid = status.get("pid") if eligible else None
        self._special_k_loaded = bool(active and profile.graphics.special_k.enabled
                                      and any(item.get("id") == "special-k" and item.get("loaded") is True
                                              for item in status.get("components", []) if isinstance(item, dict)))
        if self._special_k_loaded:
            self._special_k_binding, self._special_k_custom = special_k_menu_hotkey(self.manager.config.lossless_scaling_exe_path)
        bindings = {**DEFAULT_OVERLAY_HOTKEYS, **self.manager.config.overlay_hotkeys}
        actions = {}
        hints = []
        if active and uses_reshade:
            hints.append({"key": menu_hotkey_label(reshade, self.manager.config.reshade_overlay_hotkey),
                          "label": "Toggle ReShade menu"})
        for name in DEFAULT_OVERLAY_HOTKEYS:
            if (name == "overlay_toggle" and eligible) or (name != "overlay_toggle" and active):
                binding = bindings[name]
                if (name == "special_k_panel" and (not self._special_k_loaded or self._special_k_custom)
                        or name == "addon_manager" and (self.open_addon_panel is None or not self.manager.config.addon_manager_enabled)
                        or name == "performance_graph" and self.performance_graph is None):
                    continue
                try:
                    parsed = parse_shortcut(binding)
                except ValueError:
                    continue
                native_mask = sum(mask for key, mask in (("ctrl", 2), ("alt", 1), ("shift", 4)) if reshade.get(key) is True)
                if parsed and parsed[:2] != (native_mask, reshade.get("hotkeyVk")):
                    actions[name] = {"binding": binding, "callback": self._callbacks[name]}
        if self.listener is not None:
            if not self.foreground_check():
                actions = {}
            self.listener.set_overlay_actions(actions)
            for name, item in actions.items():
                if active and self.listener.overlay_registered.get(name) == item["binding"]:
                    hints.append({"key": parse_shortcut(item["binding"])[2], "label": ACTION_LABELS[name]})
        if active and self._special_k_loaded and self._special_k_custom:
            hints.append({"key": parse_shortcut(self._special_k_binding)[2], "label": "Special K panel"})
        self.indicator.set_reshade_context(active, anchor_hwnd=anchor if active else None, hints=hints)
        if was_active and not active:
            if self.performance_graph is not None:
                self.performance_graph(False)
            if eligible:
                self.indicator.log("Overlay closed", kind="info")

    def _foreground_allowed(self) -> bool:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if not hwnd:
            return False
        pid = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
        return pid.value in (self.host_pid, self.state.scaling_target_pid)

    def dispatch_action(self, action: str) -> bool:
        with self._lock:
            return self._dispatch_action(action)

    def _dispatch_action(self, action: str) -> bool:
        profile = self._current_profile()
        if (not self.eligible() or not profile or profile.id != self.profile_id
                or self._session != (profile.id, self.state.scaling_target_pid,
                                     self.state.scaling_target_hwnd, self.state.last_scale_toggle_time)
                or not self.foreground_check()):
            return False
        if action == "overlay_toggle":
            self._manual_active = not self.active
            self.update(self._last_status)
            return True
        if not self.active:
            return False
        if action == "profile_editor" and self.open_profile is not None:
            return bool(self.open_profile(profile.id, None))
        if action == "addon_manager" and self.open_addon_panel is not None and self.manager.config.addon_manager_enabled:
            return bool(self.open_addon_panel())
        if action == "performance_graph" and self.performance_graph is not None:
            return bool(self.performance_graph())
        if action == "special_k_panel" and self._special_k_loaded:
            parts = self._special_k_binding.split("+")
            return InputSimulator.trigger_hotkey(parts[:-1], parts[-1])
        return False

    def clear(self) -> None:
        with self._lock:
            self._manual_active = None
            self._update({"available": False})
            self._session = None
            self.profile_id = None
            if self.listener is not None:
                self.listener.set_overlay_actions({})
