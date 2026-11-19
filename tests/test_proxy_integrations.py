"""Legacy-host retirement regressions and shared x64 PE fixture."""

import unittest

from companion.services.release_providers import ProviderRegistry


def x64_pe(payload: bytes = b"") -> bytes:
    image = bytearray(0x88)
    image[0:2] = b"MZ"
    image[0x3C:0x40] = (0x80).to_bytes(4, "little")
    image[0x80:0x84] = b"PE\0\0"
    image[0x84:0x86] = (0x8664).to_bytes(2, "little")
    return bytes(image) + payload


class RetiredHostTests(unittest.TestCase):
    def test_retired_packages_are_not_downloadable(self):
        registry = ProviderRegistry()
        for provider in ("lossless-proxy", "lsp-neural-render"):
            with self.subTest(provider=provider):
                self.assertNotIn(provider, registry.list_provider_ids())
                with self.assertRaisesRegex(ValueError, "Unknown release provider"):
                    registry.get(provider)

    def test_current_feature_providers_remain_available(self):
        registry = ProviderRegistry()
        for provider in ("ls-addon-manager", "reshade", "special-k",
                         "display-commander", "dlssnr-community-runtime"):
            with self.subTest(provider=provider):
                self.assertIsNotNone(registry.get(provider))