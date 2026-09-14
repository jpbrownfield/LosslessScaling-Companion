"""Verified metadata and staged downloads from official graphics-component sources."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

from .asset_store import AssetStore, UnsafeAssetError


USER_AGENT = "LosslessScalingHelper/1.0 (+local companion)"
logger = logging.getLogger("LSCompanion.Releases")


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


def _request_json(url: str) -> object:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        if response.status != 200:
            raise RuntimeError(f"Release metadata request failed with HTTP {response.status}")
        return json.load(response)


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
    """Version notification only; downloads remain an official-site user handoff."""

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
                notes="Download/import must be completed from the official ReShade site.",
                assets=[],
            )
        ]


class ProviderRegistry:
    def __init__(self):
        self.providers: Dict[str, ReleaseProvider] = {
            "lossless-proxy": GitHubReleaseProvider(
                "lossless-proxy", "FrankBarretta/LosslessProxy", r"(?:\.zip$|Lossless\.dll$)"
            ),
            "lsp-neural-render": GitLabReleaseProvider(
                "lsp-neural-render", "andreiday/LSP-NeuralRender", r"\.zip$"
            ),
            "dlss5-feeder": GitHubReleaseProvider(
                "dlss5-feeder", "jlrouzies-fr/DLSS5-Feeder", r"\.zip$"
            ),
            "lsp-reshade": GitHubReleaseProvider(
                "lsp-reshade", "FrankBarretta/LSP-ReShade", r"(?:\.zip$|\.dll$)"
            ),
            "special-k": GitHubReleaseProvider(
                "special-k", "SpecialKO/SpecialK", r"\.(?:zip|7z)$"
            ),
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
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > max_bytes:
                        raise UnsafeAssetError("Release asset exceeded the download size limit")
                    output.write(chunk)
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
            if zipfile.is_zipfile(destination):
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
            import shutil

            shutil.rmtree(staging, ignore_errors=True)
