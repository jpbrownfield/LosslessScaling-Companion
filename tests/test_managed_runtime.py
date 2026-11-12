import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from companion.services.asset_store import AssetStore
from companion.services.managed_runtime import (
    BOOTSTRAP_FILES, MARKER, ManagedRuntime, assert_private_settings,
    await_isolation, bootstrap_directory, launch_environment,
)

ROOT = Path(__file__).resolve().parents[1]


class ManagedRuntimeTests(unittest.TestCase):
    def test_reshade_base_override_is_child_only_and_uses_private_installation(self):
        executable = Path(self.temp.name) / "fixture" / "LosslessScaling.exe"
        executable.parent.mkdir()
        executable.write_bytes(b"fixture")
        with patch.dict(os.environ, {"RESHADE_BASE_PATH_OVERRIDE": "C:/AnotherGame"}):
            environment, _, _ = launch_environment(executable)
            self.assertEqual(environment["RESHADE_BASE_PATH_OVERRIDE"], str(executable.parent.resolve()))
            self.assertEqual(os.environ["RESHADE_BASE_PATH_OVERRIDE"], "C:/AnotherGame")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        for name in ("LosslessScaling.exe", "LosslessScaling.dll", "Lossless.dll"):
            (self.source / name).write_bytes(name.encode())
        (self.source / "LosslessScaling.runtimeconfig.json").write_text(json.dumps({"runtimeOptions": {}}))
        self.bootstrap = self.root / "bootstrap"
        self.bootstrap.mkdir()
        for name in BOOTSTRAP_FILES:
            (self.bootstrap / name).write_bytes(name.encode())
        self.live = self.root / "live/Local/Lossless Scaling/Settings.xml"
        self.live.parent.mkdir(parents=True)
        self.live.write_text("<Settings><StartAsAdmin>true</StartAsAdmin><AutoScale>true</AutoScale><Keep>yes</Keep></Settings>")
        self.environment = patch.dict(os.environ, {"LOCALAPPDATA": str(self.live.parent.parent)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.runtime = ManagedRuntime(self.root / "config")
        self.store = AssetStore(self.root / "assets")

    def prepare(self):
        return self.runtime.prepare(self.source / "LosslessScaling.exe", self.store, bootstrap=self.bootstrap)

    def test_copy_and_private_settings_leave_originals_unchanged(self):
        original = {p.relative_to(self.source): p.read_bytes() for p in self.source.rglob("*") if p.is_file()}
        live = self.live.read_bytes()
        (self.source / "dxgi.dll").write_bytes(b"existing third-party proxy")
        executable = self.prepare()
        self.assertTrue(executable.is_relative_to(self.runtime.root))
        self.assertFalse((executable.parent / "dxgi.dll").exists())
        self.assertIn("<Keep>yes</Keep>", self.runtime.settings.read_text())
        self.assertIn("<StartAsAdmin>false</StartAsAdmin>", self.runtime.settings.read_text())
        self.assertEqual(self.live.read_bytes(), live)
        for path, data in original.items():
            self.assertEqual((self.source / path).read_bytes(), data)
        (executable.parent / "Lossless.dll").write_bytes(b"clone only")
        self.assertEqual((self.source / "Lossless.dll").read_bytes(), original[Path("Lossless.dll")])

    def test_private_settings_survive_new_generation(self):
        first = self.prepare()
        self.runtime.settings.write_text("<Settings><Keep>updated</Keep></Settings>")
        (self.source / "LosslessScaling.dll").write_bytes(b"new version")
        second = self.prepare()
        self.assertNotEqual(first, second)
        self.assertIn("updated", self.runtime.settings.read_text())

    def test_missing_recorded_engine_backup_does_not_clone_the_installed_wrapper(self):
        with patch("companion.services.deployment_manager.DeploymentManager._read_manifest", return_value={
            "files": [{"relative_path": "Lossless.dll", "original_backup": str(self.root / "missing-original.dll")}]
        }), self.assertRaisesRegex(RuntimeError, "original LS engine backup is missing"):
            self.prepare()

    def test_transient_windows_directory_lock_retries_the_clone_commit(self):
        rename = Path.rename
        attempts = []

        def locked_once(path, destination):
            attempts.append(path)
            if len(attempts) == 1:
                raise PermissionError("scanner has the new executable open")
            return rename(path, destination)

        with patch.object(Path, "rename", locked_once), patch("companion.services.managed_runtime.time.sleep"):
            executable = self.prepare()
        self.assertEqual(len(attempts), 2)
        self.runtime.validate(executable)

    def test_missing_or_tampered_bootstrap_refused(self):
        executable = self.prepare()
        (executable.parent / ".companion/bootstrap" / BOOTSTRAP_FILES[0]).write_bytes(b"modified")
        with self.assertRaisesRegex(RuntimeError, "modified"):
            self.runtime.validate(executable)
        (self.bootstrap / BOOTSTRAP_FILES[0]).unlink()
        with self.assertRaisesRegex(RuntimeError, "missing"):
            self.prepare()

    def test_live_write_and_unmanaged_launch_refused(self):
        with self.assertRaisesRegex(RuntimeError, "live"):
            assert_private_settings(self.live)
        with self.assertRaisesRegex(RuntimeError, "live"):
            assert_private_settings(self.live.with_suffix(".bak"))
        with self.assertRaisesRegex(RuntimeError, "prohibited"):
            launch_environment(self.source / "LosslessScaling.exe")

    def test_legacy_helpers_cannot_write_into_the_source_installation(self):
        from companion.core.models import DllOverrideConfig
        from companion.services.dll_manager import DllManager
        from companion.services.reshade_manager import ReshadeManager
        from companion.services.deployment_manager import DeploymentManager
        ini = self.source / "ReShade.ini"
        ini.write_text("[GENERAL]\nCurrentPresetPath=original.ini\n")
        original = ini.read_bytes()
        override = DllOverrideConfig(enabled=True, source_dll_path=str(self.source / "Lossless.dll"))
        self.assertFalse(DllManager.deploy_override(override, str(self.source)))
        self.assertFalse(ReshadeManager.swap_preset(str(ini), "different.ini"))
        with self.assertRaisesRegex(RuntimeError, "source LS"):
            DeploymentManager(self.store).apply(profile_id=None, lossless_scaling_exe=str(self.source / "LosslessScaling.exe"), files=[])
        self.assertEqual(ini.read_bytes(), original)
        self.assertFalse((self.source / "dxgi.dll").exists())

    def test_marker_cannot_redirect_to_live_settings(self):
        executable = self.prepare()
        marker = executable.parent / MARKER
        record = json.loads(marker.read_text())
        record["settings"] = str(self.live)
        marker.write_text(json.dumps(record))
        with self.assertRaisesRegex(RuntimeError, "identity"):
            self.runtime.validate(executable)

    def test_removed_mandatory_hook_is_rejected_before_launch(self):
        executable = self.prepare()
        (executable.parent / "LosslessScaling.runtimeconfig.json").write_text('{"runtimeOptions": {}}')
        with self.assertRaisesRegex(RuntimeError, "no longer requires"):
            launch_environment(executable)


@unittest.skipUnless(os.name == "nt" and os.environ.get("LSC_TEST_RUNTIME_ISOLATION") == "1",
                     "Build the Windows isolation fixture and set LSC_TEST_RUNTIME_ISOLATION=1")
class NativeManagedRuntimeTests(unittest.TestCase):
    def test_real_managed_and_native_folder_apis_and_direct_launch_guard(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            shutil.copytree(ROOT / "build/native/isolation-fixture", source)
            (source / "Lossless.dll").write_bytes(b"fixture has no native engine dependency")
            live = root / "live/Lossless Scaling/Settings.xml"
            live.parent.mkdir(parents=True)
            live.write_text("<Settings><Original>untouched</Original></Settings>")
            original = live.read_bytes()
            runtime = ManagedRuntime(root / "config")
            executable = runtime.prepare(source / "LosslessScaling.exe", AssetStore(root / "assets"),
                                         seed_settings=live, bootstrap=bootstrap_directory())
            environment, receipt, token = launch_environment(executable)
            result = subprocess.run([str(executable), str(runtime.local)], env=environment,
                                    capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            payload = json.loads(receipt.read_text())
            await_isolation(payload["pid"], receipt, token)
            self.assertEqual(Path(payload["settings"]), runtime.settings)
            self.assertIn("<Fixture>private</Fixture>", runtime.settings.read_text())
            self.assertEqual(live.read_bytes(), original)
            # A bare executable launch must fail before Main rather than read live settings.
            environment = {key: value for key, value in os.environ.items()
                           if not key.startswith("LSC_") and key != "DOTNET_STARTUP_HOOKS"}
            bare = subprocess.run([str(executable)], env=environment, capture_output=True, timeout=30)
            self.assertNotEqual(bare.returncode, 0, "Bare clone launch bypassed its startup hook")
            self.assertIn(b"Missing isolation contract", bare.stderr)
            self.assertEqual(live.read_bytes(), original)

    @unittest.skipUnless(os.environ.get("LSC_TEST_ADDON_HOST") == "1", "Build the native host for the combined startup test")
    def test_host_finishes_explicit_component_loading_before_managed_main(self):
        import hashlib
        from companion.services.addon_host import MANAGED_HOST_ARCHIVE
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            shutil.copytree(ROOT / "build/native/isolation-fixture", source)
            harness = ROOT / "build/native/addon-host-smoke"
            shutil.copy2(harness / "Lossless_original.dll", source / "Lossless.dll")
            seed = root / "seed.xml"
            seed.write_text("<Settings />")
            runtime = ManagedRuntime(root / "config")
            executable = runtime.prepare(source / "LosslessScaling.exe", AssetStore(root / "assets"), seed_settings=seed)
            app = executable.parent
            shutil.copy2(app / "Lossless.dll", app / "Lossless_original.dll")
            with zipfile.ZipFile(ROOT / "build/native" / MANAGED_HOST_ARCHIVE) as bundle:
                bundle.extract("Lossless.dll", app)
            (app / "addons").mkdir()
            (app / "addons/config.json").write_text(json.dumps({"global": {"companion": {"headless": True}}, "addons": {}}))
            relative = ".companion/components/custom/before-main.dll"
            component = app / relative
            component.parent.mkdir(parents=True)
            shutil.copy2(harness / "Lossless_original.dll", component)
            (app / ".companion/components.json").write_text(json.dumps({"schema": 1, "components": [{
                "id": "custom-fixture", "path": relative, "sha256": hashlib.sha256(component.read_bytes()).hexdigest()}]}))
            environment, receipt, token = launch_environment(executable)
            result = subprocess.run([str(executable), str(runtime.local), component.name], env=environment,
                                    capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            await_isolation(json.loads(receipt.read_text())["pid"], receipt, token)
            self.assertIn("<Fixture>private</Fixture>", runtime.settings.read_text())
            from companion.services.startup_guard import launch_hidden
            import psutil
            pid = launch_hidden(executable, duration_ms=1000)
            self.assertIsInstance(pid, int)
            try:
                psutil.Process(pid).wait(timeout=30)
            except psutil.NoSuchProcess:
                pass


if __name__ == "__main__":
    unittest.main()
