import tempfile
import unittest
from pathlib import Path

from companion.services.asset_store import AssetStore
from companion.services.release_providers import (
    ProviderRegistry,
    ReShadeReleaseProvider,
    ReleaseInfo,
    ReleaseManager,
    ReleaseProvider,
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
