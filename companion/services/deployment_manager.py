"""Transactional, manifest-owned deployment into Lossless Scaling only."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .asset_store import AssetStore, UnsafeAssetError


class DeploymentConflictError(RuntimeError):
    pass


class DeploymentManager:
    TARGET_ID = "lossless-scaling"
    MUTABLE_CONFIG_ROLES = {
        "lossless_proxy_config", "addon_manager_config", "special_k_config", "neural_reshade_config",
    }
    RUNTIME_MUTATED_CONFIG_ROLES = {"neural_reshade_config"}
    # Installing/updating our fork is an explicit takeover of these exact
    # component files. Unknown files beside them are left alone, and the
    # normal fail-closed rule still protects every unrelated managed binary.
    REPLACEABLE_COMPONENT_PATHS = {
        "addons/lsp-neuralrender/lsp_neuralrender.dll",
        "addons/lsp-neuralrender/nvngx.dll_lspnr.dll",
        "addons/lsp-neuralrender/addon.json",
    }
    LEGACY_ADDON_IDS = {
        "lsp-neuralrender": {
            "lsp-neuralrender", "lspneuralrender", "neural render (dlss 5)",
        },
        "lsp-reshade": {"lsp-reshade", "lspreshade", "reshade"},
        "lsp-windowed": {"lsp-windowed", "lspwindowed", "windowed"},
    }

    def __init__(self, store: AssetStore):
        self.store = store
        self._lock = threading.RLock()
        self.manifest_path = store.deployments / self.TARGET_ID / "active-manifest.json"
        self.backup_root = store.backups / self.TARGET_ID
        self.originals = self.backup_root / "originals"
        self.preinsert = self.backup_root / "preinsert"

    @staticmethod
    def _hash(path: Path) -> str:
        return AssetStore.sha256(path)

    @staticmethod
    def _safe_relative(value: str) -> Path:
        normalized = value.replace("\\", "/")
        relative = Path(normalized)
        if (
            not normalized
            or normalized.startswith("/")
            or relative.is_absolute()
            or relative.drive
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise UnsafeAssetError(f"Unsafe deployment path: {value!r}")
        return relative

    @staticmethod
    def _lossless_root(lossless_scaling_exe: str) -> Path:
        executable = Path(lossless_scaling_exe).resolve()
        if executable.name.casefold() != "losslessscaling.exe" or not executable.is_file():
            raise UnsafeAssetError("Configured deployment host must be LosslessScaling.exe")
        return executable.parent

    @classmethod
    def _destination(cls, root: Path, relative_value: str) -> Path:
        relative = cls._safe_relative(relative_value)
        destination = (root / relative).resolve()
        if destination != root and root not in destination.parents:
            raise UnsafeAssetError("Deployment destination escapes Lossless Scaling")
        return destination

    def _read_manifest(self) -> Dict:
        if not self.manifest_path.is_file():
            return {
                "schema_version": 1,
                "target": self.TARGET_ID,
                "profile_id": None,
                "fingerprint": self._fingerprint(None, []),
                "files": [],
            }
        try:
            return json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise DeploymentConflictError(f"Active deployment manifest is unreadable: {error}") from error

    @staticmethod
    def _copy_tree_or_file_once(source: Path, destination: Path) -> None:
        if destination.exists():
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            if source.is_dir():
                shutil.copytree(source, temporary)
            else:
                shutil.copy2(source, temporary)
            DeploymentManager._replace_with_retry(temporary, destination)
        finally:
            if temporary.is_dir():
                shutil.rmtree(temporary, ignore_errors=True)
            elif temporary.exists():
                temporary.unlink(missing_ok=True)

    def _consolidate_transaction_backups(self) -> None:
        """Fold old UUID rollback folders into one preinsert backup and remove them."""
        if not self.backup_root.is_dir():
            return
        removed = []
        for directory in self.backup_root.iterdir():
            if (
                not directory.is_dir()
                or len(directory.name) != 32
                or any(character not in "0123456789abcdefABCDEF" for character in directory.name)
            ):
                continue
            try:
                legacy = directory / "prechange" / "legacy-addons"
                if legacy.is_dir():
                    for item in legacy.iterdir():
                        self._copy_tree_or_file_once(
                            item, self.preinsert / "legacy-addons" / item.name
                        )
                shutil.rmtree(directory)
                removed.append(directory.name)
            except OSError:
                # Keep the old folder intact if consolidation cannot complete.
                continue
        if removed:
            self.store.audit(
                "deployment_backups_consolidated",
                target=self.TARGET_ID,
                removed_transaction_folders=removed,
                preinsert=str(self.preinsert),
                originals=str(self.originals),
            )

    @staticmethod
    def _normalize_desired(files: Iterable[Dict]) -> List[Dict]:
        desired = []
        seen = set()
        for item in files:
            relative = DeploymentManager._safe_relative(str(item["relative_path"])).as_posix()
            folded = relative.casefold()
            if folded in seen:
                raise DeploymentConflictError(f"Two assets claim the same destination: {relative}")
            seen.add(folded)
            source = Path(str(item["source_path"])).resolve(strict=True)
            if not source.is_file():
                raise UnsafeAssetError(f"Deployment source is not a file: {source}")
            desired.append(
                {
                    "relative_path": relative,
                    "source_path": str(source),
                    "sha256": AssetStore.sha256(source),
                    "role": str(item.get("role") or "support_file"),
                    "source_package": item.get("source_package"),
                }
            )
        return sorted(desired, key=lambda entry: entry["relative_path"].casefold())

    @staticmethod
    def _fingerprint(profile_id: Optional[str], desired: List[Dict]) -> str:
        # Profile identity is deliberately excluded: switching between two profiles
        # with an identical resolved file set must not restart Lossless Scaling.
        payload = [
            {key: item.get(key) for key in ("relative_path", "sha256", "role", "source_package")}
            for item in desired
        ]
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _active_matches_disk(self, root: Path, active: Dict) -> bool:
        for entry in active.get("files", []):
            destination = self._destination(root, entry["relative_path"])
            if entry.get("inactive"):
                destination = destination.with_name(destination.name + ".inactive")
            if not destination.is_file():
                return False
            if (
                entry.get("role") not in self.RUNTIME_MUTATED_CONFIG_ROLES
                and self._hash(destination) != entry.get("deployed_sha256")
            ):
                return False
        return True

    @classmethod
    def _recognized_legacy_addon_dirs(cls, root: Path, active: Dict) -> List[Path]:
        """Return only old Companion/Echo predecessors we can identify safely."""
        tracked = {
            str(item.get("relative_path") or "").replace("\\", "/").casefold()
            for item in active.get("files", [])
        }
        found = []
        addons = root / "addons"
        for folder_name, accepted_ids in cls.LEGACY_ADDON_IDS.items():
            directory = addons / folder_name
            if not directory.is_dir():
                # Windows paths are case-insensitive, but temporary test hosts may not be.
                directory = next(
                    (
                        item
                        for item in addons.iterdir()
                        if item.is_dir() and item.name.casefold() == folder_name
                    ),
                    directory,
                ) if addons.is_dir() else directory
            if not directory.is_dir():
                continue
            prefix = f"addons/{folder_name}/"
            companion_owned = any(item.startswith(prefix) for item in tracked)
            manifest_id = ""
            manifest = directory / "addon.json"
            if manifest.is_file():
                try:
                    data = json.loads(manifest.read_text(encoding="utf-8"))
                    manifest_id = str(data.get("id") or data.get("name") or "").casefold()
                except (OSError, json.JSONDecodeError, AttributeError):
                    manifest_id = ""
            if companion_owned or manifest_id in accepted_ids:
                found.append(directory)
        return found

    @classmethod
    def _recognized_legacy_paths(cls, root: Path, active: Dict) -> List[Path]:
        paths = cls._recognized_legacy_addon_dirs(root, active)
        paths.extend(
            candidate
            for name in ("LP-icon.ico", "LP-icon.png")
            if (candidate := root / name).is_file()
        )
        return paths

    @staticmethod
    def _runtime_provider(entry: Dict) -> Optional[str]:
        source = str(entry.get("source_package") or "").casefold()
        for provider in ("reshade", "special-k"):
            if source.startswith(provider + "/"):
                suffix = Path(str(entry.get("relative_path") or "")).suffix.casefold()
                if suffix in {".dll", ".addon32", ".addon64"}:
                    return provider
        return None

    def set_runtime_packages_active(
        self,
        lossless_scaling_exe: str,
        providers: Iterable[str],
        *,
        active: bool,
    ) -> bool:
        """Atomically park/unpark managed ReShade and Special K binaries in place."""
        requested = {str(item).casefold() for item in providers}
        if not requested:
            return False
        with self._lock:
            root = self._lossless_root(lossless_scaling_exe)
            manifest = self._read_manifest()
            changes = []
            for entry in manifest.get("files", []):
                provider = self._runtime_provider(entry)
                if provider not in requested or bool(entry.get("inactive")) == (not active):
                    continue
                enabled_path = self._destination(root, entry["relative_path"])
                disabled_path = enabled_path.with_name(enabled_path.name + ".inactive")
                source, destination = (
                    (disabled_path, enabled_path) if active else (enabled_path, disabled_path)
                )
                if not source.is_file() or self._hash(source) != entry.get("deployed_sha256"):
                    raise DeploymentConflictError(
                        f"Managed runtime file is missing or modified: {source.name}"
                    )
                if destination.exists():
                    raise DeploymentConflictError(
                        f"Cannot {'activate' if active else 'deactivate'} {enabled_path.name}; {destination.name} already exists"
                    )
                changes.append((entry, source, destination))
            completed = []
            try:
                for entry, source, destination in changes:
                    os.replace(source, destination)
                    entry["inactive"] = not active
                    completed.append((entry, source, destination))
                if changes:
                    AssetStore._atomic_json(self.manifest_path, manifest)
                    self.store.audit(
                        "runtime_addons_activated" if active else "runtime_addons_parked",
                        target=str(root),
                        providers=sorted(requested),
                        files=[item[0]["relative_path"] for item in changes],
                    )
                return bool(changes)
            except Exception as error:
                for entry, source, destination in reversed(completed):
                    if destination.exists() and not source.exists():
                        os.replace(destination, source)
                    entry["inactive"] = active
                self.store.audit(
                    "runtime_addon_state_change_failed",
                    outcome="failed",
                    target=str(root),
                    providers=sorted(requested),
                    requested_active=active,
                    error=type(error).__name__,
                    message=str(error),
                )
                raise

    def runtime_packages_need_state(self, providers: Iterable[str], *, active: bool) -> bool:
        requested = {str(item).casefold() for item in providers}
        manifest = self._read_manifest()
        return any(
            self._runtime_provider(entry) in requested
            and bool(entry.get("inactive")) != (not active)
            for entry in manifest.get("files", [])
        )

    def _unpark_for_redeployment(self, root: Path, manifest: Dict) -> None:
        changed = False
        for entry in manifest.get("files", []):
            if not entry.get("inactive"):
                continue
            enabled = self._destination(root, entry["relative_path"])
            disabled = enabled.with_name(enabled.name + ".inactive")
            if enabled.exists():
                raise DeploymentConflictError(
                    f"Cannot prepare {enabled.name}; both active and inactive files exist"
                )
            if not disabled.is_file() or self._hash(disabled) != entry.get("deployed_sha256"):
                raise DeploymentConflictError(
                    f"Managed inactive file is missing or modified: {disabled.name}"
                )
            os.replace(disabled, enabled)
            entry["inactive"] = False
            changed = True
        if changed:
            AssetStore._atomic_json(self.manifest_path, manifest)

    def needs_change(
        self,
        profile_id: Optional[str],
        files: Iterable[Dict],
        *,
        lossless_scaling_exe: Optional[str] = None,
    ) -> bool:
        desired = self._normalize_desired(files)
        active = self._read_manifest()
        if active.get("fingerprint") != self._fingerprint(profile_id, desired):
            return True
        if not active.get("files"):
            return False
        if lossless_scaling_exe:
            return not self._active_matches_disk(
                self._lossless_root(lossless_scaling_exe), active
            )
        return False

    def apply(
        self,
        *,
        profile_id: Optional[str],
        lossless_scaling_exe: str,
        files: Iterable[Dict],
    ) -> Dict:
        """Apply one complete desired LS file set, rolling back on every failure."""
        with self._lock:
            self._consolidate_transaction_backups()
            desired = self._normalize_desired(files)
            fingerprint = self._fingerprint(profile_id, desired)
            active = self._read_manifest()
            root = None
            installs_echo = any(
                str(item.get("source_package") or "").casefold().startswith("ls-addon-manager/")
                for item in desired
            )
            legacy_pending = False
            if installs_echo:
                root = self._lossless_root(lossless_scaling_exe)
                legacy_pending = bool(self._recognized_legacy_paths(root, active))
            if (
                active.get("fingerprint") == fingerprint
                and not active.get("files")
                and not legacy_pending
            ):
                if active.get("profile_id") != profile_id:
                    active["profile_id"] = profile_id
                    AssetStore._atomic_json(self.manifest_path, active)
                return active
            if active.get("fingerprint") == fingerprint:
                root = self._lossless_root(lossless_scaling_exe)
            if (
                active.get("fingerprint") == fingerprint
                and not legacy_pending
                and self._active_matches_disk(root, active)
            ):
                if active.get("profile_id") != profile_id:
                    active["profile_id"] = profile_id
                    AssetStore._atomic_json(self.manifest_path, active)
                return active

            root = root or self._lossless_root(lossless_scaling_exe)
            self._unpark_for_redeployment(root, active)

            active_by_path = {item["relative_path"].casefold(): item for item in active.get("files", [])}
            desired_by_path = {item["relative_path"].casefold(): item for item in desired}
            affected = sorted(set(active_by_path) | set(desired_by_path))

            for folded in active_by_path:
                entry = active_by_path[folded]
                destination = self._destination(root, entry["relative_path"])
                mutable_config = entry.get("role") in self.MUTABLE_CONFIG_ROLES
                if (
                    not mutable_config
                    and folded not in self.REPLACEABLE_COMPONENT_PATHS
                    and destination.is_file()
                    and self._hash(destination) != entry.get("deployed_sha256")
                ):
                    raise DeploymentConflictError(
                        f"Managed file was modified outside the helper: {entry['relative_path']}"
                    )

            transaction_id = uuid.uuid4().hex
            rollback_root = self.store.staging / f"deployment-rollback-{transaction_id}"
            rollback_state = []
            for folded in affected:
                relative = (desired_by_path.get(folded) or active_by_path[folded])["relative_path"]
                destination = self._destination(root, relative)
                snapshot = rollback_root / Path(relative)
                existed = destination.is_file()
                if existed:
                    snapshot.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(destination, snapshot)
                rollback_state.append((destination, snapshot, existed))

            legacy_paths = (
                self._recognized_legacy_paths(root, active) if installs_echo else []
            )
            migrated_legacy = []

            self.store.audit(
                "deployment_requested",
                target=str(root),
                profile_id=profile_id,
                transaction_id=transaction_id,
                files=[
                    {
                        "relative_path": item["relative_path"],
                        "sha256": item["sha256"],
                        "role": item["role"],
                        "source_package": item.get("source_package"),
                    }
                    for item in desired
                ],
            )

            new_entries = []
            try:
                if installs_echo:
                    original_engine = next(
                        (
                            Path(item["source_path"])
                            for item in desired
                            if item.get("role") == "lossless_original_engine"
                        ),
                        None,
                    )
                    original_backup = self.originals / "Lossless.dll"
                    if original_engine and not original_backup.exists():
                        original_backup.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(original_engine, original_backup)
                for legacy_path in legacy_paths:
                    backup = rollback_root / "legacy-addons" / legacy_path.name
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(legacy_path, backup)
                    migrated_legacy.append((legacy_path, backup))
                    self._copy_tree_or_file_once(
                        backup, self.preinsert / "legacy-addons" / legacy_path.name
                    )
                if migrated_legacy:
                    self.store.audit(
                        "legacy_addon_structure_archived",
                        target=str(root),
                        transaction_id=transaction_id,
                        paths=[item.name for item, _backup in migrated_legacy],
                        backup=str(self.preinsert / "legacy-addons"),
                    )
                for folded, old_entry in active_by_path.items():
                    if folded in desired_by_path:
                        continue
                    destination = self._destination(root, old_entry["relative_path"])
                    original_path = old_entry.get("original_backup")
                    if (
                        folded not in self.REPLACEABLE_COMPONENT_PATHS
                        and old_entry.get("original_existed")
                        and original_path
                        and Path(original_path).is_file()
                    ):
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        self._atomic_copy(Path(original_path), destination)
                    elif destination.exists():
                        destination.unlink()

                for item in desired:
                    folded = item["relative_path"].casefold()
                    destination = self._destination(root, item["relative_path"])
                    previous = active_by_path.get(folded)
                    component_takeover = folded in self.REPLACEABLE_COMPONENT_PATHS
                    original_existed = bool(
                        not component_takeover
                        and previous
                        and previous.get("original_existed")
                    )
                    original_backup = (
                        previous.get("original_backup")
                        if previous and not component_takeover
                        else None
                    )
                    if previous is None and destination.is_file() and not component_takeover:
                        original_existed = True
                        original_backup_path = self.originals / Path(item["relative_path"])
                        original_backup_path.parent.mkdir(parents=True, exist_ok=True)
                        if not original_backup_path.exists():
                            shutil.copy2(destination, original_backup_path)
                        original_backup = str(original_backup_path)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    self._atomic_copy(Path(item["source_path"]), destination)
                    new_entries.append(
                        {
                            "relative_path": item["relative_path"],
                            "deployed_sha256": item["sha256"],
                            "role": item["role"],
                            "source_package": item.get("source_package"),
                            "original_existed": original_existed,
                            "original_backup": original_backup,
                        }
                    )

                manifest = {
                    "schema_version": 1,
                    "target": self.TARGET_ID,
                    "profile_id": profile_id,
                    "fingerprint": fingerprint,
                    "transaction_id": transaction_id,
                    "files": new_entries,
                    "legacy_addon_backup": (
                        str(self.preinsert / "legacy-addons")
                        if migrated_legacy
                        else active.get("legacy_addon_backup")
                    ),
                }
                AssetStore._atomic_json(self.manifest_path, manifest)
                self.store.audit(
                    "deployment_applied",
                    target=str(root),
                    profile_id=profile_id,
                    transaction_id=transaction_id,
                    file_count=len(new_entries),
                )
                return manifest
            except Exception as error:
                for destination, backup in reversed(migrated_legacy):
                    try:
                        if destination.exists():
                            if destination.is_dir():
                                shutil.rmtree(destination)
                            else:
                                destination.unlink()
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(backup, destination)
                    except OSError:
                        pass
                for destination, snapshot, existed in reversed(rollback_state):
                    try:
                        if existed and snapshot.is_file():
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            self._atomic_copy(snapshot, destination)
                        elif destination.exists():
                            destination.unlink()
                    except OSError:
                        pass
                self.store.audit(
                    "deployment_rolled_back",
                    outcome="failed",
                    target=str(root),
                    profile_id=profile_id,
                    transaction_id=transaction_id,
                    error=type(error).__name__,
                    message=str(error),
                )
                raise
            finally:
                shutil.rmtree(rollback_root, ignore_errors=True)

    @staticmethod
    def _replace_with_retry(source, destination):
        # AV/indexers can briefly retain a closed file after LS has exited.
        # Never change ACLs, delete the destination, or force a locked DLL out.
        # Callers must stop LosslessScaling.exe first; retries only cover the
        # transient post-exit handle window. Fail fast afterwards so live
        # diagnostics report the lock instead of a pending-reboot state.
        for attempt in range(10):
            try:
                os.replace(source, destination)
                return
            except OSError as error:
                if getattr(error, "winerror", None) not in {5, 32, 33} or attempt == 9:
                    raise
                time.sleep(0.1 * (2 ** min(attempt, 3)))

    @staticmethod
    def _atomic_copy(source: Path, destination: Path) -> None:
        handle, temp_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
        os.close(handle)
        try:
            shutil.copy2(source, temp_name)
            DeploymentManager._replace_with_retry(temp_name, destination)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
