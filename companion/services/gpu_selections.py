"""Persist hardware intent independently of volatile LS GPU/display numbers."""
from __future__ import annotations

import copy
import logging
import re

logger = logging.getLogger("LSCompanion.GpuSelections")


def _text(value):
    return str(value or "").strip().casefold()


def _number(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _hardware(value):
    # Driver/instance suffixes can change. Do not guess between two cards with
    # the same PCI model: every fallback match below must be unique.
    parts = re.findall(r"(?:ven|dev|subsys)_[0-9a-f]+", _text(value))
    return tuple(parts) if len(parts) >= 2 else None


def match_device(items, device_id, name, *, gpu=False):
    if device_id:
        exact = [item for item in items if _text(item.get("deviceId")) == _text(device_id)]
        if len(exact) == 1:
            return exact[0]
        if gpu and _hardware(device_id):
            physical = [item for item in items if _hardware(item.get("deviceId")) == _hardware(device_id)]
            if len(physical) == 1:
                return physical[0]
    if name and _text(name) != "unknown gpu" and not re.fullmatch(r"display \d+", _text(name)):
        named = [item for item in items if _text(item.get("name")) == _text(name)]
        if len(named) == 1:
            return named[0]
    return None


class GpuSelectionService:
    def __init__(self, profile_manager):
        self.manager = profile_manager
        self.warnings = []
        self._last_signature = None
        self._unsaved = False

    @staticmethod
    def _signature(config, inventory):
        # Do not repeatedly serialize potentially large shader/add-on configs
        # while foreground polling sees the same inventory and GPU choices.
        return (inventory, config.gpu_inventory_snapshot,
                config.preferred_scaling_gpu_device_id, config.preferred_scaling_gpu_name,
                config.auto_route_gpu_to_display,
                [(p.id, p.name, p.native_scaling_settings.get("PreferredGpuId"),
                  p.native_scaling_settings.get("OutputDisplayId"), p.preferred_gpu_device_id,
                  p.preferred_gpu_name, p.output_display_device_id, p.output_display_name)
                 for p in config.profiles])

    def refresh(self, inventory):
        """Remap helper profiles, never restart LS or write its live XML here."""
        if not isinstance(inventory, dict) or not inventory.get("gpus"):
            # A transient empty enumeration must not erase saved intent/history.
            return False
        config = self.manager.config
        if not self._unsaved and self._signature(config, inventory) == self._last_signature:
            return False
        before = config.model_dump()
        warnings = []
        previous = config.gpu_inventory_snapshot
        gpus = inventory["gpus"]
        displays = inventory.get("displays", [])
        if self._last_signature is None or previous != inventory:
            logger.info("Refreshed GPU inventory: %s", "; ".join(
                f"{gpu.get('name')} [LS GPU {gpu.get('lsGpuId')}, {gpu.get('deviceId')}]" for gpu in gpus))
        if config.preferred_scaling_gpu_device_id:
            old = match_device(previous.get("gpus", []), config.preferred_scaling_gpu_device_id, None, gpu=True)
            name = config.preferred_scaling_gpu_name or (old or {}).get("name")
            selected = match_device(gpus, config.preferred_scaling_gpu_device_id, name, gpu=True)
            if selected:
                if config.preferred_scaling_gpu_device_id != selected["deviceId"]:
                    logger.info("Rebound default scaling GPU '%s': %s -> %s (LS GPU %s)",
                                selected["name"], config.preferred_scaling_gpu_device_id,
                                selected["deviceId"], selected.get("lsGpuId"))
                config.preferred_scaling_gpu_device_id = selected["deviceId"]
                config.preferred_scaling_gpu_name = selected["name"]
            elif not config.auto_route_gpu_to_display:
                warnings.append(f"Default scaling GPU '{name or config.preferred_scaling_gpu_device_id}' is unavailable or ambiguous. Select a GPU in General Settings; explicit routing will not silently use Auto.")
        for profile in config.profiles:
            for key, id_field, name_field, items, old_items, numeric_key, is_gpu in (
                ("PreferredGpuId", "preferred_gpu_device_id", "preferred_gpu_name", gpus, previous.get("gpus", []), "lsGpuId", True),
                ("OutputDisplayId", "output_display_device_id", "output_display_name", displays, previous.get("displays", []), "lsDisplayId", False),
            ):
                numeric = _number(profile.native_scaling_settings.get(key))
                device_id, name = getattr(profile, id_field), getattr(profile, name_field)
                if not device_id and not name and numeric:
                    old = next((item for item in old_items if _number(item.get(numeric_key)) == numeric), None)
                    if old:
                        device_id, name = old.get("deviceId"), old.get("name")
                        setattr(profile, id_field, device_id)
                        setattr(profile, name_field, name)
                if not device_id and not name and not numeric:
                    continue
                selected = match_device(items, device_id, name, gpu=is_gpu)
                new_id = _number(selected.get(numeric_key)) if selected else 0
                if selected:
                    setattr(profile, id_field, selected.get("deviceId"))
                    setattr(profile, name_field, selected.get("name"))
                else:
                    warnings.append(f"Profile '{profile.name}': saved {key} '{name or device_id or numeric}' could not be matched safely; following General Settings/automatic display routing.")
                if numeric != new_id or str(profile.native_scaling_settings.get(key)) != str(new_id):
                    profile.native_scaling_settings[key] = str(new_id)
                    logger.info("Remapped profile '%s' %s: %s -> %s (%s)", profile.name, key, numeric, new_id, name or device_id or "General Settings")
        config.gpu_inventory_snapshot = copy.deepcopy(inventory)
        for warning in warnings:
            if warning not in self.warnings:
                logger.warning(warning)
        self.warnings = warnings
        changed = before != config.model_dump()
        if changed or self._unsaved:
            self._unsaved = True
            self.manager.save_config()
            self._unsaved = False
        self._last_signature = copy.deepcopy(self._signature(config, inventory))
        return changed

    def capture_profile(self, profile, inventory, previous=None):
        """Bind a user-selected numeric option to its current stable identity.

        New dashboards also send the identity, so stale browser inventories can
        be remapped instead of accidentally selecting today's occupant of an ID.
        """
        if not isinstance(inventory, dict) or not inventory.get("gpus"):
            return
        for key, id_field, name_field, items, numeric_key, gpu in (
            ("PreferredGpuId", "preferred_gpu_device_id", "preferred_gpu_name", inventory["gpus"], "lsGpuId", True),
            ("OutputDisplayId", "output_display_device_id", "output_display_name", inventory.get("displays", []), "lsDisplayId", False),
        ):
            numeric = _number(profile.native_scaling_settings.get(key))
            if not numeric:
                setattr(profile, id_field, None)
                setattr(profile, name_field, None)
                continue
            device_id, name = getattr(profile, id_field), getattr(profile, name_field)
            if not device_id and previous and _number(previous.native_scaling_settings.get(key)) == numeric:
                device_id, name = getattr(previous, id_field), getattr(previous, name_field)
            selected = (match_device(items, device_id, name, gpu=gpu) if device_id or name else
                        next((item for item in items if _number(item.get(numeric_key)) == numeric), None))
            if not selected:
                raise ValueError(f"Selected {key} is unavailable or ambiguous; refresh the profile and choose again")
            profile.native_scaling_settings[key] = str(selected[numeric_key])
            setattr(profile, id_field, selected.get("deviceId"))
            setattr(profile, name_field, selected.get("name"))
