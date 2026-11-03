"""Deploy into a cloned Lossless Scaling folder without touching the source copy."""
import shutil
import tempfile
import unittest
from pathlib import Path

from companion.services.asset_store import AssetStore
from companion.services.deployment_manager import DeploymentManager


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

            # Clone B got payload-b; clone A still has payload-a (manifest tracks latest,
            # disk state is per-folder).
            self.assertEqual((clone_b.parent / "addons" / "probe" / "payload.dll").read_bytes(), b"payload-b")
            self.assertEqual((clone_a.parent / "addons" / "probe" / "payload.dll").read_bytes(), b"payload-a")

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
