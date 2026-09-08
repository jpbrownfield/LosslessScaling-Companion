import io
import zipfile

import pytest

from companion.core.models import ManagedReshadeProfile, Profile, ReshadeConfig
from companion.core.profile_manager import ProfileManager
from companion.services.reshade_manager import ReshadeManager
from companion.services.reshade_profiles import ReshadeProfileService


def test_named_profile_generates_overlay_disabled_config(tmp_path):
    manager = ProfileManager(config_dir=tmp_path / "config")
    service = ReshadeProfileService(manager)
    service._catalog = []

    saved = service.save(ManagedReshadeProfile(id="hdr-clean", name="HDR Clean"))

    config = (tmp_path / "config" / "reshade-profiles" / "hdr-clean" / "ReShade.ini").read_text()
    preset = tmp_path / "config" / "reshade-profiles" / "hdr-clean" / "HDR Clean.ini"
    assert saved["name"] == "HDR Clean"
    assert preset.is_file()
    assert "KeyOverlay=0,0,0,0" in config
    assert "TutorialProgress=4" in config
    assert f"CurrentPresetPath={preset.resolve()}" in config


def test_selected_shader_archive_filters_unchecked_effects(tmp_path, monkeypatch):
    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, "w") as archive:
        archive.writestr("pack-main/Shaders/Wanted.fx", "technique WantedPass { pass {} }")
        archive.writestr("pack-main/Shaders/Unchecked.fx", "unchecked")
        archive.writestr("pack-main/Shaders/Common.fxh", "include")
        archive.writestr("pack-main/Textures/noise.png", b"png")

    manager = ProfileManager(config_dir=tmp_path / "config")
    service = ReshadeProfileService(manager)
    service._catalog = [{
        "id": "pack",
        "name": "Pack",
        "description": "",
        "repositoryUrl": "https://github.com/example/pack",
        "downloadUrl": "https://github.com/example/pack/archive/main.zip",
        "shaders": ["Wanted.fx", "Unchecked.fx"],
    }]
    monkeypatch.setattr(
        "companion.services.reshade_profiles.urllib.request.urlopen",
        lambda request, timeout=0: io.BytesIO(archive_bytes.getvalue()),
    )

    service.save(ManagedReshadeProfile(
        id="filtered", name="Filtered", shaders={"pack": ["Wanted.fx"]}
    ))

    package = tmp_path / "config" / "reshade-profiles" / "filtered" / "packages" / "pack"
    assert (package / "Shaders" / "Wanted.fx").is_file()
    assert not (package / "Shaders" / "Unchecked.fx").exists()
    assert (package / "Shaders" / "Common.fxh").is_file()
    assert (package / "Textures" / "noise.png").is_file()
    preset = tmp_path / "config" / "reshade-profiles" / "filtered" / "Filtered.ini"
    assert "Techniques=WantedPass@Wanted.fx" in preset.read_text(encoding="utf-8")


def test_referenced_named_profile_cannot_be_deleted(tmp_path):
    manager = ProfileManager(config_dir=tmp_path / "config")
    service = ReshadeProfileService(manager)
    service._catalog = []
    service.save(ManagedReshadeProfile(id="used", name="Used"))
    manager.add_or_update_profile(Profile(
        id="game", name="Game", reshade=ReshadeConfig(enabled=True, managed_profile_id="used")
    ))

    with pytest.raises(ValueError, match="still selected"):
        service.delete("used")


def test_apply_managed_config_is_lossless_scaling_scoped(tmp_path):
    lossless = tmp_path / "LosslessScaling.exe"
    active = tmp_path / "ReShade.ini"
    managed = tmp_path / "managed" / "ReShade.ini"
    lossless.write_bytes(b"")
    active.write_text("original", encoding="utf-8")
    managed.parent.mkdir()
    managed.write_text("[INPUT]\nKeyOverlay=0,0,0,0\n", encoding="utf-8")

    assert ReshadeManager.apply_managed_config(str(active), str(managed))
    assert "KeyOverlay=0,0,0,0" in active.read_text(encoding="utf-8")
    assert active.with_suffix(".ini.bak").read_text(encoding="utf-8") == "original"
