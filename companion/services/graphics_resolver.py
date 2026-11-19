"""Resolve profile graphics intent into an LS-root-only concrete file plan."""

from __future__ import annotations

import json
import hashlib
import re
from pathlib import Path
from typing import Dict, List, Optional

from ..core.models import ManagedPackageConfig, Profile, SpecialKConfig
from .asset_store import AssetStore
from .overlay_shortcuts import reshade_key_data
from .deployment_manager import DeploymentManager
from .addon_host import addon_version_key
from . import sm86_sr, amd_nr, display_commander


class GraphicsResolutionError(RuntimeError):
    pass


class GraphicsResolver:
    def __init__(self, store: AssetStore):
        self.store = store

    def _package(self, provider: str, config: ManagedPackageConfig) -> Optional[Dict]:
        if not config.enabled:
            return None
        candidates = [item for item in self.store.list_packages() if item.get("provider") == provider]
        if provider == "ls-addon-manager":
            candidates = [
                item for item in candidates
                if any(
                    Path(str(file.get("relative_path") or "")).name
                    == "lossless-scaling-deployment.json"
                    for file in item.get("files", [])
                )
            ]
        if config.version:
            candidates = [item for item in candidates if item.get("version") == config.version]
        if not candidates:
            requested = config.version or "an installed version"
            raise GraphicsResolutionError(f"{provider} requires {requested}; stage it before applying")
        candidates.sort(key=lambda item: addon_version_key(item.get("version")), reverse=True)
        return candidates[0]

    @staticmethod
    def _require_x64(path: Path, label: str) -> Path:
        architecture = AssetStore.pe_architecture(path)
        if architecture != "x64":
            raise GraphicsResolutionError(
                f"{label} must be a valid x64 Windows binary (found {architecture or 'non-PE'})"
            )
        return path

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
            destination = str(item["destination"])
            # Packages imported by builds before the ReShade deployment fix
            # used the extraction filename. Normalize those already-managed
            # packages so users do not need to download/import ReShade again.
            if (
                package.get("provider") == "reshade"
                and destination.replace("\\", "/").casefold() == "reshade64.dll"
                and source.name.casefold() == "reshade64.dll"
            ):
                destination = "dxgi.dll"
            result.append(
                {
                    "relative_path": destination,
                    "source_path": str(source),
                    "role": str(item.get("role") or "support_file"),
                    "source_package": f"{package['provider']}/{package['version']}/{package['archive_sha256']}",
                }
            )
        return result

    def _manual_runtime(self, digest: str) -> Path:
        if not re.fullmatch(r"[0-9a-fA-F]{64}", digest.removeprefix("sha256:")):
            raise GraphicsResolutionError("The selected neural runtime has an invalid SHA-256 identifier")
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

    def select_neural_runtime(self, profile: Profile, *, required: bool = False) -> Optional[Path]:
        """Bind an unpinned NR profile to the most recently imported valid model.

        Downloading a model can precede enabling the profile. Both save paths and
        deployment must resolve that case; an enabled add-on alone is not a model.
        Explicit pins and backend-specific model paths remain authoritative.
        """
        neural = profile.graphics.neural_render
        addons = profile.addon_config.get("addons", {})
        settings = addons.get("DLSSNR02", {}) if isinstance(addons, dict) else {}
        settings = settings if isinstance(settings, dict) else {}
        if settings.get("_enabled", neural.implementation == "lsp_neural_render") is not True:
            return None
        if neural.runtime_asset_sha256:
            return self._manual_runtime(neural.runtime_asset_sha256)
        candidates = []
        for item in self.store.list_imports():
            digest = str(item.get("sha256") or "")
            if (item.get("filename", "").casefold() != "nvngx_dlssnr.dll"
                    or not re.fullmatch(r"[0-9a-fA-F]{64}", digest)):
                continue
            try:
                runtime = self._manual_runtime(digest)
                imported_at = (self.store.imports / digest / "asset.json").stat().st_mtime_ns
                candidates.append((imported_at, digest, runtime))
            except (OSError, ValueError, GraphicsResolutionError):
                continue
        if candidates:
            _, neural.runtime_asset_sha256, runtime = max(candidates)
            return runtime
        if required and not any(str(settings.get(key) or "").strip()
                                for key in ("modelPath", "amdModelPath")):
            raise GraphicsResolutionError(
                "Neural Rendering is enabled but no model is installed. Download the "
                "Neural Rendering model in Add-ons, import nvngx_dlssnr.dll, or set "
                "the model path for your GPU in this profile."
            )
        return None

    def _lossless_original(self, lossless_scaling_exe: Optional[str]) -> Path:
        if not lossless_scaling_exe:
            raise GraphicsResolutionError("Companion Addon Host requires a configured LosslessScaling.exe path")
        executable = Path(lossless_scaling_exe).resolve()
        if executable.name.casefold() != "losslessscaling.exe":
            raise GraphicsResolutionError("Companion Addon Host may only replace LosslessScaling.exe's engine")
        deployment = DeploymentManager(self.store)
        deployment._read_manifest(lossless_scaling_exe)
        managed_backup = deployment.originals / "Lossless.dll"
        legacy_original = executable.parent / "Lossless_original.dll"
        # LosslessProxy and early Echo installs already moved the genuine engine
        # here. Prefer it over a live Lossless.dll that may itself be a proxy host.
        source = next(
            (
                candidate
                for candidate in (managed_backup, legacy_original, executable.parent / "Lossless.dll")
                if candidate.is_file()
            ),
            executable.parent / "Lossless.dll",
        )
        if not source.is_file():
            raise GraphicsResolutionError("The original Lossless.dll could not be found")
        # Never leave the plan pointing at the live DLL: the transaction replaces
        # Lossless.dll before it writes Lossless_original.dll on some path orders.
        imported = self.store.import_file(str(source), allowed_suffixes=(".dll",))
        return Path(imported["path"]).resolve(strict=True)

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
            "[Input.Keyboard]\nEnableImGuiToggle=false\n\n"
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

    def _special_k_overlay_file(self, profile_id):
        # SPECIALK_PATH is set only in the LS child environment. Never change
        # the user's shared Special K Global/osd.ini used by other games.
        source = self.store.write_generated_text(
            profile_id, "special-k-osd.ini",
            "[SpecialK.OSD]\nShow=false\nRememberMonitoringState=false\n\n"
            "[SpecialK.VersionBanner]\nDuration=0\n\n"
            "[ImGui.Global]\nShowPlaytime=false\n",
        )
        return {"relative_path": ".companion-special-k/Global/osd.ini",
                "source_path": str(source), "role": "special_k_config",
                "source_package": "generated/special-k-overlay"}

    def _blank_neural_reshade_config(self, profile_id: str, overlay_hotkey: Optional[str] = None) -> Path:
        """Generate the safe baseline INI used until a named ReShade config is selected."""
        key_data = reshade_key_data(overlay_hotkey) if overlay_hotkey else (0, 0, 0, 0)
        overlay_key = ",".join(map(str, key_data))
        return self.store.write_generated_text(
            profile_id,
            "ReShade.ini",
            "[GENERAL]\nPerformanceMode=1\n\n"
            f"[INPUT]\nKeyOverlay={overlay_key}\n\n"
            "[ADDON]\nAddonPath=.\\\n\n"
            "[OVERLAY]\nTutorialProgress=4\nShowForceLoadEffectsButton=0\n",
        )

    def _explicit_components(self, profile: Profile, files: List[Dict]) -> List[Dict]:
        """Declare named payloads for the host, independent of Windows proxy slots."""
        components = []
        custom_digests = set()
        # In injection mode Special K caches an application INI in its own
        # Profiles directory and reads Global defaults only for a new profile.
        # A settings-specific root prevents that cache masking a profile edit.
        special_k_recipe = sorted((item["relative_path"], AssetStore.sha256(Path(item["source_path"])))
                                  for item in files if str(item.get("source_package", "")).startswith("special-k/")
                                  or item.get("role") == "special_k_config")
        special_k_root = ".companion/components/special-k/" + hashlib.sha256(json.dumps(special_k_recipe).encode()).hexdigest()[:16]
        for item in files:
            package = str(item.get("source_package") or "")
            path = Path(item["relative_path"])
            if path.suffix.casefold() != ".dll":
                continue
            component = "reshade" if package.startswith("reshade/") else "special-k" if package.startswith("special-k/") else None
            if item.get("role") == "custom_dll":
                if path.name.casefold() == "lossless.dll":
                    raise GraphicsResolutionError("A custom DLL cannot replace the Companion host")
                digest = AssetStore.sha256(Path(item["source_path"]))
                if digest in custom_digests:
                    raise GraphicsResolutionError("The same custom DLL is selected more than once")
                custom_digests.add(digest)
                # Slot names remain stable when a DLL's contents are updated.
                component = f"custom-{len(custom_digests)}"
            if component is None:
                continue
            filename = "ReShade64.dll" if component == "reshade" else "SpecialK64.dll" if component == "special-k" else f"LSC_CustomAddon{len(custom_digests)}.dll"
            destination = f"{special_k_root}/{filename}" if component == "special-k" else f".companion/components/{component}/{filename}"
            item["relative_path"] = destination
            components.append({"id": component, "path": destination,
                               "sha256": AssetStore.sha256(Path(item["source_path"]))})
        if len({item["id"] for item in components}) != len(components):
            raise GraphicsResolutionError("A component recipe declares multiple runtime entry DLLs")
        # Special K's explicit injection mode uses this API-selection marker.
        if any(item["id"] == "special-k" for item in components):
            for item in files:
                if item["relative_path"].casefold() == "dxgi.ini":
                    item["relative_path"] = f"{special_k_root}/Global/default_SpecialK.ini"
                elif item["relative_path"].casefold() == ".companion-special-k/global/osd.ini":
                    item["relative_path"] = f"{special_k_root}/Global/osd.ini"
            marker = self.store.write_generated_text(profile.id, "SpecialK.d3d11", "")
            files.append({"relative_path": "SpecialK.d3d11", "source_path": str(marker),
                          "role": "special_k_config", "source_package": "generated/special-k-config"})
        components.sort(key=lambda item: (item["id"] != "special-k", item["id"]))
        manifest = self.store.write_generated_text(profile.id, "components.json", json.dumps({"schema": 1, "components": components}, indent=2))
        files.append({"relative_path": ".companion/components.json", "source_path": str(manifest),
                      "role": "component_load_manifest", "source_package": "generated/host-components"})
        return files

    def resolve(
        self,
        profile: Optional[Profile],
        *,
        lossless_scaling_exe: Optional[str] = None,
        reshade_overlay_hotkey: str = "end",
        addon_manager_enabled: Optional[bool] = False,
        addon_manager_config: Optional[ManagedPackageConfig] = None,
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
        if addon_manager_enabled is not True and (
            graphics.reshade.enabled or graphics.special_k.enabled
            or any(override.enabled for override in profile.dll_overrides)
        ):
            raise GraphicsResolutionError("Enable the Companion Addon Host to load ReShade, Special K or custom DLLs")
        if addon_manager_enabled is True:
            manager_package = self._package(
                "ls-addon-manager",
                addon_manager_config or ManagedPackageConfig(enabled=True),
            )
            manager_files = self._recipe_files(manager_package)
            if not any(
                item["relative_path"].replace("\\", "/").casefold() == "lossless.dll"
                for item in manager_files
            ):
                raise GraphicsResolutionError(
                    "Companion Addon Host package has no Lossless.dll deployment entry"
                )
            files.extend(manager_files)
            files.extend(sm86_sr.deployment_files(self.store, profile, host_version=manager_package["version"]))
            files.extend(amd_nr.deployment_files(self.store, profile, host_version=manager_package["version"]))
            files.append(
                {
                    "relative_path": "Lossless_original.dll",
                    "source_path": str(self._lossless_original(lossless_scaling_exe)),
                    "role": "lossless_original_engine",
                    "source_package": "lossless-scaling/original-engine",
                }
            )

            # The host includes its maintained DLSS 5 Neural Render add-on and its
            # ReShade passthrough as first-class components. Do not deploy the
            # retired LosslessProxy folders or Companion's old input bridge.
            reshade_selected = bool(profile.reshade and profile.reshade.enabled)
            reshade_package = self._package(
                "reshade",
                graphics.reshade.model_copy(update={"enabled": reshade_selected}),
            )
            if reshade_package:
                files.extend(display_commander.deployment_files(self.store, profile, reshade_package))
                has_selected_reshade_config = bool(
                    profile.reshade
                    and profile.reshade.enabled
                    and (profile.reshade.managed_profile_id or profile.reshade.preset_path)
                )
                recipe_files = self._recipe_files(reshade_package)
                if has_selected_reshade_config:
                    recipe_files = [
                        item for item in recipe_files
                        if item["relative_path"].replace("\\", "/").casefold() != "reshade.ini"
                    ]
                files.extend(recipe_files)
                # External preset swaps need a seed INI too: a clean private
                # runtime may have none, and the swap cannot create one.
                has_managed_reshade_config = bool(profile.reshade and profile.reshade.managed_profile_id)
                if not has_managed_reshade_config:
                    files = [item for item in files if item["relative_path"].casefold() != "reshade.ini"]
                    files.append(dict(relative_path="ReShade.ini",
                        source_path=str(self._blank_neural_reshade_config(profile.id, reshade_overlay_hotkey)),
                        role="neural_reshade_config", source_package="generated/display-commander-reshade-config"))

            special_k_package = self._package("special-k", graphics.special_k)
            if special_k_package:
                files.extend(
                    item for item in self._recipe_files(special_k_package)
                    if item["relative_path"].replace("\\", "/").casefold() != "dxgi.ini"
                )
                generated_special_k = self._special_k_config(profile.id, graphics.special_k)
                files.append(self._special_k_overlay_file(profile.id))
                files.append({
                    "relative_path": "dxgi.ini",
                    "source_path": str(generated_special_k),
                    "role": "special_k_config",
                    "source_package": "generated/special-k-config",
                })
            neural = graphics.neural_render
            runtime = self.select_neural_runtime(profile, required=True)
            if runtime is not None:
                files.append({
                    "relative_path": "nvngx_dlssnr.dll",
                    "source_path": str(runtime),
                    "role": "neural_runtime",
                    "source_package": f"manual-import/{neural.runtime_asset_sha256}",
                })
            return self._explicit_components(profile, files)

        neural = graphics.neural_render
        if neural.implementation == "ls_reshade_feeder":
            feeder_package = self._package("dlss5-feeder", neural.package)
            if feeder_package is None:
                raise GraphicsResolutionError("The LS ReShade/Feeder recipe package is disabled")
            files.extend(self._recipe_files(feeder_package))

        has_selected_reshade_config = bool(
            profile.reshade
            and profile.reshade.enabled
            and (profile.reshade.managed_profile_id or profile.reshade.preset_path)
        )
        reshade_package = self._package("reshade", graphics.reshade)
        if reshade_package:
            files.extend(display_commander.deployment_files(self.store, profile, reshade_package))
            recipe_files = self._recipe_files(reshade_package)
            if has_selected_reshade_config:
                # Installer defaults are only a seed. The selected managed or
                # user preset is applied atomically after deployment, so do not
                # make the two valid sources collide in the deployment plan.
                recipe_files = [
                    item for item in recipe_files
                    if item["relative_path"].replace("\\", "/").casefold()
                    != "reshade.ini"
                ]
            files.extend(recipe_files)
        if graphics.display_commander.enabled and not has_selected_reshade_config:
            blank_reshade = self._blank_neural_reshade_config(
                profile.id, reshade_overlay_hotkey if graphics.display_commander.enabled else None)
            files.append(
                {
                    "relative_path": "ReShade.ini",
                    "source_path": str(blank_reshade),
                    "role": "neural_reshade_config",
                    "source_package": "generated/neural-reshade-config",
                }
            )

        special_k_package = self._package("special-k", graphics.special_k)
        if special_k_package:
            # Companion owns the per-profile local-injection config. Ignore any
            # generic dxgi.ini bundled in the package recipe.
            files.extend(
                item for item in self._recipe_files(special_k_package)
                if item["relative_path"].replace("\\", "/").casefold() != "dxgi.ini"
            )
            generated_special_k = self._special_k_config(profile.id, graphics.special_k)
            files.append(self._special_k_overlay_file(profile.id))
            files.append(
                {
                    "relative_path": "dxgi.ini",
                    "source_path": str(generated_special_k),
                    "role": "special_k_config",
                    "source_package": "generated/special-k-config",
                }
            )
        return files
