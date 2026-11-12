"""Named ReShade profiles and the official ReShade effect-package catalog."""

from __future__ import annotations

import configparser
import hashlib
import io
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import urllib.request
import zipfile
from functools import wraps
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional

from ..core.models import ManagedReshadeProfile
from ..core.profile_manager import ProfileManager
from .hdr_display import HdrDisplayDetector
from .overlay_shortcuts import reshade_key_data


logger = logging.getLogger(__name__)


def _serialized(method):
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self._capture_lock:
            return method(self, *args, **kwargs)
    return locked

LILIUM_INPUT_EFFECT = "LSC_PrepareLiliumSDR.fx"
LILIUM_INPUT_TECHNIQUE = f"LSC_PrepareLiliumSDR@{LILIUM_INPUT_EFFECT}"
# Lilium expects normalized SDR, even when its destination is an HDR swapchain.
# The captured Windows HDR image instead uses absolute scRGB/PQ luminance.
LILIUM_INPUT_SOURCE = r'''#include "lilium__include/include_main.fxh"
uniform float SdrWhiteNits <
    hidden = true;
> = 400.0; // Owned by the general ReShade settings, with monitor overrides.
void LSC_PrepareSDR(float4 position : SV_Position, out float4 output : SV_Target0)
{
    float4 colour = tex2Dfetch(SamplerBackBuffer, int2(position.xy));
#if ACTUAL_COLOUR_SPACE == CSP_SCRGB
    colour.rgb *= 80.0 / SdrWhiteNits;
#elif ACTUAL_COLOUR_SPACE == CSP_HDR10
    colour.rgb = Csp::Mat::BT2020_To::BT709(Csp::Trc::PQ_To::Linear(colour.rgb)) * (10000.0 / SdrWhiteNits);
#else
    colour.rgb = Csp::Trc::sRGB_To::Linear(colour.rgb);
#endif
    output = float4(saturate(colour.rgb), colour.a);
}
technique LSC_PrepareLiliumSDR < ui_label = "LS Companion: prepare captured SDR for Lilium"; >
{
    pass { VertexShader = VS_PostProcessWithoutTexCoord; PixelShader = LSC_PrepareSDR; }
}
'''

CATALOG_URL = "https://raw.githubusercontent.com/crosire/reshade-shaders/list/EffectPackages.ini"
# Enough to keep profile creation useful offline. The live catalog is the same feed
# consumed by ReShade Setup and normally replaces this list.
FALLBACK_CATALOG = [
    {"id": "00", "name": "Standard effects", "description": "ReShade utility effects", "repositoryUrl": "https://github.com/crosire/reshade-shaders/tree/slim", "downloadUrl": "https://github.com/crosire/reshade-shaders/archive/slim.zip", "shaders": ["Daltonize.fx", "Deband.fx", "DisplayDepth.fx", "LUT.fx", "UIMask.fx"]},
    {"id": "01", "name": "SweetFX by CeeJay.dk", "description": "The original SweetFX collection", "repositoryUrl": "https://github.com/CeeJayDK/SweetFX", "downloadUrl": "https://github.com/CeeJayDK/SweetFX/archive/master.zip", "shaders": ["CAS.fx", "Curves.fx", "Levels.fx", "LumaSharpen.fx", "SMAA.fx", "Tonemap.fx", "Vibrance.fx"]},
    {"id": "09", "name": "qUINT by Marty McFly", "description": "MXAO, Lightroom, bloom and sharpening", "repositoryUrl": "https://github.com/martymcmodding/qUINT", "downloadUrl": "https://github.com/martymcmodding/qUINT/archive/master.zip", "shaders": ["qUINT_bloom.fx", "qUINT_deband.fx", "qUINT_dof.fx", "qUINT_lightroom.fx", "qUINT_mxao.fx", "qUINT_sharp.fx", "qUINT_ssr.fx"]},
    {"id": "22", "name": "ReShade HDR shaders by Lilium", "description": "HDR analysis, correction and tone mapping", "repositoryUrl": "https://github.com/EndlesslyFlowering/ReShade_HDR_shaders", "downloadUrl": "https://github.com/EndlesslyFlowering/ReShade_HDR_shaders/archive/refs/heads/master.zip", "shaders": ["lilium__cas_hdr.fx", "lilium__hdr_and_sdr_analysis.fx", "lilium__hdr_black_floor_fix.fx", "lilium__inverse_tone_mapping.fx", "lilium__tone_mapping.fx"]},
    {"id": "29", "name": "AdvancedAutoHDR by Pumbo", "description": "HDR tone-mapping helpers", "repositoryUrl": "https://github.com/Filoppi/PumboAutoHDR", "downloadUrl": "https://github.com/Filoppi/PumboAutoHDR/archive/refs/heads/master.zip", "shaders": ["AdvancedAutoHDR.fx", "ConvertColorSpace.fx"]},
]

# Lightweight starter presets backed by Lilium's package in the official
# ReShade catalog. Keeping the selections ordered also defines execution order:
# the combined preset expands SDR into HDR before applying HDR-aware CAS.
BUILTIN_PROFILES = (
    ManagedReshadeProfile(
        id="lsc-basic-sharpening",
        name="Basic Sharpening (Low Cost)",
        shaders={"22": ["lilium__cas_hdr.fx"]},
    ),
    ManagedReshadeProfile(
        id="lsc-sdr-to-hdr",
        name="SDR to HDR (Low Cost)",
        shaders={"22": ["lilium__inverse_tone_mapping.fx"]},
    ),
    ManagedReshadeProfile(
        id="lsc-sharpening-sdr-to-hdr",
        name="Sharpening + SDR to HDR (Low Cost)",
        shaders={"22": ["lilium__inverse_tone_mapping.fx", "lilium__cas_hdr.fx"]},
    ),
)


class ReshadeProfileService:
    HDR_FALLBACK_NITS = 600
    DEPLOYMENT_ROOT = Path("reshade-shaders") / "LSCompanion"

    def __init__(
        self,
        profile_manager: ProfileManager,
        hdr_display_detector: Optional[HdrDisplayDetector] = None,
    ):
        self.profile_manager = profile_manager
        self.root = Path(profile_manager.config_dir) / "reshade-profiles"
        self.root.mkdir(parents=True, exist_ok=True)
        self._catalog: Optional[List[Dict]] = None
        self._archive_cache: Dict[str, bytes] = {}
        self._builtins_checked = False
        self._hdr_settings_checked = False
        self.hdr_display_detector = hdr_display_detector or HdrDisplayDetector()
        self._capture_lock = threading.RLock()
        self._capture_fingerprints = {}

    @staticmethod
    def _parse_preset(content: str):
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        parser.optionxform = str
        parser.read_string("[__LSC_PRESET_ROOT__]\n" + content)
        return parser

    @staticmethod
    def _preset_text(parser) -> str:
        output = io.StringIO()
        parser.write(output, space_around_delimiters=False)
        return output.getvalue().removeprefix("[__LSC_PRESET_ROOT__]\n")

    def capture_deployed(self, executable: Optional[str], manifest: Dict) -> bool:
        """Merge runtime preset edits back into the shared library, including built-ins.

        Only manifest-owned presets are accepted. The immutable deployment
        snapshot and capture receipt make this a three-way merge: a stale
        runtime cannot overwrite a newer library edit or monitor calibration.
        """
        if not executable or not isinstance(manifest, dict) or not isinstance(manifest.get("files", []), list):
            return False
        from .managed_runtime import assert_managed_executable
        host = Path(executable).resolve()
        assert_managed_executable(host)
        if host.name.casefold() != "losslessscaling.exe" or not host.is_file():
            return False
        with self._capture_lock:
            changed = False
            for entry in manifest.get("files", []):
                if not isinstance(entry, dict) or entry.get("role") != "reshade_preset":
                    continue
                parts = Path(str(entry.get("relative_path") or "")).parts
                if len(parts) != 4 or tuple(parts[:2]) != ("reshade-shaders", "LSCompanion"):
                    continue
                profile = next((p for p in self.profile_manager.config.reshade_profiles if p.id == parts[2]), None)
                if profile is None:
                    continue
                folder = self._folder(profile.id)
                shared = folder / self._preset_filename(profile.name)
                runtime = (host.parent / Path(*parts)).resolve()
                if host.parent not in runtime.parents or runtime.name != shared.name or not runtime.is_file():
                    continue
                if not shared.is_file() or not 0 < runtime.stat().st_size <= 4 * 1024 * 1024:
                    continue
                deployment_hash = entry.get("deployed_sha256") or entry.get("sha256")
                if not isinstance(deployment_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", deployment_hash):
                    continue
                stamp = (deployment_hash, runtime.stat().st_mtime_ns, runtime.stat().st_size)
                identity = str(runtime)
                if self._capture_fingerprints.get(identity) == stamp:
                    continue
                text = runtime.read_text(encoding="utf-8-sig")
                if (runtime.stat().st_mtime_ns, runtime.stat().st_size) != stamp[1:]:
                    continue
                try:
                    edited = self._parse_preset(text)
                    if "Techniques" not in edited["__LSC_PRESET_ROOT__"]:
                        continue  # Do not import a partial runtime write.
                    receipt_key = hashlib.sha256(identity.encode()).hexdigest()[:24]
                    receipt_path = folder / f".runtime-capture-{receipt_key}.json"
                    receipt = json.loads(receipt_path.read_text(encoding="utf-8")) if receipt_path.is_file() else {}
                    if receipt.get("deployment") == deployment_hash:
                        baseline_text = receipt["runtimeText"]
                    else:
                        baseline_path = folder / ".resolved" / f"{deployment_hash}.ini"
                        if not baseline_path.is_file():
                            baseline_path = shared  # Receipt from a pre-snapshot build.
                        baseline_text = baseline_path.read_text(encoding="utf-8-sig")
                        if hashlib.sha256(baseline_path.read_bytes()).hexdigest() != deployment_hash:
                            continue
                    baseline = self._parse_preset(baseline_text)
                    current = self._parse_preset(shared.read_text(encoding="utf-8-sig"))
                except (ValueError, KeyError, configparser.Error, UnicodeError):
                    continue
                merged = False
                for section in set(baseline.sections()) | set(edited.sections()):
                    before = dict(baseline[section]) if baseline.has_section(section) else {}
                    after = dict(edited[section]) if edited.has_section(section) else {}
                    # ReShade may omit inactive shader values. Their absence
                    # must not delete previously saved tuning.
                    for key in after:
                        if (section.casefold(), key.casefold()) in {
                            ("lilium__inverse_tone_mapping.fx", "targetbrightness"),
                            (LILIUM_INPUT_EFFECT.casefold(), "sdrwhitenits"),
                        } or section == "LS Companion":
                            continue
                        if after.get(key) == before.get(key):
                            continue
                        existing = current.get(section, key, fallback=None)
                        if existing not in (before.get(key), after.get(key)):
                            logger.warning("Preserved newer shared ReShade setting %s/%s; runtime copy retained in %s",
                                           section, key, receipt_path)
                            continue
                        if not current.has_section(section):
                            current.add_section(section)
                        if key in after:
                            current[section][key] = after[key]
                        else:
                            current.remove_option(section, key)
                        merged = True
                if merged:
                    if not current.has_section("LS Companion"):
                        current.add_section("LS Companion")
                    current["LS Companion"]["EffectsInitialized"] = "1"
                    self._atomic_write(shared, self._preset_text(current))
                    logger.info("Saved ReShade shader settings to shared preset '%s'", profile.name)
                    changed = True
                self._atomic_write(receipt_path, json.dumps({"deployment": deployment_hash, "runtimeText": text}))
                self._capture_fingerprints[identity] = stamp
            return changed

    def _folder(self, profile_id: str) -> Path:
        # Resolve a separate copy for containment validation, but preserve the
        # caller's lexical path representation when returning it. Windows CI
        # can expose the same temp directory through both its long user name
        # (runneradmin) and DOS 8.3 alias (RUNNER~1); Path.resolve() silently
        # switches between those spellings and makes otherwise identical
        # paths compare unequal.
        folder = self.root / profile_id
        resolved_folder = folder.resolve()
        if resolved_folder.parent != self.root.resolve():
            raise ValueError("Unsafe ReShade profile id")
        return folder

    @staticmethod
    def _parse_catalog(text: str) -> List[Dict]:
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        parser.read_string(text)
        result = []
        for section in parser.sections():
            values = parser[section]
            shaders = [item.strip() for item in values.get("EffectFiles", "").split(",") if item.strip()]
            denied = {item.strip().casefold() for item in values.get("DenyEffectFiles", "").split(",") if item.strip()}
            shaders = [item for item in shaders if item.casefold() not in denied]
            if not shaders:
                continue
            result.append({
                "id": section,
                "name": values.get("PackageName", section),
                "description": values.get("PackageDescription", ""),
                "repositoryUrl": values.get("RepositoryUrl", ""),
                "downloadUrl": values.get("DownloadUrl", ""),
                "installPath": values.get("InstallPath", ""),
                "textureInstallPath": values.get("TextureInstallPath", ""),
                "required": values.get("Required", "0") == "1",
                "shaders": shaders,
            })
        return result

    def catalog(self, *, force: bool = False) -> List[Dict]:
        if self._catalog is not None and not force:
            return self._catalog
        try:
            request = urllib.request.Request(CATALOG_URL, headers={"User-Agent": "LS-Companion/1"})
            with urllib.request.urlopen(request, timeout=10) as response:
                catalog = self._parse_catalog(response.read().decode("utf-8-sig"))
            if catalog:
                self._catalog = catalog
                return catalog
        except (OSError, ValueError, configparser.Error):
            pass
        self._catalog = [dict(item) for item in FALLBACK_CATALOG]
        return self._catalog

    @_serialized
    def payload(self) -> Dict:
        self._ensure_builtin_profiles()
        if not self._hdr_settings_checked:
            self.refresh_hdr_peak_settings()
            self._hdr_settings_checked = True
        return {
            "profiles": [self._profile_payload(item) for item in self.profile_manager.config.reshade_profiles],
            "catalog": [dict(source) for source in self.catalog()],
            "catalogUrl": CATALOG_URL,
            "hdrSettings": self.hdr_settings_payload(),
        }

    def hdr_settings_payload(self) -> Dict:
        detected = self.hdr_display_detector.detect()
        configured = self.profile_manager.config.reshade_hdr_peak_nits
        resolved = configured or detected.get("detectedNits") or self.HDR_FALLBACK_NITS
        return {
            **detected,
            "configuredNits": configured,
            "resolvedNits": int(resolved),
            "usingFallback": configured is None and detected.get("detectedNits") is None,
            "sdrWhiteNits": self.profile_manager.config.reshade_sdr_white.default_nits,
            "monitorSdrWhiteNits": dict(self.profile_manager.config.reshade_sdr_white.monitors),
            "monitors": [
                {"id": str(item["deviceId"]).strip().casefold(),
                 "name": item.get("name") or item.get("deviceName") or item["deviceId"],
                 "deviceName": item.get("deviceName", "")}
                for item in self.profile_manager.config.gpu_inventory_snapshot.get("displays", [])
                if item.get("deviceId")
            ],
        }

    def _resolved_sdr_white_nits(self, monitor_id: Optional[str] = None) -> int:
        settings = self.profile_manager.config.reshade_sdr_white
        return settings.monitors.get(str(monitor_id or "").strip().casefold(), settings.default_nits)

    @staticmethod
    def _sdr_white_content(content: str, nits: int) -> str:
        if LILIUM_INPUT_TECHNIQUE.casefold() not in content.casefold():
            return content
        section = re.compile(r"(?ims)(^\[LSC_PrepareLiliumSDR\.fx\][^\r\n]*\r?\n)(.*?)(?=^\[|\Z)")
        match = section.search(content)
        value = f"SdrWhiteNits={nits:.6f}"
        if not match:
            return content.rstrip() + f"\n\n[{LILIUM_INPUT_EFFECT}]\n{value}\n"
        key = re.compile(r"(?im)^SdrWhiteNits\s*=.*$")
        body = match.group(2)
        body = key.sub(value, body, count=1) if key.search(body) else f"{value}\n{body}"
        return content[:match.start()] + match.group(1) + body + content[match.end():]

    def _sync_sdr_white(self, preset: Path) -> None:
        original = preset.read_text(encoding="utf-8-sig", errors="ignore")
        updated = self._sdr_white_content(original, self._resolved_sdr_white_nits())
        if updated != original:
            self._atomic_write(preset, updated)

    def _resolved_hdr_peak_nits(self) -> int:
        return int(self.hdr_settings_payload()["resolvedNits"])

    def _ensure_builtin_profiles(self, *, retry: bool = False) -> None:
        """Seed and provision the curated presets without replacing user edits."""
        if self._builtins_checked and not retry:
            return
        self._builtins_checked = True
        profiles = self.profile_manager.config.reshade_profiles
        known = {profile.id for profile in profiles}
        added = False
        for template in BUILTIN_PROFILES:
            if template.id not in known:
                profiles.append(template.model_copy(deep=True))
                known.add(template.id)
                added = True
        if added:
            self.profile_manager.save_config()

        catalog = self.catalog()
        builtin_ids = {profile.id for profile in BUILTIN_PROFILES}
        for profile in profiles:
            if profile.id not in builtin_ids or profile.imported_preset:
                continue
            folder = self._folder(profile.id)
            preset = folder / self._preset_filename(profile.name)
            if (
                preset.is_file()
                and (folder / "ReShade.ini").is_file()
                and (folder / "ReShade.Interactive.ini").is_file()
                and not self._missing_selected_shaders(profile)
            ):
                if "EffectsInitialized=1" not in preset.read_text(encoding="utf-8-sig"):
                    self._write_files(profile)
                continue
            try:
                self._install_selected_shaders(profile, catalog)
                self._write_files(profile)
            except (OSError, ValueError, zipfile.BadZipFile) as exc:
                logger.warning("Could not provision built-in ReShade profile %s: %s", profile.id, exc)

    def _missing_selected_shaders(self, profile: ManagedReshadeProfile) -> set[str]:
        selected = {
            filename.casefold()
            for filenames in profile.shaders.values()
            for filename in filenames
        }
        if not selected:
            return set()
        packages = self._folder(profile.id) / "packages"
        installed = (
            {path.name.casefold() for path in packages.rglob("*.fx")}
            if packages.is_dir()
            else set()
        )
        return selected - installed

    def _profile_payload(self, profile: ManagedReshadeProfile) -> Dict:
        folder = self._folder(profile.id)
        return {
            **profile.model_dump(),
            "builtin": profile.id in {item.id for item in BUILTIN_PROFILES},
            "presetPath": str(folder / self._preset_filename(profile.name)),
            "configPath": str(folder / "ReShade.ini"),
        }

    @staticmethod
    def _preset_filename(name: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9._ -]+", " ", name)
        safe = re.sub(r"\s+", " ", safe).strip(" .") or "ReShade Profile"
        return f"{safe}.ini"

    @_serialized
    def save(self, profile: ManagedReshadeProfile) -> Dict:
        catalog = self.catalog()
        catalog_ids = {item["id"] for item in catalog}
        unknown = set(profile.shaders) - catalog_ids
        if unknown:
            raise ValueError(f"Unknown ReShade shader provider: {sorted(unknown)[0]}")
        existing = next((item for item in self.profile_manager.config.reshade_profiles if item.id == profile.id), None)
        if not profile.imported_preset:
            self._install_selected_shaders(profile, catalog)
            self._write_files(profile)
        if existing:
            old_path = self._folder(profile.id) / self._preset_filename(existing.name)
            new_path = self._folder(profile.id) / self._preset_filename(profile.name)
            if old_path != new_path and old_path.is_file():
                if profile.imported_preset:
                    new_path.parent.mkdir(parents=True, exist_ok=True)
                    old_path.replace(new_path)
                else:
                    old_path.unlink()
        if profile.imported_preset:
            preset = self._folder(profile.id) / self._preset_filename(profile.name)
            if not preset.is_file():
                raise ValueError("The imported ReShade preset is missing")
            self._write_config(profile, preset)
        index = next((i for i, item in enumerate(self.profile_manager.config.reshade_profiles) if item.id == profile.id), None)
        if index is None:
            self.profile_manager.config.reshade_profiles.append(profile)
        else:
            self.profile_manager.config.reshade_profiles[index] = profile
        self.profile_manager.save_config()
        return self._profile_payload(profile)

    @_serialized
    def import_preset(self, source_path: str) -> Dict:
        source = Path(source_path).resolve()
        if not source.is_file() or source.suffix.casefold() != ".ini":
            raise ValueError("Select an existing ReShade .ini preset")
        if source.stat().st_size > 4 * 1024 * 1024:
            raise ValueError("ReShade preset exceeds the 4 MB import limit")
        profile = ManagedReshadeProfile(
            name=source.stem,
            imported_preset=True,
        )
        folder = self._folder(profile.id)
        folder.mkdir(parents=True, exist_ok=True)
        preset = folder / self._preset_filename(profile.name)
        handle, temp_name = tempfile.mkstemp(prefix=f".{preset.name}.", suffix=".tmp", dir=folder)
        os.close(handle)
        try:
            shutil.copyfile(source, temp_name)
            os.replace(temp_name, preset)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        self._write_config(profile, preset)
        self.profile_manager.config.reshade_profiles.append(profile)
        self.profile_manager.save_config()
        return self._profile_payload(profile)

    def _install_selected_shaders(self, profile: ManagedReshadeProfile, catalog: List[Dict]) -> None:
        """Install selected effects and package support files into a managed folder."""
        folder = self._folder(profile.id)
        folder.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".packages-", dir=folder))
        by_id = {item["id"]: item for item in catalog}
        try:
            for provider_id, selected in profile.shaders.items():
                package = by_id[provider_id]
                allowed = set(package.get("shaders") or [])
                if not set(selected).issubset(allowed):
                    raise ValueError(f"A selected shader is not published by {package['name']}")
                url = str(package.get("downloadUrl") or "")
                if not url.startswith("https://github.com/") or not url.casefold().endswith(".zip"):
                    raise ValueError(f"{package['name']} does not publish a supported GitHub ZIP")
                archive_data = self._archive_cache.get(url)
                if archive_data is None:
                    request = urllib.request.Request(url, headers={"User-Agent": "LS-Companion/1"})
                    with urllib.request.urlopen(request, timeout=60) as response:
                        archive_data = response.read(128 * 1024 * 1024 + 1)
                    self._archive_cache[url] = archive_data
                if len(archive_data) > 128 * 1024 * 1024:
                    raise ValueError(f"{package['name']} archive exceeds the 128 MB safety limit")
                archive_path = staging / f"{provider_id}.zip"
                archive_path.write_bytes(archive_data)
                provider_root = staging / provider_id
                provider_root.mkdir()
                found = set()
                with zipfile.ZipFile(archive_path) as archive:
                    total_size = 0
                    for member in archive.infolist():
                        if member.is_dir():
                            continue
                        total_size += member.file_size
                        if total_size > 512 * 1024 * 1024 or member.file_size > 64 * 1024 * 1024:
                            raise ValueError(f"{package['name']} archive exceeds extraction safety limits")
                        source_path = PurePosixPath(member.filename)
                        parts = source_path.parts[1:] if len(source_path.parts) > 1 else source_path.parts
                        if not parts or any(part in {"", ".", ".."} for part in parts):
                            continue
                        filename = parts[-1]
                        if filename.casefold().endswith(".fx"):
                            match = next((item for item in selected if item.casefold() == filename.casefold()), None)
                            if match is None:
                                continue
                            found.add(match)
                        target = provider_root.joinpath(*parts).resolve()
                        if provider_root.resolve() not in target.parents:
                            raise ValueError("Unsafe path in ReShade shader archive")
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with archive.open(member) as source, open(target, "wb") as output:
                            shutil.copyfileobj(source, output)
                archive_path.unlink(missing_ok=True)
                missing = set(selected) - found
                if missing:
                    raise ValueError(f"{package['name']} archive did not contain {sorted(missing)[0]}")
            destination = folder / "packages"
            old = folder / ".packages-old"
            if old.exists():
                shutil.rmtree(old)
            if destination.exists():
                os.replace(destination, old)
            os.replace(staging, destination)
            if old.exists():
                shutil.rmtree(old)
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            raise

    @_serialized
    def delete(self, profile_id: str) -> bool:
        if any(p.reshade and p.reshade.enabled and p.reshade.managed_profile_id == profile_id for p in self.profile_manager.config.profiles):
            raise ValueError("This ReShade profile is still selected by an application profile")
        before = len(self.profile_manager.config.reshade_profiles)
        self.profile_manager.config.reshade_profiles = [item for item in self.profile_manager.config.reshade_profiles if item.id != profile_id]
        if len(self.profile_manager.config.reshade_profiles) == before:
            return False
        folder = self._folder(profile_id)
        if folder.is_dir():
            shutil.rmtree(folder)
        self.profile_manager.save_config()
        return True

    @_serialized
    def _write_files(self, profile: ManagedReshadeProfile) -> None:
        folder = self._folder(profile.id)
        folder.mkdir(parents=True, exist_ok=True)
        preset = folder / self._preset_filename(profile.name)
        previous_profile = next((p for p in self.profile_manager.config.reshade_profiles if p.id == profile.id), None)
        previous_preset = folder / self._preset_filename(previous_profile.name) if previous_profile else preset
        previous_content = previous_preset.read_text(encoding="utf-8-sig") if previous_preset.is_file() else None
        selected_names = [
            filename for files in profile.shaders.values() for filename in files
        ]
        selected_keys = {name.casefold() for name in selected_names}
        effects = {}
        for effect in (folder / "packages").rglob("*.fx"):
            if effect.name.casefold() not in selected_keys:
                continue
            source = effect.read_text(encoding="utf-8", errors="ignore")
            effects[effect.name.casefold()] = (effect.name, re.findall(
                r"(?im)^\s*technique(?:10|11)?\s+([A-Za-z_][A-Za-z0-9_]*)", source
            ))
        techniques = []
        for selected_name in selected_names:
            effect_name, effect_techniques = effects.get(selected_name.casefold(), (selected_name, []))
            for name in effect_techniques:
                entry = f"{name}@{effect_name}"
                if entry not in techniques:
                    techniques.append(entry)
        technique_list = ",".join(techniques)
        sections = []
        if "lilium__inverse_tone_mapping.fx" in selected_keys:
            self._write_lilium_input(folder)
            technique_list = ",".join([LILIUM_INPUT_TECHNIQUE, *techniques])
            sections.append(
                "[lilium__inverse_tone_mapping.fx]\n"
                "ItmMethod=0\nInputTrc=2\nOverbrightHandling=2\n"
                "Bt2446AProcessingMode=0\nBt2446AInputBrightness=100.000000\n"
                "Bt2446AMaxInputBrightness=100.000000\n"
                f"TargetBrightness={self._resolved_hdr_peak_nits():.6f}\n"
            )
            sections.append(f"[{LILIUM_INPUT_EFFECT}]\nSdrWhiteNits={self._resolved_sdr_white_nits():.6f}\n")
        settings = "\n".join(sections)
        settings_suffix = f"\n{settings}" if settings else ""
        generated = self._parse_preset(
            f"Techniques={technique_list}\nTechniqueSorting={technique_list}\n\n"
            f"[LS Companion]\nManaged=1\nLiliumInputVersion=1\nEffectsInitialized=1\n{settings_suffix}")
        if previous_content is not None:
            saved = self._parse_preset(previous_content)
            old_files = {name.casefold() for files in (previous_profile.shaders.values() if previous_profile else []) for name in files}
            available = technique_list.split(",") if technique_list else []
            root = saved["__LSC_PRESET_ROOT__"]
            enabled = [name for name in root.get("Techniques", "").split(",") if name in available]
            if saved.get("LS Companion", "EffectsInitialized", fallback="") != "1":
                enabled = available.copy()  # Initialize legacy starter presets once.
            for name in available:
                effect = name.partition("@")[2].casefold()
                if effect not in old_files and effect != LILIUM_INPUT_EFFECT.casefold() and name not in enabled:
                    enabled.append(name)
            if LILIUM_INPUT_TECHNIQUE in available and "lilium__inverse_tone_mapping.fx" not in old_files:
                enabled.insert(0, LILIUM_INPUT_TECHNIQUE)
            root["Techniques"] = ",".join(dict.fromkeys(enabled))
            root["TechniqueSorting"] = ",".join(dict.fromkeys(
                [name for name in root.get("TechniqueSorting", "").split(",") if name in available] + available))
            for section in generated.sections():
                if section == "__LSC_PRESET_ROOT__":
                    continue
                if not saved.has_section(section):
                    saved.add_section(section)
                for key, value in generated[section].items():
                    if key not in saved[section] or section == "LS Companion":
                        saved[section][key] = value
            generated = saved
        self._atomic_write(preset, self._preset_text(generated))
        self._update_imported_hdr_peak(preset)
        self._sync_sdr_white(preset)
        self._write_config(profile, preset)

    def _write_lilium_input(self, folder: Path) -> None:
        destination = folder / "packages" / "LSCompanion" / LILIUM_INPUT_EFFECT
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.is_file() or destination.read_text(encoding="utf-8") != LILIUM_INPUT_SOURCE:
            self._atomic_write(destination, LILIUM_INPUT_SOURCE)

    def _migrate_lilium_input(self, profile: ManagedReshadeProfile, preset: Path) -> None:
        if profile.imported_preset:
            return
        content = preset.read_text(encoding="utf-8-sig", errors="ignore")
        if "[lilium__inverse_tone_mapping.fx]" not in content.casefold():
            return
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        parser.optionxform = str
        # ReShade stores technique lists before the first INI section.
        parser.read_string("[PRESET]\n" + content)
        if parser.get("LS Companion", "LiliumInputVersion", fallback="") == "1":
            self._write_lilium_input(preset.parent)
            return
        section = parser["lilium__inverse_tone_mapping.fx"]
        if "InputTrc" in section:
            return  # Explicitly tuned presets retain their original input path.
        section.update(InputTrc="2", OverbrightHandling="2", ItmMethod="0",
                       Bt2446AProcessingMode="0", Bt2446AInputBrightness="100.000000",
                       Bt2446AMaxInputBrightness="100.000000")
        for key in ("Techniques", "TechniqueSorting"):
            current = parser["PRESET"].get(key, "")
            parser["PRESET"][key] = ",".join([LILIUM_INPUT_TECHNIQUE, *filter(None, current.split(","))])
        if not parser.has_section("LS Companion"):
            parser.add_section("LS Companion")
        parser["LS Companion"]["LiliumInputVersion"] = "1"
        output = io.StringIO()
        parser.write(output, space_around_delimiters=False)
        self._write_lilium_input(preset.parent)
        self._atomic_write(preset, output.getvalue().removeprefix("[PRESET]\n"))

    @_serialized
    def refresh_hdr_peak_settings(self) -> None:
        """Migrate untuned SDR input and update peak without erasing shader edits."""
        for profile in self.profile_manager.config.reshade_profiles:
            folder = self._folder(profile.id)
            preset = folder / self._preset_filename(profile.name)
            if not preset.is_file():
                continue
            self._migrate_lilium_input(profile, preset)
            self._update_imported_hdr_peak(preset)
            self._sync_sdr_white(preset)

    def _sync_profile_hdr_peak(self, profile: ManagedReshadeProfile) -> None:
        folder = self._folder(profile.id)
        preset = folder / self._preset_filename(profile.name)
        if not preset.is_file():
            return
        self._migrate_lilium_input(profile, preset)
        self._sync_sdr_white(preset)
        content = preset.read_text(encoding="utf-8-sig", errors="ignore")
        if "[lilium__inverse_tone_mapping.fx]" not in content.casefold():
            return
        expected = f"TargetBrightness={self._resolved_hdr_peak_nits():.6f}"
        if expected in content:
            return
        self._update_imported_hdr_peak(preset)

    def _update_imported_hdr_peak(self, preset: Path) -> None:
        content = preset.read_text(encoding="utf-8-sig", errors="ignore")
        section = re.compile(
            r"(?ims)(^\[lilium__inverse_tone_mapping\.fx\][^\r\n]*\r?\n)(.*?)(?=^\[|\Z)"
        )
        match = section.search(content)
        if not match:
            return
        value = f"TargetBrightness={self._resolved_hdr_peak_nits():.6f}"
        body = match.group(2)
        key = re.compile(r"(?im)^TargetBrightness\s*=.*$")
        body = key.sub(value, body, count=1) if key.search(body) else f"{value}\n{body}"
        updated = content[:match.start()] + match.group(1) + body + content[match.end():]
        if updated != content:
            self._atomic_write(preset, updated)

    def _write_config(self, profile: ManagedReshadeProfile, preset: Path) -> None:
        folder = self._folder(profile.id)
        # ReShade runs inside Lossless Scaling, so keep every path in the
        # generated config relative to that installation. The corresponding
        # preset/package files are added to the transactional deployment plan
        # by ``deployment_files`` below. This avoids relying on ReShade being
        # able to traverse back into the companion's LocalAppData directory.
        deployed_folder = self.DEPLOYMENT_ROOT / profile.id
        deployed_preset = deployed_folder / preset.name
        deployed_packages = deployed_folder / "packages"

        def reshade_path(path: Path, *, recursive: bool = False) -> str:
            value = ".\\" + str(path).replace("/", "\\")
            return value + ("\\**" if recursive else "")

        key_data = reshade_key_data(
            self.profile_manager.config.reshade_overlay_hotkey
        )

        def content(overlay_key: str) -> str:
            return (
                "[GENERAL]\n"
                f"PresetPath={reshade_path(deployed_preset)}\n"
                f"CurrentPresetPath={reshade_path(deployed_preset)}\n"
                f"StartupPresetPath={reshade_path(deployed_preset)}\n"
                f"EffectSearchPaths={reshade_path(deployed_packages, recursive=True)}\n"
                f"TextureSearchPaths={reshade_path(deployed_packages, recursive=True)}\n"
                "PerformanceMode=0\nNoReloadOnInit=0\nSkipLoadingDisabledEffects=0\n\n"
                "[ADDON]\nAddonPath=.\\\n\n"
                "[INPUT]\n"
                f"KeyOverlay={overlay_key}\n\n"
                "[OVERLAY]\n"
                "AutoSavePreset=1\n"
                "TutorialProgress=4\n"
                "ShowForceLoadEffectsButton=0\n"
                "ShowPresetName=0\n"
                "ShowScreenshotMessage=0\n"
            )

        self._atomic_write(folder / "ReShade.ini", content("0,0,0,0"))
        self._atomic_write(
            folder / "ReShade.Interactive.ini",
            content(",".join(map(str, key_data))),
        )

    @_serialized
    def deployment_files(self, profile_id: str, *, monitor_id: Optional[str] = None) -> List[Dict]:
        """Return the preset, shaders, includes and textures for LS deployment."""
        profile = next(
            (item for item in self.profile_manager.config.reshade_profiles if item.id == profile_id),
            None,
        )
        if profile is None:
            raise ValueError(f"Managed ReShade profile {profile_id!r} is missing")

        # This also retries acquisition for a built-in whose first download
        # attempt happened while offline and refreshes both generated configs.
        if self.config_path(profile_id) is None:
            raise ValueError(f"Managed ReShade profile {profile_id!r} is not provisioned")
        missing = self._missing_selected_shaders(profile)
        if missing:
            raise ValueError(
                f"Managed ReShade profile {profile_id!r} is missing shader {sorted(missing)[0]}"
            )

        folder = self._folder(profile.id)
        preset = folder / self._preset_filename(profile.name)
        content = preset.read_text(encoding="utf-8-sig", errors="ignore")
        resolved = self._sdr_white_content(content, self._resolved_sdr_white_nits(monitor_id))
        # Keep an immutable baseline for merging edits made by ReShade back
        # into the shared preset, including when no monitor override is used.
        digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()
        resolved_preset = folder / ".resolved" / f"{digest}.ini"
        resolved_preset.parent.mkdir(parents=True, exist_ok=True)
        if not resolved_preset.is_file():
            self._atomic_write(resolved_preset, resolved)
        sources = [preset]
        packages = folder / "packages"
        if packages.is_dir():
            sources.extend(path for path in packages.rglob("*") if path.is_file())

        deployed_folder = self.DEPLOYMENT_ROOT / profile.id
        result = []
        for source in sources:
            relative = source.relative_to(folder)
            result.append({
                "relative_path": (deployed_folder / relative).as_posix(),
                "source_path": str(resolved_preset if source == preset else source),
                "role": "reshade_preset" if source == preset else "shader_bundle",
                "source_package": f"managed-reshade-profile/{profile.id}",
            })
        return result

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    @_serialized
    def sync_all_configs(self) -> None:
        """Keep each hidden interactive/non-interactive INI pair synchronized."""
        for profile in self.profile_manager.config.reshade_profiles:
            preset = self._folder(profile.id) / self._preset_filename(profile.name)
            if preset.is_file():
                self._write_config(profile, preset)

    @_serialized
    def config_path(self, profile_id: str, *, interactive: bool = False) -> Optional[Path]:
        if profile_id in {profile.id for profile in BUILTIN_PROFILES}:
            self._ensure_builtin_profiles(retry=True)
        profile = next(
            (item for item in self.profile_manager.config.reshade_profiles if item.id == profile_id),
            None,
        )
        if profile:
            self._sync_profile_hdr_peak(profile)
            preset = self._folder(profile.id) / self._preset_filename(profile.name)
            if not preset.is_file():
                return None
            self._write_config(profile, preset)
            filename = "ReShade.Interactive.ini" if interactive else "ReShade.ini"
            path = self._folder(profile_id) / filename
            return path if path.is_file() else None
        return None
