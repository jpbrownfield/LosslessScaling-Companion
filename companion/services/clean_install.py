"""Recoverable reset of Companion configuration and its private LS installation."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import shutil
import uuid

from .asset_store import AssetStore

logger = logging.getLogger("LSCompanion.CleanInstall")
JOURNAL = "clean-install-transaction.json"
TARGETS = {
    "runtime": "runtime/lossless-scaling",
    "profiles": "profile-settings",
    "reshade": "reshade-profiles",
    "addons": "addon-configs",
    "settings": "settings.json",
    "settings-temp": "settings.json.tmp",
}


def _check_path(path: Path, root: Path) -> None:
    """Reject links/junctions before moving or recursively deleting owned data."""
    if path == root or not path.absolute().is_relative_to(root) or path.absolute() != path.resolve():
        raise RuntimeError(f"Clean install path escapes its configuration directory: {path}")
    for candidate in (path, *path.parents):
        if candidate == root:
            break
        if candidate.exists() or candidate.is_symlink():
            if candidate.is_symlink() or getattr(candidate.lstat(), "st_file_attributes", 0) & 0x400:
                raise RuntimeError(f"Clean install cannot operate on linked paths: {candidate}")
    if path.is_dir():
        for candidate in path.rglob("*"):
            if candidate.is_symlink() or getattr(candidate.lstat(), "st_file_attributes", 0) & 0x400:
                raise RuntimeError(f"Clean install cannot operate on linked paths: {candidate}")


def _remove(path: Path, root: Path) -> None:
    _check_path(path, root)
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def recover_clean_install(config_dir: Path) -> list[str]:
    """Rollback interrupted work, or finish deletion after a committed reset."""
    root = Path(config_dir).resolve()
    journal = root / JOURNAL
    _check_path(journal, root)
    if not journal.exists():
        return []
    record = json.loads(journal.read_text(encoding="utf-8"))
    if (record.get("schema") != 1 or not re.fullmatch(r"[0-9a-f]{32}", str(record.get("id", "")))
            or record.get("phase") not in {"preparing", "committed"}
            or set(record.get("existed", {})) != set(TARGETS)
            or any(type(value) is not bool for value in record["existed"].values())):
        raise RuntimeError("Invalid clean install recovery journal; saved files were preserved")
    backup = root / (".clean-install-" + record["id"])
    _check_path(backup, root)
    if record["phase"] == "preparing":
        for name, relative in TARGETS.items():
            target, original = root / relative, backup / name
            _check_path(target, root)
            _check_path(original, root)
            if original.exists():
                _remove(target, root)
                target.parent.mkdir(parents=True, exist_ok=True)
                original.rename(target)
            elif not record["existed"][name]:
                _remove(target, root)
        logger.warning("Restored configuration and cloned LS after an interrupted clean install")
    try:
        _remove(backup, root)
        journal.unlink()
        return []
    except OSError as error:
        # The new installation is already committed. Keep the journal so the
        # next launch can retry deleting locked old files without resetting again.
        logger.exception("Clean install old-file cleanup needs a retry")
        return [f"Old installation cleanup is pending: {error}"]


def clean_install(automation, cleanup_integrations) -> dict:
    manager, state = automation.profile_manager, automation.state
    with automation._lock, manager._lock:
        if (manager.repair_session or state.update_in_progress or state.benchmark_mode_active
                or state.gpu_mapping_in_progress):
            raise RuntimeError("Finish settings repair, updates and live tests before a clean install")
        runtime = getattr(manager, "managed_runtime", None)
        cfg = manager.config
        if runtime is None or not cfg.lossless_source_exe_path or automation.process_watcher is None:
            raise RuntimeError("A managed LS installation, source installation and process controller are required")
        root = manager.config_dir.resolve()
        if runtime.root != root / TARGETS["runtime"]:
            raise RuntimeError("Clean install is restricted to Companion's private LS directory")
        source = Path(cfg.lossless_source_exe_path).resolve(strict=True)
        if source.is_relative_to(root):
            raise RuntimeError("The LS source must be outside Companion's configuration directory")
        warnings = recover_clean_install(root)
        if warnings:
            raise RuntimeError("Previous clean install cleanup is still pending; close LS and retry")
        for relative in TARGETS.values():
            _check_path(root / relative, root)
        previous = cfg.model_copy(deep=True)
        backup = root / (".clean-install-" + uuid.uuid4().hex)
        record = {"schema": 1, "id": backup.name.removeprefix(".clean-install-"), "phase": "preparing",
                  "existed": {name: (root / relative).exists() for name, relative in TARGETS.items()}}
        state.update_in_progress = True
        try:
            automation._cancel_delayed_scale()
            if (automation.process_watcher.check_is_lossless_scaling_running()
                    and not automation.process_watcher.stop_lossless_scaling()):
                raise RuntimeError("Could not stop Lossless Scaling; clean install was not started")
            automation._reset_scaling_to_idle()
            state.current_active_profile = None
            logger.warning("Clean install started: source=%s; managed runtime=%s", source, runtime.root)
            backup.mkdir()
            AssetStore._atomic_json(root / JOURNAL, record)
            try:
                for name, relative in TARGETS.items():
                    target = root / relative
                    if record["existed"][name]:
                        target.rename(backup / name)
                executable = runtime.prepare(source, automation.asset_store, fresh_settings=True)
                if automation.lossless_settings.ensure_initial_backup() is None:
                    raise RuntimeError("Could not protect the fresh private LS settings with an initial backup")
                cleanup_integrations()
                defaults = manager._get_default_config()
                defaults.lossless_source_exe_path = str(source)
                defaults.lossless_scaling_exe_path = str(executable)
                defaults.lossless_settings_xml_path = str(runtime.settings)
                # Keep the cache location and local server transport working;
                # all user-facing general settings and package selections reset.
                defaults.asset_store_path = cfg.asset_store_path
                defaults.host, defaults.port = cfg.host, cfg.port
                for field in type(cfg).model_fields:
                    setattr(cfg, field, getattr(defaults, field))
                manager.profile_settings._written.clear()
                manager.save_config()
                record["phase"] = "committed"
                AssetStore._atomic_json(root / JOURNAL, record)
            except Exception:
                for field in type(cfg).model_fields:
                    setattr(cfg, field, getattr(previous, field))
                manager.profile_settings._written.clear()
                recover_clean_install(root)
                logger.exception("Clean install failed; previous files and settings restored")
                raise
            warnings = recover_clean_install(root)
            automation._reshade_inis.clear()
            automation._pending_native_profiles.clear()
            automation._pending_native_failed = False
            automation._last_verified_route_key = None
            automation._clone_rebuild_pending = False
            automation._reset_scaling_failure_streak()
            automation.addon_configs._pending_profiles.clear()
            state.current_scaled_target = {}
            state.lossless_scaling_running = False
            state.auto_scale_enabled = cfg.disable_native_auto_scale
            state.gpu_mapping_status = {"state": "pending", "message": "GPU check pending after clean install"}
            state.dynamic_limiter_status = {"state": "inactive", "message": "Settings reset to defaults"}
            if automation.runtime_reset_callback is not None:
                try:
                    automation.runtime_reset_callback()
                except Exception as error:
                    logger.exception("Settings reset, but runtime monitoring could not be refreshed")
                    warnings.append(f"Restart Companion to refresh runtime monitoring: {error}")
            logger.warning("Clean install committed; old files removed=%s", not warnings)
            return {"ok": not warnings, "settingsReset": True, "warnings": warnings,
                    "message": ("Clean install complete. Settings reset and old clone deleted. Start scaling when ready."
                                if not warnings else "Settings reset and LS recloned. " + " ".join(warnings))}
        finally:
            state.update_in_progress = False
