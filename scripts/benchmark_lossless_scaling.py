"""Benchmark Lossless Scaling with PresentMon and an external 3D workload.

This is intentionally separate from evaluate_graphics_stack.py. It measures the
currently deployed LS configuration and can optionally exercise the production
dynamic-limiter controller through a temporary, reversible RTSS profile.
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from companion.core.models import Profile, RtssLimiterConfig
from companion.core.profile_manager import ProfileManager
from companion.core.state import AppState
from companion.services.asset_store import AssetStore
from companion.services.automation import AutomationController
from companion.services.benchmark_metrics import (
    summarize_presentmon_files,
    summarize_dynamic_limiter,
    summarize_values,
)
from companion.services.dynamic_limiter import DynamicLimiterController
from companion.services.ls_inspector import LosslessScalingInspector
from companion.services.input_simulator import InputSimulator
from companion.services.performance_benchmark import (
    GpuSampleRecorder,
    PresentMonCapture,
    find_largest_window,
    find_process_id,
    focus_window,
    measure_visible_marker,
)
from companion.services.process_watcher import ProcessWatcher
from companion.services.release_providers import ReleaseManager
from companion.services.rtss_manager import RtssProfileManager


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--benchmark-exe",
        help="Windowed 3D workload executable; defaults to bundled LSBenchmark.exe",
    )
    parser.add_argument(
        "--benchmark-arg", action="append", default=[],
        help="One argument passed to the workload; repeat for multiple arguments",
    )
    parser.add_argument("--presentmon-exe", help="Existing PresentMon x64 console executable")
    parser.add_argument(
        "--download-presentmon", action="store_true",
        help="Download and stage the latest official PresentMon x64 console release",
    )
    parser.add_argument("--duration", type=float, default=30.0)
    parser.add_argument("--warmup", type=float, default=5.0)
    parser.add_argument("--window-timeout", type=float, default=20.0)
    parser.add_argument("--report-dir", help="Default: diagnostics/performance-benchmark")
    parser.add_argument("--no-scale", action="store_true", help="Capture an unscaled baseline")
    parser.add_argument(
        "--use-default-profile", action="store_true",
        help="Use the helper default profile and its existing RTSS settings without calibration",
    )
    parser.add_argument(
        "--dynamic-limiter", action="store_true",
        help="Exercise the real DynamicLimiterController using a temporary RTSS profile",
    )
    parser.add_argument("--adaptive-acceptance", action="store_true", help="Add fixed-cap monitoring ABBA and independent LS feedback acceptance verdicts")
    parser.add_argument("--monitoring-phase-seconds", type=float, default=20.)
    parser.add_argument("--rtss-path")
    parser.add_argument("--minimum-fps", type=int, default=30)
    parser.add_argument("--maximum-fps", type=int, default=120)
    parser.add_argument("--gpu-headroom", "--ls-gpu-target", dest="ls_gpu_target", type=float, default=15.0,
                        help="Minimum spare GPU capacity reserved on LS adapters (percent); old option name remains an alias")
    parser.add_argument("--saturation-threshold", type=float, default=90.0)
    parser.add_argument("--width", type=int, default=1280, help="Bundled workload width")
    parser.add_argument("--height", type=int, default=720, help="Bundled workload height")
    parser.add_argument("--shader-load", type=int, default=64, help="Bundled workload GPU pressure")
    parser.add_argument("--monitor-resolution", action="store_true", help="Use the foreground monitor's full pixel resolution for the bundled workload")
    parser.add_argument("--settling-seconds", type=float, default=30.0)
    parser.add_argument("--target-tolerance", type=float, default=2.0, help="GPU target tolerance in percentage points")
    parser.add_argument(
        "--input-probes", type=int, default=10,
        help="Synthetic F15 input samples emitted after warm-up for PresentMon input latency",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="Acknowledge process launch, scaling hotkey, and temporary RTSS changes",
    )
    return parser.parse_args(argv)


def monitor_workload_bounds() -> Dict:
    class MonitorInfo(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("monitor", wintypes.RECT),
                    ("work", wintypes.RECT), ("flags", wintypes.DWORD)]

    native = ctypes.windll.user32
    native.GetForegroundWindow.argtypes = []
    native.GetForegroundWindow.restype = wintypes.HWND
    native.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    native.MonitorFromWindow.restype = wintypes.HANDLE
    native.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
    native.GetMonitorInfoW.restype = wintypes.BOOL
    dpi_context = native.SetThreadDpiAwarenessContext
    dpi_context.argtypes = [ctypes.c_void_p]
    dpi_context.restype = ctypes.c_void_p
    previous = dpi_context(ctypes.c_void_p(-4))
    try:
        handle = native.MonitorFromWindow(native.GetForegroundWindow(), 2)
        info = MonitorInfo()
        info.size = ctypes.sizeof(info)
        if not handle or not native.GetMonitorInfoW(handle, ctypes.byref(info)):
            raise RuntimeError("Could not determine the active monitor resolution")
        rect = info.monitor
        width, height = rect.right - rect.left, rect.bottom - rect.top
        if not 320 <= width <= 7680 or not 240 <= height <= 4320:
            raise ValueError("Monitor resolution is outside the bundled workload's supported range")
        return {"left": rect.left, "top": rect.top, "width": width, "height": height}
    finally:
        if previous:
            dpi_context(previous)


def limiter_profile_readback(rtss: RtssProfileManager, profile: Profile) -> Optional[int]:
    if not profile.rtss.managed_install_path or not profile.rtss.managed_target_process:
        return None
    path = rtss._profiles_dir(Path(profile.rtss.managed_install_path)) / f"{profile.rtss.managed_target_process}.cfg"
    try:
        value = rtss._get_value(rtss._read_text(path), "Framerate", "Limit")
        return int(value) if value is not None else None
    except (OSError, ValueError):
        return None


def _presentmon_from_package(package: Dict) -> Optional[Path]:
    payload = Path(str(package.get("payload_path") or ""))
    for item in package.get("files", []):
        relative = str(item.get("relative_path") or "")
        candidate = payload / relative
        if (
            candidate.name.casefold().startswith("presentmon-")
            and candidate.suffix.casefold() == ".exe"
            and item.get("architecture") == "x64"
            and candidate.is_file()
        ):
            return candidate.resolve()
    return None


def resolve_presentmon(args: argparse.Namespace, manager: ProfileManager) -> Path:
    if args.presentmon_exe:
        path = Path(args.presentmon_exe).resolve(strict=True)
        if AssetStore.pe_architecture(path) != "x64":
            raise RuntimeError("PresentMon must be an x64 Windows executable")
        return path

    store = AssetStore(
        manager.config.asset_store_path or str(manager.config_dir / "managed-assets")
    )
    packages = [
        package for package in store.list_packages()
        if package.get("provider") == "presentmon"
    ]
    for package in reversed(packages):
        verification = (package.get("source") or {}).get("verification") or {}
        if not verification.get("publisher_digest_verified"):
            continue
        path = _presentmon_from_package(package)
        if path:
            return path
    if not args.download_presentmon:
        print(
            "PresentMon is required and is not staged. The benchmark runner can download the official "
            "x64 console release from GameTechDev/PresentMon and verify its publisher SHA-256."
        )
        if input("Type DOWNLOAD to continue: ").strip() != "DOWNLOAD":
            raise RuntimeError(
                "PresentMon download declined; provide --presentmon-exe to use a local binary"
            )
        args.download_presentmon = True

    release_manager = ReleaseManager(store)
    releases = release_manager.check("presentmon", force=True)
    release = next((item for item in releases if item.get("assets")), None)
    if release is None:
        raise RuntimeError("The official PresentMon release has no matching x64 console asset")
    asset = release["assets"][0]
    if not str(asset.get("digest") or "").startswith("sha256:"):
        raise RuntimeError(
            "The official release does not publish a SHA-256 digest; provide a separately "
            "verified binary with --presentmon-exe instead"
        )
    package = release_manager.stage_release(
        "presentmon",
        str(release["version"]),
        str(asset["name"]),
        f"presentmon-{uuid.uuid4().hex}",
        max_bytes=128 * 1024 * 1024,
    )
    path = _presentmon_from_package(package)
    if path is None:
        raise RuntimeError("The staged PresentMon package did not contain an x64 console executable")
    return path


def _gpu_summary(samples: list[Dict], benchmark_pid: int, lossless_pid: Optional[int]) -> Dict:
    result = {}
    for label, pid in (("benchmark", benchmark_pid), ("lossless_scaling", lossless_pid)):
        if not pid:
            result[label] = summarize_values([])
            continue
        values = [
            sample["utilization"].get(str(pid))
            for sample in samples
            if sample["utilization"].get(str(pid)) is not None
        ]
        result[label] = summarize_values(values)
    return result


def _write_json(path: Path, payload: Dict) -> None:
    AssetStore._atomic_json(path, payload)


def resolve_benchmark_executable(requested: Optional[str]) -> tuple[Path, bool]:
    if requested:
        path = Path(requested).resolve(strict=True)
        return path, path.name.casefold() in {"lsbenchmark.exe", "lsbenchmark-x64.exe"}
    candidates = []
    if getattr(sys, "frozen", False):
        bundle_root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        candidates.append(bundle_root / "tools" / "LSBenchmark.exe")
        candidates.append(Path(sys.executable).resolve().parent / "tools" / "LSBenchmark.exe")
    candidates.extend(
        (
            PROJECT_ROOT / "build" / "native" / "LSBenchmark.exe",
            PROJECT_ROOT / "tools" / "LSBenchmark.exe",
        )
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve(), True
    raise RuntimeError(
        "Bundled LSBenchmark.exe was not found. Build benchmark_app/ls_benchmark.cpp "
        "or supply --benchmark-exe."
    )


def default_profile_limiter_settings(
    profile: Profile, *, globally_enabled: bool, global_mode: str
) -> Optional[Dict]:
    """Resolve the saved limiter state without learning or mutating the profile."""
    if not globally_enabled or not profile.rtss.enabled:
        return None
    effective_mode = global_mode if profile.rtss.limit_mode == "inherit" else profile.rtss.limit_mode
    configured_limit = (
        profile.rtss.framerate_limit
        if effective_mode == "static"
        else (profile.rtss.learned_framerate_limit if profile.rtss.automatic_calibration_disabled else None) or profile.rtss.maximum_framerate_limit
    )
    return {
        "effective_mode": effective_mode,
        "configured_limit": configured_limit,
        "limit_method": profile.rtss.limit_method,
    }


def main(argv: Optional[Sequence[str]] = None, *, progress_callback: Optional[Callable[[Dict], None]] = None) -> int:
    args = parse_args(argv)
    if os.name != "nt":
        print("This benchmark requires Windows.", file=sys.stderr)
        return 2
    if args.duration <= args.warmup or args.warmup < 0:
        print("--duration must be greater than --warmup, and warmup cannot be negative.", file=sys.stderr)
        return 2
    if not 1 <= args.minimum_fps <= args.maximum_fps <= 1000:
        print("The limiter range must satisfy 1 <= minimum <= maximum <= 1000.", file=sys.stderr)
        return 2
    if not 1 <= args.ls_gpu_target <= 100:
        print("--gpu-headroom must be between 1 and 100.", file=sys.stderr)
        return 2
    if not 0 <= args.saturation_threshold <= 100:
        print("--saturation-threshold must be between 0 and 100.", file=sys.stderr)
        return 2
    if args.input_probes < 0:
        print("--input-probes cannot be negative.", file=sys.stderr)
        return 2
    if not 0 <= args.target_tolerance <= 10 or args.settling_seconds < 10:
        print("Target tolerance must be 0-10 percentage points and settling time at least 10 seconds.", file=sys.stderr)
        return 2
    if args.dynamic_limiter and args.duration - args.warmup < args.settling_seconds:
        print("Dynamic limiter capture must include the full settling window after warmup.", file=sys.stderr)
        return 2
    if args.use_default_profile and args.dynamic_limiter:
        print(
            "--use-default-profile cannot be combined with --dynamic-limiter because "
            "the default-profile test must not recalibrate or adjust limiter settings.",
            file=sys.stderr,
        )
        return 2

    try:
        benchmark_exe, bundled_workload = resolve_benchmark_executable(args.benchmark_exe)
        monitor_bounds = monitor_workload_bounds() if args.monitor_resolution else None
        if monitor_bounds:
            if not bundled_workload:
                raise ValueError("--monitor-resolution requires the bundled workload")
            args.width, args.height = monitor_bounds["width"], monitor_bounds["height"]
    except Exception as error:
        print(f"Benchmark workload preflight failed: {error}", file=sys.stderr)
        return 2
    if benchmark_exe.suffix.casefold() != ".exe":
        print("--benchmark-exe must name a Windows executable.", file=sys.stderr)
        return 2
    manager = ProfileManager()
    if args.dynamic_limiter:
        status = RtssProfileManager().status(args.rtss_path or manager.config.rtss_install_path)
        if not status.get("installed") or not status.get("running"):
            print("Dynamic limiter benchmarking requires RTSS installed and running.", file=sys.stderr)
            return 2
    if not args.no_scale:
        from companion.services.managed_runtime import ManagedRuntime
        try:
            runtime = ManagedRuntime(manager.config_dir)
            runtime.validate(Path(manager.config.lossless_scaling_exe_path or ""))
            if Path(manager.config.lossless_settings_xml_path or "").resolve() != runtime.settings.resolve():
                raise RuntimeError("Benchmark requires the managed runtime's private Settings.xml")
            manager.managed_runtime = runtime
        except (OSError, ValueError, RuntimeError) as error:
            print(f"Start Companion once to prepare its managed LS copy before benchmarking: {error}", file=sys.stderr)
            return 2
    default_profile = next(
        (profile for profile in manager.config.profiles if profile.is_default),
        None,
    ) if args.use_default_profile else None
    if args.use_default_profile and default_profile is None:
        print("The helper default profile is unavailable.", file=sys.stderr)
        return 2
    try:
        presentmon_exe = resolve_presentmon(args, manager)
    except Exception as error:
        print(f"PresentMon preflight failed: {error}", file=sys.stderr)
        return 2
    if args.adaptive_acceptance and not args.dynamic_limiter:
        print("--adaptive-acceptance requires --dynamic-limiter", file=sys.stderr)
        return 2
    if args.adaptive_acceptance and args.monitoring_phase_seconds < 15:
        print("Monitoring phases must last at least 15 seconds", file=sys.stderr)
        return 2
    if args.dynamic_limiter and args.no_scale:
        print("--dynamic-limiter cannot be combined with --no-scale.", file=sys.stderr)
        return 2
    if not args.yes:
        print("This launches the benchmark and PresentMon and may toggle Lossless Scaling.")
        if args.dynamic_limiter:
            print("It also creates a temporary RTSS profile and restores/removes it afterward.")
        if input("Type BENCHMARK to continue: ").strip() != "BENCHMARK":
            print("Cancelled.")
            return 1

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if args.report_dir:
        parent = Path(args.report_dir)
    elif getattr(sys, "frozen", False):
        local_root = Path(os.environ.get("LOCALAPPDATA", Path.home()))
        parent = local_root / "LosslessScalingHelper" / "benchmarks"
    else:
        parent = PROJECT_ROOT / "diagnostics" / "performance-benchmark"
    run_dir = parent.resolve() / stamp
    run_dir.mkdir(parents=True, exist_ok=False)

    state = AppState()
    watcher = ProcessWatcher(manager, state)
    inspector = LosslessScalingInspector(
        settings_xml_path=manager.config.lossless_settings_xml_path,
        lossless_exe_path=manager.config.lossless_scaling_exe_path,
    )
    automation = AutomationController(
        manager,
        state,
        process_watcher=watcher,
        scaling_state_probe=lambda: inspector.detect_lossless_scaling_overlay_window()[0],
    )
    workload: Optional[subprocess.Popen] = None
    capture: Optional[PresentMonCapture] = None
    recorder: Optional[GpuSampleRecorder] = None
    limiter: Optional[DynamicLimiterController] = None
    limiter_profile: Optional[Profile] = None
    rtss: Optional[RtssProfileManager] = None
    monitoring_restore = None
    scaling_may_have_started = False
    report = {
        "schema_version": 1,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "benchmark": str(benchmark_exe),
        "benchmark_arguments": list(args.benchmark_arg),
        "bundled_workload": bundled_workload,
        "workload_resolution": {"width": args.width, "height": args.height},
        "monitor_bounds": monitor_bounds,
        "presentmon": str(presentmon_exe),
        "duration_seconds": args.duration,
        "warmup_seconds": args.warmup,
        "scaled": not args.no_scale,
        "dynamic_limiter_enabled": args.dynamic_limiter,
        "uses_default_profile": args.use_default_profile,
        "default_profile": default_profile.model_dump() if default_profile else None,
        "limiter_status_samples": [],
        "input_probe_key": "F15",
        "input_probe_samples_requested": args.input_probes,
        "input_probe_events": [],
        "software_visible_latency_scope": (
            "Input injection to detection of the bundled marker in Windows-composed screen "
            "output; this excludes physical display scanout and pixel response"
        ),
        "measurement_note": (
            "PresentMon input latency is a software input-to-displayed-frame metric. "
            "It is not a physical panel click-to-photon measurement, and cross-process "
            "source-input to Lossless Scaling output correlation is not assumed."
        ),
    }
    exit_code = 0
    try:
        workload_command = [str(benchmark_exe)]
        if bundled_workload:
            workload_command.extend(
                (
                    "--width", str(args.width),
                    "--height", str(args.height),
                    "--shader-load", str(args.shader_load),
                    "--duration", str(int(args.duration + 60 + (4*args.monitoring_phase_seconds+35 if args.adaptive_acceptance else 0))),
                    "--event-log", str(run_dir / "source-input-events.csv"),
                )
            )
        workload_command.extend(args.benchmark_arg)
        report["workload_command"] = workload_command
        workload = subprocess.Popen(workload_command, cwd=benchmark_exe.parent)
        hwnd = find_largest_window(workload.pid, timeout=args.window_timeout)
        if monitor_bounds:
            ctypes.windll.user32.SetWindowPos(hwnd, 0, monitor_bounds["left"], monitor_bounds["top"], 0, 0, 0x0005)
        if not focus_window(hwnd):
            raise RuntimeError("Windows did not grant the benchmark foreground ownership")
        report.update({"benchmark_pid": workload.pid, "benchmark_hwnd": hwnd})

        if default_profile:
            automation.activate_profile(
                default_profile,
                str(benchmark_exe),
                force=True,
                target_pid=workload.pid,
                target_hwnd=hwnd,
                suppress_auto_scale=True,
            )
            if state.current_active_profile is not default_profile:
                raise RuntimeError("The default profile could not be activated for the workload")

        if not args.no_scale:
            if inspector.detect_lossless_scaling_overlay_window()[0]:
                raise RuntimeError(
                    "Lossless Scaling is already active; stop scaling before starting a benchmark"
                )
            if not watcher.check_is_lossless_scaling_running() and not watcher.launch_lossless_scaling(force=True):
                raise RuntimeError("Lossless Scaling could not be launched")
            scaling_may_have_started = True
            if not automation.set_scaling(
                True,
                profile=default_profile,
                reason="performance_benchmark",
                force=True,
                target_pid=workload.pid,
                target_hwnd=hwnd,
                park_runtime_on_stop=False,
            ):
                raise RuntimeError("Lossless Scaling did not confirm activation")
        lossless_pid = find_process_id("LosslessScaling.exe")
        if args.dynamic_limiter:
            from companion.services.adapter_report import configured_process
            instance = configured_process(manager.config.lossless_scaling_exe_path)
            if not instance:
                raise RuntimeError("The configured LS instance is unavailable for limiter telemetry")
            lossless_pid = instance[0]
        report["lossless_scaling_pid"] = lossless_pid
        output_hwnd = hwnd
        if not args.no_scale:
            _active, overlay_hwnd = inspector.detect_lossless_scaling_overlay_window()
            if overlay_hwnd:
                output_hwnd = int(overlay_hwnd)
        report["latency_output_hwnd"] = output_hwnd
        process_ids = [workload.pid, *([lossless_pid] if lossless_pid else [])]

        default_limiter = default_profile_limiter_settings(
            default_profile,
            globally_enabled=manager.config.rtss_frame_limiting_enabled,
            global_mode=manager.config.rtss_default_limit_mode,
        ) if args.use_default_profile and default_profile else None
        if default_limiter and default_profile:
            limiter_profile = default_profile.model_copy(deep=True)
            limiter_profile.id = f"benchmark-{uuid.uuid4().hex}"
            limiter_profile.target_process = benchmark_exe.name
            limiter_profile.target_executable_path = str(benchmark_exe)
            rtss = RtssProfileManager(args.rtss_path or manager.config.rtss_install_path)
            rtss.apply_profile(
                limiter_profile,
                configured_path=args.rtss_path or manager.config.rtss_install_path,
                framerate_limit=default_limiter["configured_limit"],
            )
            report["default_profile_rtss"] = {
                "enabled": True,
                **default_limiter,
                "settings_adjusted": False,
            }
        elif args.dynamic_limiter:
            rtss = RtssProfileManager(args.rtss_path or manager.config.rtss_install_path)
            rtss_status = rtss.status(args.rtss_path or manager.config.rtss_install_path)
            if not rtss_status.get("installed") or not rtss_status.get("running"):
                raise RuntimeError("Dynamic limiter benchmarking requires RTSS installed and running")
            limiter_manager = ProfileManager(run_dir / "limiter-state")
            limiter_profile = Profile(
                id=f"benchmark-{uuid.uuid4().hex}",
                name="Temporary LS performance benchmark",
                target_process=benchmark_exe.name,
                target_executable_path=str(benchmark_exe),
                rtss=RtssLimiterConfig(
                    enabled=True,
                    limit_mode="dynamic",
                    framerate_limit=args.maximum_fps,
                    gpu_target_percent=args.ls_gpu_target,
                    minimum_framerate_limit=args.minimum_fps,
                    maximum_framerate_limit=args.maximum_fps,
                ),
            )
            limiter_manager.config.profiles = [limiter_profile]
            limiter_manager.config.active_profile_id = limiter_profile.id
            limiter_manager.config.lossless_scaling_exe_path = manager.config.lossless_scaling_exe_path
            limiter_manager.config.rtss_frame_limiting_enabled = True
            limiter_manager.config.rtss_install_path = args.rtss_path or manager.config.rtss_install_path
            limiter_manager.save_config()
            limiter_state = AppState()
            limiter_state.is_scaling_active = True
            limiter_state.current_active_profile = limiter_profile
            limiter_state.current_scaled_target = {
                "pid": workload.pid,
                "processName": benchmark_exe.name,
                "hwnd": hwnd,
            }
            limiter = DynamicLimiterController(limiter_manager, limiter_state, rtss)
            limiter._lossless_pid = lossless_pid
            limiter_state.scaling_target_hwnd = hwnd
        else:
            report["default_profile_rtss"] = {
                "enabled": False,
                "settings_adjusted": False,
            }

        capture = PresentMonCapture(presentmon_exe, run_dir, qpc_time_ms=args.dynamic_limiter)
        process_names = [benchmark_exe.name]
        if lossless_pid:
            process_names.append("LosslessScaling.exe")
        if args.adaptive_acceptance:
            from companion.services.adaptive_benchmark import monitoring_comparison, cap_response_check
            from companion.services.addon_host import presentation_status
            initial_host = presentation_status(manager.config.lossless_scaling_exe_path, instance)
            monitoring_restore = (manager.config.lossless_scaling_exe_path, instance, initial_host["presentationPacing"]["enabled"])
            presentation_status(manager.config.lossless_scaling_exe_path, instance, enabled=True)
            # Establish the production binding and initial cap before fixed-cap A/B.
            limiter.poll()
            if limiter._session is None:
                raise RuntimeError("Adaptive benchmark could not establish source/LS identity")
            limiter.shadow_only = True
            limiter._session.policy.shadow_only = True
            def monitoring_progress(index, enabled, pacing):
                if enabled:
                    limiter.poll()
                if progress_callback:
                    progress_callback({"type": "DYNAMIC_LIMITER_PROGRESS", "sample": {
                        "state": "monitoring_comparison", "message": f"Monitoring {'on' if enabled else 'off'}: phase {index+1}/4", "pacing": pacing}})
            report["monitoring_overhead"] = monitoring_comparison(
                executable=manager.config.lossless_scaling_exe_path, instance=instance,
                binding=limiter._session.binding, presentmon=presentmon_exe, directory=run_dir,
                source_pid=workload.pid, process_names=process_names,
                fixed_cap=limiter._session.current_limit, rtss=rtss, profile=limiter_profile,
                phase_seconds=args.monitoring_phase_seconds, cancelled=lambda: workload.poll() is not None,
                sample_callback=monitoring_progress)
            report["cap_response"] = cap_response_check(rtss=rtss, profile=limiter_profile,
                presentmon=presentmon_exe, directory=run_dir, process_names=process_names,
                source_pid=workload.pid, fixed_cap=limiter._session.current_limit,
                minimum=args.minimum_fps, baseline_phases=report["monitoring_overhead"].get("phases", []))
            limiter.shadow_only = False
            # The adaptive arm starts with fresh history, and does not write during ABBA.
            limiter.clear_sessions()
        capture.start(process_names, args.duration)
        recorder = GpuSampleRecorder(process_ids)
        recorder.start()
        deadline = time.monotonic() + args.duration
        capture_started = time.monotonic()
        probe_interval = (
            max(0.25, (args.duration - args.warmup) / args.input_probes)
            if args.input_probes
            else 0.0
        )
        next_probe = capture_started + args.warmup + probe_interval / 2
        while time.monotonic() < deadline:
            if workload.poll() is not None:
                raise RuntimeError("Benchmark workload exited during capture")
            if limiter:
                status = limiter.poll()
                readback = limiter_profile_readback(rtss, limiter_profile)
                report["limiter_status_samples"].append(
                    {"elapsed_seconds": args.duration - max(0.0, deadline - time.monotonic()), **status,
                     "rtss_readback_limit": readback,
                     "rtss_profile_verified": readback is not None and readback == status.get("current_limit")}
                )
                if progress_callback:
                    progress_callback({"type": "DYNAMIC_LIMITER_PROGRESS", "sample": report["limiter_status_samples"][-1],
                                       "targetPercent": args.ls_gpu_target, "workloadResolution": report["workload_resolution"]})
            now = time.monotonic()
            if (
                not bundled_workload
                and args.input_probes
                and len(report["input_probe_events"]) < args.input_probes
                and now >= next_probe
            ):
                focused = focus_window(hwnd, timeout=0.5)
                sent = focused and InputSimulator.trigger_hotkey([], "f15", hold_ms=30)
                marker = {"sent": sent, "latency_ms": None, "color": None}
                report["input_probe_events"].append(
                    {
                        "elapsed_seconds": time.monotonic() - capture_started,
                        "foreground_verified": focused,
                        **marker,
                    }
                )
                next_probe += probe_interval
            time.sleep(min(1.0, max(0.05, deadline - time.monotonic())))
        capture.wait(timeout=15)
        recorder.stop()
        if bundled_workload and args.input_probes:
            f15 = InputSimulator.vk_from_string("f15")
            for _index in range(args.input_probes):
                focused = focus_window(hwnd, timeout=0.5)

                def trigger_marker() -> bool:
                    try:
                        InputSimulator.send_scancode_event(f15, keyup=False)
                        InputSimulator.send_scancode_event(f15, keyup=True)
                        return True
                    except Exception:
                        return False

                marker = (
                    measure_visible_marker(output_hwnd, trigger_marker)
                    if focused
                    else {"sent": False, "latency_ms": None, "color": None}
                )
                report["input_probe_events"].append(
                    {
                        "elapsed_seconds": time.monotonic() - capture_started,
                        "foreground_verified": focused,
                        **marker,
                    }
                )
                time.sleep(0.15)
        report["gpu_samples"] = recorder.samples
        report["gpu_utilization"] = _gpu_summary(recorder.samples, workload.pid, lossless_pid)
        report["presentmon_captures"] = summarize_presentmon_files(
            capture.csv_files(), warmup_seconds=args.warmup
        )
        source_capture = next(
            (
                item for item in report["presentmon_captures"]
                if item.get("process_id") == workload.pid
            ),
            None,
        )
        input_count = (
            source_capture["metrics"]["all_input_to_visible_ms"]["count"]
            if source_capture
            else 0
        )
        sent_probes = sum(
            1 for event in report["input_probe_events"] if event.get("sent")
        )
        visible_values = [
            event["latency_ms"] for event in report["input_probe_events"]
            if event.get("latency_ms") is not None
        ]
        report["software_visible_latency_ms"] = summarize_values(visible_values)
        report["input_probe_qualification"] = {
            "requested": args.input_probes,
            "sent": sent_probes,
            "presentmon_source_samples": input_count,
            "software_visible_samples": len(visible_values),
            "available": bool(input_count or visible_values),
            "note": (
                "A false result means neither PresentMon nor the composed-output marker "
                "produced a valid latency sample; no latency value should be inferred."
            ),
        }
        game_median = report["gpu_utilization"]["benchmark"]["median"]
        report["workload_qualification"] = {
            "threshold_percent": args.saturation_threshold,
            "qualified": bool(game_median is not None and game_median >= args.saturation_threshold),
            "observed_median_percent": game_median,
        }
        if limiter and limiter_profile:
            report["dynamic_limiter_result"] = limiter_profile.rtss.model_dump()
            from companion.services.adaptive_benchmark import summarize_adaptive
            convergence = summarize_adaptive(report["limiter_status_samples"], duration=args.duration,
                                             warmup=args.warmup, overhead=report.get("monitoring_overhead"),
                                             csv_paths=capture.csv_files(), source_pid=workload.pid, ls_pid=lossless_pid, cap_response=report.get("cap_response"))
            # Keep the legacy headroom result as descriptive evidence, never as
            # the LS-performance acceptance gate.
            convergence["headroom_observation"] = summarize_dynamic_limiter(
                report["limiter_status_samples"], target_percent=args.ls_gpu_target,
                duration_seconds=args.duration, warmup_seconds=args.warmup,
                settling_seconds=args.settling_seconds, tolerance_percent=args.target_tolerance)
            report["dynamic_limiter_benchmark"] = convergence
            if convergence["status"] != "pass":
                exit_code = 4
    except KeyboardInterrupt:
        report["interrupted"] = True
        exit_code = 1
    except Exception as error:
        report["error"] = str(error)
        print(f"Benchmark failed: {error}", file=sys.stderr)
        exit_code = 2
    finally:
        if recorder:
            recorder.stop()
        if limiter:
            try:
                limiter.close()
            except Exception as error:
                report["limiter_cleanup_error"] = str(error)
                exit_code = max(exit_code, 3)
        if rtss and limiter_profile and limiter_profile.rtss.managed_target_process:
            try:
                report["rtss_profile_restored"] = rtss.remove_profile(limiter_profile)
            except Exception as error:
                report["rtss_restore_error"] = str(error)
                exit_code = max(exit_code, 3)
        if capture:
            capture.stop()
        if monitoring_restore:
            try:
                from companion.services.addon_host import presentation_status
                exe, original_instance, original_enabled = monitoring_restore
                presentation_status(exe, original_instance, enabled=original_enabled)
                report["monitoring_state_restored"] = True
            except Exception as error:
                report["monitoring_restore_error"] = str(error)
                exit_code = max(exit_code, 3)
        if scaling_may_have_started:
            try:
                overlay_active = inspector.detect_lossless_scaling_overlay_window()[0]
                if overlay_active and focus_window(report["benchmark_hwnd"]):
                    stopped = automation.set_scaling(
                        False,
                        reason="performance_benchmark_cleanup",
                        force=True,
                        park_runtime_on_stop=False,
                    )
                    if not stopped or inspector.detect_lossless_scaling_overlay_window()[0]:
                        raise RuntimeError("Lossless Scaling did not confirm benchmark cleanup")
            except Exception as error:
                report["scaling_cleanup_error"] = str(error)
                exit_code = max(exit_code, 3)
        if workload and workload.poll() is None:
            workload.terminate()
            try:
                workload.wait(timeout=5)
            except subprocess.TimeoutExpired:
                workload.kill()
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        report["exit_code"] = exit_code
        _write_json(run_dir / "result.json", report)

    print(f"Report: {run_dir / 'result.json'}")
    qualification = report.get("workload_qualification")
    if qualification and not qualification["qualified"]:
        print(
            "WARNING: workload did not meet the configured GPU saturation threshold; "
            "do not use this run to judge limiter convergence."
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
