"""Named ReShade profiles and the official ReShade effect-package catalog."""

from __future__ import annotations

import configparser
import logging
import os
import re
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional

from ..core.models import ManagedReshadeProfile
from ..core.profile_manager import ProfileManager
from .hdr_display import HdrDisplayDetector
from .input_simulator import InputSimulator


logger = logging.getLogger(__name__)

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
        }

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
            ):
                continue
            try:
                self._install_selected_shaders(profile, catalog)
                self._write_files(profile)
            except (OSError, ValueError, zipfile.BadZipFile) as exc:
                logger.warning("Could not provision built-in ReShade profile %s: %s", profile.id, exc)

    def _profile_payload(self, profile: ManagedReshadeProfile) -> Dict:
        folder = self._folder(profile.id)
        return {
            **profile.model_dump(),
            "presetPath": str(folder / self._preset_filename(profile.name)),
            "configPath": str(folder / "ReShade.ini"),
        }

    @staticmethod
    def _preset_filename(name: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9._ -]+", " ", name)
        safe = re.sub(r"\s+", " ", safe).strip(" .") or "ReShade Profile"
        return f"{safe}.ini"

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

    def _write_files(self, profile: ManagedReshadeProfile) -> None:
        folder = self._folder(profile.id)
        folder.mkdir(parents=True, exist_ok=True)
        preset = folder / self._preset_filename(profile.name)
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
            sections.append(
                "[lilium__inverse_tone_mapping.fx]\n"
                f"TargetBrightness={self._resolved_hdr_peak_nits():.6f}\n"
            )
        settings = "\n".join(sections)
        settings_suffix = f"\n{settings}" if settings else ""
        self._atomic_write(
            preset,
            f"Techniques={technique_list}\nTechniqueSorting={technique_list}\n\n"
            f"[LS Companion]\nManaged=1\n{settings_suffix}",
        )
        self._write_config(profile, preset)

    def refresh_hdr_peak_settings(self) -> None:
        """Rewrite generated presets and update known HDR keys in imported INIs."""
        for profile in self.profile_manager.config.reshade_profiles:
            folder = self._folder(profile.id)
            preset = folder / self._preset_filename(profile.name)
            if not preset.is_file():
                continue
            if profile.imported_preset:
                self._update_imported_hdr_peak(preset)
            elif (folder / "packages").is_dir():
                self._write_files(profile)

    def _sync_profile_hdr_peak(self, profile: ManagedReshadeProfile) -> None:
        folder = self._folder(profile.id)
        preset = folder / self._preset_filename(profile.name)
        if not preset.is_file():
            return
        content = preset.read_text(encoding="utf-8-sig", errors="ignore")
        if "[lilium__inverse_tone_mapping.fx]" not in content.casefold():
            return
        expected = f"TargetBrightness={self._resolved_hdr_peak_nits():.6f}"
        if expected in content:
            return
        if profile.imported_preset:
            self._update_imported_hdr_peak(preset)
        elif (folder / "packages").is_dir():
            self._write_files(profile)

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
        virtual_key = InputSimulator.vk_from_string(
            self.profile_manager.config.reshade_overlay_hotkey
        )

        def content(overlay_key: str) -> str:
            return (
                "[GENERAL]\n"
                f"CurrentPresetPath={preset.resolve()}\n"
                f"EffectSearchPaths={(folder / 'packages').resolve()}\\**\n"
                f"TextureSearchPaths={(folder / 'packages').resolve()}\\**\n"
                "PerformanceMode=1\n\n"
                "[INPUT]\n"
                f"KeyOverlay={overlay_key}\n\n"
                "[OVERLAY]\n"
                "TutorialProgress=4\n"
                "ShowForceLoadEffectsButton=0\n"
                "ShowPresetName=0\n"
                "ShowScreenshotMessage=0\n"
            )

        self._atomic_write(folder / "ReShade.ini", content("0,0,0,0"))
        self._atomic_write(
            folder / "ReShade.Interactive.ini",
            content(f"{virtual_key},0,0,0"),
        )

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

    def sync_all_configs(self) -> None:
        """Keep each hidden interactive/non-interactive INI pair synchronized."""
        for profile in self.profile_manager.config.reshade_profiles:
            preset = self._folder(profile.id) / self._preset_filename(profile.name)
            if preset.is_file():
                self._write_config(profile, preset)

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
