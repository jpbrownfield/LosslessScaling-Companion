"""Named ReShade profiles and the official ReShade effect-package catalog."""

from __future__ import annotations

import configparser
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


CATALOG_URL = "https://raw.githubusercontent.com/crosire/reshade-shaders/list/EffectPackages.ini"
RHI_HDR_REPOSITORIES = (
    "crosire/reshade-shaders",
    "filoppi/pumboautohdr",
    "smolbbsoop/smolbbsoopshaders",
    "maxg2d/reshadesimplehdrshaders",
    "gimlelarpes/potatofx",
    "endlesslyflowering/reshade_hdr_shaders",
)

# Enough to keep profile creation useful offline. The live catalog is the same feed
# consumed by ReShade Setup and normally replaces this list.
FALLBACK_CATALOG = [
    {"id": "00", "name": "Standard effects", "description": "ReShade utility effects", "repositoryUrl": "https://github.com/crosire/reshade-shaders/tree/slim", "downloadUrl": "https://github.com/crosire/reshade-shaders/archive/slim.zip", "shaders": ["Daltonize.fx", "Deband.fx", "DisplayDepth.fx", "LUT.fx", "UIMask.fx"]},
    {"id": "01", "name": "SweetFX by CeeJay.dk", "description": "The original SweetFX collection", "repositoryUrl": "https://github.com/CeeJayDK/SweetFX", "downloadUrl": "https://github.com/CeeJayDK/SweetFX/archive/master.zip", "shaders": ["CAS.fx", "Curves.fx", "Levels.fx", "LumaSharpen.fx", "SMAA.fx", "Tonemap.fx", "Vibrance.fx"]},
    {"id": "09", "name": "qUINT by Marty McFly", "description": "MXAO, Lightroom, bloom and sharpening", "repositoryUrl": "https://github.com/martymcmodding/qUINT", "downloadUrl": "https://github.com/martymcmodding/qUINT/archive/master.zip", "shaders": ["qUINT_bloom.fx", "qUINT_deband.fx", "qUINT_dof.fx", "qUINT_lightroom.fx", "qUINT_mxao.fx", "qUINT_sharp.fx", "qUINT_ssr.fx"]},
    {"id": "22", "name": "ReShade HDR shaders by Lilium", "description": "HDR analysis, correction and tone mapping", "repositoryUrl": "https://github.com/EndlesslyFlowering/ReShade_HDR_shaders", "downloadUrl": "https://github.com/EndlesslyFlowering/ReShade_HDR_shaders/archive/refs/heads/master.zip", "shaders": ["lilium__cas_hdr.fx", "lilium__hdr_and_sdr_analysis.fx", "lilium__hdr_black_floor_fix.fx", "lilium__inverse_tone_mapping.fx", "lilium__tone_mapping.fx"]},
    {"id": "29", "name": "AdvancedAutoHDR by Pumbo", "description": "HDR tone-mapping helpers", "repositoryUrl": "https://github.com/Filoppi/PumboAutoHDR", "downloadUrl": "https://github.com/Filoppi/PumboAutoHDR/archive/refs/heads/master.zip", "shaders": ["AdvancedAutoHDR.fx", "ConvertColorSpace.fx"]},
]


class ReshadeProfileService:
    def __init__(self, profile_manager: ProfileManager):
        self.profile_manager = profile_manager
        self.root = Path(profile_manager.config_dir) / "reshade-profiles"
        self.root.mkdir(parents=True, exist_ok=True)
        self._catalog: Optional[List[Dict]] = None

    def _folder(self, profile_id: str) -> Path:
        folder = (self.root / profile_id).resolve()
        if folder.parent != self.root.resolve():
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
        catalog = []
        for source in self.catalog():
            item = dict(source)
            repository = str(item.get("repositoryUrl") or "").casefold()
            item["hdrInstallerRecommended"] = any(
                marker in repository for marker in RHI_HDR_REPOSITORIES
            )
            catalog.append(item)
        return {
            "profiles": [self._profile_payload(item) for item in self.profile_manager.config.reshade_profiles],
            "catalog": catalog,
            "catalogUrl": CATALOG_URL,
        }

    def _profile_payload(self, profile: ManagedReshadeProfile) -> Dict:
        folder = self._folder(profile.id)
        return {
            **profile.model_dump(),
            "presetPath": str(folder / self._preset_filename(profile.name)),
            "configPath": str(folder / "ReShade.ini"),
        }

    @staticmethod
    def _preset_filename(name: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9._ -]+", "", name).strip(" .") or "ReShade Profile"
        return f"{safe}.ini"

    def save(self, profile: ManagedReshadeProfile) -> Dict:
        catalog = self.catalog()
        catalog_ids = {item["id"] for item in catalog}
        unknown = set(profile.shaders) - catalog_ids
        if unknown:
            raise ValueError(f"Unknown ReShade shader provider: {sorted(unknown)[0]}")
        existing = next((item for item in self.profile_manager.config.reshade_profiles if item.id == profile.id), None)
        self._install_selected_shaders(profile, catalog)
        self._write_files(profile)
        if existing:
            old_path = self._folder(profile.id) / self._preset_filename(existing.name)
            new_path = self._folder(profile.id) / self._preset_filename(profile.name)
            if old_path != new_path and old_path.is_file():
                old_path.unlink()
        index = next((i for i, item in enumerate(self.profile_manager.config.reshade_profiles) if item.id == profile.id), None)
        if index is None:
            self.profile_manager.config.reshade_profiles.append(profile)
        else:
            self.profile_manager.config.reshade_profiles[index] = profile
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
                request = urllib.request.Request(url, headers={"User-Agent": "LS-Companion/1"})
                with urllib.request.urlopen(request, timeout=60) as response:
                    archive_data = response.read(128 * 1024 * 1024 + 1)
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
        selected_names = {
            filename.casefold() for files in profile.shaders.values() for filename in files
        }
        techniques = []
        for effect in (folder / "packages").rglob("*.fx"):
            if effect.name.casefold() not in selected_names:
                continue
            source = effect.read_text(encoding="utf-8", errors="ignore")
            for name in re.findall(
                r"(?im)^\s*technique(?:10|11)?\s+([A-Za-z_][A-Za-z0-9_]*)", source
            ):
                entry = f"{name}@{effect.name}"
                if entry not in techniques:
                    techniques.append(entry)
        technique_list = ",".join(techniques)
        self._atomic_write(
            preset,
            f"Techniques={technique_list}\nTechniqueSorting={technique_list}\n\n[LS Companion]\nManaged=1\n",
        )
        overlay_key = "36,0,0,0" if profile.overlay_enabled else "0,0,0,0"
        config = (
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
        self._atomic_write(folder / "ReShade.ini", config)

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

    def config_path(self, profile_id: str) -> Optional[Path]:
        if any(item.id == profile_id for item in self.profile_manager.config.reshade_profiles):
            path = self._folder(profile_id) / "ReShade.ini"
            return path if path.is_file() else None
        return None
