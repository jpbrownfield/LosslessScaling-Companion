"""Regression contracts for the two LosslessProxy integration stacks."""

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from companion.core.models import Profile
from companion.services.asset_store import AssetStore
from companion.services.deployment_manager import DeploymentConflictError, DeploymentManager
from companion.services.diagnostic_runner import DiagnosticRunner
from companion.services.graphics_resolver import GraphicsResolutionError, GraphicsResolver


def x64_pe(payload: bytes = b"") -> bytes:
    image = bytearray(0x88)
    image[0:2] = b"MZ"
    image[0x3C:0x40] = (0x80).to_bytes(4, "little")
    image[0x80:0x84] = b"PE\0\0"
    image[0x84:0x86] = (0x8664).to_bytes(2, "little")
    return bytes(image) + payload


class ProxyIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = AssetStore(str(self.root / "store"))
        self.ls_root = self.root / "Lossless Scaling"
        self.ls_root.mkdir()
        self.executable = self.ls_root / "LosslessScaling.exe"
        self.executable.write_bytes(b"exe")
        (self.ls_root / "Lossless.dll").write_bytes(b"original-engine")
        self.bridge = self.root / "bundled" / "LSP-ReShade"
        self.bridge.mkdir(parents=True)
        (self.bridge / "LSC_ReShadeBridge.dll").write_bytes(x64_pe(b"bridge"))
        (self.bridge / "addon.json").write_text(json.dumps({
            "name": "LS Companion ReShade Bridge",
            "version": "1.0.0",
            "min_host_version": "0.3.0",
            "dll": "LSC_ReShadeBridge.dll",
        }), encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def package(self, provider: str, version: str, members: dict[str, object]):
        archive = self.root / f"{provider}-{version}.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            for name, content in members.items():
                bundle.writestr(name, content)
        return self.store.import_release_archive(
            str(archive), provider=provider, version=version, source_metadata={}
        )

    def proxy(self):
        return self.package("lossless-proxy", "v0.3.0", {
            "Lossless.dll": x64_pe(b"proxy"),
        })

    def reshade(self):
        return self.package("reshade", "6.8.1", {
            "ReShade64.dll": x64_pe(b"reshade"),
            "lossless-scaling-deployment.json": json.dumps({
                "schema_version": 1,
                "files": [{
                    "source": "ReShade64.dll", "destination": "dxgi.dll",
                    "role": "injector",
                }],
            }),
        })

    def resolver(self):
        return GraphicsResolver(self.store, bundled_reshade_bridge_dir=str(self.bridge))

    def test_proxy_reshade_legacy_package_deploys_under_loadable_dxgi_name(self):
        self.proxy()
        self.package("reshade", "6.8.1", {
            "ReShade64.dll": x64_pe(b"reshade"),
            "lossless-scaling-deployment.json": json.dumps({
                "schema_version": 1,
                "files": [{
                    "source": "ReShade64.dll",
                    "destination": "ReShade64.dll",
                    "role": "injector",
                }],
            }),
        })
        profile = Profile.model_validate({
            "id": "proxy-reshade-contract",
            "name": "Proxy ReShade",
            "reshade": {"enabled": True, "menu_proxy_enabled": True},
            "graphics": {
                "lossless_proxy": {"enabled": True, "version": "v0.3.0"},
                "reshade": {"enabled": True, "version": "6.8.1"},
            },
        })

        plan = self.resolver().resolve(
            profile, lossless_scaling_exe=str(self.executable), reshade_overlay_hotkey="end"
        )
        destinations = {item["relative_path"] for item in plan}
        self.assertEqual(destinations, {
            "Lossless.dll", "Lossless_original.dll", "dxgi.dll",
            "addons/LSP-ReShade/LSC_ReShadeBridge.dll",
            "addons/LSP-ReShade/addon.json", "addons/config.json",
        })
        self.assertNotIn("ReShade64.dll", destinations)
        DeploymentManager(self.store).apply(
            profile_id=profile.id, lossless_scaling_exe=str(self.executable), files=plan
        )
        self.assertTrue((self.ls_root / "dxgi.dll").read_bytes().endswith(b"reshade"))
        config = json.loads((self.ls_root / "addons" / "config.json").read_text())
        self.assertIs(config["addons"]["LSP-ReShade"]["enabled"], True)
        self.assertEqual(config["addons"]["LSP-ReShade"]["hotkey_vk"], "35")

    def test_proxy_neural_render_deploys_exact_entrypoint_forwarder_runtime_and_config(self):
        self.proxy()
        self.reshade()
        self.package("lsp-neural-render", "v0.2.1", {
            "addons/LSP-NeuralRender/LSP_NeuralRender.dll": x64_pe(b"addon"),
            "addons/LSP-NeuralRender/nvngx.dll_lspnr.dll": x64_pe(b"forwarder"),
            "addons/LSP-NeuralRender/addon.json": json.dumps({
                "name": "LSP-NeuralRender",
                "version": "0.2.1",
                "min_host_version": "0.2.0",
                "dll": "wrong-forwarder.dll",
                "dependencies": ["nvngx_dlssnr.dll"],
            }),
        })
        runtime_source = self.root / "nvngx_dlssnr.dll"
        runtime_source.write_bytes(x64_pe(b"runtime"))
        runtime = self.store.import_file(str(runtime_source), allowed_suffixes=(".dll",))
        profile = Profile.model_validate({
            "id": "proxy-neural-contract",
            "name": "Proxy Neural",
            "graphics": {
                "lossless_proxy": {"enabled": True, "version": "v0.3.0"},
                "neural_render": {
                    "implementation": "lsp_neural_render",
                    "package": {"enabled": True, "version": "v0.2.1"},
                    "runtime_asset_sha256": runtime["sha256"],
                    "style": "natural", "intensity": 0.8,
                    "auto_skin_mask": True, "use_lsfg_optical_flow": True,
                },
            },
        })

        plan = self.resolver().resolve(profile, lossless_scaling_exe=str(self.executable))
        destinations = {item["relative_path"] for item in plan}
        self.assertEqual(destinations, {
            "Lossless.dll", "Lossless_original.dll", "nvngx_dlssnr.dll",
            "ReShade.ini", "dxgi.dll",
            "addons/LSP-NeuralRender/LSP_NeuralRender.dll",
            "addons/LSP-NeuralRender/nvngx.dll_lspnr.dll",
            "addons/LSP-NeuralRender/addon.json", "addons/config.json",
        })
        DeploymentManager(self.store).apply(
            profile_id=profile.id, lossless_scaling_exe=str(self.executable), files=plan
        )
        manifest = json.loads(
            (self.ls_root / "addons" / "LSP-NeuralRender" / "addon.json").read_text()
        )
        self.assertEqual(manifest["dll"], "LSP_NeuralRender.dll")
        self.assertEqual(manifest["dependencies"], ["nvngx_dlssnr.dll"])
        config = json.loads((self.ls_root / "addons" / "config.json").read_text())
        neural = config["addons"]["LSP-NeuralRender"]
        self.assertEqual(neural["style"], "1")
        self.assertEqual(neural["intensity"], "0.8")
        self.assertEqual(neural["autoMask"], "1")
        self.assertEqual(neural["useFlow"], "1")
        self.assertEqual(neural["workingScale"], "0.35")
        baseline = (self.ls_root / "ReShade.ini").read_text(encoding="utf-8")
        self.assertIn("KeyOverlay=0,0,0,0", baseline)
        self.assertIn("TutorialProgress=4", baseline)

        # ReShade may persist harmless runtime state in this generated config;
        # it remains managed and can still be cleanly removed/restored.
        (self.ls_root / "ReShade.ini").write_text(baseline + "RuntimeState=1\n", encoding="utf-8")
        self.assertFalse(DeploymentManager(self.store).needs_change(
            profile.id, plan, lossless_scaling_exe=str(self.executable)
        ))
        DeploymentManager(self.store).apply(
            profile_id=None, lossless_scaling_exe=str(self.executable), files=[]
        )
        self.assertFalse((self.ls_root / "ReShade.ini").exists())

        selected_data = profile.model_dump()
        selected_data["reshade"] = {
            "enabled": True, "managed_profile_id": "named-config",
        }
        selected = Profile.model_validate(selected_data)
        selected_plan = self.resolver().resolve(
            selected, lossless_scaling_exe=str(self.executable)
        )
        self.assertNotIn("ReShade.ini", {
            item["relative_path"] for item in selected_plan
        })

    def test_neural_render_rejects_an_invalid_or_unnamed_manifest(self):
        self.proxy()
        self.package("lsp-neural-render", "broken", {
            "addons/LSP-NeuralRender/LSP_NeuralRender.dll": x64_pe(),
            "addons/LSP-NeuralRender/nvngx.dll_lspnr.dll": x64_pe(),
            "addons/LSP-NeuralRender/addon.json": "{}",
        })
        profile = Profile.model_validate({
            "id": "broken-neural", "name": "Broken Neural",
            "graphics": {
                "lossless_proxy": {"enabled": True, "version": "v0.3.0"},
                "neural_render": {
                    "implementation": "lsp_neural_render",
                    "package": {"enabled": True, "version": "broken"},
                    "runtime_asset_sha256": "unused",
                },
            },
        })
        with self.assertRaisesRegex(GraphicsResolutionError, "no add-on name"):
            self.resolver().resolve(profile, lossless_scaling_exe=str(self.executable))

    def test_neural_render_rejects_a_proxy_older_than_its_minimum_host_version(self):
        self.package("lossless-proxy", "v0.1.0", {"Lossless.dll": x64_pe(b"old-proxy")})
        self.reshade()
        self.package("lsp-neural-render", "v0.2.1", {
            "addons/LSP-NeuralRender/LSP_NeuralRender.dll": x64_pe(),
            "addons/LSP-NeuralRender/nvngx.dll_lspnr.dll": x64_pe(),
            "addons/LSP-NeuralRender/addon.json": json.dumps({
                "name": "LSP-NeuralRender", "min_host_version": "0.2.0",
            }),
        })
        runtime_source = self.root / "nvngx_dlssnr.dll"
        runtime_source.write_bytes(x64_pe())
        runtime = self.store.import_file(str(runtime_source), allowed_suffixes=(".dll",))
        profile = Profile.model_validate({
            "id": "old-host", "name": "Old Host",
            "graphics": {
                "lossless_proxy": {"enabled": True, "version": "v0.1.0"},
                "neural_render": {
                    "implementation": "lsp_neural_render",
                    "package": {"enabled": True, "version": "v0.2.1"},
                    "runtime_asset_sha256": runtime["sha256"],
                },
            },
        })
        with self.assertRaisesRegex(GraphicsResolutionError, "requires LosslessProxy 0.2.0 or newer"):
            self.resolver().resolve(profile, lossless_scaling_exe=str(self.executable))

    def test_reshade_and_special_k_conflicting_dxgi_injectors_are_rejected(self):
        for provider, source in (("reshade", "ReShade64.dll"), ("special-k", "SpecialK64.dll")):
            self.package(provider, "v1", {
                source: x64_pe(provider.encode()),
                "lossless-scaling-deployment.json": json.dumps({
                    "schema_version": 1,
                    "files": [{"source": source, "destination": "dxgi.dll"}],
                }),
            })
        profile = Profile.model_validate({
            "id": "conflict", "name": "Conflict",
            "graphics": {
                "reshade": {"enabled": True, "version": "v1"},
                "special_k": {"enabled": True, "version": "v1"},
            },
        })
        plan = self.resolver().resolve(profile, lossless_scaling_exe=str(self.executable))
        with self.assertRaises(DeploymentConflictError):
            DeploymentManager(self.store).apply(
                profile_id=profile.id, lossless_scaling_exe=str(self.executable), files=plan
            )

    def test_live_log_evidence_distinguishes_proxy_reshade_and_neural_readiness(self):
        evidence = DiagnosticRunner._runtime_log_evidence({
            "LosslessProxy.log": (
                "LosslessProxy v0.3.0 starting\n"
                "LS Companion ReShade Bridge initialized (hotkey VK=35)\n"
            ),
            "LSP-NeuralRender.log": (
                "NrEngine ready (float slot 6)\nPrepare: frame 1920x1080\ntaps 3\n"
            ),
        })
        self.assertEqual(evidence, {
            "proxyStarted": True,
            "reshadeBridgeInitialized": True,
            "neuralEngineReady": True,
            "neuralModelPrepared": True,
            "neuralTapObserved": True,
        })


if __name__ == "__main__":
    unittest.main()
