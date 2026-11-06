"""Deploy into a cloned Lossless Scaling folder without touching the source copy."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from companion.services.asset_store import AssetStore
from companion.services.deployment_manager import DeploymentManager, DeploymentConflictError
from companion.services.graphics_resolver import GraphicsResolver


def make_ls_folder(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "LosslessScaling.exe").write_bytes(b"exe")
    (root / "Lossless.dll").write_bytes(b"original-engine")
    return root / "LosslessScaling.exe"


def make_source(root: Path, name: str, data: bytes) -> str:
    path = root / name
    path.write_bytes(data)
    return str(path)


class ClonedFolderDeploymentTests(unittest.TestCase):
    def test_installations_keep_independent_originals_and_state_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            a, b = make_ls_folder(root / "a"), make_ls_folder(root / "b")
            (a.parent / "Lossless.dll").write_bytes(b"original A")
            (b.parent / "Lossless.dll").write_bytes(b"original B")
            source = make_source(root, "source.dll", b"managed")
            store = AssetStore(str(root / "store"))
            files = [dict(relative_path="Lossless.dll", source_path=source)]
            manager = DeploymentManager(store)
            for exe in (a, b):
                manager.apply(profile_id="p", lossless_scaling_exe=str(exe), files=files)
            manager = DeploymentManager(store)
            for exe, original in ((a, b"original A"), (b, b"original B")):
                self.assertFalse(manager.needs_change("p", files, lossless_scaling_exe=str(exe)))
                self.assertEqual(GraphicsResolver(store)._lossless_original(str(exe)).read_bytes(), original)
                manager.apply(profile_id=None, lossless_scaling_exe=str(exe), files=[])
                self.assertEqual((exe.parent / "Lossless.dll").read_bytes(), original)

    def test_legacy_manifest_migrates_only_to_its_audited_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            a, b = make_ls_folder(root / "a"), make_ls_folder(root / "b")
            store = AssetStore(str(root / "store"))
            source = make_source(root, "source.dll", b"managed")
            manager = DeploymentManager(store)
            manifest = manager.apply(profile_id="p", lossless_scaling_exe=str(a),
                                     files=[dict(relative_path="Lossless.dll", source_path=source)])
            # Recreate the pre-upgrade layout, including its existing audit receipt.
            old_backups = store.backups / manager.TARGET_ID
            manager.originals.rename(old_backups / "originals")
            manifest.pop("installation_root")
            manifest["files"][0]["original_backup"] = str(old_backups / "originals" / "Lossless.dll")
            AssetStore._atomic_json(store.deployments / manager.TARGET_ID / "active-manifest.json", manifest)
            manager.manifest_path.unlink()
            restarted = DeploymentManager(store)
            restarted.apply(profile_id=None, lossless_scaling_exe=str(b), files=[])
            self.assertEqual((b.parent / "Lossless.dll").read_bytes(), b"original-engine")
            restarted.apply(profile_id=None, lossless_scaling_exe=str(a), files=[])
            self.assertEqual((a.parent / "Lossless.dll").read_bytes(), b"original-engine")
            self.assertTrue(restarted.manifest_path.is_file())

    def test_unidentified_legacy_manifest_preserves_all_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            exe = make_ls_folder(root / "host")
            store = AssetStore(str(root / "store"))
            legacy = store.deployments / "lossless-scaling" / "active-manifest.json"
            AssetStore._atomic_json(legacy, {"files": [{"relative_path": "Lossless.dll"}]})
            original = legacy.read_bytes()
            with self.assertRaisesRegex(DeploymentConflictError, "Cannot identify"):
                DeploymentManager(store).apply(profile_id=None, lossless_scaling_exe=str(exe), files=[])
            self.assertEqual(legacy.read_bytes(), original)
            self.assertEqual((exe.parent / "Lossless.dll").read_bytes(), b"original-engine")

    def test_failed_rollback_retains_snapshots_and_blocks_further_deployments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            exe = make_ls_folder(root / "host")
            for name in ("a.dll", "b.dll"):
                (exe.parent / name).write_bytes(b"old " + name.encode())
            source = make_source(root, "source.dll", b"managed")
            manager = DeploymentManager(AssetStore(str(root / "store")))
            real_copy = manager._atomic_copy
            calls = 0

            def fail_after_first_copy(src, dst):
                nonlocal calls
                calls += 1
                if calls > 1:
                    raise PermissionError("simulated lock")
                real_copy(src, dst)

            with patch.object(manager, "_atomic_copy", side_effect=fail_after_first_copy):
                with self.assertRaisesRegex(DeploymentConflictError, "rollback is incomplete"):
                    manager.apply(profile_id="p", lossless_scaling_exe=str(exe), files=[
                        dict(relative_path=name, source_path=source) for name in ("a.dll", "b.dll")])
            recovery = json.loads((manager.backup_root / "pending-recovery.json").read_text())
            snapshots = Path(recovery["recovery_directory"])
            self.assertEqual((snapshots / "a.dll").read_bytes(), b"old a.dll")
            self.assertEqual((snapshots / "b.dll").read_bytes(), b"old b.dll")
            self.assertTrue((snapshots / "recovery.json").is_file())
            with self.assertRaisesRegex(DeploymentConflictError, "needs recovery"):
                DeploymentManager(manager.store).apply(profile_id=None, lossless_scaling_exe=str(exe), files=[])
            (snapshots / "a.dll").write_bytes(b"tampered")
            with self.assertRaisesRegex(DeploymentConflictError, "missing or modified"):
                manager.retry_recovery(str(exe))
            self.assertEqual((exe.parent / "a.dll").read_bytes(), b"managed")
            self.assertTrue((manager.backup_root / "pending-recovery.json").exists())
            (snapshots / "a.dll").write_bytes(b"old a.dll")
            manager.retry_recovery(str(exe))
            self.assertEqual((exe.parent / "a.dll").read_bytes(), b"old a.dll")
            self.assertEqual((exe.parent / "b.dll").read_bytes(), b"old b.dll")
            self.assertFalse(snapshots.exists())
            self.assertFalse((manager.backup_root / "pending-recovery.json").exists())

    def test_deploy_targets_clone_leaving_source_copy_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            source_exe = make_ls_folder(tmp / "source-ls")
            clone_exe = make_ls_folder(tmp / "clone-ls")
            payload = make_source(tmp, "payload.dll", b"managed-payload")
            manager = DeploymentManager(AssetStore(str(tmp / "store")))
            files = [{
                "relative_path": "addons/probe/payload.dll",
                "source_path": payload,
                "role": "support_file",
                "source_package": "test/v1",
            }]

            manifest = manager.apply(
                profile_id="game", lossless_scaling_exe=str(clone_exe), files=files,
            )

            deployed = clone_exe.parent / "addons" / "probe" / "payload.dll"
            self.assertTrue(deployed.is_file())
            self.assertEqual(deployed.read_bytes(), b"managed-payload")
            self.assertEqual(manifest["files"][0]["deployed_sha256"],
                             AssetStore.sha256(deployed))
            # The folder it was cloned from is never a deployment target.
            self.assertFalse((source_exe.parent / "addons").exists())

    def test_same_file_set_is_noop_across_profile_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            clone_exe = make_ls_folder(tmp / "clone-ls")
            payload = make_source(tmp, "payload.dll", b"managed-payload")
            manager = DeploymentManager(AssetStore(str(tmp / "store")))
            files = [{
                "relative_path": "addons/probe/payload.dll",
                "source_path": payload,
                "role": "support_file",
                "source_package": "test/v1",
            }]
            manager.apply(profile_id="game", lossless_scaling_exe=str(clone_exe), files=files)

            # Fingerprint excludes profile identity: same files, new profile -> no change.
            self.assertFalse(manager.needs_change(
                "other-profile", files, lossless_scaling_exe=str(clone_exe)))

    def test_two_clones_stay_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            clone_a = make_ls_folder(tmp / "clone-a")
            clone_b = make_ls_folder(tmp / "clone-b")
            payload_a = make_source(tmp, "a.dll", b"payload-a")
            payload_b = make_source(tmp, "b.dll", b"payload-b")
            manager = DeploymentManager(AssetStore(str(tmp / "store")))
            files_a = [{
                "relative_path": "addons/probe/payload.dll",
                "source_path": payload_a,
                "role": "support_file",
                "source_package": "test/v1",
            }]
            files_b = [{
                "relative_path": "addons/probe/payload.dll",
                "source_path": payload_b,
                "role": "support_file",
                "source_package": "test/v1",
            }]
            manager.apply(profile_id="a", lossless_scaling_exe=str(clone_a), files=files_a)
            manager.apply(profile_id="b", lossless_scaling_exe=str(clone_b), files=files_b)

            # Each clone retains its own manifest, payload, and original backups.
            self.assertEqual((clone_b.parent / "addons" / "probe" / "payload.dll").read_bytes(), b"payload-b")
            self.assertEqual((clone_a.parent / "addons" / "probe" / "payload.dll").read_bytes(), b"payload-a")

    def test_runtime_parking_uses_the_requested_installation_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            a, b = make_ls_folder(root / "a"), make_ls_folder(root / "b")
            store = AssetStore(str(root / "store"))
            source = make_source(root, "source.dll", b"reshade")
            files = [dict(relative_path="dxgi.dll", source_path=source, source_package="reshade/1")]
            manager = DeploymentManager(store)
            for exe in (a, b):
                manager.apply(profile_id="p", lossless_scaling_exe=str(exe), files=files)
            manager.set_runtime_packages_active(str(a), {"reshade"}, active=False)
            restarted = DeploymentManager(store)
            self.assertTrue(restarted.runtime_packages_need_state(
                {"reshade"}, active=True, lossless_scaling_exe=str(a)))
            self.assertFalse(restarted.runtime_packages_need_state(
                {"reshade"}, active=True, lossless_scaling_exe=str(b)))
            restarted.set_runtime_packages_active(str(a), {"reshade"}, active=True)
            self.assertTrue((a.parent / "dxgi.dll").is_file())
            self.assertTrue((b.parent / "dxgi.dll").is_file())

    def test_successful_rollback_restores_files_and_removes_transaction_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            exe = make_ls_folder(root / "host")
            for name in ("a.dll", "b.dll"):
                (exe.parent / name).write_bytes(b"old " + name.encode())
            source = make_source(root, "source.dll", b"managed")
            manager = DeploymentManager(AssetStore(str(root / "store")))
            real_copy = manager._atomic_copy
            calls = 0

            def fail_second_copy(src, dst):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise PermissionError("simulated install failure")
                real_copy(src, dst)

            with patch.object(manager, "_atomic_copy", side_effect=fail_second_copy):
                with self.assertRaises(PermissionError):
                    manager.apply(profile_id="p", lossless_scaling_exe=str(exe), files=[
                        dict(relative_path=name, source_path=source) for name in ("a.dll", "b.dll")])
            for name in ("a.dll", "b.dll"):
                self.assertEqual((exe.parent / name).read_bytes(), b"old " + name.encode())
            self.assertFalse((manager.backup_root / "pending-recovery.json").exists())
            self.assertEqual(list(manager.store.staging.iterdir()), [])

    def test_clone_reset_restores_original_engine(self):
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            clone_exe = make_ls_folder(tmp / "clone-ls")
            engine = make_source(tmp, "engine.dll", b"proxy-engine")
            manager = DeploymentManager(AssetStore(str(tmp / "store")))
            manager.apply(
                profile_id="game", lossless_scaling_exe=str(clone_exe),
                files=[{
                    "relative_path": "Lossless.dll",
                    "source_path": engine,
                    "role": "addon_manager_host",
                    "source_package": "ls-addon-manager/v1",
                }],
            )
            self.assertEqual((clone_exe.parent / "Lossless.dll").read_bytes(), b"proxy-engine")

            manager.apply(profile_id=None, lossless_scaling_exe=str(clone_exe), files=[])
            self.assertEqual((clone_exe.parent / "Lossless.dll").read_bytes(), b"original-engine")


if __name__ == "__main__":
    unittest.main()
