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
    addon_version_key, runtime_status, validate_status, set_passthrough,
)
from companion.services.automatic_updates import newer_addon_version
from companion.services.release_providers import BundledAddonManagerReleaseProvider, ReleaseManager, ProviderRegistry
from companion.services.asset_store import AssetStore

ROOT = Path(__file__).resolve().parents[1]


def manager_hardware_available():
    """The upstream window needs a hardware D3D11 device; hosted CI may lack one."""
    import ctypes
    try:
        create = ctypes.WinDLL("d3d11.dll", winmode=0x800).D3D11CreateDevice
        create.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint,
                           ctypes.POINTER(ctypes.c_uint), ctypes.c_uint, ctypes.c_uint,
                           ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint), ctypes.c_void_p]
        create.restype = ctypes.c_long
        levels = (ctypes.c_uint * 2)(0xB000, 0xA000)
        feature = ctypes.c_uint()
        # Null device/context outputs query support without retaining a device.
        return create(None, 1, None, 0, levels, 2, 7, None, ctypes.byref(feature), None) >= 0
    except (OSError, AttributeError):
        return False


class AddonHostTests(unittest.TestCase):
    def sample(self, **changes):
        return {"schema": 1, "pid": 123, "createdAt": 900.0, "sampledAt": 1000.0,
                "headless": True, "hostVersion": MANAGED_HOST_VERSION, "addons": [], "metrics": {}, **changes}

    def test_pin_and_python_contract_agree(self):
        pin = json.loads((ROOT / "native/addon_manager/upstream.lock.json").read_text())
        self.assertEqual(pin["version"], MANAGED_HOST_VERSION)
        self.assertEqual(pin["bridge_protocol"], 1)
        self.assertEqual(pin["version"], f"{pin['tag']}-lsc.{pin['companion_revision']}")

    def test_passthrough_command_is_explicit_and_waits_for_confirmed_native_state(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                before = self.sample(reshade={"interactive": not enabled, "controlAvailable": True})
                after = self.sample(reshade={"interactive": enabled, "controlAvailable": True})
                receipts = []

                def read(pid, **kwargs):
                    self.assertEqual(pid, 123)
                    if "acknowledge" in kwargs:
                        receipts.append(kwargs["acknowledge"](before))
                        return before
                    return after

                with patch("companion.services.adapter_report.configured_process", return_value=(123, 900)), \
                        patch("companion.services.addon_host.time.time", return_value=1001), \
                        patch("companion.services.addon_host._read_pipe", side_effect=read):
                    result = set_passthrough("LS.exe", enabled, expected_pid=123)
                self.assertIs(result["reshade"]["interactive"], enabled)
                self.assertEqual(receipts, [b"E" if enabled else b"D"])

    def test_passthrough_rejects_older_host_stale_data_and_process_replacement_before_command(self):
        for payload in (self.sample(), self.sample(sampledAt=990),
                        self.sample(pid=456), self.sample(createdAt=850)):
            def read(pid, **kwargs):
                return kwargs["acknowledge"](payload)
            with self.subTest(payload=payload), \
                    patch("companion.services.adapter_report.configured_process", return_value=(123, 900)), \
                    patch("companion.services.addon_host.time.time", return_value=1001), \
                    patch("companion.services.addon_host._read_pipe", side_effect=read), self.assertRaises(ValueError):
                set_passthrough("LS.exe", True, expected_pid=123)
        with patch("companion.services.adapter_report.configured_process", return_value=(456, 900)), \
                patch("companion.services.addon_host._read_pipe") as read, self.assertRaises(ValueError):
            set_passthrough("LS.exe", True, expected_pid=123)
        read.assert_not_called()

    def test_passthrough_confirmation_tolerates_pipe_recreation(self):
        before = self.sample(reshade={"interactive": False, "controlAvailable": True})
        after = self.sample(reshade={"interactive": True, "controlAvailable": True})
        reads = iter((OSError("Pipe is being recreated"), after))

        def read(pid, **kwargs):
            if "acknowledge" in kwargs:
                self.assertEqual(kwargs["acknowledge"](before), b"E")
                return before
            result = next(reads)
            if isinstance(result, OSError):
                raise result
            return result

        with patch("companion.services.adapter_report.configured_process", return_value=(123, 900)), \
                patch("companion.services.addon_host.time.time", return_value=1001), \
                patch("companion.services.addon_host.time.sleep"), \
                patch("companion.services.addon_host._read_pipe", side_effect=read):
            self.assertTrue(set_passthrough("LS.exe", True)["reshade"]["interactive"])

    def test_passthrough_can_confirm_remembered_preference_while_suspended(self):
        payload = self.sample(reshade={"interactive": False, "controlAvailable": True,
                                      "controlRequested": True})
        with patch("companion.services.adapter_report.configured_process", return_value=(123, 900)), \
                patch("companion.services.addon_host.time.time", return_value=1001), \
                patch("companion.services.addon_host._read_pipe", return_value=payload):
            self.assertFalse(set_passthrough("LS.exe", True)["reshade"]["interactive"])
            with patch("companion.services.addon_host.time.monotonic", side_effect=[0, 0, 2]), \
                    patch("companion.services.addon_host.time.sleep"), self.assertRaises(TimeoutError):
                set_passthrough("LS.exe", False)

    def test_builds_sort_numerically_and_compare_companion_revisions(self):
        self.assertGreater(addon_version_key("build-10-lsc.1"), addon_version_key("build-9-lsc.3"))
        self.assertTrue(newer_addon_version("build-5-lsc.1", "build-5-lsc.2"))
        self.assertTrue(newer_addon_version("build-5-lsc.2", "build-6-lsc.1"))
        self.assertFalse(newer_addon_version("build-6-lsc.1", "build-5-lsc.2"))
        self.assertFalse(newer_addon_version("v1.0.6", "build-5-lsc.1"))

    def test_status_requires_exact_process_instance_and_fresh_sample(self):
        self.assertTrue(validate_status(self.sample(), (123, 900), now=1001)["available"])
        self.assertTrue(validate_status(self.sample(headless=False, managed=True), (123, 900), now=1001)["available"])
        for payload in (self.sample(pid=456), self.sample(createdAt=899), self.sample(sampledAt=990),
                        self.sample(sampledAt=float("nan")), self.sample(schema=2),
                        self.sample(headless=False), self.sample(metrics=[])):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                validate_status(payload, (123, 900), now=1001)

    def test_restarted_process_invalidates_successful_pipe_read(self):
        with patch("companion.services.adapter_report.configured_process", side_effect=[(123, 900), (123, 950)]), \
             patch("companion.services.addon_host._read_pipe", return_value=self.sample()):
            self.assertFalse(runtime_status("LosslessScaling.exe")["available"])

    def test_missing_telemetry_keeps_only_a_reverified_scaling_process_identity(self):
        for current, has_pid in (((123, 900), True), ((123, 950), False), (None, False)):
            with patch("companion.services.adapter_report.configured_process", side_effect=[(123, 900), current]), \
                    patch("companion.services.addon_host._read_pipe", side_effect=OSError("No status pipe")):
                result = runtime_status("LosslessScaling.exe")
                self.assertFalse(result["available"])
                self.assertEqual("pid" in result, has_pid)

    def test_bundled_host_stages_offline_and_deploys_matching_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / MANAGED_HOST_ARCHIVE
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("Lossless.dll", b"host")
                bundle.writestr("addons/DLSSNR02/NeuralRendering-source.zip", b"corresponding source")
                bundle.writestr("licenses/LSAddonManager/CompanionHost-source.zip", b"host source")
                bundle.writestr("lossless-scaling-deployment.json", json.dumps({"schema_version": 1, "version": MANAGED_HOST_VERSION, "files": [
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

    def test_bundled_version_and_cache_identity_follow_the_actual_archive_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / MANAGED_HOST_ARCHIVE
            provider = BundledAddonManagerReleaseProvider()
            registry = ProviderRegistry()
            registry.providers = {"ls-addon-manager": provider}
            manager = ReleaseManager(AssetStore(str(root / "store")), registry)
            with patch.object(provider, "archive_path", return_value=archive):
                for version in ("build-92-lsc.8", "build-93-lsc.1"):
                    with zipfile.ZipFile(archive, "w") as bundle:
                        bundle.writestr("lossless-scaling-deployment.json", json.dumps({"version": version}))
                    release = manager.check("ls-addon-manager")[0]
                    self.assertEqual(release["version"], version)
                    self.assertTrue(release["html_url"].endswith(version.split("-lsc.")[0]))
                    self.assertEqual(provider.cache_identity, version)

    def test_bundled_host_does_not_invent_a_version_for_invalid_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / MANAGED_HOST_ARCHIVE
            for manifest in ({}, {"version": "unknown"}, [], {"version": [7]}):
                with zipfile.ZipFile(archive, "w") as bundle:
                    bundle.writestr("lossless-scaling-deployment.json", json.dumps(manifest))
                with self.subTest(manifest=manifest), self.assertRaisesRegex(RuntimeError, "version manifest"):
                    BundledAddonManagerReleaseProvider._bundle_version(archive)


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
                    self.assertFalse(status["hdrProbe"]["enabled"])
                    self.assertEqual(status["hdrProbe"]["scans"], 0)
                    self.assertEqual(status["hdrProbe"]["sampleDeviceName"], "")
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
        self._check_real_host(True)

    def test_real_host_panel_starts_hidden_with_software_rendering(self):
        self._check_real_host(False, reopen=False)

    def test_real_host_panel_starts_hidden_and_reopens(self):
        if not manager_hardware_available():
            self.skipTest("Native manager panel requires a hardware D3D11 device; other native host tests still run")
        self._check_real_host(False)

    def _check_real_host(self, headless, *, reopen=True):
        import ctypes
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
            config = {"global": {"auto_load": True, "companion": {"headless": headless, "managed": True},
                                 "ui": {"open_on_start": False, "hotkey_enabled": False}},
                      "addons": {"DLSSNR02": {"_enabled": True, "localStructure": "0.65", "skinStructure": "-1"},
                                 "LSC-AdapterReport": {"_enabled": True},
                                 "ReShadePassthrough": {"_enabled": True, "companion_control": "1"}}}
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
                        if not headless and (not AddonManagerMenuController._find_windows(process.pid) or not any(
                                item["id"] == "LSC-AdapterReport" and item["loaded"] for item in status["addons"])):
                            time.sleep(0.05)
                            continue
                        break
                    except (OSError, ValueError):
                        time.sleep(0.05)
                self.assertIsNotNone(status, "The real host did not publish bridge status")
                self.assertEqual(status["startupError"], "")
                self.assertEqual(status["hostVersion"], MANAGED_HOST_VERSION)
                self.assertTrue(status["reshade"]["controlAvailable"])
                # Exercise the actual pipe receipt and watcher without taking
                # focus or enabling input on the user's desktop.
                controlled = set_passthrough(str(root / "LosslessScaling.exe"), False,
                                             expected_pid=process.pid)
                self.assertFalse(controlled["reshade"]["interactive"])
                self.assertFalse(controlled["reshade"]["controlRequested"])
                self.assertFalse(controlled["reshade"]["controlPending"])
                log = (root / "logs/LSAddonManager.log").read_text(encoding="utf-8", errors="replace")
                self.assertIn("FPS counter position override ready", log,
                              "Managed startup must retain upstream's FPS drawing hook")
                self.assertIs(status["reshade"]["interactive"], False)
                self.assertIsInstance(status["reshade"]["hotkeyVk"], int)
                self.assertFalse(status["capture"]["runtimeVerified"], "Synthetic LS must be rejected by the runtime gate")
                neural = next(item for item in status["addons"] if item["id"] == "DLSSNR02")
                self.assertTrue(neural["loaded"])
                self.assertFalse(neural["faulted"])
                # In panel mode LoadAddons publishes loaded modules before the
                # GUI creates ImGui and calls AddonInitialize. Wait for that
                # initialization's saved migration rather than racing the GUI.
                deadline = time.monotonic() + 5
                while True:
                    saved = json.loads((root / "addons/config.json").read_text(encoding="utf-8"))
                    if float(saved["addons"]["DLSSNR02"]["skinStructure"]) >= 0 or time.monotonic() >= deadline:
                        break
                    time.sleep(0.05)
                self.assertAlmostEqual(float(saved["addons"]["DLSSNR02"]["skinStructure"]), 0.65)
                adapter = next(item for item in status["addons"] if item["id"] == "LSC-AdapterReport")
                self.assertTrue(adapter["loaded"])
                self.assertFalse(adapter["faulted"])
                self.assertEqual(adapter["error"], "")
                self.assertEqual(status["headless"], headless)
                windows = AddonManagerMenuController._find_windows(process.pid)
                if headless:
                    self.assertEqual(windows, [])
                else:
                    from companion.core.state import AppState
                    self.assertEqual(len(windows), 1)
                    hwnd = ctypes.wintypes.HWND(windows[0])
                    user32 = ctypes.windll.user32
                    self.assertFalse(user32.IsWindowVisible(hwnd))
                    controller = AddonManagerMenuController(AppState())
                    for _ in range(2 if reopen else 0):
                        self.assertTrue(controller.open(process.pid)["opened"])
                        deadline = time.monotonic() + 5
                        while not user32.IsWindowVisible(hwnd) and time.monotonic() < deadline:
                            time.sleep(0.05)
                        self.assertTrue(user32.IsWindowVisible(hwnd))
                        # Synchronous close proves the GUI is pumping messages.
                        reply = ctypes.c_size_t()
                        self.assertTrue(user32.SendMessageTimeoutW(hwnd, 0x0010, 0, 0, 2, 2000, ctypes.byref(reply)))
                        self.assertFalse(user32.IsWindowVisible(hwnd))
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
