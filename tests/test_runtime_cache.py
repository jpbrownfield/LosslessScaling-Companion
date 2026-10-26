import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from companion.services.asset_store import UnsafeAssetError
from companion.services.runtime_cache import RuntimeCache, RuntimeCacheConflict, create_directory_junction


def unavailable_junction(link, target):
    raise OSError("Junctions unavailable")


class RuntimeCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.steam = self.directory / "Steam"
        self.steam.mkdir()
        self.exe = self.steam / "LosslessScaling.exe"
        self.engine = self.steam / "Lossless.dll"
        self.exe.write_bytes(b"fake executable - never launched")
        self.engine.write_bytes(b"genuine engine")
        self.host = self.directory / "echo.dll"
        self.host.write_bytes(b"echo manager")
        self.proxy = self.directory / "dxgi.dll"
        self.proxy.write_bytes(b"reshade proxy")
        self.shaders = self.directory / "Shaders"
        self.shaders.mkdir()
        (self.shaders / "test.fx").write_text("original shader", encoding="utf-8")
        self.baseline = {"LosslessScaling.exe": self.exe, "Lossless.dll": self.engine}
        self.cache = RuntimeCache(self.directory / "cache", junction_creator=unavailable_junction)

    def prepare(self, profile="game", **kwargs):
        return self.cache.prepare(profile, self.baseline, **kwargs)

    def test_echo_stages_private_engine_pair_without_changing_steam(self):
        runtime = self.prepare(echo_host=self.host, config_files={"config.json": "{}"})
        self.assertEqual((runtime.root / "Lossless.dll").read_bytes(), b"echo manager")
        self.assertEqual((runtime.root / "Lossless_original.dll").read_bytes(), b"genuine engine")
        self.assertEqual(self.engine.read_bytes(), b"genuine engine")
        self.assertEqual(sorted(p.name for p in self.steam.iterdir()), ["Lossless.dll", "LosslessScaling.exe"])
        self.assertEqual((runtime.root / "Lossless.dll").stat().st_nlink, 1)

    def test_variants_are_independent(self):
        vanilla = self.prepare()
        echo = self.prepare(echo_host=self.host)
        reshade = self.prepare(echo_host=self.host, reshade_files={"dxgi.dll": self.proxy})
        self.assertEqual(len({vanilla.root, echo.root, reshade.root}), 3)
        self.assertEqual(reshade.variant, "echo-reshade")
        self.assertFalse((vanilla.root / "Lossless_original.dll").exists())
        self.assertFalse((vanilla.root / "dxgi.dll").exists())
        self.assertEqual((vanilla.root / "Lossless.dll").read_bytes(), b"genuine engine")

    def test_reuse_does_not_copy_files_or_reset_runtime_edits(self):
        runtime = self.prepare(config_files={"config.json": "{}"})
        config = runtime.root / "config.json"
        config.write_text('{"edited":true}', encoding="utf-8")
        with patch("companion.services.runtime_cache.shutil.copy2", side_effect=AssertionError("copied on cache hit")):
            reused = self.prepare(config_files={"config.json": "{}"})
        self.assertTrue(reused.reused)
        self.assertEqual(runtime.root, reused.root)
        self.assertEqual(config.read_text(encoding="utf-8"), '{"edited":true}')

    def test_profiles_and_changed_configs_are_isolated(self):
        first = self.prepare(config_files={"config.json": "{}"})
        second = self.prepare("other", config_files={"config.json": "{}"})
        changed = self.prepare(config_files={"config.json": '{"new":true}'})
        self.assertEqual(len({first.root, second.root, changed.root}), 3)
        (first.root / "config.json").write_text("edited", encoding="utf-8")
        self.assertEqual((second.root / "config.json").read_text(encoding="utf-8"), "{}")

    def test_source_update_creates_generation_without_overwriting_old_runtime(self):
        old = self.prepare()
        self.engine.write_bytes(b"updated genuine engine")
        new = self.prepare()
        self.assertNotEqual(old.root, new.root)
        self.assertEqual((old.root / "Lossless.dll").read_bytes(), b"genuine engine")
        self.assertEqual((new.root / "Lossless.dll").read_bytes(), b"updated genuine engine")

    def test_modified_binary_and_unexpected_injection_fail_closed(self):
        runtime = self.prepare()
        (runtime.root / "dxgi.dll").write_bytes(b"unexpected injection")
        with self.assertRaises(RuntimeCacheConflict):
            self.prepare()
        (runtime.root / "dxgi.dll").unlink()
        (runtime.root / "Lossless.dll").write_bytes(b"tampered")
        with self.assertRaises(RuntimeCacheConflict):
            self.prepare()

    def test_failed_copy_leaves_no_published_generation(self):
        with patch("companion.services.runtime_cache.shutil.copy2", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.prepare()
        self.assertEqual(list((self.cache.root / ".building").iterdir()), [])
        self.assertFalse((self.cache.root / "runtimes").exists())

    def test_corrupt_copy_is_rejected_before_publish(self):
        def corrupt_copy(source, target):
            Path(target).write_bytes(b"bad copy")
        with patch("companion.services.runtime_cache.shutil.copy2", side_effect=corrupt_copy):
            with self.assertRaises(RuntimeCacheConflict):
                self.prepare()
        self.assertFalse((self.cache.root / "runtimes").exists())

    def test_copy_fallback_uses_private_asset_snapshot(self):
        runtime = self.prepare(shared_assets={"reshade-shaders/Shaders": self.shaders})
        shader = runtime.root / "reshade-shaders/Shaders/test.fx"
        self.assertEqual(runtime.linked_assets, ())
        shader.write_text("runtime edit", encoding="utf-8")
        self.assertEqual((self.shaders / "test.fx").read_text(encoding="utf-8"), "original shader")
        with self.assertRaises(RuntimeCacheConflict):
            self.prepare(shared_assets={"reshade-shaders/Shaders": self.shaders})

    def test_paths_and_reserved_configs_are_rejected(self):
        for name in ("../config.json", "C:/config.json", "config.json:stream", "CON.json", ".companion-runtime.json", "nested/../../x.ini"):
            with self.subTest(name=name), self.assertRaises(UnsafeAssetError):
                self.prepare(config_files={name: "{}"})
        with self.assertRaises(UnsafeAssetError):
            self.prepare("../escape")
        with self.assertRaises(UnsafeAssetError):
            self.prepare(addon_files={"plugin.dll": self.host})

    def test_only_passive_asset_directories_can_be_shared(self):
        with self.assertRaises(UnsafeAssetError):
            self.prepare(shared_assets={".": self.steam})
        (self.shaders / "config.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(UnsafeAssetError):
            self.prepare(shared_assets={"reshade-shaders/Shaders": self.shaders})

    def test_cache_cannot_be_inside_steam(self):
        cache = RuntimeCache(self.steam / "cache")
        with self.assertRaises(UnsafeAssetError):
            cache.prepare("game", self.baseline)
        self.assertFalse(cache.root.exists())

    @unittest.skipUnless(os.name == "nt", "Windows junction integration")
    def test_real_junction_targets_cache_not_steam_and_cleanup_preserves_target(self):
        self.cache.junction_creator = create_directory_junction
        runtime = self.prepare(shared_assets={"reshade-shaders/Shaders": self.shaders})
        self.assertEqual(runtime.linked_assets, ("reshade-shaders/Shaders",))
        link = runtime.root / "reshade-shaders/Shaders"
        target = link.resolve(strict=True)
        self.assertTrue(link.is_junction())
        self.assertTrue(target.is_relative_to(self.cache.root / "assets"))
        shutil.rmtree(runtime.root)
        self.assertTrue((target / "test.fx").exists())
        self.assertTrue((self.shaders / "test.fx").exists())

    @unittest.skipUnless(os.name == "nt", "Windows junction integration")
    def test_internal_cache_junction_is_rejected_before_writes(self):
        self.cache.root.mkdir()
        outside = self.directory / "outside"
        outside.mkdir()
        create_directory_junction(self.cache.root / ".building", outside)
        with self.assertRaises(RuntimeCacheConflict):
            self.prepare()
        self.assertEqual(list(outside.iterdir()), [])
