import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

from companion.services.addon_host import (
    MANAGED_HOST_VERSION, MANAGED_HOST_ARCHIVE, _read_pipe,
    addon_version_key, runtime_status, validate_status,
)
from companion.services.automatic_updates import newer_addon_version
from companion.services.release_providers import BundledAddonManagerReleaseProvider, ReleaseManager, ProviderRegistry
from companion.services.asset_store import AssetStore

ROOT = Path(__file__).resolve().parents[1]


class AddonHostTests(unittest.TestCase):
    def sample(self, **changes):
        return {"schema": 1, "pid": 123, "createdAt": 900.0, "sampledAt": 1000.0,
                "headless": True, "hostVersion": MANAGED_HOST_VERSION, "addons": [], "metrics": {}, **changes}

    def test_pin_and_python_contract_agree(self):
        pin = json.loads((ROOT / "native/addon_manager/upstream.lock.json").read_text())
        self.assertEqual(pin["version"], MANAGED_HOST_VERSION)
        self.assertEqual(pin["bridge_protocol"], 1)
        self.assertEqual(pin["version"], f"{pin['tag']}-lsc.{pin['companion_revision']}")

    def test_builds_sort_numerically_and_compare_companion_revisions(self):
        self.assertGreater(addon_version_key("build-10-lsc.1"), addon_version_key("build-9-lsc.3"))
        self.assertTrue(newer_addon_version("build-5-lsc.1", "build-5-lsc.2"))
        self.assertTrue(newer_addon_version("build-5-lsc.2", "build-6-lsc.1"))
        self.assertFalse(newer_addon_version("build-6-lsc.1", "build-5-lsc.2"))
        self.assertFalse(newer_addon_version("v1.0.6", "build-5-lsc.1"))

    def test_status_requires_exact_process_instance_and_fresh_sample(self):
        self.assertTrue(validate_status(self.sample(), (123, 900), now=1001)["available"])
        for payload in (self.sample(pid=456), self.sample(createdAt=899), self.sample(sampledAt=990),
                        self.sample(sampledAt=float("nan")), self.sample(schema=2),
                        self.sample(headless=False), self.sample(metrics=[])):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                validate_status(payload, (123, 900), now=1001)

    def test_restarted_process_invalidates_successful_pipe_read(self):
        with patch("companion.services.adapter_report.configured_process", side_effect=[(123, 900), (123, 950)]), \
             patch("companion.services.addon_host._read_pipe", return_value=self.sample()):
            self.assertFalse(runtime_status("LosslessScaling.exe")["available"])

    def test_bundled_host_stages_offline_and_deploys_matching_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / MANAGED_HOST_ARCHIVE
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("Lossless.dll", b"host")
                bundle.writestr("addons/DLSSNR02/NeuralRendering-source.zip", b"corresponding source")
                bundle.writestr("licenses/LSAddonManager/CompanionHost-source.zip", b"host source")
                bundle.writestr("lossless-scaling-deployment.json", json.dumps({"schema_version": 1, "files": [
                    {"source": path, "destination": path, "role": "addon_manager_support"}
                    for path in ("addons/DLSSNR02/NeuralRendering-source.zip", "licenses/LSAddonManager/CompanionHost-source.zip")]}))
            provider = BundledAddonManagerReleaseProvider()
            registry = ProviderRegistry()
            registry.providers = {"ls-addon-manager": provider}
            manager = ReleaseManager(AssetStore(str(root / "store")), registry)
            with patch.object(provider, "archive_path", return_value=archive), \
                 patch("urllib.request.urlopen", side_effect=AssertionError("network")):
                release = manager.check("ls-addon-manager")[0]
                package = manager.stage_release("ls-addon-manager", release["version"], release["assets"][0]["name"], "test-host")
            self.assertEqual(package["version"], MANAGED_HOST_VERSION)
            self.assertTrue((Path(package["payload_path"]) / "addons/DLSSNR02/NeuralRendering-source.zip").is_file())


@unittest.skipUnless(os.name == "nt" and os.environ.get("LSC_TEST_ADDON_HOST") == "1",
                     "Set LSC_TEST_ADDON_HOST=1 after building the real native host")
class NativeAddonHostTests(unittest.TestCase):
    def test_explicit_component_loads_with_arbitrary_filename_and_rejects_bad_plans(self):
        import hashlib
        import psutil
        archive = ROOT / "build/native" / MANAGED_HOST_ARCHIVE
        harness = ROOT / "build/native/addon-host-smoke"
        for scenario in ("valid", "hash-mismatch", "escape", "duplicate"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                shutil.copy2(harness / "lsc_headless_smoke.exe", root / "LosslessScaling.exe")
                shutil.copy2(harness / "Lossless_original.dll", root / "Lossless_original.dll")
                with zipfile.ZipFile(archive) as bundle:
                    bundle.extract("Lossless.dll", root)
                (root / "addons").mkdir()
                (root / "addons/config.json").write_text(json.dumps({
                    "global": {"auto_load": True, "companion": {"headless": True}}, "addons": {}}))
                relative = ".companion/components/custom/any-name-we-choose.dll"
                component = root / relative
                component.parent.mkdir(parents=True)
                shutil.copy2(harness / "Lossless_original.dll", component)
                digest = hashlib.sha256(component.read_bytes()).hexdigest()
                entry = {"id": "custom-fixture", "path": relative, "sha256": digest}
                if scenario == "hash-mismatch":
                    entry["sha256"] = "0" * 64
                if scenario == "escape":
                    entry["path"] = "Lossless_original.dll"
                entries = [entry, entry] if scenario == "duplicate" else [entry]
                (root / ".companion/components.json").write_text(json.dumps({"schema": 1, "components": entries}))
                process = subprocess.Popen([str(root / "LosslessScaling.exe")], cwd=root,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    env={**os.environ, "LOCALAPPDATA": str(root / "local-app-data")})
                try:
                    instance = (process.pid, psutil.Process(process.pid).create_time())
                    deadline = time.monotonic() + 30
                    status = None
                    while time.monotonic() < deadline and process.poll() is None:
                        try:
                            status = validate_status(_read_pipe(process.pid), instance)
                            break
                        except (OSError, ValueError):
                            time.sleep(0.05)
                    self.assertIsNotNone(status, "The real host did not publish component status")
                    if scenario == "valid":
                        self.assertEqual(status["startupError"], "", status.get("components"))
                        self.assertEqual(status["components"][0]["id"], "custom-fixture")
                        self.assertTrue(status["components"][0]["loaded"])
                        # Windows temp paths may use an 8.3 alias such as RUNNER~1.
                        self.assertTrue(Path(status["components"][0]["path"]).samefile(component))
                        loaded = {Path(item.path).resolve() for item in psutil.Process(process.pid).memory_maps()}
                        self.assertIn(component.resolve(), loaded)
                    else:
                        self.assertTrue(status["startupError"])
                        self.assertFalse(any(item["loaded"] for item in status["components"]))
                    process.communicate(b"\n", timeout=10)
                    self.assertEqual(process.returncode, 0)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.communicate(timeout=10)

    @unittest.skipUnless(os.environ.get("LSC_TEST_SM86_SR_ARCHIVE"), "Set LSC_TEST_SM86_SR_ARCHIVE to the pinned SM86 ZIP for optional loader integration tests")
    def test_private_sm86_proxy_redirects_sr_and_stays_unloaded_when_off(self):
        import psutil
        from companion.core.models import Profile
        from companion.services import sm86_sr
        archive = Path(os.environ["LSC_TEST_SM86_SR_ARCHIVE"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "LS"
            root.mkdir()
            store = AssetStore(str(Path(directory) / "store"))
            store.import_release_archive(str(archive), provider=sm86_sr.PROVIDER, version=sm86_sr.VERSION,
                expected_sha256=sm86_sr.ARCHIVE_SHA256, source_metadata={"kind": "native-test"})
            harness = ROOT / "build/native/addon-host-smoke"
            shutil.copy2(harness / "lsc_headless_smoke.exe", root / "LosslessScaling.exe")
            shutil.copy2(harness / "Lossless_original.dll", root / "Lossless_original.dll")
            with zipfile.ZipFile(ROOT / "build/native" / MANAGED_HOST_ARCHIVE) as bundle:
                for name in bundle.namelist():
                    if name == "Lossless.dll" or name.startswith("addons/DLSS4DLAA/"):
                        bundle.extract(name, root)
            for mode in ("L-INT8", "M-INT8", "Off"):
                with self.subTest(mode=mode):
                    config = {"global": {"auto_load": True, "companion": {"headless": True}},
                              "addons": {"DLSS4DLAA": {"_enabled": True, "companionSm86Mode": mode}}}
                    profile = Profile(name="Smoke", addon_config=config)
                    for item in sm86_sr.deployment_files(store, profile, host_version=MANAGED_HOST_VERSION):
                        target = root / item["relative_path"]
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(item["source_path"], target)
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
                                time.sleep(0.05)
                        self.assertIsNotNone(status, "The native SR smoke host never became ready")
                        self.assertEqual(status["sm86Sr"]["mode"], mode)
                        self.assertEqual(status["sm86Sr"]["loaderReady"], mode != "Off")
                        if mode != "Off" and not status["sm86Sr"]["gateAllowed"]:
                            self.assertIn("SM86 SR unavailable:", status["startupError"])
                            self.assertEqual(status["sm86Sr"]["error"], status["startupError"])
                            upscaler = next(item for item in status["addons"] if item["id"] == "DLSS4DLAA")
                            self.assertFalse(upscaler["enabled"], "A rejected device must not silently run stock DLSS")
                            self.assertFalse(upscaler["loaded"])
                        else:
                            self.assertEqual(status["startupError"], "")
                        modules = [item.path.lower().replace("/", "\\") for item in psutil.Process(process.pid).memory_maps()]
                        proxy = str(root / sm86_sr.ROOT / "version.dll").lower().replace("/", "\\")
                        self.assertEqual(proxy in modules, mode != "Off")
                        self.assertFalse(any("nvngx_dlssg.dll" in item or "fg_kernels.dll" in item or "xefg_kernels.dll" in item for item in modules))
                        probe = mode != "Off" and status["sm86Sr"]["gateAllowed"]
                        output, error = process.communicate(b"r\n" if probe else b"\n", timeout=30)
                        self.assertEqual(process.returncode, 0, error.decode(errors="replace"))
                        if probe:
                            # An accepted route must redirect to its pinned
                            # snippet; a load error is visible rather than stock.
                            text = output.decode(errors="replace").lower().replace("/", "\\")
                            self.assertTrue("runtime_error=" in text or "companion-runtimes\\sm86sr\\dlssg_sm86\\sr\\nvngx_dlss.dll" in text, text)
                    finally:
                        if process.poll() is None:
                            process.kill()
                            process.communicate(timeout=10)

    def test_real_host_is_headless_and_reports_addon_errors_without_a_model(self):
        import psutil
        from companion.services.proxy_menu import AddonManagerMenuController
        archive = ROOT / "build/native" / MANAGED_HOST_ARCHIVE
        harness = ROOT / "build/native/addon-host-smoke"
        self.assertTrue(archive.is_file(), "Build the pinned host before enabling native tests")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copy2(harness / "lsc_headless_smoke.exe", root / "LosslessScaling.exe")
            shutil.copy2(harness / "Lossless_original.dll", root / "Lossless_original.dll")
            with zipfile.ZipFile(archive) as bundle:
                for name in bundle.namelist():
                    if name == "Lossless.dll" or name.startswith(("addons/DLSSNR02/", "addons/LSC-AdapterReport/")):
                        bundle.extract(name, root)
            helper = root / "addons/LSC-AdapterReport"
            self.assertTrue((helper / "LSC_AdapterReport.dll").is_file(), "The host package must include its GPU reporter")
            config = {"global": {"auto_load": True, "companion": {"headless": True}},
                      "addons": {"DLSSNR02": {"_enabled": True}, "LSC-AdapterReport": {"_enabled": True}}}
            (root / "addons/config.json").write_text(json.dumps(config))
            environment = {**os.environ, "LOCALAPPDATA": str(root / "local-app-data")}
            process = subprocess.Popen([str(root / "LosslessScaling.exe")], cwd=root,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW, env=environment)
            try:
                instance = (process.pid, psutil.Process(process.pid).create_time())
                deadline = time.monotonic() + 30
                status = None
                while time.monotonic() < deadline and process.poll() is None:
                    try:
                        status = validate_status(_read_pipe(process.pid), instance)
                        break
                    except (OSError, ValueError):
                        time.sleep(0.05)
                self.assertIsNotNone(status, "The real host did not publish bridge status")
                self.assertEqual(status["startupError"], "")
                self.assertEqual(status["hostVersion"], MANAGED_HOST_VERSION)
                self.assertIs(status["reshade"]["interactive"], False)
                self.assertIsInstance(status["reshade"]["hotkeyVk"], int)
                self.assertFalse(status["capture"]["runtimeVerified"], "Synthetic LS must be rejected by the runtime gate")
                neural = next(item for item in status["addons"] if item["id"] == "DLSSNR02")
                self.assertTrue(neural["loaded"])
                self.assertFalse(neural["faulted"])
                adapter = next(item for item in status["addons"] if item["id"] == "LSC-AdapterReport")
                self.assertTrue(adapter["loaded"])
                self.assertFalse(adapter["faulted"])
                self.assertEqual(adapter["error"], "")
                self.assertEqual(AddonManagerMenuController._find_windows(process.pid), [])
                # A second client proves the first connection was acknowledged and retired.
                self.assertEqual(_read_pipe(process.pid)["pid"], process.pid)
                process.communicate(b"\n", timeout=10)
                self.assertEqual(process.returncode, 0)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=10)


if __name__ == "__main__":
    unittest.main()
