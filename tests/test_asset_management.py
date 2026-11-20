import json
import tempfile
import unittest
import zipfile
import os
from unittest.mock import patch
from pathlib import Path

from companion.core.models import DllOverrideConfig, ManagedPackageConfig, Profile, SpecialKConfig
from companion.services.asset_store import AssetStore, UnsafeAssetError
from companion.services.deployment_manager import DeploymentConflictError, DeploymentManager
from companion.services.graphics_resolver import GraphicsResolutionError, GraphicsResolver


class AssetStoreTests(unittest.TestCase):
    def test_mutable_deployment_copy_does_not_inherit_source_readonly_attribute(self):
        import stat

        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "preset.ini", Path(directory) / "deployed.ini"
            source.write_text("[Effect]\nValue=1\n", encoding="utf-8")
            target.write_text("old", encoding="utf-8")
            source.chmod(stat.S_IREAD)
            target.chmod(stat.S_IREAD)
            DeploymentManager._atomic_copy(source, target, writable=True)
            self.assertTrue(target.stat().st_mode & stat.S_IWUSR)
            self.assertFalse(source.stat().st_mode & stat.S_IWUSR)
            target.write_text("[Effect]\nValue=2\n", encoding="utf-8")
            self.assertIn("Value=1", source.read_text(encoding="utf-8"))
            source.chmod(stat.S_IREAD | stat.S_IWRITE)
    def test_atomic_copy_retries_transient_windows_lock_without_touching_original(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "source.dll", Path(directory) / "target.dll"
            source.write_bytes(b"new")
            target.write_bytes(b"original")
            error = PermissionError("temporarily locked")
            error.winerror = 32
            replace = os.replace
            attempts = []

            def retry(source_name, target_name):
                attempts.append(1)
                if len(attempts) == 1:
                    self.assertEqual(target.read_bytes(), b"original")
                    raise error
                replace(source_name, target_name)

            with patch("companion.services.deployment_manager.os.replace", side_effect=retry), patch(
                "companion.services.deployment_manager.time.sleep"
            ):
                DeploymentManager._atomic_copy(source, target)
            self.assertEqual(target.read_bytes(), b"new")
            self.assertEqual(len(attempts), 2)

    def test_persistent_dll_lock_preserves_original_and_removes_only_temporary_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "source.dll", Path(directory) / "target.dll"
            source.write_bytes(b"new")
            target.write_bytes(b"original")
            error = PermissionError("still locked")
            error.winerror = 5
            with patch("companion.services.deployment_manager.os.replace", side_effect=error) as replace, patch(
                "companion.services.deployment_manager.time.sleep"
            ):
                with self.assertRaises(PermissionError):
                    DeploymentManager._atomic_copy(source, target)
            self.assertEqual(replace.call_count, 10)
            self.assertEqual(target.read_bytes(), b"original")
            self.assertEqual(sorted(path.name for path in Path(directory).iterdir()), ["source.dll", "target.dll"])

    def test_manual_import_writes_security_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "runtime.dll"
            source.write_bytes(b"runtime")
            store = AssetStore(str(root / "store"))

            imported = store.import_file(str(source), allowed_suffixes=(".dll",))

            records = [
                json.loads(line)
                for line in (store.logs / "security-audit.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertEqual(records[-1]["event"], "manual_asset_imported")
            self.assertEqual(records[-1]["sha256"], imported["sha256"])

    def test_list_imports_returns_imported_asset_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "runtime.dll"
            source.write_bytes(b"runtime")
            store = AssetStore(str(root / "store"))
            imported = store.import_file(str(source), allowed_suffixes=(".dll",))

            self.assertEqual(store.list_imports(), [imported])

    def test_release_archive_is_content_addressed_and_safely_extracted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "release.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("addons/Test/addon.json", '{"name":"Test"}')
            store = AssetStore(str(root / "store"))

            package = store.import_release_archive(
                str(archive),
                provider="test-provider",
                version="v1.0.0",
                source_metadata={"url": "https://example.invalid/release"},
            )

            self.assertEqual(package["provider"], "test-provider")
            self.assertEqual(len(package["files"]), 1)
            self.assertTrue(Path(package["payload_path"], "addons", "Test", "addon.json").is_file())

    def test_release_archive_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "unsafe.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("../escape.dll", b"bad")
            store = AssetStore(str(root / "store"))

            with self.assertRaises(UnsafeAssetError):
                store.import_release_archive(
                    str(archive),
                    provider="test-provider",
                    version="v1",
                    source_metadata={},
                )
            self.assertFalse((root / "escape.dll").exists())

    def test_release_archive_rejects_windows_device_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "unsafe-device.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("addons/CON/config.json", b"bad")
            store = AssetStore(str(root / "store"))

            with self.assertRaises(UnsafeAssetError):
                store.import_release_archive(
                    str(archive), provider="test-provider", version="v1", source_metadata={}
                )


class DeploymentManagerTests(unittest.TestCase):
    def test_old_transaction_backups_are_consolidated_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "LosslessScaling.exe"
            executable.write_bytes(b"exe")
            manager = DeploymentManager(AssetStore(str(root / "store")))
            manager._select_installation(root)
            old_transaction = (
                manager.backup_root
                / ("a" * 32)
                / "prechange"
                / "legacy-addons"
                / "LSP-ReShade"
            )
            old_transaction.mkdir(parents=True)
            (old_transaction / "old.dll").write_bytes(b"legacy")

            manager.apply(
                profile_id=None,
                lossless_scaling_exe=str(executable),
                files=[],
            )

            self.assertFalse((manager.backup_root / ("a" * 32)).exists())
            self.assertEqual(
                (
                    manager.preinsert
                    / "legacy-addons"
                    / "LSP-ReShade"
                    / "old.dll"
                ).read_bytes(),
                b"legacy",
            )

    def test_echo_install_archives_recognized_legacy_addons_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "LosslessScaling.exe"
            executable.write_bytes(b"exe")
            legacy = root / "addons" / "LSP-NeuralRender"
            legacy.mkdir(parents=True)
            (legacy / "addon.json").write_text(
                json.dumps({"id": "LSP-NeuralRender"}), encoding="utf-8"
            )
            (legacy / "old.dll").write_bytes(b"old")
            unknown = root / "addons" / "My-Custom-Addon"
            unknown.mkdir()
            (unknown / "addon.json").write_text(
                json.dumps({"id": "My-Custom-Addon"}), encoding="utf-8"
            )
            legacy_icon = root / "LP-icon.ico"
            legacy_icon.write_bytes(b"old icon")
            source = root / "echo-host.dll"
            source.write_bytes(b"echo")
            manager = DeploymentManager(AssetStore(str(root / "store")))

            manifest = manager.apply(
                profile_id="game",
                lossless_scaling_exe=str(executable),
                files=[{
                    "relative_path": "Lossless.dll",
                    "source_path": str(source),
                    "role": "addon_manager_host",
                    "source_package": "ls-addon-manager/v1/hash",
                }],
            )

            self.assertFalse(legacy.exists())
            self.assertTrue(unknown.is_dir())
            self.assertFalse(legacy_icon.exists())
            backup = Path(manifest["legacy_addon_backup"])
            self.assertEqual((backup / "LSP-NeuralRender" / "old.dll").read_bytes(), b"old")
            self.assertEqual((backup / "LP-icon.ico").read_bytes(), b"old icon")
            persistent_backup_folders = {
                item.name for item in manager.backup_root.iterdir() if item.is_dir()
            }
            self.assertEqual(persistent_backup_folders, {"preinsert"})
            self.assertFalse(any(
                item.name.startswith("deployment-rollback-")
                for item in manager.store.staging.iterdir()
            ))
            self.assertIn("legacy_addon_structure_archived", (
                manager.store.logs / "security-audit.jsonl"
            ).read_text(encoding="utf-8"))

    def test_neural_render_update_replaces_modified_existing_component(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "LosslessScaling.exe"
            executable.write_bytes(b"exe")
            old_source = root / "old-neural.dll"
            new_source = root / "companion-neural.dll"
            old_source.write_bytes(b"old-upstream")
            new_source.write_bytes(b"companion-hdr-fork")
            manager = DeploymentManager(AssetStore(str(root / "store")))
            relative = "addons/LSP-NeuralRender/LSP_NeuralRender.dll"

            manager.apply(
                profile_id="neural",
                lossless_scaling_exe=str(executable),
                files=[{
                    "relative_path": relative,
                    "source_path": str(old_source),
                    "role": "lossless_addon",
                    "source_package": "lsp-neural-render/0.2.0/old",
                }],
            )
            deployed = root / Path(relative)
            deployed.write_bytes(b"another-existing-neural-build")

            manager.apply(
                profile_id="neural",
                lossless_scaling_exe=str(executable),
                files=[{
                    "relative_path": relative,
                    "source_path": str(new_source),
                    "role": "lossless_addon",
                    "source_package": "lsp-neural-render/0.3.0-lsc-hdr/new",
                }],
            )

            self.assertEqual(deployed.read_bytes(), b"companion-hdr-fork")

    def test_neural_render_takeover_does_not_restore_superseded_manual_install(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "LosslessScaling.exe"
            executable.write_bytes(b"exe")
            relative = "addons/LSP-NeuralRender/LSP_NeuralRender.dll"
            deployed = root / Path(relative)
            deployed.parent.mkdir(parents=True)
            deployed.write_bytes(b"manual-old-install")
            companion_source = root / "companion-neural.dll"
            companion_source.write_bytes(b"companion-hdr-fork")
            manager = DeploymentManager(AssetStore(str(root / "store")))

            manifest = manager.apply(
                profile_id="neural",
                lossless_scaling_exe=str(executable),
                files=[{
                    "relative_path": relative,
                    "source_path": str(companion_source),
                    "role": "lossless_addon",
                    "source_package": "lsp-neural-render/0.3.0-lsc-hdr/new",
                }],
            )
            self.assertEqual(deployed.read_bytes(), b"companion-hdr-fork")
            self.assertFalse(manifest["files"][0]["original_existed"])
            self.assertIsNone(manifest["files"][0]["original_backup"])

            manager.apply(
                profile_id="without-neural",
                lossless_scaling_exe=str(executable),
                files=[],
            )
            self.assertFalse(deployed.exists())

    def test_runtime_addon_dlls_are_parked_and_reactivated_in_place(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "LosslessScaling.exe"
            executable.write_bytes(b"exe")
            source = root / "ReShade64.dll.source"
            source.write_bytes(b"reshade")
            manager = DeploymentManager(AssetStore(str(root / "store")))
            files = [{
                "relative_path": "ReShade64.dll",
                "source_path": str(source),
                "role": "injector",
                "source_package": "reshade/6.8.0/hash",
            }]
            manager.apply(profile_id="game", lossless_scaling_exe=str(executable), files=files)

            self.assertTrue(manager.set_runtime_packages_active(
                str(executable), {"reshade"}, active=False
            ))
            self.assertFalse((root / "ReShade64.dll").exists())
            self.assertEqual((root / "ReShade64.dll.inactive").read_bytes(), b"reshade")
            self.assertFalse(manager.needs_change(
                "game", files, lossless_scaling_exe=str(executable)
            ))

            self.assertTrue(manager.set_runtime_packages_active(
                str(executable), {"reshade"}, active=True
            ))
            self.assertEqual((root / "ReShade64.dll").read_bytes(), b"reshade")
            self.assertFalse((root / "ReShade64.dll.inactive").exists())
            audit = (manager.store.logs / "security-audit.jsonl").read_text(
                encoding="utf-8"
            )
            self.assertIn('"event": "runtime_addons_parked"', audit)
            self.assertIn('"event": "runtime_addons_activated"', audit)

    def test_retired_display_commander_receipt_removes_only_managed_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "LosslessScaling.exe"
            executable.write_bytes(b"exe")
            binary = root / "source.addon64"
            binary.write_bytes(b"managed-addon")
            config = root / "source.ini"
            config.write_text("[DisplayCommander]\nReflex.InjectionEnabled=1\n", encoding="utf-8")
            unrelated = root / "user.addon64"
            unrelated.write_bytes(b"user-addon")
            manager = DeploymentManager(AssetStore(str(root / "store")))
            files = [{
                "relative_path": "zzz_display_commander.addon64", "source_path": str(binary),
                "role": "reshade_addon", "source_package": "display-commander/old/hash",
            }, {
                "relative_path": "DisplayCommander.ini", "source_path": str(config),
                "role": "display_commander_config", "source_package": "generated/display-commander-config",
            }]
            manager.apply(profile_id="legacy", lossless_scaling_exe=str(executable), files=files)
            (root / "DisplayCommander.ini").write_text("runtime-modified=true", encoding="utf-8")
            manager.apply(profile_id=None, lossless_scaling_exe=str(executable), files=[])
            self.assertFalse((root / "zzz_display_commander.addon64").exists())
            self.assertFalse((root / "DisplayCommander.ini").exists())
            self.assertEqual(unrelated.read_bytes(), b"user-addon")

    def test_deploy_and_empty_plan_restore_original(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ls_root = root / "Lossless Scaling"
            ls_root.mkdir()
            executable = ls_root / "LosslessScaling.exe"
            executable.write_bytes(b"exe")
            source = root / "managed.dll"
            source.write_bytes(b"managed")
            destination = ls_root / "dxgi.dll"
            destination.write_bytes(b"original")
            manager = DeploymentManager(AssetStore(str(root / "store")))

            manager.apply(
                profile_id="one",
                lossless_scaling_exe=str(executable),
                files=[{"relative_path": "dxgi.dll", "source_path": str(source)}],
            )
            self.assertEqual(destination.read_bytes(), b"managed")

            manager.apply(profile_id=None, lossless_scaling_exe=str(executable), files=[])
            self.assertEqual(destination.read_bytes(), b"original")

    def test_identical_files_across_profiles_do_not_require_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "LosslessScaling.exe"
            executable.write_bytes(b"exe")
            source = root / "addon.dll"
            source.write_bytes(b"same")
            files = [{"relative_path": "addons/Test/addon.dll", "source_path": str(source)}]
            manager = DeploymentManager(AssetStore(str(root / "store")))
            manager.apply(profile_id="one", lossless_scaling_exe=str(executable), files=files)

            self.assertFalse(manager.needs_change("two", files))

    def test_mutable_proxy_config_is_repaired_but_binary_changes_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "LosslessScaling.exe"
            executable.write_bytes(b"exe")
            dll_source = root / "managed.dll"
            config_source = root / "managed.json"
            dll_source.write_bytes(b"managed")
            config_source.write_text('{"managed":true}', encoding="utf-8")
            files = [
                {"relative_path": "addons/config.json", "source_path": str(config_source),
                 "role": "lossless_proxy_config"},
                {"relative_path": "addons/Test.dll", "source_path": str(dll_source),
                 "role": "lossless_addon"},
            ]
            manager = DeploymentManager(AssetStore(str(root / "store")))
            manager.apply(profile_id="one", lossless_scaling_exe=str(executable), files=files)

            deployed_config = root / "addons" / "config.json"
            deployed_config.write_text('{"managed":false}', encoding="utf-8")
            self.assertTrue(manager.needs_change(
                "one", files, lossless_scaling_exe=str(executable)
            ))
            manager.apply(profile_id="one", lossless_scaling_exe=str(executable), files=files)
            self.assertEqual(deployed_config.read_text(encoding="utf-8"), '{"managed":true}')

            deployed_dll = root / "addons" / "Test.dll"
            deployed_dll.write_bytes(b"external-change")
            with self.assertRaises(DeploymentConflictError):
                manager.apply(profile_id="one", lossless_scaling_exe=str(executable), files=files)
            self.assertEqual(deployed_dll.read_bytes(), b"external-change")

    def test_runtime_modified_special_k_config_can_be_restored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "LosslessScaling.exe"
            executable.write_bytes(b"exe")
            source = root / "generated.ini"
            source.write_text("generated=true", encoding="utf-8")
            destination = root / "dxgi.ini"
            destination.write_text("original=true", encoding="utf-8")
            manager = DeploymentManager(AssetStore(str(root / "store")))
            manager.apply(
                profile_id="special-k",
                lossless_scaling_exe=str(executable),
                files=[{
                    "relative_path": "dxgi.ini",
                    "source_path": str(source),
                    "role": "special_k_config",
                }],
            )

            destination.write_text("runtime-added=true", encoding="utf-8")
            manager.apply(profile_id=None, lossless_scaling_exe=str(executable), files=[])

            self.assertEqual(destination.read_text(encoding="utf-8"), "original=true")

    def test_game_destination_is_not_part_of_resolved_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "mod.dll"
            source.write_bytes(b"managed")
            profile = Profile(
                name="unsafe",
                target_executable_path=str(root / "game" / "game.exe"),
                dll_overrides=[
                    DllOverrideConfig(
                        enabled=True,
                        source_dll_path=str(source),
                        deployment_target="target_application",
                    )
                ],
            )

            with self.assertRaises(GraphicsResolutionError):
                GraphicsResolver(AssetStore(str(root / "store"))).resolve(profile)


class GraphicsResolverTests(unittest.TestCase):
    def test_custom_component_does_not_retain_a_windows_proxy_basename(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "selected.dll"
            source.write_bytes(self._x64_pe(b"custom"))
            store = AssetStore(str(root / "store"))
            plan = GraphicsResolver(store)._explicit_components(Profile(name="Custom"), [{
                "relative_path": "dxgi.dll", "source_path": str(source), "role": "custom_dll"}])
            entry = next(item for item in plan if item["role"] == "custom_dll")
            self.assertEqual(Path(entry["relative_path"]).name, "LSC_CustomAddon1.dll")
            self.assertNotEqual(Path(entry["relative_path"]).name, "dxgi.dll")
            manifest = next(item for item in plan if item["role"] == "component_load_manifest")
            self.assertEqual(json.loads(Path(manifest["source_path"]).read_text())["components"][0]["path"], entry["relative_path"])

    def test_custom_component_names_stay_stable_across_binary_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "selected.dll"
            second = root / "other.dll"
            source.write_bytes(self._x64_pe(b"original"))
            second.write_bytes(self._x64_pe(b"other"))
            resolver = GraphicsResolver(AssetStore(str(root / "store")))
            profile = Profile(name="Stable names")

            def resolve():
                plan = resolver._explicit_components(profile, [
                    {"relative_path": "dxgi.dll", "source_path": str(source), "role": "custom_dll"},
                    {"relative_path": "d3d11.dll", "source_path": str(second), "role": "custom_dll"}])
                manifest = next(item for item in plan if item["role"] == "component_load_manifest")
                return json.loads(Path(manifest["source_path"]).read_text())["components"]

            original = resolve()
            source.write_bytes(self._x64_pe(b"updated"))
            updated = resolve()
            self.assertEqual([item["path"] for item in original], [item["path"] for item in updated])
            self.assertEqual([Path(item["path"]).name for item in updated], ["LSC_CustomAddon1.dll", "LSC_CustomAddon2.dll"])
            self.assertNotEqual(original[0]["sha256"], updated[0]["sha256"])
            self.assertEqual(original[1]["sha256"], updated[1]["sha256"])

    def test_duplicate_custom_binary_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "selected.dll"
            source.write_bytes(self._x64_pe(b"same binary"))
            resolver = GraphicsResolver(AssetStore(str(root / "store")))
            with self.assertRaisesRegex(GraphicsResolutionError, "same custom DLL"):
                resolver._explicit_components(Profile(name="Duplicate"), [
                    {"relative_path": name, "source_path": str(source), "role": "custom_dll"}
                    for name in ("dxgi.dll", "d3d11.dll")])

    def test_host_components_keep_both_injectors_out_of_windows_proxy_slots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = AssetStore(str(root / "store"))
            executable = root / "LosslessScaling.exe"
            executable.write_bytes(b"exe")
            (root / "Lossless.dll").write_bytes(self._x64_pe(b"original"))
            for provider, filename in (("ls-addon-manager", "Lossless.dll"),
                                       ("reshade", "ReShade64.dll"), ("special-k", "SpecialK64.dll")):
                self._package(store, root, provider, "v1", {
                    filename: self._x64_pe(provider.encode()),
                    "lossless-scaling-deployment.json": json.dumps({"schema_version": 1, "files": [{
                        "source": filename, "destination": "Lossless.dll" if provider == "ls-addon-manager" else "dxgi.dll",
                        "role": "addon_manager_host" if provider == "ls-addon-manager" else "injector"}]}),
                })
            profile = Profile.model_validate({"name": "Both components", "reshade": {"enabled": True},
                "graphics": {"reshade": {"enabled": True}, "special_k": {"enabled": True}}})
            plan = GraphicsResolver(store).resolve(profile, lossless_scaling_exe=str(executable),
                addon_manager_enabled=True, addon_manager_config=ManagedPackageConfig(enabled=True, version="v1"))
            paths = {item["relative_path"] for item in plan}
            self.assertNotIn("dxgi.dll", paths)
            self.assertIn(".companion/components/reshade/ReShade64.dll", paths)
            special_k = next(path for path in paths if path.endswith("/SpecialK64.dll"))
            special_k_root = special_k.rsplit("/", 1)[0]
            self.assertTrue(special_k_root.startswith(".companion/components/special-k/"))
            self.assertIn(f"{special_k_root}/Global/default_SpecialK.ini", paths)
            self.assertIn(f"{special_k_root}/Global/osd.ini", paths)
            manifest = next(item for item in plan if item["role"] == "component_load_manifest")
            components = json.loads(Path(manifest["source_path"]).read_text())["components"]
            self.assertEqual([item["id"] for item in components], ["special-k", "reshade"])
            for entry in components:
                item = next(item for item in plan if item["relative_path"] == entry["path"])
                self.assertEqual(entry["sha256"], store.sha256(Path(item["source_path"])))
            profile.graphics.special_k.hdr_peak_brightness_nits = 1200
            changed = GraphicsResolver(store).resolve(profile, lossless_scaling_exe=str(executable),
                addon_manager_enabled=True, addon_manager_config=ManagedPackageConfig(enabled=True, version="v1"))
            self.assertNotEqual(special_k, next(item["relative_path"] for item in changed if item["relative_path"].endswith("/SpecialK64.dll")))

    @staticmethod
    def _x64_pe(payload=b""):
        image = bytearray(0x86)
        image[0:2] = b"MZ"
        image[0x3C:0x40] = (0x80).to_bytes(4, "little")
        image[0x80:0x84] = b"PE\0\0"
        image[0x84:0x86] = (0x8664).to_bytes(2, "little")
        return bytes(image) + payload

    @staticmethod
    def _package(store, root, provider, version, members):
        archive = root / f"{provider}.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            for name, content in members.items():
                bundle.writestr(name, content)
        return store.import_release_archive(
            str(archive), provider=provider, version=version, source_metadata={}
        )

    def test_echo_neural_plan_deploys_the_selected_model_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = AssetStore(str(root / "store"))
            executable = root / "LosslessScaling.exe"
            executable.write_bytes(b"exe")
            (root / "Lossless.dll").write_bytes(self._x64_pe(b"original"))
            self._package(
                store,
                root,
                "ls-addon-manager",
                "v1",
                {
                    "Lossless.dll": self._x64_pe(b"echo"),
                    "lossless-scaling-deployment.json": json.dumps({
                        "schema_version": 1,
                        "files": [{
                            "source": "Lossless.dll",
                            "destination": "Lossless.dll",
                            "role": "addon_manager_host",
                        }],
                    }),
                },
            )
            runtime_source = root / "runtime-source" / "nvngx_dlssnr.dll"
            runtime_source.parent.mkdir()
            runtime_source.write_bytes(self._x64_pe(b"model"))
            runtime = store.import_file(str(runtime_source), allowed_suffixes=(".dll",))
            profile = Profile(name="Echo Neural")
            profile.graphics.neural_render.implementation = "lsp_neural_render"
            profile.graphics.neural_render.runtime_asset_sha256 = runtime["sha256"]

            plan = GraphicsResolver(store).resolve(
                profile,
                lossless_scaling_exe=str(executable),
                addon_manager_enabled=True,
                addon_manager_config=ManagedPackageConfig(enabled=True, version="v1"),
            )

            model = next(item for item in plan if item["role"] == "neural_runtime")
            self.assertEqual(model["relative_path"], "nvngx_dlssnr.dll")
            self.assertEqual(
                Path(model["source_path"]).read_bytes(), runtime_source.read_bytes()
            )

    def test_special_k_plan_generates_profile_hdr_and_experimental_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = AssetStore(str(root / "store"))
            executable = root / "LosslessScaling.exe"
            executable.write_bytes(b"exe")
            self._package(
                store,
                root,
                "special-k",
                "v1",
                {
                    "SpecialK64.dll": self._x64_pe(b"special-k"),
                    "packaged.ini": b"[ignored]",
                    "lossless-scaling-deployment.json": json.dumps({
                        "schema_version": 1,
                        "files": [
                            {"source": "SpecialK64.dll", "destination": "dxgi.dll", "role": "injector"},
                            {"source": "packaged.ini", "destination": "dxgi.ini", "role": "config"},
                        ],
                    }),
                },
            )
            profile = Profile.model_validate({
                "id": "special-k-profile",
                "name": "Special K",
                "graphics": {"special_k": {
                    "enabled": True,
                    "version": "v1",
                    "sdr_to_hdr": True,
                    "hdr_peak_brightness_nits": 1000,
                    "hdr_paper_white_nits": 200,
                    "temporary_windows_hdr": True,
                    "experimental_smooth_motion": True,
                }},
            })

            plan = self._resolve_with_host(store, root, profile)
            by_destination = {item["relative_path"]: item for item in plan}
            self.assertNotIn("dxgi.dll", by_destination)
            overlay_file = next(item for path, item in by_destination.items() if path.endswith("/Global/osd.ini"))
            overlay = Path(overlay_file["source_path"]).read_text(encoding="utf-8")
            self.assertIn("[SpecialK.OSD]\nShow=false", overlay)
            self.assertIn("[SpecialK.VersionBanner]\nDuration=0", overlay)
            config_file = next(item for path, item in by_destination.items() if path.endswith("/Global/default_SpecialK.ini"))
            generated = Path(config_file["source_path"]).read_text(
                encoding="utf-8"
            )
            self.assertIn("Use16BitSwapChain=true", generated)
            self.assertIn("scRGBLuminance_[0]=12.5", generated)
            self.assertIn("scRGBPaperWhite_[0]=2.5", generated)
            self.assertIn("TemporaryDesktopHDRMode=true", generated)
            self.assertNotIn("[NVIDIA.Reflex]", generated)
            self.assertIn("AllowFlipMetering=true", generated)
            self.assertIn("EnableImGuiToggle=false", generated)

    def test_special_k_rejects_paper_white_above_peak_brightness(self):
        with self.assertRaises(ValueError):
            SpecialKConfig(
                hdr_peak_brightness_nits=400,
                hdr_paper_white_nits=500,
            )

    def test_legacy_proxy_cannot_bypass_the_current_host_requirement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = AssetStore(str(root / "store"))
            ls_root = root / "Lossless Scaling"
            ls_root.mkdir()
            executable = ls_root / "LosslessScaling.exe"
            executable.write_bytes(b"exe")
            (ls_root / "Lossless.dll").write_bytes(b"original-engine")
            self._package(
                store, root, "lossless-proxy", "v1",
                {"Lossless.dll": self._x64_pe(b"proxy")},
            )
            self._package(
                store, root, "reshade", "v2",
                {
                    "ReShade64.dll": self._x64_pe(b"reshade"),
                    "lossless-scaling-deployment.json": json.dumps({
                        "schema_version": 1,
                        "files": [{
                            "source": "ReShade64.dll",
                            "destination": "ReShade64.dll",
                            "role": "injector",
                        }],
                    }),
                },
            )
            profile = Profile.model_validate({
                "id": "proxy-reshade",
                "name": "Proxy + ReShade",
                "reshade": {"enabled": True, "menu_proxy_enabled": True},
                "graphics": {
                    "lossless_proxy": {"enabled": True, "version": "v1"},
                    "reshade": {"enabled": True, "version": "v2"},
                },
            })

            for enabled in (False, None):
                with self.subTest(enabled=enabled):
                    with self.assertRaisesRegex(GraphicsResolutionError, "Enable the Companion Addon Host"):
                        GraphicsResolver(store).resolve(
                            profile, lossless_scaling_exe=str(executable),
                            addon_manager_enabled=enabled,
                        )
            plan = self._resolve_with_host(store, root, profile)
            destinations = {item["relative_path"] for item in plan}
            self.assertIn(".companion/components/reshade/ReShade64.dll", destinations)
            self.assertFalse(any("LSP-ReShade" in path for path in destinations))
            self.assertFalse(any(item["source_package"].startswith("lossless-proxy/") for item in plan))

    def test_reshade_without_host_requires_the_companion_host(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = AssetStore(str(root / "store"))
            self._package(
                store, root, "reshade", "v2",
                {
                    "ReShade64.dll": self._x64_pe(b"reshade"),
                    "lossless-scaling-deployment.json": json.dumps({
                        "schema_version": 1,
                        "files": [{
                            "source": "ReShade64.dll",
                            "destination": "ReShade64.dll",
                        }],
                    }),
                },
            )
            profile = Profile.model_validate({
                "id": "reshade-only",
                "name": "ReShade",
                "graphics": {"reshade": {"enabled": True, "version": "v2"}},
            })

            with self.assertRaisesRegex(GraphicsResolutionError, "Enable the Companion Addon Host"):
                GraphicsResolver(store).resolve(profile)

    def test_selected_reshade_profile_replaces_installer_default_ini(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = AssetStore(str(root / "store"))
            self._package(
                store, root, "reshade", "v2",
                {
                    "ReShade64.dll": self._x64_pe(b"reshade"),
                    "ReShade.ini": "[GENERAL]\nPerformanceMode=0\n",
                    "lossless-scaling-deployment.json": json.dumps({
                        "schema_version": 1,
                        "files": [
                            {"source": "ReShade64.dll", "destination": "dxgi.dll"},
                            {"source": "ReShade.ini", "destination": "ReShade.ini"},
                        ],
                    }),
                },
            )
            profile = Profile.model_validate({
                "id": "managed-reshade",
                "name": "Managed ReShade",
                "reshade": {
                    "enabled": True,
                    "managed_profile_id": "my-preset",
                },
                "graphics": {"reshade": {"enabled": True, "version": "v2"}},
            })

            plan = self._resolve_with_host(store, root, profile)
            destinations = {item["relative_path"] for item in plan}
            self.assertIn(".companion/components/reshade/ReShade64.dll", destinations)
            self.assertNotIn("ReShade.ini", destinations)

    def test_default_and_disabled_resolution_never_deploy_the_retired_host(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = AssetStore(str(root / "store"))
            ls_root = root / "Lossless Scaling"
            ls_root.mkdir()
            executable = ls_root / "LosslessScaling.exe"
            executable.write_bytes(b"exe")
            (ls_root / "Lossless.dll").write_bytes(b"original-engine")
            self._package(
                store, root, "lossless-proxy", "v1",
                {"Lossless.dll": self._x64_pe(b"proxy")},
            )
            profile = Profile.model_validate({
                "id": "proxy-only",
                "name": "Proxy only",
                "graphics": {
                    "lossless_proxy": {"enabled": True, "version": "v1"},
                },
            })

            resolver = GraphicsResolver(store)
            self.assertEqual(resolver.resolve(profile, lossless_scaling_exe=str(executable)), [])
            for enabled in (False, None):
                with self.subTest(enabled=enabled):
                    self.assertEqual(resolver.resolve(
                        profile, lossless_scaling_exe=str(executable),
                        addon_manager_enabled=enabled,
                    ), [])

    def _resolve_with_host(self, store, root, profile):
        executable = root / "LosslessScaling.exe"
        executable.write_bytes(b"exe")
        (root / "Lossless.dll").write_bytes(self._x64_pe(b"original"))
        self._package(store, root, "ls-addon-manager", "v1", {
            "Lossless.dll": self._x64_pe(b"host"),
            "lossless-scaling-deployment.json": json.dumps({"schema_version": 1, "files": [{
                "source": "Lossless.dll", "destination": "Lossless.dll", "role": "addon_manager_host",
            }]}),
        })
        return GraphicsResolver(store).resolve(
            profile, lossless_scaling_exe=str(executable), addon_manager_enabled=True,
        )


if __name__ == "__main__":
    unittest.main()
