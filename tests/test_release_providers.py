import tempfile
import unittest
from pathlib import Path

from companion.services.asset_store import AssetStore
from companion.services.release_providers import (
    ProviderRegistry,
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


if __name__ == "__main__":
    unittest.main()
