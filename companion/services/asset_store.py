"""Immutable, content-addressed storage for managed Lossless Scaling assets."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import struct
import tempfile
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional


_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9._-]+$")
_AUDIT_LOCK = threading.RLock()


class UnsafeAssetError(ValueError):
    pass


class AssetStore:
    """Owns packages, imports, staging, deployment manifests, and backups."""

    @staticmethod
    def default_root() -> Path:
        """Return a per-user writable store unless explicitly overridden.

        ``C:\\ProgramData`` requires elevation, so a non-elevated companion
        can read manifests created by an elevated run but can never write
        them (WinError 5). Defaulting to LOCALAPPDATA keeps dev runs and the
        elevated scheduled task on the same user-writable path, and an
        explicit ``asset_store_path`` still wins.
        """
        configured = os.environ.get("LOSSLESS_COMPANION_ASSET_STORE")
        if configured:
            return Path(os.path.expandvars(configured))
        local_root = os.environ.get("LOCALAPPDATA")
        if local_root:
            return Path(local_root) / "LosslessScalingHelper"
        return (
            Path(os.environ.get("PROGRAMDATA", "."))
            / "LosslessScalingHelper"
        )

    def __init__(self, root: Optional[str] = None):
        configured = root or str(self.default_root())
        self.root = Path(os.path.expandvars(configured)).resolve()
        self.packages = self.root / "assets" / "packages"
        self.imports = self.root / "assets" / "imports"
        self.profiles = self.root / "profiles"
        self.deployments = self.root / "deployments"
        self.backups = self.root / "backups"
        self.staging = self.root / "staging"
        self.logs = self.root / "logs"
        for directory in (
            self.packages,
            self.imports,
            self.profiles,
            self.deployments,
            self.backups,
            self.staging,
            self.logs,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def audit(self, event: str, *, outcome: str = "success", **details: object) -> None:
        """Append a durable, human-readable receipt without affecting the operation.

        Security logging is deliberately best-effort: a full or locked log directory
        must never leave Lossless Scaling half-deployed.
        """
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": str(event),
            "outcome": str(outcome),
            **details,
        }
        try:
            encoded = json.dumps(record, sort_keys=True, default=str) + "\n"
            with _AUDIT_LOCK:
                audit_path = self.logs / "security-audit.jsonl"
                with audit_path.open("a", encoding="utf-8", newline="\n") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
        except OSError:
            # Deployment rollback and integrity checks remain the source of truth.
            pass

    @staticmethod
    def pe_architecture(path: Path) -> Optional[str]:
        """Return a PE architecture without loading or executing the file."""
        try:
            with path.open("rb") as stream:
                if stream.read(2) != b"MZ":
                    return None
                stream.seek(0x3C)
                offset_raw = stream.read(4)
                if len(offset_raw) != 4:
                    return None
                stream.seek(struct.unpack("<I", offset_raw)[0])
                if stream.read(4) != b"PE\0\0":
                    return None
                machine_raw = stream.read(2)
                if len(machine_raw) != 2:
                    return None
            return {0x014C: "x86", 0x8664: "x64", 0xAA64: "arm64"}.get(
                struct.unpack("<H", machine_raw)[0], "unknown"
            )
        except OSError:
            return None

    @staticmethod
    def _component(value: str, label: str) -> str:
        if not value or not _SAFE_COMPONENT.fullmatch(value):
            raise UnsafeAssetError(f"Invalid {label}: {value!r}")
        return value

    @staticmethod
    def _atomic_json(path: Path, data: Dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(data, stream, indent=2, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.replace(temp_name, path)
            except PermissionError as error:
                # A previous elevated run may own the destination file while
                # the directory stays writable (WinError 5 on replace only).
                # Removing first lets the rename succeed without changing
                # ownership semantics for fresh files.
                try:
                    os.remove(path)
                except OSError:
                    raise error
                os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def new_staging_directory(self, operation_id: str) -> Path:
        operation_id = self._component(operation_id, "operation id")
        destination = self.staging / operation_id
        destination.mkdir(parents=False, exist_ok=False)
        return destination

    def import_file(
        self,
        source: str,
        *,
        expected_sha256: Optional[str] = None,
        allowed_suffixes: Optional[Iterable[str]] = None,
    ) -> Dict:
        source_path = Path(source).resolve(strict=True)
        if not source_path.is_file():
            raise UnsafeAssetError("Imported asset must be a file")
        if allowed_suffixes:
            suffixes = {suffix.casefold() for suffix in allowed_suffixes}
            if source_path.suffix.casefold() not in suffixes:
                raise UnsafeAssetError(f"Unsupported imported file type: {source_path.suffix}")
        digest = self.sha256(source_path)
        if expected_sha256 and digest.casefold() != expected_sha256.removeprefix("sha256:").casefold():
            raise UnsafeAssetError("Imported asset SHA-256 does not match")
        destination_dir = self.imports / digest
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / source_path.name
        if not destination.exists():
            temp = destination.with_name(f".{destination.name}.partial")
            shutil.copy2(source_path, temp)
            os.replace(temp, destination)
        metadata = {
            "schema_version": 1,
            "sha256": digest,
            "filename": source_path.name,
            "size": destination.stat().st_size,
            "path": str(destination),
            "architecture": self.pe_architecture(destination)
            if destination.suffix.casefold() in {".dll", ".exe"}
            else None,
        }
        self._atomic_json(destination_dir / "asset.json", metadata)
        self.audit(
            "manual_asset_imported",
            filename=source_path.name,
            sha256=digest,
            size=metadata["size"],
            architecture=metadata["architecture"],
            expected_sha256_supplied=bool(expected_sha256),
        )
        return metadata

    @staticmethod
    def _safe_zip_member(member: zipfile.ZipInfo) -> Path:
        raw = member.filename.replace("\\", "/")
        candidate = Path(raw)
        if (
            not raw
            or raw.startswith("/")
            or candidate.is_absolute()
            or candidate.drive
            or any(part in {"", ".", ".."} for part in candidate.parts)
        ):
            raise UnsafeAssetError(f"Unsafe archive member: {member.filename!r}")
        reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
        for part in candidate.parts:
            stem = part.rstrip(" .").split(".", 1)[0].upper()
            if not part or part != part.rstrip(" .") or ":" in part or "\0" in part or stem in reserved:
                raise UnsafeAssetError(f"Unsafe Windows archive member: {member.filename!r}")
        mode = (member.external_attr >> 16) & 0xFFFF
        if mode and stat.S_ISLNK(mode):
            raise UnsafeAssetError(f"Archive links are not allowed: {member.filename!r}")
        return candidate

    def import_release_archive(
        self,
        archive: str,
        *,
        provider: str,
        version: str,
        source_metadata: Dict,
        expected_sha256: Optional[str] = None,
        max_files: int = 4096,
        max_uncompressed_bytes: int = 2 * 1024 * 1024 * 1024,
    ) -> Dict:
        provider = self._component(provider, "provider")
        version = self._component(version, "version")
        archive_path = Path(archive).resolve(strict=True)
        digest = self.sha256(archive_path)
        if expected_sha256 and digest.casefold() != expected_sha256.removeprefix("sha256:").casefold():
            raise UnsafeAssetError("Release archive SHA-256 does not match")

        destination = self.packages / provider / version / digest
        metadata_path = destination / "package.json"
        if metadata_path.is_file():
            return json.loads(metadata_path.read_text(encoding="utf-8"))

        quarantine_parent = self.staging / f"quarantine-{digest[:16]}"
        if quarantine_parent.exists():
            shutil.rmtree(quarantine_parent)
        quarantine_parent.mkdir(parents=True)
        payload = quarantine_parent / "payload"
        payload.mkdir()
        try:
            with zipfile.ZipFile(archive_path) as bundle:
                members = bundle.infolist()
                if len(members) > max_files:
                    raise UnsafeAssetError("Archive contains too many files")
                if sum(member.file_size for member in members) > max_uncompressed_bytes:
                    raise UnsafeAssetError("Archive is too large when extracted")
                for member in members:
                    relative = self._safe_zip_member(member)
                    target = (payload / relative).resolve()
                    if payload not in target.parents and target != payload:
                        raise UnsafeAssetError(f"Archive member escapes quarantine: {member.filename!r}")
                    if member.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.open(member) as source, target.open("wb") as output:
                        shutil.copyfileobj(source, output)

            files: List[Dict] = []
            for file_path in sorted(path for path in payload.rglob("*") if path.is_file()):
                files.append(
                    {
                        "relative_path": file_path.relative_to(payload).as_posix(),
                        "sha256": self.sha256(file_path),
                        "size": file_path.stat().st_size,
                        "architecture": self.pe_architecture(file_path)
                        if file_path.suffix.casefold() in {".dll", ".exe", ".addon64"}
                        else None,
                    }
                )
            metadata = {
                "schema_version": 1,
                "provider": provider,
                "version": version,
                "archive_sha256": digest,
                "source": source_metadata,
                "files": files,
                "payload_path": str(destination / "payload"),
            }
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(quarantine_parent, destination)
            self._atomic_json(metadata_path, metadata)
            return metadata
        except Exception:
            shutil.rmtree(quarantine_parent, ignore_errors=True)
            raise

    def import_release_file(
        self,
        source: str,
        *,
        provider: str,
        version: str,
        source_metadata: Dict,
        expected_sha256: Optional[str] = None,
    ) -> Dict:
        """Import a single official release asset as a one-file package."""
        provider = self._component(provider, "provider")
        version = self._component(version, "version")
        source_path = Path(source).resolve(strict=True)
        digest = self.sha256(source_path)
        if expected_sha256 and digest.casefold() != expected_sha256.removeprefix("sha256:").casefold():
            raise UnsafeAssetError("Release asset SHA-256 does not match")
        destination = self.packages / provider / version / digest
        payload = destination / "payload"
        payload.mkdir(parents=True, exist_ok=True)
        target = payload / source_path.name
        if not target.exists():
            temp = target.with_name(f".{target.name}.partial")
            shutil.copy2(source_path, temp)
            os.replace(temp, target)
        metadata = {
            "schema_version": 1,
            "provider": provider,
            "version": version,
            "archive_sha256": digest,
            "source": source_metadata,
            "files": [
                {
                    "relative_path": source_path.name,
                    "sha256": digest,
                    "size": target.stat().st_size,
                    "architecture": self.pe_architecture(target)
                    if target.suffix.casefold() in {".dll", ".exe", ".addon64"}
                    else None,
                }
            ],
            "payload_path": str(payload),
        }
        self._atomic_json(destination / "package.json", metadata)
        return metadata

    def list_packages(self) -> List[Dict]:
        packages = []
        if not self.packages.exists():
            return packages
        for metadata_path in self.packages.glob("*/*/*/package.json"):
            try:
                packages.append(json.loads(metadata_path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        return packages

    def list_imports(self) -> List[Dict]:
        """Return valid metadata for immutable user-selected runtime files."""
        imports = []
        if not self.imports.exists():
            return imports
        for metadata_path in self.imports.glob("*/asset.json"):
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                if Path(str(metadata.get("path") or "")).is_file():
                    imports.append(metadata)
            except (OSError, json.JSONDecodeError, TypeError):
                continue
        return imports

    def write_profile_manifest(self, profile_id: str, manifest: Dict) -> Path:
        profile_id = self._component(profile_id, "profile id")
        destination = self.profiles / profile_id / "manifest.json"
        self._atomic_json(destination, manifest)
        return destination

    def write_generated_json(self, profile_id: str, filename: str, data: Dict) -> Path:
        """Write deterministic, profile-owned generated input for a deployment plan."""
        encoded = json.dumps(data, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        return self.write_generated_bytes(profile_id, filename, encoded)

    def write_generated_text(self, profile_id: str, filename: str, text: str) -> Path:
        """Write deterministic UTF-8 text owned by one profile."""
        return self.write_generated_bytes(profile_id, filename, text.encode("utf-8"))

    def write_generated_bytes(self, profile_id: str, filename: str, encoded: bytes) -> Path:
        """Write deterministic, content-addressed bytes owned by one profile."""
        profile_id = self._component(profile_id, "profile id")
        filename = self._component(filename, "generated filename")
        digest = hashlib.sha256(encoded).hexdigest()
        destination = self.profiles / profile_id / "generated" / digest / filename
        if destination.is_file() and self.sha256(destination) == digest:
            return destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(
            prefix=f".{filename}.", suffix=".tmp", dir=destination.parent
        )
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, destination)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        return destination
