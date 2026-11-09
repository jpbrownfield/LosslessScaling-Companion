import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from companion.core.models import Profile
from companion.services import amd_nr
from companion.services.addon_config import AddonConfigService
from companion.services.addon_host import MANAGED_HOST_VERSION
from companion.services.asset_store import AssetStore
from companion.services.release_providers import ProviderRegistry

ROOT = Path(__file__).resolve().parents[1]


class AmdNrTests(unittest.TestCase):
    def test_optional_provider_is_pinned_and_offline(self):
        with patch("urllib.request.urlopen", side_effect=AssertionError("No metadata network")):
            release = ProviderRegistry().get(amd_nr.PROVIDER).list_releases()[0]
        self.assertEqual(release.version, "1.3.2")
        self.assertEqual(release.assets[0].digest, "sha256:" + amd_nr.ARCHIVE_SHA256)
        self.assertEqual(release.assets[0].size, amd_nr.LOCK["archive_size"])

    def test_header_and_pin_agree_and_whitelist_excludes_proprietary_models(self):
        self.assertEqual(hashlib.sha256((ROOT / "native/amd_neuralrender/vendor/dlss5nr.h").read_bytes()).hexdigest(), amd_nr.LOCK["header_sha256"])
        names = set(amd_nr.FILES)
        self.assertIn("dlss5nr_runtime.dll", names)
        self.assertIn("zluda/nvcuda.dll", names)
        self.assertIn("cache/ComputeCache/zluda2.db", names)
        self.assertFalse(any(name in names for name in ("dxgi.dll", "version.dll", "OptiScaler.dll", "nvngx_dlssnr.dll", "nvngx_dlss.dll")))
        self.assertFalse(any(name.lower().endswith(".exe") or "ngx_fg" in name or "kernels_fg" in name for name in names))
        self.assertNotIn("kernels/kernels_sr.txt", names)
        self.assertNotIn("kernels/kernels_rr.txt", names)
        self.assertFalse(any("dltss_pwin" in name or name.startswith("kernels/rr") for name in names))
        for name, expected in amd_nr.LOCK["zluda"]["licenses"].items():
            self.assertEqual(hashlib.sha256((ROOT / "native/amd_neuralrender/vendor" / name).read_bytes()).hexdigest(), expected)

    def fixture(self, root):
        store = AssetStore(str(root / "store"))
        payload = root / "payload"
        files = {}
        for name in ("dlss5nr_runtime.dll", "zluda/nvcuda.dll", "kernels/kernels.txt", "cache/ComputeCache/zluda2.db", "cache/ComputeCache/coverage.txt", "LICENSE"):
            path = payload / amd_nr.LOCK["prefix"] / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(name.encode())
            files[name] = {"sha256": hashlib.sha256(name.encode()).hexdigest(), "size": len(name.encode())}
        package = {"provider": amd_nr.PROVIDER, "version": amd_nr.VERSION,
                   "archive_sha256": amd_nr.ARCHIVE_SHA256, "payload_path": str(payload)}
        profile = Profile(name="AMD", addon_config={"addons": {"DLSSNR02": {"_enabled": True}}})
        return store, payload, files, package, profile

    def test_plan_is_private_and_only_emits_verified_runtime_files(self):
        with tempfile.TemporaryDirectory() as folder:
            store, payload, files, package, profile = self.fixture(Path(folder))
            with patch.object(store, "list_packages", return_value=[package]), patch.object(amd_nr, "FILES", files):
                plan = amd_nr.deployment_files(store, profile, host_version=MANAGED_HOST_VERSION)
                self.assertEqual({p["relative_path"] for p in plan}, {f"{amd_nr.ROOT}/{name}" for name in files if not name.startswith("cache/") and name != "dlss5nr_runtime.dll"})
                self.assertTrue(all(p["role"] == "amd_nr_runtime" for p in plan))
                (payload / amd_nr.LOCK["prefix"] / "dlss5nr_runtime.dll").write_bytes(b"tampered")
                with self.assertRaisesRegex(ValueError, "failed verification: dlss5nr_runtime.dll"):
                    amd_nr.deployment_files(store, profile, host_version=MANAGED_HOST_VERSION)

    def test_mutable_cache_is_separate_and_not_overwritten_on_profile_reapply(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store, payload, files, package, profile = self.fixture(root)
            with patch.object(store, "list_packages", return_value=[package]), patch.object(amd_nr, "FILES", files):
                amd_nr.prepare_cache(store, root / "config")
                cache = amd_nr.cache_path(root / "config") / "ComputeCache/zluda2.db"
                original = payload / amd_nr.LOCK["prefix"] / "cache/ComputeCache/zluda2.db"
                cache.write_bytes(b"runtime updated translation cache")
                amd_nr.prepare_cache(store, root / "config")
                self.assertEqual(cache.read_bytes(), b"runtime updated translation cache")
                self.assertEqual(original.read_bytes(), b"cache/ComputeCache/zluda2.db")

    def test_missing_optional_package_does_not_block_nvidia_and_disabled_omits_runtime(self):
        with tempfile.TemporaryDirectory() as folder:
            store, _, files, package, profile = self.fixture(Path(folder))
            self.assertEqual(amd_nr.deployment_files(store, profile, host_version=MANAGED_HOST_VERSION), [])
            with patch.object(store, "list_packages", return_value=[package]), patch.object(amd_nr, "FILES", files):
                with self.assertRaisesRegex(ValueError, "current bundled Companion host"):
                    amd_nr.deployment_files(store, profile, host_version="build-5-lsc.2")
                profile.addon_config["addons"]["DLSSNR02"]["_enabled"] = False
                self.assertEqual(amd_nr.deployment_files(store, profile, host_version=MANAGED_HOST_VERSION), [])

    def test_internal_addon_follows_explicit_nr_opt_in_and_owns_cache_path(self):
        manager = SimpleNamespace(config_dir="test-config", config=SimpleNamespace(default_addon_config={}))
        service = AddonConfigService(manager)
        service.available_addon_ids.add("LSC-AMDNR")
        for flag in (True, False, "true", None):
            profile = Profile(name="Game", addon_config={"addons": {"DLSSNR02": {"_enabled": flag}, "LSC-AMDNR": {"_enabled": True, "dataPath": "untrusted"}}})
            effective = service.effective(profile, reshade_enabled=False)
            self.assertEqual(effective["addons"]["LSC-AMDNR"]["_enabled"], flag is True)
            self.assertEqual(effective["addons"]["LSC-AMDNR"]["dataPath"], str(amd_nr.cache_path("test-config")))

    def test_catalog_hides_internal_amd_addon(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manifest = root / "addons/LSC-AMDNR/addon.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text('{"name":"Internal AMD"}')
            manager = SimpleNamespace(config_dir=str(root), config=SimpleNamespace(default_addon_config={}))
            service = AddonConfigService(manager)
            catalog = service.catalog([{"provider": "ls-addon-manager", "payload_path": str(root), "files": [{"relative_path": "addons/LSC-AMDNR/addon.json"}]}])
            self.assertIn("LSC-AMDNR", service.available_addon_ids)
            self.assertNotIn("LSC-AMDNR", {a["id"] for a in catalog})
            nr = next(a for a in catalog if a["id"] == "DLSSNR02")
            self.assertIn("separate AMD backend", nr["description"])

    @unittest.skipUnless(os.environ.get("LSC_TEST_AMD_NR_ARCHIVE"), "Set LSC_TEST_AMD_NR_ARCHIVE to audit the pinned publisher archive")
    def test_real_archive_matches_every_selected_file(self):
        archive = Path(os.environ["LSC_TEST_AMD_NR_ARCHIVE"])
        self.assertEqual(AssetStore.sha256(archive), amd_nr.ARCHIVE_SHA256)
        with zipfile.ZipFile(archive) as bundle:
            for name, info in amd_nr.FILES.items():
                with self.subTest(name=name):
                    member = bundle.getinfo(amd_nr.LOCK["prefix"] + name)
                    self.assertEqual(member.file_size, info["size"])
                    with bundle.open(member) as data:
                        self.assertEqual(hashlib.file_digest(data, "sha256").hexdigest(), info["sha256"])
            member = bundle.getinfo(amd_nr.LOCK["prefix"] + "nvngx_dlssnr.dll")
            self.assertEqual(member.file_size, amd_nr.LOCK["model"]["size"])
            with bundle.open(member) as data:
                self.assertEqual(hashlib.file_digest(data, "sha256").hexdigest(), amd_nr.LOCK["model"]["sha256"])


@unittest.skipUnless(os.name == "nt" and os.environ.get("LSC_TEST_ADDON_HOST") == "1", "Opt-in native Windows validation")
class NativeAmdNrTests(unittest.TestCase):
    def test_bundled_runtime_identifies_its_adapter_selection_extension(self):
        import ctypes
        runtime = ctypes.WinDLL(str(ROOT / "build/native/LSC-AMDNR/runtime/dlss5nr_runtime.dll"))
        runtime.lsc_dlss5nr_adapter_selection_version.restype = ctypes.c_uint
        runtime.lsc_dlss5nr_selected_cuda_ordinal.restype = ctypes.c_int
        runtime.lsc_dlss5nr_selected_adapter_luid.restype = ctypes.c_uint64
        self.assertEqual(runtime.lsc_dlss5nr_adapter_selection_version(), 1)
        self.assertEqual(runtime.lsc_dlss5nr_selected_cuda_ordinal(), -1)
        self.assertEqual(runtime.lsc_dlss5nr_selected_adapter_luid(), 0)

    def test_compute_device_selection_translates_map_identity_to_non_primary_cuda_device(self):
        binary = ROOT / "build/native/LSC-AMDNR/LSC_AMDAdapterTest.exe"
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ordinals 1 and 2", result.stdout)

    def test_model_preflight_rejects_unmatched_dll_without_loading_it(self):
        with tempfile.TemporaryDirectory() as folder:
            dll = Path(folder) / "nvngx_dlssnr.dll"
            dll.write_bytes(b"not the supported original model")
            probe = ROOT / "build/native/LSC-AMDNR/LSC_AMDNRProbe.exe"
            result = subprocess.run([str(probe), "--check-model", str(dll)], capture_output=True, text=True, timeout=15)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("original signed 310.9.16", result.stderr)

    def test_host_archive_deploys_addon_sources_and_licenses_without_probe_runtime(self):
        with zipfile.ZipFile(ROOT / "build/native/LSAddonManager-Companion.zip") as bundle:
            recipe = json.loads(bundle.read("lossless-scaling-deployment.json"))
            names = {entry["destination"] for entry in recipe["files"]}
            for name in ("LSC_AMDNR.dll", "addon.json", "LICENSE", "FlowEstimator-LICENSE.txt", "Runtime-API-LICENSE.txt", "ZLUDA-LICENSE-MIT", "ZLUDA-LICENSE-APACHE"):
                self.assertIn("addons/LSC-AMDNR/" + name, names)
            self.assertIn("addons/LSC-AMDNR/runtime/dlss5nr_runtime.dll", names)
            self.assertFalse(any("probe-fixture" in name for name in names))
            with zipfile.ZipFile(io.BytesIO(bundle.read("licenses/LSAddonManager/CompanionHost-source.zip"))) as source:
                self.assertIn("native/amd_neuralrender/src/pipeline.cpp", source.namelist())
                self.assertIn("native/addon_manager/companion_real_frame.inc", source.namelist())
                self.assertIn("companion/services/amd_nr.py", source.namelist())
                self.assertIn("amd-runtime/runtime/core/companion_adapter.h", source.namelist())

    def test_both_real_addons_start_without_a_model_or_loading_a_cuda_stack(self):
        import psutil
        from companion.services.addon_host import _read_pipe, validate_status
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            harness = ROOT / "build/native/addon-host-smoke"
            shutil.copy2(harness / "lsc_headless_smoke.exe", root / "LosslessScaling.exe")
            shutil.copy2(harness / "Lossless_original.dll", root / "Lossless_original.dll")
            with zipfile.ZipFile(ROOT / "build/native/LSAddonManager-Companion.zip") as bundle:
                for name in bundle.namelist():
                    if name == "Lossless.dll" or name.startswith(("addons/DLSSNR02/", "addons/LSC-AMDNR/")):
                        bundle.extract(name, root)
            config = {"global": {"auto_load": True, "companion": {"headless": True}}, "addons": {
                "DLSSNR02": {"_enabled": True}, "LSC-AMDNR": {"_enabled": True},
                "DLSS4DLAA": {"_enabled": False, "companionSm86Mode": "L-INT8"}}}
            (root / "addons/config.json").write_text(json.dumps(config))
            process = subprocess.Popen([str(root / "LosslessScaling.exe")], cwd=root,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW, env={**os.environ, "LOCALAPPDATA": str(root / "local")})
            try:
                instance = (process.pid, psutil.Process(process.pid).create_time())
                deadline = time.monotonic() + 30
                status = None
                while time.monotonic() < deadline and process.poll() is None:
                    try:
                        status = validate_status(_read_pipe(process.pid), instance)
                        break
                    except (OSError, ValueError):
                        time.sleep(.05)
                self.assertIsNotNone(status, "No headless telemetry")
                self.assertEqual(status["startupError"], "")
                self.assertEqual(status["sm86Sr"]["mode"], "Off", "A disabled SR addon must not arm its saved optimized mode")
                for addon_id in ("DLSSNR02", "LSC-AMDNR"):
                    addon = next(a for a in status["addons"] if a["id"] == addon_id)
                    self.assertTrue(addon["loaded"])
                    self.assertFalse(addon["faulted"])
                    self.assertEqual(addon["error"], "")
                    self.assertNotIn("owned by another", addon["status"])
                modules = {Path(m.path).name.casefold() for m in psutil.Process(process.pid).memory_maps()}
                self.assertNotIn("nvcuda.dll", modules, "Adapter detection must precede driver-stack loading")
                self.assertNotIn("dlss5nr_runtime.dll", modules)
                process.communicate(b"\n", timeout=10)
                self.assertEqual(process.returncode, 0)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=10)

    def test_actual_host_routing_and_callback_ownership(self):
        root = ROOT / "build/native/addon-host-smoke"
        result = subprocess.run([str(root / "lsc_routing_test.exe")], cwd=root, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_software_gpu_compute_flow_motion_and_history_reset(self):
        probe = ROOT / "build/native/LSC-AMDNR/LSC_AMDNRProbe.exe"
        result = subprocess.run([str(probe), "--warp", "--flow-only"], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("No NR inference tested", result.stdout)

    def test_real_pipeline_shared_textures_fences_resize_and_ring_reuse(self):
        probe = ROOT / "build/native/LSC-AMDNR/LSC_AMDNRProbe.exe"
        result = subprocess.run([str(probe), "--warp", "--bridge-only"], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("no NR inference tested", result.stdout)
