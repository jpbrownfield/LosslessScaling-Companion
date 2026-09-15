"""Resolve profile graphics intent into an LS-root-only concrete file plan."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict, List, Optional

from ..core.models import ManagedPackageConfig, Profile, SpecialKConfig
from .asset_store import AssetStore


class GraphicsResolutionError(RuntimeError):
    pass


class GraphicsResolver:
    def __init__(self, store: AssetStore, bundled_reshade_bridge_dir: Optional[str] = None):
        self.store = store
        self._reshade_bridge_dir = (
            Path(bundled_reshade_bridge_dir).resolve()
            if bundled_reshade_bridge_dir else None
        )

    def _bundled_reshade_bridge_files(self) -> List[Dict]:
        if self._reshade_bridge_dir is not None:
            candidates = [self._reshade_bridge_dir]
        else:
            candidates = []
            frozen_root = getattr(sys, "_MEIPASS", None)
            if frozen_root:
                candidates.append(Path(frozen_root) / "addons" / "LSP-ReShade")
            candidates.append(
                Path(__file__).resolve().parents[2] / "build" / "native" / "LSP-ReShade"
            )

        directory = next(
            (
                item for item in candidates
                if (item / "LSC_ReShadeBridge.dll").is_file()
                and (item / "addon.json").is_file()
            ),
            None,
        )
        if directory is None:
            raise GraphicsResolutionError(
                "ReShade with LosslessProxy requires the bundled LS Companion ReShade bridge; "
                "reinstall LS Companion or build its native bridge"
            )

        binary = self._require_x64(
            (directory / "LSC_ReShadeBridge.dll").resolve(strict=True),
            "The bundled LS Companion ReShade bridge",
        )
        manifest = (directory / "addon.json").resolve(strict=True)
        try:
            metadata = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise GraphicsResolutionError(f"The bundled ReShade bridge manifest is invalid: {error}") from error
        if not isinstance(metadata, dict) or metadata.get("dll") != binary.name:
            raise GraphicsResolutionError("The bundled ReShade bridge manifest does not match its DLL")

        package = "reshade/bundled-ls-companion-bridge/1.0.0"
        return [
            {
                "relative_path": f"addons/LSP-ReShade/{source.name}",
                "source_path": str(source),
                "role": "lossless_addon",
                "source_package": package,
            }
            for source in (binary, manifest)
        ]

    def _package(self, provider: str, config: ManagedPackageConfig) -> Optional[Dict]:
        if not config.enabled:
            return None
        candidates = [item for item in self.store.list_packages() if item.get("provider") == provider]
        if config.version:
            candidates = [item for item in candidates if item.get("version") == config.version]
        if not candidates:
            requested = config.version or "an installed version"
            raise GraphicsResolutionError(f"{provider} requires {requested}; stage it before applying")
        candidates.sort(key=lambda item: str(item.get("version") or ""), reverse=True)
        return candidates[0]

    @staticmethod
    def _require_x64(path: Path, label: str) -> Path:
        architecture = AssetStore.pe_architecture(path)
        if architecture != "x64":
            raise GraphicsResolutionError(
                f"{label} must be a valid x64 Windows binary (found {architecture or 'non-PE'})"
            )
        return path

    @staticmethod
    def _find_payload_file(package: Dict, filename: str, *, contains: Optional[str] = None) -> Path:
        payload = Path(package["payload_path"])
        matches = []
        for item in package.get("files", []):
            relative = str(item.get("relative_path") or "")
            if Path(relative).name.casefold() != filename.casefold():
                continue
            if contains and contains.casefold() not in relative.casefold():
                continue
            matches.append(payload / Path(relative))
        if len(matches) != 1:
            raise GraphicsResolutionError(
                f"Expected exactly one {filename} in {package.get('provider')} package; found {len(matches)}"
            )
        return matches[0]

    def _recipe_files(self, package: Dict) -> List[Dict]:
        payload = Path(package["payload_path"])
        recipes = [
            payload / Path(item["relative_path"])
            for item in package.get("files", [])
            if Path(item.get("relative_path") or "").name == "lossless-scaling-deployment.json"
        ]
        if len(recipes) != 1:
            raise GraphicsResolutionError(
                f"{package.get('provider')} has no single verified Lossless Scaling deployment recipe"
            )
        data = json.loads(recipes[0].read_text(encoding="utf-8"))
        if data.get("schema_version") != 1 or not isinstance(data.get("files"), list):
            raise GraphicsResolutionError("Unsupported Lossless Scaling deployment recipe")
        result = []
        for item in data["files"]:
            source = (payload / Path(str(item["source"]))).resolve(strict=True)
            if payload.resolve() not in source.parents:
                raise GraphicsResolutionError("Deployment recipe source escapes its package")
            result.append(
                {
                    "relative_path": str(item["destination"]),
                    "source_path": str(source),
                    "role": str(item.get("role") or "support_file"),
                    "source_package": f"{package['provider']}/{package['version']}/{package['archive_sha256']}",
                }
            )
        return result

    def _manual_runtime(self, digest: str) -> Path:
        directory = self.store.imports / digest.removeprefix("sha256:")
        metadata_path = directory / "asset.json"
        if not metadata_path.is_file():
            raise GraphicsResolutionError("The selected DLSSNR runtime is not in the managed asset store")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata.get("filename", "").casefold() != "nvngx_dlssnr.dll":
            raise GraphicsResolutionError("The selected neural runtime must be nvngx_dlssnr.dll")
        return self._require_x64(
            Path(metadata["path"]).resolve(strict=True), "The selected neural runtime"
        )

    def _lossless_original(self, lossless_scaling_exe: Optional[str]) -> Path:
        if not lossless_scaling_exe:
            raise GraphicsResolutionError("LosslessProxy requires a configured LosslessScaling.exe path")
        executable = Path(lossless_scaling_exe).resolve()
        if executable.name.casefold() != "losslessscaling.exe":
            raise GraphicsResolutionError("LosslessProxy may only replace LosslessScaling.exe's engine")
        managed_backup = (
            self.store.backups / "lossless-scaling" / "originals" / "Lossless.dll"
        )
        source = managed_backup if managed_backup.is_file() else executable.parent / "Lossless.dll"
        if not source.is_file():
            raise GraphicsResolutionError("The original Lossless.dll could not be found")
        # Never leave the plan pointing at the live DLL: the transaction replaces
        # Lossless.dll before it writes Lossless_original.dll on some path orders.
        imported = self.store.import_file(str(source), allowed_suffixes=(".dll",))
        return Path(imported["path"]).resolve(strict=True)

    def _neural_render_config(self, profile: Profile, lossless_scaling_exe: str) -> Path:
        """Merge LSP-NR keys into LosslessProxy config without dropping other addons."""
        executable = Path(lossless_scaling_exe).resolve()
        if executable.name.casefold() != "losslessscaling.exe":
            raise GraphicsResolutionError("LSP-NeuralRender configuration must target LosslessScaling.exe")
        config_path = executable.parent / "addons" / "config.json"
        data: Dict = {"global": {}, "addons": {}}
        if config_path.is_file():
            try:
                loaded = json.loads(config_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise GraphicsResolutionError(f"LosslessProxy config.json is invalid: {error}") from error
            if not isinstance(loaded, dict):
                raise GraphicsResolutionError("LosslessProxy config.json must contain a JSON object")
            data = loaded
        addons = data.setdefault("addons", {})
        data.setdefault("global", {})
        if not isinstance(addons, dict):
            raise GraphicsResolutionError("LosslessProxy config.json has an invalid addons section")

        neural = profile.graphics.neural_render
        styles = {"standard": 0, "natural": 1, "cinematic": 2}
        existing = addons.get("LSP-NeuralRender", {})
        if not isinstance(existing, dict):
            existing = {}
        # LosslessProxy owns the boolean `enabled` field. LSP-NR's other settings
        # use strings through IHost::GetConfig/SetConfig.
        existing.update(
            {
                "enabled": True,
                "style": str(styles[neural.style]),
                "autoMask": "1" if neural.auto_skin_mask else "0",
                "intensity": f"{neural.intensity:g}",
                "localStructure": f"{neural.local_structure:g}",
                "localTone": f"{neural.local_tone:g}",
                "skinStructure": f"{neural.skin_structure:g}",
                "useFlow": "1" if neural.use_lsfg_optical_flow else "0",
                "workingScale": f"{neural.working_scale:g}",
                "lsFirst": "1" if neural.prioritize_ls_gpu_work else "0",
                "composeIntensity": f"{neural.apply_strength:g}",
                "maxDelta": f"{neural.max_delta:g}",
                "hiProtect": f"{neural.protect_highlights_above:g}",
                "watchdogMs": str(neural.watchdog_ms),
                "snippetPath": "",
            }
        )
        addons["LSP-NeuralRender"] = existing
        return self.store.write_generated_json(profile.id, "config.json", data)

    def _special_k_config(self, profile_id: str, config: SpecialKConfig) -> Path:
        """Generate the local-injection dxgi.ini consumed by Special K in LS."""
        enabled = "true" if config.sdr_to_hdr else "false"
        temporary_hdr = "true" if config.temporary_windows_hdr else "false"
        reflex = "true" if config.experimental_reflex else "false"
        flip_metering = "true" if config.experimental_smooth_motion else "false"
        # Special K persists scRGB luminance as a multiplier of the 80-nit SDR
        # reference white, despite presenting these values as nits in its UI.
        peak = config.hdr_peak_brightness_nits / 80.0
        paper_white = config.hdr_paper_white_nits / 80.0
        text = (
            "; Generated by LS Companion for this application profile.\n"
            "; Smooth Motion itself is an NVIDIA driver-profile setting; AllowFlipMetering\n"
            "; below only prepares the Special K side of that experimental path.\n\n"
            "[SpecialK.HDR]\n"
            f"Use16BitSwapChain={enabled}\n"
            "Use10BitSwapChain=false\n"
            "Preset=0\n"
            f"scRGBLuminance_[0]={peak:g}\n"
            f"scRGBPaperWhite_[0]={paper_white:g}\n\n"
            "[Render.DXGI]\n"
            f"TemporaryDesktopHDRMode={temporary_hdr}\n\n"
            "[NVIDIA.Reflex]\n"
            f"Enable={reflex}\n"
            f"LowLatency={reflex}\n"
            "LowLatencyBoost=false\n"
            "UseFramerateLimiter=false\n\n"
            "[NVIDIA.DLSS]\n"
            f"AllowFlipMetering={flip_metering}\n"
        )
        return self.store.write_generated_text(profile_id, "dxgi.ini", text)

    def resolve(
        self, profile: Optional[Profile], *, lossless_scaling_exe: Optional[str] = None
    ) -> List[Dict]:
        if profile is None:
            return []
        files: List[Dict] = []
        for override in profile.dll_overrides:
            if not override.enabled:
                continue
            if override.deployment_target != "lossless_scaling":
                raise GraphicsResolutionError("Target-application DLL deployment is prohibited")
            files.append(
                {
                    "relative_path": override.target_dll_name,
                    "source_path": override.source_dll_path,
                    "role": "custom_dll",
                    "source_package": "manual-profile-override",
                }
            )

        graphics = profile.graphics
        proxy_package = self._package("lossless-proxy", graphics.lossless_proxy)
        if proxy_package:
            proxy = self._require_x64(
                self._find_payload_file(proxy_package, "Lossless.dll"), "LosslessProxy"
            )
            original = self._lossless_original(lossless_scaling_exe)
            files.append(
                {
                    "relative_path": "Lossless.dll",
                    "source_path": str(proxy),
                    "role": "lossless_proxy",
                    "source_package": f"lossless-proxy/{proxy_package['version']}/{proxy_package['archive_sha256']}",
                }
            )
            files.append(
                {
                    "relative_path": "Lossless_original.dll",
                    "source_path": str(original),
                    "role": "lossless_original_engine",
                    "source_package": "lossless-scaling/original-engine",
                }
            )

        neural = graphics.neural_render
        if neural.implementation == "lsp_neural_render":
            if not proxy_package:
                raise GraphicsResolutionError("LSP-NeuralRender requires LosslessProxy")
            neural_package = self._package("lsp-neural-render", neural.package)
            if neural_package is None:
                raise GraphicsResolutionError("LSP-NeuralRender is selected but its package is disabled")
            for filename in ("LSP_NeuralRender.dll", "nvngx.dll_lspnr.dll", "addon.json"):
                source = self._find_payload_file(neural_package, filename, contains="LSP-NeuralRender")
                if filename.casefold().endswith(".dll"):
                    source = self._require_x64(source, filename)
                files.append(
                    {
                        "relative_path": f"addons/LSP-NeuralRender/{filename}",
                        "source_path": str(source),
                        "role": "lossless_addon",
                        "source_package": (
                            f"lsp-neural-render/{neural_package['version']}/"
                            f"{neural_package['archive_sha256']}"
                        ),
                    }
                )
            if not neural.runtime_asset_sha256:
                raise GraphicsResolutionError("LSP-NeuralRender requires a manually imported DLSSNR runtime")
            runtime = self._manual_runtime(neural.runtime_asset_sha256)
            files.append(
                {
                    "relative_path": "nvngx_dlssnr.dll",
                    "source_path": str(runtime),
                    "role": "neural_runtime",
                    "source_package": f"manual-import/{neural.runtime_asset_sha256}",
                }
            )
            if not lossless_scaling_exe:
                raise GraphicsResolutionError(
                    "LSP-NeuralRender requires a configured LosslessScaling.exe path"
                )
            generated_config = self._neural_render_config(profile, lossless_scaling_exe)
            files.append(
                {
                    "relative_path": "addons/config.json",
                    "source_path": str(generated_config),
                    "role": "lossless_proxy_config",
                    "source_package": "generated/lsp-neural-render-config",
                }
            )
        elif neural.implementation == "ls_reshade_feeder":
            feeder_package = self._package("dlss5-feeder", neural.package)
            if feeder_package is None:
                raise GraphicsResolutionError("The LS ReShade/Feeder recipe package is disabled")
            files.extend(self._recipe_files(feeder_package))

        reshade_package = self._package("reshade", graphics.reshade)
        if reshade_package:
            files.extend(self._recipe_files(reshade_package))
            if proxy_package:
                files.extend(self._bundled_reshade_bridge_files())

        special_k_package = self._package("special-k", graphics.special_k)
        if special_k_package:
            # Companion owns the per-profile local-injection config. Ignore any
            # generic dxgi.ini bundled in the package recipe.
            files.extend(
                item for item in self._recipe_files(special_k_package)
                if item["relative_path"].replace("\\", "/").casefold() != "dxgi.ini"
            )
            generated_special_k = self._special_k_config(profile.id, graphics.special_k)
            files.append(
                {
                    "relative_path": "dxgi.ini",
                    "source_path": str(generated_special_k),
                    "role": "special_k_config",
                    "source_package": "generated/special-k-config",
                }
            )
        if (
            profile.reshade
            and profile.reshade.enabled
            and any(item["relative_path"].replace("\\", "/").casefold() == "reshade.ini" for item in files)
        ):
            raise GraphicsResolutionError(
                "A live ReShade preset cannot also modify a recipe-owned ReShade.ini"
            )
        return files
