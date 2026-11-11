"""Verified metadata and staged downloads from allowlisted component publishers."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

from .asset_store import AssetStore, UnsafeAssetError
from .external_process import external_dll_search
from .addon_host import MANAGED_HOST_VERSION, MANAGED_HOST_ARCHIVE
from . import sm86_sr, amd_nr


USER_AGENT = "LosslessScalingHelper/1.0 (+local companion)"
logger = logging.getLogger("LSCompanion.Releases")


def github_request_headers(*, accept: str = "application/json") -> Dict[str, str]:
    """Build GitHub headers, optionally authenticating private test builds.

    The app-specific variable avoids ever baking a credential into a release.
    Standard GitHub CLI/action variable names are also accepted for developer
    launches. Public repositories continue to work without any token.
    """
    headers = {"Accept": accept, "User-Agent": USER_AGENT}
    token = (
        os.environ.get("LOSSLESS_COMPANION_GITHUB_TOKEN")
        or os.environ.get("GH_TOKEN")
        or os.environ.get("GITHUB_TOKEN")
    )
    if token:
        headers["Authorization"] = f"Bearer {token.strip()}"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    return headers


@dataclass
class ReleaseAsset:
    name: str
    url: str
    size: int = 0
    content_type: Optional[str] = None
    digest: Optional[str] = None
    asset_id: Optional[str] = None


@dataclass
class ReleaseInfo:
    provider: str
    version: str
    name: str
    published_at: Optional[str]
    prerelease: bool
    html_url: str
    notes: str = ""
    assets: List[ReleaseAsset] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {**asdict(self), "assets": [asdict(asset) for asset in self.assets]}


class ReleaseProvider:
    provider_id: str
    allowed_hosts: frozenset[str]

    def list_releases(self, *, channel: str = "stable") -> List[ReleaseInfo]:
        raise NotImplementedError

    def get_release(self, version: str, *, channel: str = "stable") -> ReleaseInfo:
        for release in self.list_releases(channel=channel):
            if release.version.casefold() == version.casefold():
                return release
        raise ValueError(f"Unknown {self.provider_id} release: {version}")


class BundledArchiveReleaseProvider(ReleaseProvider):
    """Expose a native component built with this Companion as a local release."""

    allowed_hosts = frozenset()

    def __init__(self, provider_id: str, archive_name: str):
        self.provider_id = provider_id
        self.archive_name = archive_name

    def archive_path(self) -> Path:
        candidates = []
        frozen_root = getattr(sys, "_MEIPASS", None)
        if frozen_root:
            candidates.append(Path(frozen_root) / "addons" / self.provider_id / self.archive_name)
        candidates.append(
            Path(__file__).resolve().parents[2] / "build" / "native" / self.archive_name
        )
        archive = next((path for path in candidates if path.is_file()), None)
        if archive is None:
            raise RuntimeError(
                f"Bundled {self.provider_id} archive is missing; reinstall Companion or rebuild it"
            )
        return archive.resolve(strict=True)

    def list_releases(self, *, channel: str = "stable") -> List[ReleaseInfo]:
        archive = self.archive_path()
        # Keep this version aligned with native/lsp_neuralrender/addon.json.
        version = "0.3.0-lsc-hdr"
        return [ReleaseInfo(
            provider=self.provider_id,
            version=version,
            name="LS Companion NeuralRender HDR fork",
            published_at=None,
            prerelease=False,
            html_url="https://github.com/jpbrownfield/LosslessScaling-Companion",
            notes="Companion-built NeuralRender fork with scRGB HDR-preserving composition.",
            assets=[ReleaseAsset(
                name=self.archive_name,
                url=f"bundled://{self.archive_name}",
                size=archive.stat().st_size,
                digest=f"sha256:{AssetStore.sha256(archive)}",
            )],
        )]


class BundledAddonManagerReleaseProvider(BundledArchiveReleaseProvider):
    """Only Companion-reviewed host builds may replace the headless integration."""

    cache_identity = MANAGED_HOST_VERSION

    def __init__(self):
        super().__init__("ls-addon-manager", MANAGED_HOST_ARCHIVE)

    def list_releases(self, *, channel: str = "stable") -> List[ReleaseInfo]:
        archive = self.archive_path()
        return [ReleaseInfo(
            provider=self.provider_id, version=MANAGED_HOST_VERSION,
            name="Companion Addon Host (FriedSnowman build-5)",
            published_at=None, prerelease=False,
            html_url="https://github.com/FriedSnowman/ls-addon-manager/releases/tag/build-5",
            notes="Pinned upstream addon suite with Companion's headless host and status bridge. Updated with Companion.",
            assets=[ReleaseAsset(name=self.archive_name, url=f"bundled://{self.archive_name}",
                                 size=archive.stat().st_size, digest=f"sha256:{AssetStore.sha256(archive)}")],
        )]


def _request_json(url: str) -> object:
    parsed = urlparse(url)
    headers = (
        github_request_headers()
        if parsed.hostname == "api.github.com"
        else {"Accept": "application/json", "User-Agent": USER_AGENT}
    )
    request = urllib.request.Request(
        url,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            if response.status != 200:
                raise RuntimeError(f"Release metadata request failed with HTTP {response.status}")
            return json.load(response)
    except urllib.error.HTTPError as error:
        if parsed.hostname == "api.github.com" and error.code == 404:
            raise RuntimeError(
                "GitHub release repository was not found or is private. Public releases work "
                "without credentials; private test builds require the "
                "LOSSLESS_COMPANION_GITHUB_TOKEN environment variable."
            ) from error
        raise


class GitHubReleaseProvider(ReleaseProvider):
    allowed_hosts = frozenset(
        {"api.github.com", "github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"}
    )

    def __init__(self, provider_id: str, repository: str, asset_pattern: str = r"\.zip$"):
        self.provider_id = provider_id
        self.repository = repository
        self.asset_pattern = re.compile(asset_pattern, re.IGNORECASE)

    def list_releases(self, *, channel: str = "stable") -> List[ReleaseInfo]:
        raw = _request_json(f"https://api.github.com/repos/{self.repository}/releases?per_page=30")
        if not isinstance(raw, list):
            raise RuntimeError("Unexpected GitHub release response")
        releases = []
        for item in raw:
            if item.get("draft") or (channel == "stable" and item.get("prerelease")):
                continue
            assets = []
            for asset in item.get("assets", []):
                name = str(asset.get("name") or "")
                if not self.asset_pattern.search(name):
                    continue
                assets.append(
                    ReleaseAsset(
                        name=name,
                        url=str(asset.get("browser_download_url") or ""),
                        size=int(asset.get("size") or 0),
                        content_type=asset.get("content_type"),
                        digest=asset.get("digest"),
                        asset_id=str(asset.get("id")) if asset.get("id") is not None else None,
                    )
                )
            releases.append(
                ReleaseInfo(
                    provider=self.provider_id,
                    version=str(item.get("tag_name") or ""),
                    name=str(item.get("name") or item.get("tag_name") or ""),
                    published_at=item.get("published_at"),
                    prerelease=bool(item.get("prerelease")),
                    html_url=str(item.get("html_url") or ""),
                    notes=str(item.get("body") or ""),
                    assets=assets,
                )
            )
        return releases


class DisplayCommanderReleaseProvider(GitHubReleaseProvider):
    def __init__(self):
        super().__init__("display-commander", "pmnoxx/display-commander", r"^zzz_display_commander\.addon64$")

    def list_releases(self, *, channel="stable"):
        # The rolling debug release is not marked as a GitHub prerelease.
        return [release for release in super().list_releases(channel=channel)
                if "debug" not in (release.version + " " + release.name).casefold()]


class Sm86SrReleaseProvider(ReleaseProvider):
    """Review/pin new binaries before making them available to Companion."""
    provider_id = sm86_sr.PROVIDER
    allowed_hosts = GitHubReleaseProvider.allowed_hosts
    cache_identity = sm86_sr.ARCHIVE_SHA256

    def list_releases(self, *, channel="stable"):
        return [ReleaseInfo(provider=self.provider_id, version=sm86_sr.VERSION,
            name="SM86 Super Resolution (L/M INT8)", published_at=None, prerelease=False,
            html_url=f"https://github.com/sdli1995/dlssg_for_sm86/releases/tag/{sm86_sr.VERSION}",
            notes="Optional community SR backend. Companion deploys only SR; frame generation and architecture spoofing stay off.",
            assets=[ReleaseAsset(name=sm86_sr.ARCHIVE, url=sm86_sr.URL, size=88819934,
                                 digest=f"sha256:{sm86_sr.ARCHIVE_SHA256}")])]


class AmdNrReleaseProvider(ReleaseProvider):
    provider_id = amd_nr.PROVIDER
    allowed_hosts = GitHubReleaseProvider.allowed_hosts
    cache_identity = amd_nr.ARCHIVE_SHA256

    def list_releases(self, *, channel="stable"):
        return [ReleaseInfo(provider=self.provider_id, version=amd_nr.VERSION,
            name="AMD Neural Rendering runtime", published_at=None, prerelease=False,
            html_url=f"https://github.com/{amd_nr.LOCK['repository']}/releases/tag/{amd_nr.VERSION}",
            notes="Optional AMD NR backend. Requires compatible AMD hardware, Windows HIP and your imported NR model. Companion excludes game proxies and FG.",
            assets=[ReleaseAsset(name=amd_nr.ARCHIVE, url=amd_nr.URL, size=amd_nr.LOCK["archive_size"],
                                 digest=f"sha256:{amd_nr.ARCHIVE_SHA256}")])]


class GitLabReleaseProvider(ReleaseProvider):
    allowed_hosts = frozenset({"gitlab.com", "storage.googleapis.com"})

    def __init__(self, provider_id: str, project: str, asset_pattern: str = r"\.zip$"):
        self.provider_id = provider_id
        self.project = project
        self.asset_pattern = re.compile(asset_pattern, re.IGNORECASE)

    def list_releases(self, *, channel: str = "stable") -> List[ReleaseInfo]:
        project = urllib.parse.quote(self.project, safe="")
        raw = _request_json(f"https://gitlab.com/api/v4/projects/{project}/releases")
        if not isinstance(raw, list):
            raise RuntimeError("Unexpected GitLab release response")
        releases = []
        for item in raw:
            tag = str(item.get("tag_name") or "")
            is_prerelease = bool(re.search(r"(?:alpha|beta|rc|pre)", tag, re.IGNORECASE))
            if channel == "stable" and is_prerelease:
                continue
            assets = []
            for asset in item.get("assets", {}).get("links", []):
                name = str(asset.get("name") or "")
                url = str(asset.get("direct_asset_url") or asset.get("url") or "")
                if self.asset_pattern.search(name) or self.asset_pattern.search(url):
                    assets.append(
                        ReleaseAsset(
                            name=name or Path(urlparse(url).path).name,
                            url=url,
                            asset_id=str(asset.get("id")) if asset.get("id") is not None else None,
                        )
                    )
            releases.append(
                ReleaseInfo(
                    provider=self.provider_id,
                    version=tag,
                    name=str(item.get("name") or tag),
                    published_at=item.get("released_at"),
                    prerelease=is_prerelease,
                    html_url=f"https://gitlab.com/{self.project}/-/releases/{urllib.parse.quote(tag)}",
                    notes=str(item.get("description") or ""),
                    assets=assets,
                )
            )
        return releases


class ReShadeReleaseProvider(ReleaseProvider):
    """Official Full Add-On installer, extracted without running Setup."""

    provider_id = "reshade"
    allowed_hosts = frozenset({"reshade.me"})
    # The live homepage wraps the version in markup, e.g.
    # "<strong>Version 6.8.0</strong> was <a ...>released</a> on ...".
    # Strip tags first, then try each pattern in order.
    _version_patterns = (
        re.compile(r"Version\s+([0-9]+(?:\.[0-9]+){1,3})\s+was\s+released", re.IGNORECASE),
        re.compile(r"Download\s+ReShade\s+([0-9]+(?:\.[0-9]+){1,3})", re.IGNORECASE),
        re.compile(r"ReShade\s+([0-9]+(?:\.[0-9]+){1,3})\s+was\s+released", re.IGNORECASE),
    )

    def list_releases(self, *, channel: str = "stable") -> List[ReleaseInfo]:
        request = urllib.request.Request("https://reshade.me/", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read(2 * 1024 * 1024).decode("utf-8", errors="replace")
        text = re.sub(r"<[^>]+>", " ", body)
        text = re.sub(r"\s+", " ", text)
        version = None
        for pattern in self._version_patterns:
            match = pattern.search(text)
            if match:
                version = match.group(1)
                break
        if not version:
            raise RuntimeError("Could not identify the current ReShade version from the official site")
        return [
            ReleaseInfo(
                provider=self.provider_id,
                version=version,
                name=f"ReShade {version}",
                published_at=None,
                prerelease=False,
                html_url="https://reshade.me/",
                notes="Official Full Add-On runtime; Setup is extracted, not executed.",
                assets=[ReleaseAsset(name=f"ReShade_Setup_{version}_Addon.exe",
                                     url=f"https://reshade.me/downloads/ReShade_Setup_{version}_Addon.exe")],
            )
        ]


class RhiDlssNrReleaseProvider(ReleaseProvider):
    """Resolve RHI's DLSSNR catalog to digest-bearing GitHub release assets."""

    provider_id = "dlssnr-community-runtime"
    allowed_hosts = GitHubReleaseProvider.allowed_hosts
    manifest_url = "https://raw.githubusercontent.com/RankFTW/RHI/main/dlss_manifest.json"
    repository = "RankFTW/rhi-repo"
    _release_path = re.compile(
        r"^/RankFTW/rhi-repo/releases/download/([^/]+)/([^/]+)$", re.IGNORECASE
    )

    def list_releases(self, *, channel: str = "stable") -> List[ReleaseInfo]:
        raw = _request_json(self.manifest_url)
        entries = raw.get("dlssnr") if isinstance(raw, dict) else None
        if not isinstance(entries, list):
            raise RuntimeError("RHI's DLSSNR manifest has an unexpected format")

        releases = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            version = str(entry.get("version") or "")
            url = str(entry.get("url") or "")
            parsed = urlparse(url)
            match = self._release_path.fullmatch(parsed.path)
            if parsed.scheme != "https" or parsed.hostname != "github.com" or not match:
                raise RuntimeError("RHI's DLSSNR manifest referenced an unapproved download")
            tag = urllib.parse.unquote(match.group(1))
            asset_name = urllib.parse.unquote(match.group(2))
            # RHI changed the human-readable SF-v2 version from
            # ``310.8.SF-v2`` to ``310.8.2 (20/30/40/50)`` while retaining the
            # stable release tag and asset name.  Those verified URL components
            # are the compatibility identity; filtering on the display label
            # made every current NeuralRender install report that no runtime
            # was available.
            compatibility_id = f"{tag}/{asset_name}".casefold()
            if (
                not version
                or "sf-v2" not in compatibility_id
                or not asset_name.casefold().endswith(".zip")
            ):
                continue

            metadata = _request_json(
                "https://api.github.com/repos/RankFTW/rhi-repo/releases/tags/"
                + urllib.parse.quote(tag, safe="")
            )
            if not isinstance(metadata, dict):
                raise RuntimeError("Unexpected RHI runtime release response")
            asset = next(
                (
                    item for item in metadata.get("assets", [])
                    if str(item.get("name") or "") == asset_name
                    and str(item.get("browser_download_url") or "") == url
                ),
                None,
            )
            if asset is None:
                raise RuntimeError("RHI's DLSSNR manifest does not match its GitHub release")
            releases.append(
                ReleaseInfo(
                    provider=self.provider_id,
                    version=version,
                    name=f"Community DLSSNR {version}",
                    published_at=metadata.get("published_at"),
                    prerelease=False,
                    html_url=str(metadata.get("html_url") or ""),
                    notes=(
                        "Community-modified, unsigned NVIDIA-derived runtime published by "
                        "RankFTW/rhi-repo; it is not an official NVIDIA release."
                    ),
                    assets=[ReleaseAsset(
                        name=asset_name,
                        url=url,
                        size=int(asset.get("size") or 0),
                        content_type=asset.get("content_type"),
                        digest=asset.get("digest"),
                        asset_id=str(asset.get("id")) if asset.get("id") is not None else None,
                    )],
                )
            )
        if not releases:
            raise RuntimeError("RHI's manifest contains no community-modified DLSSNR runtime")
        return releases


class ProviderRegistry:
    def __init__(self):
        self.providers: Dict[str, ReleaseProvider] = {
            "companion": GitHubReleaseProvider(
                "companion",
                "jpbrownfield/LosslessScaling-Companion",
                r"^LosslessCompanion-Setup-x64\.exe(?:\.sha256)?$",
            ),
            "lossless-proxy": GitHubReleaseProvider(
                "lossless-proxy", "FrankBarretta/LosslessProxy", r"(?:\.zip$|Lossless\.dll$)"
            ),
            "ls-addon-manager": BundledAddonManagerReleaseProvider(),
            "sm86-sr": Sm86SrReleaseProvider(),
            amd_nr.PROVIDER: AmdNrReleaseProvider(),
            "lsp-neural-render": BundledArchiveReleaseProvider(
                "lsp-neural-render", "LSP-NeuralRender.zip"
            ),
            "dlss5-feeder": GitHubReleaseProvider(
                "dlss5-feeder", "jlrouzies-fr/DLSS5-Feeder", r"\.zip$"
            ),
            "dlssnr-community-runtime": RhiDlssNrReleaseProvider(),
            "special-k": GitHubReleaseProvider(
                "special-k", "SpecialKO/SpecialK", r"\.(?:zip|7z)$"
            ),
            "display-commander": DisplayCommanderReleaseProvider(),
            "presentmon": GitHubReleaseProvider(
                "presentmon",
                "GameTechDev/PresentMon",
                r"^PresentMon-(?!.*(?:arm|x86(?!_64)))[^/]*x64\.exe$",
            ),
            "reshade": ReShadeReleaseProvider(),
        }

    def get(self, provider_id: str) -> ReleaseProvider:
        try:
            return self.providers[provider_id]
        except KeyError as error:
            raise ValueError(f"Unknown release provider: {provider_id}") from error

    def list_provider_ids(self) -> List[str]:
        return sorted(self.providers)


class ReleaseManager:
    def __init__(
        self,
        store: AssetStore,
        registry: Optional[ProviderRegistry] = None,
        *,
        check_interval_hours: int = 24,
    ):
        self.store = store
        self.registry = registry or ProviderRegistry()
        self.check_interval_seconds = max(1, check_interval_hours) * 3600
        self.cache_path = store.root / "release-cache.json"
        self._cache_lock = threading.RLock()

    def _read_cache(self) -> Dict:
        if not self.cache_path.is_file():
            return {"schema_version": 1, "entries": {}}
        try:
            loaded = json.loads(self.cache_path.read_text(encoding="utf-8"))
            return loaded if isinstance(loaded.get("entries"), dict) else {"schema_version": 1, "entries": {}}
        except (OSError, json.JSONDecodeError, AttributeError):
            return {"schema_version": 1, "entries": {}}

    def check(
        self, provider_id: str, *, channel: str = "stable", force: bool = False
    ) -> List[Dict]:
        provider = self.registry.get(provider_id)
        if channel not in {"stable", "prerelease"}:
            raise ValueError("Unsupported release channel")
        key = f"{provider_id}:{channel}"
        if getattr(provider, "cache_identity", None):
            key += ":" + provider.cache_identity
        with self._cache_lock:
            cache = self._read_cache()
            cached = cache["entries"].get(key)
            if (
                not force
                and cached
                and time.time() - float(cached.get("checked_at") or 0) < self.check_interval_seconds
            ):
                return list(cached.get("releases") or [])
            try:
                releases = [release.to_dict() for release in provider.list_releases(channel=channel)]
            except Exception:
                if cached:
                    logger.warning("Release check failed; using cached %s metadata", key, exc_info=True)
                    return list(cached.get("releases") or [])
                raise
            cache["entries"][key] = {"checked_at": time.time(), "releases": releases}
            AssetStore._atomic_json(self.cache_path, cache)
            return releases

    def stage_release(
        self,
        provider_id: str,
        version: str,
        asset_name: str,
        operation_id: str,
        *,
        channel: str = "stable",
        max_bytes: int = 1024 * 1024 * 1024,
        progress_callback=None,
    ) -> Dict:
        provider = self.registry.get(provider_id)
        release_data = next(
            (
                candidate
                for candidate in self.check(provider_id, channel=channel)
                if str(candidate.get("version") or "").casefold() == version.casefold()
            ),
            None,
        )
        if release_data is None:
            raise ValueError(f"Unknown {provider_id} release: {version}")
        asset_data = next(
            (candidate for candidate in release_data.get("assets", []) if candidate.get("name") == asset_name),
            None,
        )
        if asset_data is None:
            raise ValueError("Release asset is not part of the selected official release")
        asset = ReleaseAsset(**asset_data)
        def progress(phase, received=0, total=0):
            if progress_callback:
                progress_callback({"phase": phase, "receivedBytes": received, "totalBytes": total})
        release = ReleaseInfo(
            **{key: value for key, value in release_data.items() if key != "assets"},
            assets=[ReleaseAsset(**value) for value in release_data.get("assets", [])],
        )
        if asset.size and asset.size > max_bytes:
            raise UnsafeAssetError("Release asset exceeds the download size limit")
        if (
            not asset.name
            or asset.name != Path(asset.name).name
            or any(character in asset.name for character in ("/", "\\", ":", "\0"))
        ):
            raise UnsafeAssetError("Release asset has an unsafe filename")
        if isinstance(provider, BundledArchiveReleaseProvider):
            progress("verifying")
            archive = provider.archive_path()
            expected_digest = asset.digest if asset.digest and asset.digest.startswith("sha256:") else None
            result = self.store.import_release_archive(
                str(archive),
                provider=provider_id,
                version=version,
                expected_sha256=expected_digest,
                source_metadata={
                    "kind": "bundled-native-component",
                    "release": release.to_dict(),
                    "verification": {
                        "calculated_sha256": self.store.sha256(archive),
                        "publisher_sha256": expected_digest.removeprefix("sha256:") if expected_digest else None,
                        "publisher_digest_verified": bool(expected_digest),
                        "downloaded_over_https": False,
                        "host_allowlisted": True,
                    },
                },
            )
            self.store.audit(
                "bundled_release_staged",
                provider=provider_id,
                version=version,
                asset=asset.name,
                archive_sha256=result.get("archive_sha256"),
            )
            return result

        parsed = urlparse(asset.url)
        if parsed.scheme != "https" or parsed.hostname not in provider.allowed_hosts:
            raise UnsafeAssetError("Release asset URL is outside the provider allowlist")

        staging = self.store.new_staging_directory(operation_id)
        destination = staging / asset.name
        request = urllib.request.Request(asset.url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as output:
                final_url = urlparse(response.geturl())
                if final_url.scheme != "https" or final_url.hostname not in provider.allowed_hosts:
                    raise UnsafeAssetError("Release download redirected outside the provider allowlist")
                received = 0
                total = asset.size or int(response.headers.get("Content-Length") or 0)
                progress("downloading", 0, total)
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > max_bytes:
                        raise UnsafeAssetError("Release asset exceeded the download size limit")
                    output.write(chunk)
                    progress("downloading", received, total)
            progress("verifying", received, total)
            if asset.size and destination.stat().st_size != asset.size:
                raise UnsafeAssetError("Downloaded release size does not match metadata")
            expected_digest = asset.digest if asset.digest and asset.digest.startswith("sha256:") else None
            calculated_digest = self.store.sha256(destination)
            if (
                expected_digest
                and calculated_digest.casefold()
                != expected_digest.removeprefix("sha256:").casefold()
            ):
                raise UnsafeAssetError("Downloaded release SHA-256 does not match publisher metadata")
            metadata = {
                "release": release.to_dict(),
                "asset": asdict(asset),
                "verification": {
                    "calculated_sha256": calculated_digest,
                    "publisher_sha256": expected_digest.removeprefix("sha256:")
                    if expected_digest
                    else None,
                    "publisher_digest_available": bool(expected_digest),
                    "publisher_digest_verified": bool(expected_digest),
                    "downloaded_over_https": True,
                    "host_allowlisted": True,
                },
            }
            self.store.audit(
                "official_release_downloaded",
                provider=provider_id,
                version=version,
                asset=asset.name,
                sha256=calculated_digest,
                publisher_digest_verified=bool(expected_digest),
                size=destination.stat().st_size,
            )
            progress("staging", received, total)
            if provider_id == "display-commander":
                if AssetStore.pe_architecture(destination) != "x64":
                    raise UnsafeAssetError("Display Commander must be a valid x64 ReShade add-on")
                result = self.store.import_release_file(
                    str(destination), provider=provider_id, version=version,
                    expected_sha256=expected_digest, source_metadata=metadata,
                )
            elif provider_id in {"reshade", "special-k"}:
                result = self._stage_graphics_runtime(destination, staging, provider_id, version, metadata)
            elif zipfile.is_zipfile(destination):
                result = self.store.import_release_archive(
                    str(destination),
                    provider=provider_id,
                    version=version,
                    expected_sha256=expected_digest,
                    source_metadata=metadata,
                )
            else:
                result = self.store.import_release_file(
                    str(destination),
                    provider=provider_id,
                    version=version,
                    expected_sha256=expected_digest,
                    source_metadata=metadata,
                )
            self.store.audit(
                "official_release_staged",
                provider=provider_id,
                version=version,
                asset=asset.name,
                archive_sha256=result.get("archive_sha256"),
            )
            return result
        except Exception as error:
            self.store.audit(
                "official_release_stage_failed",
                outcome="failed",
                provider=provider_id,
                version=version,
                asset=asset.name,
                error=type(error).__name__,
                message=str(error),
            )
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _stage_graphics_runtime(self, archive, staging, provider, version, metadata):
        from .graphics_source_importer import GraphicsSourceImporter

        importer = GraphicsSourceImporter(self.store)
        if provider == "reshade":
            package = importer._stage_reshade({"path": str(archive), "version": version}, "reshade-" + uuid.uuid4().hex)
        else:
            extracted = staging / "special-k-runtime"
            extracted.mkdir()
            if zipfile.is_zipfile(archive):
                with zipfile.ZipFile(archive) as bundle:
                    members = [(item, self.store._safe_zip_member(item)) for item in bundle.infolist()]
                    matches = [(item, name) for item, name in members if name.name.casefold() == "specialk64.dll" and not item.is_dir()]
                    if len(matches) != 1 or not 0 < matches[0][0].file_size <= importer.MAX_RUNTIME_BYTES:
                        raise UnsafeAssetError("Special K archive must contain one bounded SpecialK64.dll")
                    runtime = extracted / "SpecialK64.dll"
                    with bundle.open(matches[0][0]) as source, runtime.open("wb") as output:
                        shutil.copyfileobj(source, output)
            else:
                import py7zr

                with py7zr.SevenZipFile(archive, "r") as bundle:
                    members = bundle.list()
                    if len(members) > 4096 or sum(item.uncompressed or 0 for item in members) > 1024 ** 3:
                        raise UnsafeAssetError("Special K archive exceeds extraction limits")
                    names = [(item, self.store._safe_zip_member(zipfile.ZipInfo(item.filename))) for item in members]
                    matches = [(item, name) for item, name in names if name.name.casefold() == "specialk64.dll" and not item.is_directory]
                    if len(matches) != 1 or not 0 < (matches[0][0].uncompressed or 0) <= importer.MAX_RUNTIME_BYTES:
                        raise UnsafeAssetError("Special K archive must contain one bounded SpecialK64.dll")
                    bundle.extract(path=extracted, targets=[matches[0][0].filename])
                    runtime = (extracted / matches[0][1]).resolve(strict=True)
                    if extracted.resolve() not in runtime.parents or runtime.is_symlink():
                        raise UnsafeAssetError("Special K runtime escaped extraction")
            package = importer._stage_special_k({"path": str(runtime), "version": version})
        package["source"]["download"] = metadata
        AssetStore._atomic_json(Path(package["payload_path"]).parent / "package.json", package)
        return package
