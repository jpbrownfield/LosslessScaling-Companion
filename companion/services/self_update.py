"""Verified companion update discovery, download, and installer handoff."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable, Dict, Optional
from urllib.parse import urlparse

from ..version import current_version, stable_version_tuple
from .asset_store import AssetStore, UnsafeAssetError
from .release_providers import (
    GitHubReleaseProvider,
    ReleaseManager,
    github_request_headers,
)


INSTALLER_NAME = "LosslessCompanion-Setup-x64.exe"
CHECKSUM_NAME = INSTALLER_NAME + ".sha256"
_CHECKSUM_LINE = re.compile(
    rf"^([0-9a-fA-F]{{64}})\s+\*?{re.escape(INSTALLER_NAME)}$"
)


class CompanionUpdateService:
    def __init__(
        self, release_manager: ReleaseManager, config_dir: Path, *, enabled: Optional[bool] = None
    ):
        self.release_manager = release_manager
        self.root = Path(config_dir) / "updates"
        self.enabled = bool(getattr(sys, "frozen", False)) if enabled is None else enabled
        self._lock = threading.RLock()

    def automatic_retry_allowed(self, version: str) -> bool:
        try:
            attempt = json.loads((self.root / 'automatic-attempt.json').read_text(encoding='utf-8'))
            return attempt.get('version') != version or time.time() - float(attempt['time']) >= 3600
        except (OSError, ValueError, TypeError, KeyError):
            return True

    @staticmethod
    def _asset(release: Dict, name: str) -> Optional[Dict]:
        return next(
            (item for item in release.get("assets") or [] if item.get("name") == name),
            None,
        )

    def _verified_installer(self, version: str) -> tuple[Path, Dict]:
        normalized = str(version or "").strip().removeprefix("v")
        if stable_version_tuple(normalized) is None:
            raise UnsafeAssetError("A stable companion version is required")
        directory = (self.root / normalized).resolve()
        receipt_path = directory / "update.json"
        if directory.parent != self.root.resolve() or not receipt_path.is_file():
            raise UnsafeAssetError("Verified companion installer is unavailable")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if (not isinstance(receipt, dict) or receipt.get("version") != normalized
                or receipt.get("filename") != INSTALLER_NAME):
            raise UnsafeAssetError("Companion installer receipt does not match the requested version")
        installer = (directory / INSTALLER_NAME).resolve()
        if installer.parent != directory or not installer.is_file():
            raise UnsafeAssetError("Verified companion installer is unavailable")
        if AssetStore.sha256(installer).casefold() != str(receipt.get("sha256") or "").casefold():
            raise UnsafeAssetError("Companion installer changed after verification")
        return installer, receipt

    def _cached_receipt(self, version: str) -> Optional[Dict]:
        with self._lock:
            try:
                return self._verified_installer(version)[1]
            except (OSError, ValueError, TypeError):
                return None

    def check(self, *, force: bool = False) -> Dict:
        if not self.enabled:
            return {
                "type": "COMPANION_UPDATE_STATUS",
                "currentVersion": current_version(),
                "latestVersion": None,
                "updateAvailable": False,
                "downloaded": False,
                "supported": False,
            }
        releases = self.release_manager.check("companion", channel="stable", force=force)
        valid = [
            item for item in releases
            if stable_version_tuple(str(item.get("version") or ""))
            and self._asset(item, INSTALLER_NAME)
            and self._asset(item, CHECKSUM_NAME)
        ]
        valid.sort(
            key=lambda item: stable_version_tuple(str(item.get("version") or "")),
            reverse=True,
        )
        latest = valid[0] if valid else None
        installed = current_version()
        installed_tuple = stable_version_tuple(installed)
        latest_tuple = stable_version_tuple(str(latest.get("version") or "")) if latest else None
        update_available = bool(
            latest_tuple and (installed_tuple is None or latest_tuple > installed_tuple)
        )
        return {
            "type": "COMPANION_UPDATE_STATUS",
            "currentVersion": installed,
            "latestVersion": str(latest.get("version") or "").removeprefix("v") if latest else None,
            "updateAvailable": update_available,
            "releaseUrl": latest.get("html_url") if latest else None,
            "releaseNotes": latest.get("notes") if latest else None,
            "downloaded": bool(latest and self._cached_receipt(str(latest.get("version") or ""))),
            "supported": True,
        }

    @staticmethod
    def _download_bytes(
        asset: Dict,
        *,
        max_bytes: int,
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> bytes:
        parsed = urlparse(str(asset.get("url") or ""))
        if parsed.scheme != "https" or parsed.hostname not in GitHubReleaseProvider.allowed_hosts:
            raise UnsafeAssetError("Companion update URL is outside the GitHub allowlist")
        request = urllib.request.Request(
            str(asset["url"]),
            headers=github_request_headers(accept="application/octet-stream"),
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            final = urlparse(response.geturl())
            if final.scheme != "https" or final.hostname not in GitHubReleaseProvider.allowed_hosts:
                raise UnsafeAssetError("Companion update redirected outside the GitHub allowlist")
            chunks, received = [], 0
            expected_size = max(0, int(asset.get("size") or 0))
            if progress_callback:
                progress_callback(0, expected_size)
            while True:
                chunk = response.read(min(1024 * 1024, max_bytes + 1 - received))
                if not chunk:
                    break
                received += len(chunk)
                if received > max_bytes:
                    raise UnsafeAssetError("Companion update exceeded its size limit")
                chunks.append(chunk)
                if progress_callback:
                    progress_callback(received, expected_size)
            return b"".join(chunks)

    @staticmethod
    def _expected_checksum(content: bytes) -> str:
        text = content.decode("ascii", errors="strict").strip()
        match = _CHECKSUM_LINE.fullmatch(text)
        if not match:
            raise UnsafeAssetError("Release checksum file has an invalid format")
        return match.group(1).casefold()

    def download(
        self,
        version: str,
        *,
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> Dict:
        with self._lock:
            if not self.enabled:
                raise RuntimeError("Companion updates are available only in packaged builds")
            normalized = str(version or "").strip().removeprefix("v")
            if stable_version_tuple(normalized) is None:
                raise ValueError("A stable companion version is required")
            cached = self._cached_receipt(normalized)
            if cached:
                return {
                    "type": "COMPANION_UPDATE_DOWNLOADED",
                    "version": normalized,
                    "sha256": cached["sha256"],
                    "filename": INSTALLER_NAME,
                }
            releases = self.release_manager.check("companion", channel="stable", force=True)
            release = next(
                (item for item in releases if str(item.get("version") or "").removeprefix("v") == normalized),
                None,
            )
            if release is None:
                raise ValueError("The requested companion release is unavailable")
            installer_asset = self._asset(release, INSTALLER_NAME)
            checksum_asset = self._asset(release, CHECKSUM_NAME)
            if not installer_asset or not checksum_asset:
                raise UnsafeAssetError("The companion release is missing its installer or checksum")
            expected = self._expected_checksum(
                self._download_bytes(checksum_asset, max_bytes=4096)
            )
            payload = self._download_bytes(
                installer_asset,
                max_bytes=512 * 1024 * 1024,
                progress_callback=progress_callback,
            )
            calculated = hashlib.sha256(payload).hexdigest()
            if calculated != expected:
                raise UnsafeAssetError("Downloaded companion installer SHA-256 does not match")
            destination_dir = self.root / normalized
            destination_dir.mkdir(parents=True, exist_ok=True)
            destination = destination_dir / INSTALLER_NAME
            temporary = destination.with_suffix(".exe.partial")
            temporary.write_bytes(payload)
            os.replace(temporary, destination)
            receipt = {
                "version": normalized,
                "filename": INSTALLER_NAME,
                "sha256": calculated,
                "path": str(destination.resolve()),
                "releaseUrl": release.get("html_url"),
            }
            AssetStore._atomic_json(destination_dir / "update.json", receipt)
            return {
                "type": "COMPANION_UPDATE_DOWNLOADED",
                "version": normalized,
                "sha256": calculated,
                "filename": INSTALLER_NAME,
            }

    def launch_installer(self, version: str, *, automatic: bool = False) -> Dict:
        with self._lock:
            if not self.enabled:
                raise RuntimeError("Companion updates are available only in packaged builds")
            normalized = str(version or "").strip().removeprefix("v")
            installer, _receipt = self._verified_installer(normalized)
            directory = installer.parent
            options = [
                "/SP-", "/CLOSEAPPLICATIONS", "/FORCECLOSEAPPLICATIONS",
            ]
            if automatic:
                options.extend([
                    "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                    "/AUTOMATICUPDATE=1", f"/WAITPID={os.getpid()}",
                    f"/DIR={Path(sys.executable).resolve().parent}",
                    f"/LOG={directory / 'install.log'}",
                ])
            parameters = subprocess.list2cmdline(options)
            if automatic:
                AssetStore._atomic_json(self.root / 'automatic-attempt.json', {'version': normalized, 'time': time.time()})
            result = ctypes.windll.shell32.ShellExecuteW(
                None,
                "runas",
                str(installer),
                parameters,
                str(directory),
                1,
            )
            if int(result) <= 32:
                raise OSError(f"Windows could not elevate the companion installer (ShellExecute code {result})")
            return {
                "type": "COMPANION_INSTALLER_LAUNCHED",
                "version": normalized,
                "filename": INSTALLER_NAME,
            }
