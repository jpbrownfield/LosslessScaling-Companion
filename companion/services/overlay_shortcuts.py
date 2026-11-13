"""Configurable shortcuts for the temporary in-game controls."""
from __future__ import annotations

import configparser
from pathlib import Path
from ..core.keyboard_keys import KEY_LABELS
from .input_simulator import InputSimulator

DEFAULT_OVERLAY_HOTKEYS = {
    "overlay_toggle": "ctrl+home",
    "profile_editor": "delete", "addon_manager": "insert",
    "performance_graph": "=", "special_k_panel": "-",
}
ACTION_LABELS = {
    "overlay_toggle": "Close Companion overlay",
    "profile_editor": "Edit current profile", "addon_manager": "Advanced add-on panel",
    "performance_graph": "Performance graph", "special_k_panel": "Special K panel",
}
MODIFIERS = {"ctrl": 2, "alt": 1, "shift": 4, "win": 8}


def parse_shortcut(value: str):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("Overlay shortcuts must be key names or combinations such as Ctrl+F8")
    parts = [part.strip().lower() for part in value.split("+")]
    if parts == [""]:
        return None
    key, modifiers = parts[-1], parts[:-1]
    if not key or key in MODIFIERS or any(part not in MODIFIERS for part in modifiers) or len(set(modifiers)) != len(modifiers):
        raise ValueError(f"Invalid overlay shortcut: {value}")
    vk = InputSimulator.vk_from_string(key)
    mask = sum(MODIFIERS[part] for part in modifiers)
    label_key = {**KEY_LABELS, "pageup": "Page Up", "pagedown": "Page Down"}.get(key, key.title())
    label = "+".join([part.title() for part in MODIFIERS if part in modifiers] + [label_key])
    return mask, vk, label


def reshade_key_data(value: str) -> tuple[int, int, int, int]:
    """ReShade serializes a shortcut as virtual key, Ctrl, Shift, Alt."""
    parsed = parse_shortcut(value)
    if not parsed:
        raise ValueError("Enter a ReShade menu hotkey")
    mask, vk, _label = parsed
    if mask & MODIFIERS["win"]:
        raise ValueError("ReShade menu shortcuts support Ctrl, Alt and Shift; the Win modifier is unavailable")
    return vk, int(bool(mask & 2)), int(bool(mask & 4)), int(bool(mask & 1))


def validate_overlay_hotkeys(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULT_OVERLAY_HOTKEYS):
        raise ValueError("Unknown overlay hotkey setting")
    result = {**DEFAULT_OVERLAY_HOTKEYS, **value}
    seen = set()
    for key, binding in result.items():
        parsed = parse_shortcut(binding)
        if parsed:
            if parsed[:2] in seen:
                raise ValueError("Overlay shortcuts must use different keys")
            seen.add(parsed[:2])
        result[key] = binding.strip().lower()
    return result


def special_k_menu_hotkey(executable: str | None):
    """Read Special K's private osd.ini, including its separately saved binding."""
    binding = "ctrl+shift+backspace"
    if executable:
        path = Path(executable).parent / ".companion-special-k/Global/osd.ini"
        try:
            if path.stat().st_size > 1024 * 1024:
                return binding, False
            raw = path.read_bytes()
            parser = configparser.ConfigParser(interpolation=None, strict=False)
            parser.read_string(raw.decode("utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"))
            selected = parser.get("ImGui.Global", "ControlPanelToggle", fallback="").strip()
            if selected:
                parsed = parse_shortcut(selected)
                if parsed:
                    return selected.lower(), parsed[:2] != parse_shortcut(binding)[:2]
        except (OSError, UnicodeError, ValueError, configparser.Error):
            pass
    return binding, False
