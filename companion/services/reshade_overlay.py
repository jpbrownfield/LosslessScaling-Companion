"""Connect verified ReShade interaction telemetry to Companion's overlay."""
from __future__ import annotations

import ctypes
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
                 performance_graph=None):
        self.manager, self.state, self.indicator = manager, state, indicator
        self.listener, self.open_profile = listener, open_profile
        self.performance_graph = performance_graph
        self.foreground_check = foreground_check or self._foreground_allowed
        self.active = False
        self.profile_id = None
        self.host_pid = None
        self._special_k_loaded = False
        self._special_k_binding = "ctrl+shift+backspace"
        self._special_k_custom = False
        self._callbacks = {name: (lambda name=name: self.dispatch_action(name)) for name in DEFAULT_OVERLAY_HOTKEYS}

    def eligible(self) -> bool:
        profile, config = self.state.current_active_profile, self.manager.config
        return bool(profile and self.state.is_scaling_active and config.addon_manager_enabled
                    and config.overlay_indicator_enabled and not self.state.benchmark_mode_active
                    and not self.state.update_in_progress and not self.manager.repair_session
                    and (profile.graphics.reshade.enabled or (profile.reshade and profile.reshade.enabled)))

    def update(self, status: dict) -> None:
        reshade = status.get("reshade") or {}
        if not isinstance(reshade, dict):
            reshade = {}
        active = bool(self.eligible() and status.get("available") is True
                      and reshade.get("interactive") is True)
        target = self.state.current_scaled_target or {}
        anchor = (reshade.get("presentationHwnd") or self.state.scaling_target_hwnd or target.get("hwnd"))
        if type(anchor) is not int or anchor <= 0:
            active = False
        was_active = self.active
        self.active = active
        self.profile_id = self.state.current_active_profile.id if active else None
        self.host_pid = status.get("pid") if active else None
        self._special_k_loaded = bool(active and self.state.current_active_profile.graphics.special_k.enabled
                                      and any(item.get("id") == "special-k" and item.get("loaded") is True
                                              for item in status.get("components", []) if isinstance(item, dict)))
        if self._special_k_loaded:
            self._special_k_binding, self._special_k_custom = special_k_menu_hotkey(self.manager.config.lossless_scaling_exe_path)
        bindings = {**DEFAULT_OVERLAY_HOTKEYS, **self.manager.config.overlay_hotkeys}
        actions = {}
        hints = []
        if active:
            hints.append({"key": menu_hotkey_label(reshade, self.manager.config.reshade_overlay_hotkey),
                          "label": "Toggle ReShade menu"})
            for name in DEFAULT_OVERLAY_HOTKEYS:
                binding = bindings[name]
                if (name == "special_k_panel" and (not self._special_k_loaded or self._special_k_custom)
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
            self.listener.set_overlay_actions(actions)
            for name, item in actions.items():
                if self.listener.overlay_registered.get(name) == item["binding"]:
                    hints.append({"key": parse_shortcut(item["binding"])[2], "label": ACTION_LABELS[name]})
        if active and self._special_k_loaded and self._special_k_custom:
            hints.append({"key": parse_shortcut(self._special_k_binding)[2], "label": "Special K panel"})
        self.indicator.set_reshade_context(active, anchor_hwnd=anchor if active else None, hints=hints)
        if was_active and not active:
            if self.performance_graph is not None:
                self.performance_graph(False)
            self.indicator.log("Overlay closed", kind="info")

    def _foreground_allowed(self) -> bool:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if not hwnd:
            return False
        pid = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
        return pid.value in (self.host_pid, self.state.scaling_target_pid)

    def dispatch_action(self, action: str) -> bool:
        profile = self.state.current_active_profile
        if (not self.active or not self.eligible() or not profile or profile.id != self.profile_id
                or not self.foreground_check()):
            return False
        if action in {"profile_editor", "addon_manager"} and self.open_profile is not None:
            return bool(self.open_profile(profile.id, "addons" if action == "addon_manager" else None))
        if action == "performance_graph" and self.performance_graph is not None:
            return bool(self.performance_graph())
        if action == "special_k_panel" and self._special_k_loaded:
            parts = self._special_k_binding.split("+")
            return InputSimulator.trigger_hotkey(parts[:-1], parts[-1])
        return False

    def clear(self) -> None:
        self.update({"available": False})
