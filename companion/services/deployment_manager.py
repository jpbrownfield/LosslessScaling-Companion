"""Transactional, manifest-owned deployment into Lossless Scaling only."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .asset_store import AssetStore, UnsafeAssetError


class DeploymentConflictError(RuntimeError):
    pass


class DeploymentManager:
    TARGET_ID = "lossless-scaling"

    def __init__(self, store: AssetStore):
        self.store = store
        self._lock = threading.RLock()
        self.manifest_path = store.deployments / self.TARGET_ID / "active-manifest.json"
        self.originals = store.backups / self.TARGET_ID / "originals"

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
            if not destination.is_file() or self._hash(destination) != entry.get("deployed_sha256"):
                return False
        return True

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
            desired = self._normalize_desired(files)
            fingerprint = self._fingerprint(profile_id, desired)
            active = self._read_manifest()
            root = None
            if active.get("fingerprint") == fingerprint and not active.get("files"):
                if active.get("profile_id") != profile_id:
                    active["profile_id"] = profile_id
                    AssetStore._atomic_json(self.manifest_path, active)
                return active
            if active.get("fingerprint") == fingerprint:
                root = self._lossless_root(lossless_scaling_exe)
            if active.get("fingerprint") == fingerprint and self._active_matches_disk(root, active):
                if active.get("profile_id") != profile_id:
                    active["profile_id"] = profile_id
                    AssetStore._atomic_json(self.manifest_path, active)
                return active

            root = root or self._lossless_root(lossless_scaling_exe)

            active_by_path = {item["relative_path"].casefold(): item for item in active.get("files", [])}
            desired_by_path = {item["relative_path"].casefold(): item for item in desired}
            affected = sorted(set(active_by_path) | set(desired_by_path))

            for folded in active_by_path:
                entry = active_by_path[folded]
                destination = self._destination(root, entry["relative_path"])
                mutable_config = (
                    entry.get("role") == "lossless_proxy_config"
                    and folded in desired_by_path
                )
                if (
                    not mutable_config
                    and destination.is_file()
                    and self._hash(destination) != entry.get("deployed_sha256")
                ):
                    raise DeploymentConflictError(
                        f"Managed file was modified outside the helper: {entry['relative_path']}"
                    )

            transaction_id = uuid.uuid4().hex
            rollback_root = self.store.backups / self.TARGET_ID / transaction_id / "prechange"
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

            new_entries = []
            try:
                for folded, old_entry in active_by_path.items():
                    if folded in desired_by_path:
                        continue
                    destination = self._destination(root, old_entry["relative_path"])
                    original_path = old_entry.get("original_backup")
                    if old_entry.get("original_existed") and original_path and Path(original_path).is_file():
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        self._atomic_copy(Path(original_path), destination)
                    elif destination.exists():
                        destination.unlink()

                for item in desired:
                    folded = item["relative_path"].casefold()
                    destination = self._destination(root, item["relative_path"])
                    previous = active_by_path.get(folded)
                    original_existed = bool(previous and previous.get("original_existed"))
                    original_backup = previous.get("original_backup") if previous else None
                    if previous is None and destination.is_file():
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
                }
                AssetStore._atomic_json(self.manifest_path, manifest)
                return manifest
            except Exception:
                for destination, snapshot, existed in reversed(rollback_state):
                    try:
                        if existed and snapshot.is_file():
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            self._atomic_copy(snapshot, destination)
                        elif destination.exists():
                            destination.unlink()
                    except OSError:
                        pass
                raise

    @staticmethod
    def _atomic_copy(source: Path, destination: Path) -> None:
        handle, temp_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
        os.close(handle)
        try:
            shutil.copy2(source, temp_name)
            os.replace(temp_name, destination)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
