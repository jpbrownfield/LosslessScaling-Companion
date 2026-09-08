"""Interactively evaluate LS-side LSP-NeuralRender/ReShade/Special K combinations.

The script uses LS Companion's normal resolver and transactional deployment manager.
It never writes to a game directory and restores the managed deployment that was
active when the run began, including when interrupted with Ctrl+C.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import psutil

from companion.core.models import Profile
from companion.core.profile_manager import ProfileManager
from companion.core.state import AppState
from companion.services.asset_store import AssetStore
from companion.services.deployment_manager import DeploymentManager
from companion.services.graphics_resolver import GraphicsResolver
from companion.services.process_watcher import ProcessWatcher
from companion.services.reshade_manager import ReshadeManager
from companion.services.reshade_profiles import ReshadeProfileService


COMPONENTS = ("lsp", "reshade", "special-k")


def build_cases(base: Profile) -> List[Tuple[str, Profile]]:
    """Build baseline plus every non-empty component combination."""
    cases: List[Tuple[str, Profile]] = []
    for count in range(0, len(COMPONENTS) + 1):
        for selected_tuple in combinations(COMPONENTS, count):
            selected = set(selected_tuple)
            profile = base.model_copy(deep=True)
            profile.id = f"graphics-eval-{'-'.join(selected_tuple) or 'baseline'}"
            profile.name = f"Graphics evaluation: {' + '.join(selected_tuple) or 'baseline'}"
            profile.auto_scale = False
            profile.dll_overrides = []

            profile.graphics.lossless_proxy.enabled = "lsp" in selected
            profile.graphics.neural_render.implementation = (
                "lsp_neural_render" if "lsp" in selected else "disabled"
            )
            profile.graphics.neural_render.package.enabled = "lsp" in selected
            profile.graphics.reshade.enabled = "reshade" in selected
            profile.graphics.special_k.enabled = "special-k" in selected
            profile.reshade = (
                base.reshade.model_copy(deep=True)
                if "reshade" in selected and base.reshade
                else None
            )
            cases.append(("+".join(selected_tuple) or "baseline", profile))
    return cases


def select_cases(
    cases: Sequence[Tuple[str, Profile]], requested: Optional[str]
) -> List[Tuple[str, Profile]]:
    if not requested:
        return list(cases)
    names = [item.strip().casefold() for item in requested.split(",") if item.strip()]
    available = {name.casefold(): (name, profile) for name, profile in cases}
    unknown = [name for name in names if name not in available]
    if unknown:
        raise ValueError(
            f"Unknown case(s): {', '.join(unknown)}. Available: {', '.join(available)}"
        )
    return [available[name] for name in names]


def _log_roots(ls_root: Path) -> List[Path]:
    roots = [ls_root]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(Path(local) / "LosslessScaling")
    documents = Path.home() / "Documents" / "My Mods" / "SpecialK" / "Profiles"
    roots.append(documents / "LosslessScaling.exe")
    roots.append(documents / "LosslessScaling")
    result = []
    seen = set()
    for root in roots:
        try:
            resolved = root.resolve()
        except OSError:
            continue
        folded = str(resolved).casefold()
        if folded not in seen:
            seen.add(folded)
            result.append(resolved)
    return result


def discover_logs(ls_root: Path) -> List[Path]:
    logs = set()
    for root in _log_roots(ls_root):
        if not root.is_dir():
            continue
        try:
            for path in root.rglob("*.log"):
                if path.is_file():
                    logs.add(path.resolve())
            for path in root.rglob("log.txt"):
                if path.is_file():
                    logs.add(path.resolve())
        except (OSError, PermissionError):
            continue
    return sorted(logs, key=lambda item: str(item).casefold())


def snapshot_log_offsets(ls_root: Path) -> Dict[str, int]:
    offsets = {}
    for path in discover_logs(ls_root):
        try:
            offsets[str(path)] = path.stat().st_size
        except OSError:
            pass
    return offsets


def read_log_deltas(ls_root: Path, offsets: Dict[str, int]) -> Dict[str, str]:
    deltas = {}
    paths = {str(path): path for path in discover_logs(ls_root)}
    for raw_path in offsets:
        paths.setdefault(raw_path, Path(raw_path))
    for raw_path, path in paths.items():
        if not path.is_file():
            continue
        try:
            size = path.stat().st_size
            start = offsets.get(raw_path, 0)
            if size < start:
                start = 0
            if size == start:
                continue
            with path.open("rb") as stream:
                stream.seek(start)
                data = stream.read(2 * 1024 * 1024)
            deltas[raw_path] = data.decode("utf-8", errors="replace")
        except (OSError, PermissionError) as error:
            deltas[raw_path] = f"[could not read log: {error}]\n"
    return deltas


def find_ls_processes() -> List[psutil.Process]:
    result = []
    for process in psutil.process_iter(["name", "exe", "create_time"]):
        try:
            if (process.info.get("name") or "").casefold() == "losslessscaling.exe":
                result.append(process)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return result


def inspect_processes() -> List[Dict]:
    result = []
    for process in find_ls_processes():
        item = {
            "pid": process.pid,
            "exe": process.info.get("exe"),
            "create_time": process.info.get("create_time"),
            "modules": [],
            "module_error": None,
        }
        try:
            paths = {
                mapping.path
                for mapping in process.memory_maps(grouped=False)
                if getattr(mapping, "path", None)
            }
            item["modules"] = sorted(paths, key=str.casefold)
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError) as error:
            item["module_error"] = str(error)
        result.append(item)
    return result


def analyze_evidence(files: Sequence[Dict], processes: Sequence[Dict], logs: Dict[str, str]) -> Dict:
    """Summarize observable load evidence without claiming visual correctness."""
    expected = sorted(
        {
            Path(str(item["relative_path"])).name.casefold()
            for item in files
            if Path(str(item["relative_path"])).suffix.casefold()
            in {".dll", ".addon32", ".addon64"}
        }
    )
    loaded_paths = {
        str(path)
        for process in processes
        for path in process.get("modules", [])
    }
    loaded_names = {Path(path).name.casefold() for path in loaded_paths}
    markers = []
    marker_pattern = re.compile(
        r"\b(error|failed|failure|fatal|crash|disabled|unsupported|exception)\b",
        re.IGNORECASE,
    )
    for source, content in logs.items():
        for line_number, line in enumerate(content.splitlines(), start=1):
            if marker_pattern.search(line):
                markers.append(
                    {"log": source, "line": line_number, "text": line[:1000]}
                )
                if len(markers) >= 200:
                    break
        if len(markers) >= 200:
            break
    return {
        "expected_module_names": expected,
        "observed_expected_modules": [name for name in expected if name in loaded_names],
        "unobserved_expected_modules": [name for name in expected if name not in loaded_names],
        "module_enumeration_succeeded": any(
            process.get("module_error") is None for process in processes
        ),
        "diagnostic_log_markers": markers,
        "interpretation": (
            "An unobserved module is evidence to investigate, not definitive failure; some runtimes "
            "load only while scaling and may unload before capture. Log markers may include benign text."
        ),
    }


def _safe_log_name(path: str) -> str:
    digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:10]
    name = Path(path).name.replace(" ", "_")
    return f"{digest}-{name}"


def write_case_report(case_dir: Path, report: Dict, log_deltas: Dict[str, str]) -> None:
    case_dir.mkdir(parents=True, exist_ok=True)
    (case_dir / "result.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    logs_dir = case_dir / "logs"
    logs_dir.mkdir(exist_ok=True)
    index = {}
    for source, content in log_deltas.items():
        destination = logs_dir / _safe_log_name(source)
        destination.write_text(content, encoding="utf-8", errors="replace")
        index[source] = str(destination.relative_to(case_dir))
    (case_dir / "log-index.json").write_text(
        json.dumps(index, indent=2), encoding="utf-8"
    )


def snapshot_active_deployment(
    deployment: DeploymentManager, ls_root: Path, snapshot_root: Path
) -> Tuple[Dict, List[Dict]]:
    manifest = deployment._read_manifest()
    restore_files = []
    for entry in manifest.get("files", []):
        relative = str(entry["relative_path"])
        source = deployment._destination(ls_root, relative)
        if entry.get("inactive"):
            source = source.with_name(source.name + ".inactive")
        expected = entry.get("deployed_sha256")
        if not source.is_file() or (expected and AssetStore.sha256(source) != expected):
            raise RuntimeError(
                f"Managed file differs from the active manifest; refusing to test: {relative}"
            )
        snapshot = snapshot_root / Path(relative)
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, snapshot)
        restore_files.append(
            {
                "relative_path": relative,
                "source_path": str(snapshot),
                "role": entry.get("role") or "support_file",
                "source_package": entry.get("source_package"),
            }
        )
    return manifest, restore_files


def preflight(
    cases: Iterable[Tuple[str, Profile]],
    resolver: GraphicsResolver,
    deployment: DeploymentManager,
    lossless_exe: str,
    reshade_profiles: Optional[ReshadeProfileService] = None,
) -> Tuple[List[Tuple[str, Profile, List[Dict]]], List[Dict]]:
    ready = []
    results = []
    for name, profile in cases:
        try:
            files = resolver.resolve(profile, lossless_scaling_exe=lossless_exe)
            deployment.needs_change(profile.id, files, lossless_scaling_exe=lossless_exe)
            managed_config = None
            if profile.reshade and profile.reshade.managed_profile_id:
                if reshade_profiles is None:
                    raise RuntimeError("Named ReShade profile validation is unavailable")
                managed_config = reshade_profiles.config_path(
                    profile.reshade.managed_profile_id
                )
                if managed_config is None:
                    raise RuntimeError(
                        f"Managed ReShade profile is missing: {profile.reshade.managed_profile_id}"
                    )
            ready.append((name, profile, files))
            results.append(
                {
                    "case": name,
                    "status": "ready",
                    "files": [item["relative_path"] for item in files],
                    "managedReshadeConfig": str(managed_config) if managed_config else None,
                    "runtimeProviders": sorted(
                        provider for provider in ("reshade", "special-k")
                        if getattr(profile.graphics, provider.replace("-", "_"), None)
                        and getattr(profile.graphics, provider.replace("-", "_")).enabled
                    ),
                    "specialKSettings": (
                        profile.graphics.special_k.model_dump()
                        if profile.graphics.special_k.enabled
                        else None
                    ),
                }
            )
        except Exception as error:
            results.append({"case": name, "status": "blocked", "error": str(error)})
    return ready, results


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate every LS-side LSP-NeuralRender/ReShade/Special K combination."
    )
    parser.add_argument("--profile-id", help="Profile supplying package versions and DLSSNR hash; defaults to the active profile")
    parser.add_argument("--cases", help="Comma-separated subset, e.g. lsp,lsp+reshade")
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Validate combinations without stopping or modifying Lossless Scaling",
    )
    parser.add_argument(
        "--capture-seconds",
        type=float,
        default=0,
        help="Capture automatically for N seconds; default pauses for Enter",
    )
    parser.add_argument("--report-dir", help="Report parent (default: diagnostics/graphics-stack)")
    return parser.parse_args(argv)


def choose_profile(manager: ProfileManager, requested_id: Optional[str]) -> Optional[Profile]:
    if requested_id:
        return manager.get_profile_by_id(requested_id)
    profiles = manager.config.profiles
    if not profiles:
        return None
    active = manager.get_profile_by_id(manager.config.active_profile_id or "")
    if len(profiles) == 1:
        return profiles[0]

    default = active or profiles[0]
    print("Profiles:")
    for index, profile in enumerate(profiles, start=1):
        marker = " (active/default)" if profile.id == default.id else ""
        print(f"  {index}. {profile.name} [{profile.id}]{marker}")
    response = input(f"Select profile [default: {default.name}]: ").strip()
    if not response:
        return default
    if response.isdigit() and 1 <= int(response) <= len(profiles):
        return profiles[int(response) - 1]
    return manager.get_profile_by_id(response)


def print_run_summary(summary: Dict) -> None:
    print("\nEvaluation summary")
    for case in summary.get("cases", []):
        evidence = case.get("evidence", {})
        missing = evidence.get("unobserved_expected_modules", [])
        markers = evidence.get("diagnostic_log_markers", [])
        state = "RUNNING" if case.get("lossless_scaling_running") else "STOPPED"
        details = []
        if missing:
            details.append(f"unobserved modules: {', '.join(missing)}")
        if markers:
            details.append(f"diagnostic log markers: {len(markers)}")
        if case.get("visual_notes"):
            details.append(f"notes: {case['visual_notes']}")
        suffix = f" — {'; '.join(details)}" if details else ""
        print(f"  {case['case']}: LS {state}{suffix}")
    for item in summary.get("preflight", []):
        if item.get("status") == "blocked":
            print(f"  {item['case']}: BLOCKED — {item.get('error', 'unknown error')}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    manager = ProfileManager()
    base = choose_profile(manager, args.profile_id)
    if base is None:
        requested = args.profile_id or "active profile"
        print(f"Profile not found: {requested}", file=sys.stderr)
        return 2
    lossless_exe = manager.config.lossless_scaling_exe_path
    if not lossless_exe or not Path(lossless_exe).is_file():
        print("Configure a valid LosslessScaling.exe path first.", file=sys.stderr)
        return 2

    try:
        requested_cases = select_cases(build_cases(base), args.cases)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2

    asset_root = manager.config.asset_store_path or str(manager.config_dir / "managed-assets")
    store = AssetStore(asset_root)
    resolver = GraphicsResolver(store)
    deployment = DeploymentManager(store)
    reshade_profiles = ReshadeProfileService(manager)
    ready, preflight_results = preflight(
        requested_cases, resolver, deployment, lossless_exe, reshade_profiles
    )
    print(json.dumps(preflight_results, indent=2))
    blocked = [item for item in preflight_results if item["status"] == "blocked"]
    if args.preflight_only:
        print("\nPreflight-only run complete; Lossless Scaling was not changed.")
        return 1 if blocked else 0
    if not ready:
        print("No cases passed preflight; nothing will be changed.", file=sys.stderr)
        return 2

    print("\nThis will repeatedly stop Lossless Scaling and replace its managed graphics stack.")
    print("Exit LS Companion from the tray first so it cannot reapply a profile during the test.")
    if input("Type EVALUATE to continue: ").strip() != "EVALUATE":
        print("Cancelled.")
        return 1

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    report_parent = Path(args.report_dir) if args.report_dir else PROJECT_ROOT / "diagnostics" / "graphics-stack"
    run_dir = report_parent.resolve() / stamp
    snapshot_root = run_dir / "initial-deployment"
    run_dir.mkdir(parents=True, exist_ok=False)

    ls_root = Path(lossless_exe).resolve().parent
    initial_manifest, restore_files = snapshot_active_deployment(
        deployment, ls_root, snapshot_root
    )
    state = AppState()
    watcher = ProcessWatcher(manager, state)
    initially_running = watcher.check_is_lossless_scaling_running()
    summary = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "lossless_scaling_exe": str(Path(lossless_exe).resolve()),
        "profile_id": base.id,
        "preflight": preflight_results,
        "cases": [],
        "restored": False,
    }

    try:
        managed_reshade_ini = None
        for index, (name, profile, files) in enumerate(ready, start=1):
            print(f"\n[{index}/{len(ready)}] Testing {name}")
            if not watcher.stop_lossless_scaling():
                raise RuntimeError("Could not stop Lossless Scaling")
            offsets = snapshot_log_offsets(ls_root)
            manifest = deployment.apply(
                profile_id=profile.id,
                lossless_scaling_exe=lossless_exe,
                files=files,
            )
            if profile.reshade and profile.reshade.managed_profile_id:
                managed_config = reshade_profiles.config_path(
                    profile.reshade.managed_profile_id
                )
                if managed_config is None:
                    raise RuntimeError(
                        f"Managed ReShade profile is missing: {profile.reshade.managed_profile_id}"
                    )
                managed_reshade_ini = ls_root / "ReShade.ini"
                if not ReshadeManager.apply_managed_config(
                    str(managed_reshade_ini), str(managed_config)
                ):
                    raise RuntimeError("Could not apply the managed ReShade configuration")
            started_at = datetime.now(timezone.utc)
            if not watcher.launch_lossless_scaling(force=True):
                raise RuntimeError("Could not launch Lossless Scaling")
            time.sleep(2.0)
            print("Start scaling a representative application and inspect the image and overlays.")
            if args.capture_seconds > 0:
                print(f"Collecting evidence for {args.capture_seconds:g} seconds...")
                time.sleep(args.capture_seconds)
                visual_notes = None
            else:
                input("Press Enter after testing this combination...")
                visual_notes = input("Visual notes for this case (optional): ").strip() or None
            processes = inspect_processes()
            deltas = read_log_deltas(ls_root, offsets)
            ended_at = datetime.now(timezone.utc)
            case_report = {
                "case": name,
                "started_at": started_at.isoformat(),
                "ended_at": ended_at.isoformat(),
                "lossless_scaling_running": bool(processes),
                "deployment": manifest,
                "processes": processes,
                "captured_logs": list(deltas),
                "expected_deployed_files": [item["relative_path"] for item in files],
                "evidence": analyze_evidence(files, processes, deltas),
                "visual_notes": visual_notes,
            }
            if not watcher.stop_lossless_scaling():
                raise RuntimeError("Could not stop Lossless Scaling after evidence capture")
            runtime_providers = {
                provider for provider in ("reshade", "special-k")
                if getattr(profile.graphics, provider.replace("-", "_")).enabled
            }
            case_report["runtime_files_parked"] = deployment.set_runtime_packages_active(
                lossless_exe, runtime_providers, active=False
            )
            if managed_reshade_ini:
                if not ReshadeManager.restore_backup(str(managed_reshade_ini)):
                    managed_reshade_ini.unlink(missing_ok=True)
                managed_reshade_ini = None
            write_case_report(run_dir / f"{index:02d}-{name}", case_report, deltas)
            summary["cases"].append(case_report)
    except KeyboardInterrupt:
        summary["interrupted"] = True
        print("\nInterrupted; restoring the initial managed deployment.")
    except Exception as error:
        summary["error"] = str(error)
        print(f"\nEvaluation stopped: {error}", file=sys.stderr)
    finally:
        if 'managed_reshade_ini' in locals() and managed_reshade_ini:
            if not ReshadeManager.restore_backup(str(managed_reshade_ini)):
                managed_reshade_ini.unlink(missing_ok=True)
        if not watcher.stop_lossless_scaling():
            summary["restore_error"] = "Could not stop Lossless Scaling before restoration"
        else:
            try:
                deployment.apply(
                    profile_id=initial_manifest.get("profile_id"),
                    lossless_scaling_exe=lossless_exe,
                    files=restore_files,
                )
                initially_inactive = {
                    provider
                    for entry in initial_manifest.get("files", [])
                    if entry.get("inactive")
                    for provider in [deployment._runtime_provider(entry)]
                    if provider
                }
                initially_active = {
                    provider
                    for entry in initial_manifest.get("files", [])
                    for provider in [deployment._runtime_provider(entry)]
                    if provider and not entry.get("inactive")
                }
                deployment.set_runtime_packages_active(
                    lossless_exe, initially_active, active=True
                )
                deployment.set_runtime_packages_active(
                    lossless_exe, initially_inactive, active=False
                )
                summary["restored"] = True
                if initially_running:
                    summary["restarted_after_restore"] = watcher.launch_lossless_scaling(force=True)
            except Exception as error:
                summary["restore_error"] = str(error)
        summary["finished_at"] = datetime.now(timezone.utc).isoformat()
        (run_dir / "summary.json").write_text(
            json.dumps(summary, indent=2), encoding="utf-8"
        )

    print(f"\nReport: {run_dir / 'summary.json'}")
    print_run_summary(summary)
    if not summary["restored"]:
        print("WARNING: the initial managed deployment was not restored; inspect restore_error.")
        return 3
    return 1 if summary.get("error") or summary.get("interrupted") else 0


if __name__ == "__main__":
    raise SystemExit(main())
