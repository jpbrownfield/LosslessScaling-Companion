import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from companion.core.models import DllOverrideConfig, Profile, SpecialKConfig
from companion.services.asset_store import AssetStore, UnsafeAssetError
from companion.services.deployment_manager import DeploymentConflictError, DeploymentManager
from companion.services.graphics_resolver import GraphicsResolutionError, GraphicsResolver


class AssetStoreTests(unittest.TestCase):
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

    def test_lsp_neural_render_plan_preserves_proxy_config_and_original_engine(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = AssetStore(str(root / "store"))
            ls_root = root / "Lossless Scaling"
            (ls_root / "addons").mkdir(parents=True)
            executable = ls_root / "LosslessScaling.exe"
            executable.write_bytes(b"exe")
            original_engine = ls_root / "Lossless.dll"
            original_engine.write_bytes(b"original-engine")
            (ls_root / "addons" / "config.json").write_text(
                json.dumps({
                    "global": {"theme": "dark"},
                    "addons": {"OtherAddon": {"custom": "keep"},
                               "LSP-NeuralRender": {"futureKey": "keep"}},
                }),
                encoding="utf-8",
            )
            self._package(
                store, root, "lossless-proxy", "v1",
                {"Lossless.dll": self._x64_pe(b"proxy-engine")},
            )
            self._package(
                store, root, "lsp-neural-render", "v2",
                {
                    "addons/LSP-NeuralRender/LSP_NeuralRender.dll": self._x64_pe(b"addon"),
                    "addons/LSP-NeuralRender/nvngx.dll_lspnr.dll": self._x64_pe(b"forwarder"),
                    "addons/LSP-NeuralRender/addon.json": json.dumps({
                        "name": "LSP-NeuralRender",
                        "version": "v2",
                        "min_host_version": "0.2.0",
                        "future": {"preserved": True},
                    }),
                },
            )
            self._package(
                store, root, "reshade", "v6",
                {
                    "ReShade64.dll": self._x64_pe(b"reshade"),
                    "lossless-scaling-deployment.json": json.dumps({
                        "schema_version": 1,
                        "files": [{
                            "source": "ReShade64.dll",
                            "destination": "dxgi.dll",
                            "role": "injector",
                        }],
                    }),
                },
            )
            bridge_dir = root / "bundle" / "LSP-ReShade"
            bridge_dir.mkdir(parents=True)
            (bridge_dir / "LSC_ReShadeBridge.dll").write_bytes(self._x64_pe(b"bridge"))
            (bridge_dir / "addon.json").write_text(json.dumps({
                "name": "LS Companion ReShade Bridge",
                "dll": "LSC_ReShadeBridge.dll",
                "min_host_version": "0.3.0",
            }), encoding="utf-8")
            runtime_source = root / "nvngx_dlssnr.dll"
            runtime_source.write_bytes(self._x64_pe(b"user-runtime"))
            runtime = store.import_file(str(runtime_source), allowed_suffixes=(".dll",))
            profile = Profile.model_validate({
                "id": "neural-profile",
                "name": "Neural",
                "graphics": {
                    "lossless_proxy": {"enabled": True, "version": "v1"},
                    "neural_render": {
                        "implementation": "lsp_neural_render",
                        "package": {"enabled": True, "version": "v2"},
                        "runtime_asset_sha256": runtime["sha256"],
                        "style": "cinematic",
                        "intensity": 0.75,
                    },
                },
            })

            plan = GraphicsResolver(
                store, bundled_reshade_bridge_dir=str(bridge_dir)
            ).resolve(
                profile, lossless_scaling_exe=str(executable)
            )
            by_destination = {item["relative_path"]: item for item in plan}
            self.assertEqual(
                Path(by_destination["Lossless_original.dll"]["source_path"]).read_bytes(),
                b"original-engine",
            )
            generated = json.loads(
                Path(by_destination["addons/config.json"]["source_path"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(generated["global"]["theme"], "dark")
            self.assertEqual(generated["addons"]["OtherAddon"]["custom"], "keep")
            neural = generated["addons"]["LSP-NeuralRender"]
            self.assertEqual(neural["futureKey"], "keep")
            self.assertEqual(neural["style"], "2")
            self.assertEqual(neural["intensity"], "0.75")
            self.assertIs(neural["enabled"], True)
            self.assertIn("ReShade.ini", by_destination)
            baseline_reshade = Path(by_destination["ReShade.ini"]["source_path"]).read_text(
                encoding="utf-8"
            )
            self.assertIn("KeyOverlay=0,0,0,0", baseline_reshade)
            self.assertIn("TutorialProgress=4", baseline_reshade)
            addon_manifest = json.loads(Path(
                by_destination["addons/LSP-NeuralRender/addon.json"]["source_path"]
            ).read_text(encoding="utf-8"))
            self.assertEqual(addon_manifest["dll"], "LSP_NeuralRender.dll")
            self.assertEqual(addon_manifest["future"], {"preserved": True})

            DeploymentManager(store).apply(
                profile_id=profile.id,
                lossless_scaling_exe=str(executable),
                files=plan,
            )
            self.assertTrue((ls_root / "Lossless.dll").read_bytes().endswith(b"proxy-engine"))
            self.assertEqual((ls_root / "Lossless_original.dll").read_bytes(), b"original-engine")

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
                    "experimental_reflex": True,
                    "experimental_smooth_motion": True,
                }},
            })

            plan = GraphicsResolver(store).resolve(
                profile, lossless_scaling_exe=str(executable)
            )
            by_destination = {item["relative_path"]: item for item in plan}
            self.assertEqual(set(by_destination), {"dxgi.dll", "dxgi.ini"})
            generated = Path(by_destination["dxgi.ini"]["source_path"]).read_text(
                encoding="utf-8"
            )
            self.assertIn("Use16BitSwapChain=true", generated)
            self.assertIn("scRGBLuminance_[0]=12.5", generated)
            self.assertIn("scRGBPaperWhite_[0]=2.5", generated)
            self.assertIn("TemporaryDesktopHDRMode=true", generated)
            self.assertIn("[NVIDIA.Reflex]\nEnable=true", generated)
            self.assertIn("AllowFlipMetering=true", generated)

    def test_special_k_rejects_paper_white_above_peak_brightness(self):
        with self.assertRaises(ValueError):
            SpecialKConfig(
                hdr_peak_brightness_nits=400,
                hdr_paper_white_nits=500,
            )

    def test_reshade_with_proxy_deploys_bundled_companion_bridge(self):
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
            bridge_dir = root / "bundle" / "LSP-ReShade"
            bridge_dir.mkdir(parents=True)
            (bridge_dir / "LSC_ReShadeBridge.dll").write_bytes(self._x64_pe(b"bridge"))
            (bridge_dir / "addon.json").write_text(
                json.dumps({"dll": "LSC_ReShadeBridge.dll"}), encoding="utf-8"
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

            plan = GraphicsResolver(
                store, bundled_reshade_bridge_dir=str(bridge_dir)
            ).resolve(
                profile,
                lossless_scaling_exe=str(executable),
                reshade_overlay_hotkey="f8",
            )
            by_destination = {item["relative_path"]: item for item in plan}
            self.assertIn("dxgi.dll", by_destination)
            self.assertIn("addons/LSP-ReShade/LSC_ReShadeBridge.dll", by_destination)
            self.assertIn("addons/LSP-ReShade/addon.json", by_destination)
            self.assertIn("addons/config.json", by_destination)
            proxy_config = json.loads(
                Path(by_destination["addons/config.json"]["source_path"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                proxy_config["addons"]["LSP-ReShade"]["hotkey_vk"], "119"
            )
            self.assertIs(
                proxy_config["addons"]["LSP-ReShade"]["enabled"], True
            )
            self.assertTrue(
                by_destination["addons/LSP-ReShade/LSC_ReShadeBridge.dll"]
                ["source_package"].startswith("reshade/bundled-")
            )

    def test_reshade_without_proxy_does_not_deploy_bridge(self):
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

            plan = GraphicsResolver(store).resolve(profile)
            self.assertEqual([item["relative_path"] for item in plan], ["dxgi.dll"])

    def test_proxy_without_reshade_does_not_deploy_bridge(self):
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

            plan = GraphicsResolver(store).resolve(
                profile, lossless_scaling_exe=str(executable)
            )
            destinations = [item["relative_path"] for item in plan]

            self.assertEqual(destinations, ["Lossless.dll", "Lossless_original.dll"])
            self.assertFalse(any("LSP-ReShade" in item for item in destinations))


if __name__ == "__main__":
    unittest.main()
