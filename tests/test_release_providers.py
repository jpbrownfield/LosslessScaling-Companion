import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from companion.services.asset_store import AssetStore
from companion.services.release_providers import (
    ProviderRegistry,
    RhiDlssNrReleaseProvider,
    ReShadeReleaseProvider,
    ReleaseInfo,
    ReleaseManager,
    ReleaseProvider,
    _request_json,
    github_request_headers,
)


class CountingProvider(ReleaseProvider):
    provider_id = "counting"
    allowed_hosts = frozenset({"example.invalid"})

    def __init__(self):
        self.calls = 0

    def list_releases(self, *, channel="stable"):
        self.calls += 1
        return [
            ReleaseInfo(
                provider=self.provider_id,
                version="v1",
                name="Version 1",
                published_at=None,
                prerelease=False,
                html_url="https://example.invalid/releases/v1",
            )
        ]


class ReleaseManagerTests(unittest.TestCase):
    def test_private_github_token_is_added_only_when_configured(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertNotIn("Authorization", github_request_headers())
        with patch.dict(
            "os.environ", {"LOSSLESS_COMPANION_GITHUB_TOKEN": "private-test-token"}, clear=True
        ):
            headers = github_request_headers()
        self.assertEqual(headers["Authorization"], "Bearer private-test-token")

    def test_private_repository_404_has_actionable_message(self):
        error = urllib.error.HTTPError(
            "https://api.github.com/repos/example/private/releases",
            404,
            "Not Found",
            {},
            None,
        )
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "private test builds require"):
                _request_json("https://api.github.com/repos/example/private/releases")

    def test_companion_provider_accepts_only_installer_and_checksum(self):
        provider = ProviderRegistry().get("companion")
        self.assertIsNotNone(provider.asset_pattern.search("LosslessCompanion-Setup-x64.exe"))
        self.assertIsNotNone(provider.asset_pattern.search("LosslessCompanion-Setup-x64.exe.sha256"))
        self.assertIsNone(provider.asset_pattern.search("LosslessCompanion-portable.zip"))

    def test_presentmon_provider_accepts_only_x64_console_binary(self):
        provider = ProviderRegistry().get("presentmon")
        self.assertIsNotNone(provider.asset_pattern.search("PresentMon-2.4.1-x64.exe"))
        self.assertIsNone(provider.asset_pattern.search("PresentMon-2.4.1-x86.exe"))
        self.assertIsNone(provider.asset_pattern.search("PresentMon-v2.4.1.msi"))

    def test_release_checks_use_persistent_interval_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AssetStore(str(Path(directory) / "store"))
            provider = CountingProvider()
            registry = ProviderRegistry()
            registry.providers = {"counting": provider}
            manager = ReleaseManager(store, registry, check_interval_hours=24)

            self.assertEqual(manager.check("counting")[0]["version"], "v1")
            self.assertEqual(manager.check("counting")[0]["version"], "v1")
            self.assertEqual(provider.calls, 1)

            second_manager = ReleaseManager(store, registry, check_interval_hours=24)
            self.assertEqual(second_manager.check("counting")[0]["version"], "v1")
            self.assertEqual(provider.calls, 1)


class RhiDlssNrReleaseProviderTests(unittest.TestCase):
    def test_resolves_modified_runtime_through_digest_bearing_github_release(self):
        download = (
            "https://github.com/RankFTW/rhi-repo/releases/download/"
            "dlssnr-310.8.SF-v2/nvngx_dlssnr_310.8.SF-v2.zip"
        )

        def metadata(url):
            if url == RhiDlssNrReleaseProvider.manifest_url:
                return {"dlssnr": [
                    {"version": "310.8.SF-v2", "url": download},
                    {
                        "version": "310.8.0",
                        "url": "https://github.com/RankFTW/rhi-repo/releases/download/"
                        "dlssnr-310.8.0/nvngx_dlssnr_310.8.0.zip",
                    },
                ]}
            return {
                "published_at": "2026-09-01T00:00:00Z",
                "html_url": "https://github.com/RankFTW/rhi-repo/releases/tag/dlssnr-310.8.SF-v2",
                "assets": [{
                    "id": 123,
                    "name": "nvngx_dlssnr_310.8.SF-v2.zip",
                    "browser_download_url": download,
                    "size": 100,
                    "content_type": "application/zip",
                    "digest": "sha256:" + "a" * 64,
                }],
            }

        with patch("companion.services.release_providers._request_json", side_effect=metadata):
            releases = RhiDlssNrReleaseProvider().list_releases()

        self.assertEqual(len(releases), 1)
        self.assertEqual(releases[0].version, "310.8.SF-v2")
        self.assertEqual(releases[0].assets[0].digest, "sha256:" + "a" * 64)
        self.assertIn("unsigned", releases[0].notes)

class ReShadeReleaseProviderTests(unittest.TestCase):
    def _run_with_body(self, body: str):
        import urllib.request

        provider = ReShadeReleaseProvider()

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, size=-1):
                return body.encode("utf-8")

        original = urllib.request.urlopen
        urllib.request.urlopen = lambda request, timeout=20: FakeResponse()
        try:
            return provider.list_releases()
        finally:
            urllib.request.urlopen = original

    def test_parses_homepage_with_markup_between_words(self):
        # Live markup (Sep 2026): "<strong>Version 6.8.0</strong> was
        # <a ...>released</a> on ...". The old pattern expected the literal
        # text "Version X was" and failed.
        body = (
            '<h1>Download</h1><p><strong>Version 6.8.0</strong> was '
            '<a href="/releases">released</a> on August 2nd 2026.</p>'
        )
        releases = self._run_with_body(body)
        self.assertEqual(releases[0].version, "6.8.0")

    def test_falls_back_to_download_link(self):
        body = '<p><a href="/downloads/x">Download ReShade 6.7.3</a></p>'
        releases = self._run_with_body(body)
        self.assertEqual(releases[0].version, "6.7.3")

    def test_raises_when_no_version_found(self):
        with self.assertRaises(RuntimeError):
            self._run_with_body("<html><body>No version here</body></html>")


if __name__ == "__main__":
    unittest.main()
