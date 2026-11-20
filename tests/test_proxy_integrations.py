"""Legacy-host retirement regressions and shared x64 PE fixture."""

import unittest

from companion.core.models import Profile
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
        for provider in ("lossless-proxy", "lsp-neural-render", "display-commander"):
            with self.subTest(provider=provider):
                self.assertNotIn(provider, registry.list_provider_ids())
                with self.assertRaisesRegex(ValueError, "Unknown release provider"):
                    registry.get(provider)

    def test_current_feature_providers_remain_available(self):
        registry = ProviderRegistry()
        for provider in ("ls-addon-manager", "reshade", "special-k",
                         "dlssnr-community-runtime"):
            with self.subTest(provider=provider):
                self.assertIsNotNone(registry.get(provider))

    def test_retired_settings_are_ignored_without_enabling_reshade(self):
        profile = Profile.model_validate({
            "name": "Legacy", "graphics": {
                "display_commander": {"enabled": True, "inject_reflex": True},
                "special_k": {"enabled": True, "experimental_reflex": True,
                              "sdr_to_hdr": True, "experimental_smooth_motion": True},
            },
            "rtss": {"enabled": True, "limit_method": "nvidia_reflex"},
        })
        data = profile.model_dump()
        self.assertNotIn("display_commander", data["graphics"])
        self.assertNotIn("experimental_reflex", data["graphics"]["special_k"])
        self.assertFalse(profile.graphics.reshade.enabled)
        self.assertEqual(profile.rtss.limit_method, "async")
        self.assertTrue(profile.graphics.special_k.sdr_to_hdr)
        self.assertTrue(profile.graphics.special_k.experimental_smooth_motion)
        self.assertEqual(Profile.model_validate(data).model_dump(), data)