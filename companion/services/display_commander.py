"""Display Commander runs as a ReShade add-on inside the managed LS process."""
from pathlib import Path
import re

from .asset_store import AssetStore

PROVIDER = "display-commander"
FILENAME = "zzz_display_commander.addon64"


def deployment_files(store, profile, reshade_package):
    config = profile.graphics.display_commander
    if not config.enabled:
        return []
    version = tuple(int(part) for part in re.findall(r"\d+", str((reshade_package or {}).get("version", ""))))
    if version < (6, 6, 2):
        raise ValueError("Display Commander requires ReShade Full Add-On 6.6.2 or later")
    packages = [p for p in store.list_packages() if p.get("provider") == PROVIDER
                and (not config.version or p.get("version") == config.version)]
    if not packages:
        raise ValueError("Install Display Commander in Add-ons before enabling it for this profile")
    package = max(packages, key=lambda p: (
        str(p.get("source", {}).get("release", {}).get("published_at") or ""),
        tuple(int(n) for n in re.findall(r"\d+", str(p.get("version") or ""))),
        # Upstream replaces assets on its rolling 'latest' tag. Equal tag and
        # publication date must still select the most recently staged payload.
        (Path(p["payload_path"]).parent / "package.json").stat().st_mtime_ns,
    ))
    root = Path(package["payload_path"]).resolve()
    candidates = [(root / f["relative_path"]).resolve() for f in package.get("files", [])
                  if Path(f["relative_path"]).name == FILENAME]
    if len(candidates) != 1 or root not in candidates[0].parents or AssetStore.pe_architecture(candidates[0]) != "x64":
        raise ValueError("Display Commander package must contain one valid x64 add-on")
    expected = next(f.get("sha256") for f in package["files"] if Path(f["relative_path"]).name == FILENAME)
    if not expected or AssetStore.sha256(candidates[0]) != expected:
        raise ValueError("Display Commander package hash changed; download it again")
    reflex = int(config.inject_reflex)
    # Verified against upstream 1.0.6 PascalCase settings. Legacy keys also
    # support older releases; migration removes them in current versions.
    settings = {
        "FpsLimiter.Enabled": 0, "fps_limiter_enabled": 0,
        "FpsLimiter.TargetFps": 0, "fps_limit": 0,
        "FpsLimiter.BackgroundLimitEnabled": 0, "background_fps_enabled": 0,
        "Reflex.InjectionEnabled": reflex, "inject_reflex": reflex,
        "Reflex.OnPresentSyncMode": 0 if reflex else 2, "onpresent_reflex_mode": 0 if reflex else 2,
        "Reflex.ReflexLimiterMode": 0 if reflex else 2, "reflex_limiter_reflex_mode": 0 if reflex else 2,
        "Reflex.LimiterOffMode": 0 if reflex else 2, "reflex_disabled_limiter_mode": 0 if reflex else 2,
        "Reflex.SuppressSleep": 0, "suppress_reflex_sleep": 0,
        "WindowMode": 0, "window_mode": 0,
        "ShowDisplayCommanderUi": 0, "show_display_commander_ui": 0,
        "ShowPerformanceOverlay": 0, "show_performance_overlay": 0,
    }
    ini = store.write_generated_text(profile.id, "DisplayCommander.ini",
        "; Managed per profile by LS Companion.\n[DisplayCommander]\n"
        + "".join(f"{key}={value}\n" for key, value in settings.items()))
    return [
        dict(relative_path=FILENAME, source_path=str(candidates[0]), role="reshade_addon",
             source_package=f"{PROVIDER}/{package['version']}/{expected}"),
        dict(relative_path="DisplayCommander.ini", source_path=str(ini), role="display_commander_config",
             source_package="generated/display-commander-config"),
    ]
