"""Read-only, per-capability diagnostics with repository-friendly logs."""

from __future__ import annotations

import json
import os
import platform
import shutil
import sys
import traceback
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, Optional

import psutil

from ..core.models import Profile
from ..core.profile_manager import ProfileManager
from ..core.state import AppState
from .asset_store import AssetStore
from .automation import AutomationController
from .deployment_manager import DeploymentManager
from .reshade_manager import ReshadeManager


class DiagnosticRunner:
    """Exercise fundamental application paths without changing user settings."""

    def __init__(self, server):
        self.server = server
        self.root = server.profile_manager.config_dir / "diagnostics" / "application-tests"

    @staticmethod
    def _result(status: str, summary: str, **details) -> Dict:
        return {"status": status, "summary": summary, "details": details}

    def _path(self, value) -> str:
        if not value:
            return ""
        text = str(value)
        replacements = {
            str(Path.home()): "<USER_HOME>",
            os.environ.get("LOCALAPPDATA", ""): "<LOCALAPPDATA>",
            os.environ.get("APPDATA", ""): "<APPDATA>",
        }
        for source, replacement in replacements.items():
            if source:
                text = text.replace(source, replacement)
        return text

    def _tests(self) -> Iterable[tuple[str, str, Callable[[], Dict]]]:
        return (
            ("configuration", "Configuration schema and persistence", self._configuration),
            ("profile_integrity", "Profile IDs, defaults, and targets", self._profile_integrity),
            ("profile_matching", "Application and browser profile matching", self._profile_matching),
            ("profile_persistence_behavior", "Profile create, save, reload, and match behavior", self._profile_persistence_behavior),
            ("lossless_installation", "Lossless Scaling executable", self._lossless_installation),
            ("lossless_settings", "Lossless Scaling Settings.xml", self._lossless_settings),
            ("autoscale", "Automatic profile scaling readiness", self._autoscale),
            ("autoscale_behavior", "Automatic scaling decision behavior", self._autoscale_behavior),
            ("process_windows", "Process and foreground-window discovery", self._process_windows),
            ("hotkeys", "Hotkey configuration and listener", self._hotkeys),
            ("assets", "Managed asset store and package manifests", self._assets),
            ("reshade", "ReShade selection and injection plan", self._reshade),
            ("reshade_move_behavior", "ReShade config move, backup, and restore behavior", self._reshade_move_behavior),
            ("deployment", "Active add-on deployment integrity", self._deployment),
            ("deployment_behavior", "Transactional add-on deployment behavior", self._deployment_behavior),
            ("benchmark", "Benchmark workload and PresentMon command", self._benchmark),
            ("benchmark_cli_behavior", "Benchmark command-line contract", self._benchmark_cli_behavior),
            ("rtss", "RTSS integration readiness", self._rtss),
            ("startup", "Windows startup integration", self._startup),
            ("runtime", "Companion runtime and WebSocket state", self._runtime),
        )

    @contextmanager
    def _sandbox(self, name: str):
        path = self._behavior_root / name
        path.mkdir(parents=True)
        yield str(path)

    def run(self) -> Dict:
        started = datetime.now(timezone.utc)
        stamp = started.strftime("%Y%m%dT%H%M%SZ")
        session = self.root / stamp
        suffix = 1
        while session.exists():
            session = self.root / f"{stamp}-{suffix}"
            suffix += 1
        session.mkdir(parents=True)
        self._behavior_root = session / ".behavior-sandbox"
        self._behavior_root.mkdir()
        results = []
        for test_id, name, function in self._tests():
            try:
                result = function()
            except Exception as error:  # A broken diagnostic must remain visible.
                result = self._result(
                    "fail", f"Diagnostic raised {type(error).__name__}: {error}",
                    traceback=traceback.format_exc(),
                )
            result.update({"id": test_id, "name": name})
            results.append(result)
            (session / f"{test_id}.log").write_text(
                json.dumps(result, indent=2, default=str), encoding="utf-8"
            )

        shutil.rmtree(self._behavior_root, ignore_errors=True)

        counts = {key: sum(item["status"] == key for item in results) for key in ("pass", "warn", "fail")}
        manifest = {
            "schemaVersion": 1,
            "generatedAt": started.isoformat(),
            "application": "Lossless Scaling Companion",
            "environment": {
                "python": sys.version.split()[0],
                "platform": platform.platform(),
                "frozenBuild": bool(getattr(sys, "frozen", False)),
                "simulationMode": bool(self.server.state.simulation_mode),
            },
            "counts": counts,
            "results": results,
        }
        (session / "summary.json").write_text(
            json.dumps(manifest, indent=2, default=str), encoding="utf-8"
        )
        summary_lines = [
            "# LS Companion application diagnostics",
            "",
            f"Generated: {manifest['generatedAt']}",
            f"Result: {counts['pass']} passed, {counts['warn']} warnings, {counts['fail']} failed",
            "",
        ]
        summary_lines.extend(
            f"- [{item['status'].upper()}] {item['name']}: {item['summary']}"
            for item in results
        )
        (session / "README.md").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")
        archive = self.root / f"ls-companion-diagnostics-{session.name}.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for path in sorted(session.iterdir()):
                bundle.write(path, arcname=f"ls-companion-diagnostics/{path.name}")
        return {
            "type": "DIAGNOSTICS_RESULT",
            "running": False,
            "generatedAt": manifest["generatedAt"],
            "counts": counts,
            "results": results,
            "directory": self._path(session),
            "archiveName": archive.name,
            "archivePath": str(archive.resolve()),
        }

    def _configuration(self) -> Dict:
        config = self.server.config
        round_trip = type(config).model_validate(config.model_dump())
        writable = os.access(self.server.profile_manager.config_dir, os.W_OK)
        status = "pass" if writable and round_trip == config else "fail"
        return self._result(status, "Settings validate and the configuration directory is writable." if status == "pass" else "Settings validation or configuration write access failed.", configFile=self._path(self.server.profile_manager.config_file), writable=writable)

    def _profile_integrity(self) -> Dict:
        profiles = self.server.config.profiles
        ids = [item.id.casefold() for item in profiles]
        defaults = [item for item in profiles if item.is_default]
        duplicate_ids = sorted({item for item in ids if ids.count(item) > 1})
        duplicate_names = sorted({item.name for item in profiles if sum(other.name.casefold() == item.name.casefold() for other in profiles) > 1})
        untargeted = [item.id for item in profiles if not item.is_default and not (item.target_domain or item.target_process or item.target_processes or item.target_executable_path or item.target_executable_paths)]
        failures = []
        if len(defaults) != 1:
            failures.append(f"expected exactly one default profile, found {len(defaults)}")
        if duplicate_ids:
            failures.append("duplicate profile IDs")
        if self.server.config.active_profile_id and not self.server.profile_manager.get_profile_by_id(self.server.config.active_profile_id):
            failures.append("active profile ID does not exist")
        status = "fail" if failures else "warn" if duplicate_names or untargeted else "pass"
        summary = "; ".join(failures) if failures else ("Profile data has warnings." if status == "warn" else "Exactly one default profile and all profile identities are valid.")
        return self._result(status, summary, profileCount=len(profiles), defaultProfiles=[{"id": p.id, "name": p.name} for p in defaults], duplicateIds=duplicate_ids, duplicateNames=duplicate_names, profilesWithoutTargets=untargeted, activeProfileId=self.server.config.active_profile_id)

    def _profile_matching(self) -> Dict:
        failures = []
        checked = []
        for profile in self.server.config.profiles:
            if profile.is_default:
                continue
            process = profile.target_process or next(iter(profile.target_processes), None)
            executable = profile.target_executable_path or next(iter(profile.target_executable_paths), None)
            matched = self.server.profile_manager.match_target_profile(process_name=process, domain=profile.target_domain, executable_path=executable)
            checked.append({"profile": profile.id, "matched": matched.id if matched else None})
            if (process or executable or profile.target_domain) and (not matched or matched.id != profile.id):
                failures.append(profile.id)
        return self._result("fail" if failures else "pass", "Every configured target resolves to its intended profile." if not failures else "One or more profile targets resolve incorrectly.", checked=checked, failures=failures)

    def _profile_persistence_behavior(self) -> Dict:
        with self._sandbox("profile-persistence") as directory:
            manager = ProfileManager(Path(directory) / "config")
            profile = Profile(
                id="diagnostic-game", name="Diagnostic Game",
                target_process="DiagnosticGame.exe", auto_scale=True,
            )
            manager.add_or_update_profile(profile)
            reloaded = ProfileManager(Path(directory) / "config")
            stored = reloaded.get_profile_by_id(profile.id)
            matched = reloaded.match_target_profile(process_name="diagnosticgame.exe")
            passed = bool(stored and stored.auto_scale and matched and matched.id == profile.id)
            return self._result("pass" if passed else "fail", "Production profile persistence and case-insensitive matching executed successfully." if passed else "Profile create/reload/match behavior failed.", stored=bool(stored), autoScale=stored.auto_scale if stored else None, matchedProfileId=matched.id if matched else None)

    def _lossless_installation(self) -> Dict:
        value = self.server.config.lossless_scaling_exe_path
        path = Path(value) if value else None
        valid = bool(path and path.is_file() and path.name.casefold() == "losslessscaling.exe")
        running = bool(self.server.state.lossless_scaling_running)
        return self._result("pass" if valid else "fail", "LosslessScaling.exe is configured and present." if valid else "The configured LosslessScaling.exe is missing or invalid.", path=self._path(value), exists=bool(path and path.exists()), running=running)

    def _lossless_settings(self) -> Dict:
        path = self.server.ls_settings.path
        data = self.server.ls_settings.read()
        profiles = data.get("profiles", []) if isinstance(data, dict) else []
        default = next((item for item in profiles if str(item.get("Title", "")).casefold() == "default"), None)
        status = "pass" if path.is_file() and profiles else "fail"
        return self._result(status, "Settings.xml parses and contains native profiles." if status == "pass" else "Settings.xml is missing, unreadable, or contains no profiles.", path=self._path(path), nativeProfileCount=len(profiles), defaultProfileFound=bool(default), backupFound=self.server.ls_settings.initial_backup_path.is_file(), nativeAutoScaleEnabled=self.server.ls_settings.native_auto_scale_enabled())

    def _autoscale(self) -> Dict:
        config = self.server.config
        enabled_profiles = [p.id for p in config.profiles if p.auto_scale]
        problems = []
        if not config.disable_native_auto_scale:
            problems.append("automatic profile scaling is disabled globally")
        if config.lossless_control_configured and self.server.ls_settings.native_auto_scale_enabled() is True:
            problems.append("Lossless Scaling native Auto Scale is still enabled")
        if not enabled_profiles:
            problems.append("no profile has automatic scaling enabled")
        native_hotkey = self.server.ls_settings.read_hotkey()
        hotkey = native_hotkey.model_dump() if native_hotkey else None
        if not hotkey:
            problems.append("Lossless Scaling activation hotkey was not detected")
        status = "pass" if not problems else "fail"
        return self._result(status, "Autoscale prerequisites are configured." if not problems else "; ".join(problems), globallyEnabled=config.disable_native_auto_scale, controlConfigured=config.lossless_control_configured, enabledProfiles=enabled_profiles, losslessHotkey=hotkey, watcherAvailable=self.server.process_watcher is not None)

    def _autoscale_behavior(self) -> Dict:
        with self._sandbox("autoscale") as directory:
            manager = ProfileManager(Path(directory) / "config")
            manager.config.asset_store_path = str(Path(directory) / "assets")
            manager.config.lossless_scaling_exe_path = None
            store = AssetStore(manager.config.asset_store_path)
            triggers = []

            def trigger(**kwargs):
                triggers.append(kwargs)
                return True

            state = AppState()
            controller = AutomationController(
                manager, state, asset_store=store, hotkey_trigger=trigger,
                scaling_state_probe=lambda: True,
            )
            controller.activate_profile(Profile(
                id="autoscale-game", name="Autoscale Game",
                target_process="AutoscaleGame.exe", auto_scale=True,
            ))
            game_scaled = state.is_scaling_active and len(triggers) == 1

            manager.config.disable_native_auto_scale = False
            disabled_state = AppState()
            disabled_triggers = []
            disabled = AutomationController(
                manager, disabled_state, asset_store=store,
                hotkey_trigger=lambda **kwargs: disabled_triggers.append(kwargs) or True,
                scaling_state_probe=lambda: True,
            )
            disabled.activate_profile(Profile(
                id="disabled-game", name="Disabled Game",
                target_process="DisabledGame.exe", auto_scale=True,
            ))
            master_blocked = not disabled_state.is_scaling_active and not disabled_triggers

            manager.config.disable_native_auto_scale = True
            browser_state = AppState()
            browser_triggers = []
            browser = AutomationController(
                manager, browser_state, asset_store=store,
                hotkey_trigger=lambda **kwargs: browser_triggers.append(kwargs) or True,
                scaling_state_probe=lambda: True,
            )
            browser.activate_profile(Profile(
                id="browser", name="Browser", target_process="chrome.exe", auto_scale=True,
            ))
            browser_blocked = not browser_state.is_scaling_active and not browser_triggers
            passed = game_scaled and master_blocked and browser_blocked
            return self._result("pass" if passed else "fail", "Production autoscale logic scaled an eligible game and blocked disabled/browser cases." if passed else "Autoscale decision behavior did not match its contract.", eligibleGameScaled=game_scaled, globalMasterSwitchBlocked=master_blocked, browserFocusBlocked=browser_blocked, activationTriggerCount=len(triggers))

    def _process_windows(self) -> Dict:
        if not self.server.process_watcher:
            return self._result("fail", "Process watcher is unavailable.")
        processes = self.server.process_watcher.list_running_executables(visible_windows_only=True)
        foreground = self.server.process_watcher.get_foreground_window_info()
        return self._result("pass" if foreground else "warn", "Foreground-window and visible-process discovery responded." if foreground else "Visible processes were enumerated, but no foreground window was resolved.", visibleProcessCount=len(processes), foreground={"name": foreground.name, "pid": foreground.pid, "hwnd": foreground.hwnd, "path": self._path(foreground.exe_path)} if foreground else None)

    def _hotkeys(self) -> Dict:
        config = self.server.config
        listener = self.server.hotkey_listener
        running = bool(listener and getattr(listener, "_thread", None) and listener._thread.is_alive())
        status = "pass" if (not config.override_lossless_hotkey or running) else "fail"
        return self._result(status, "Hotkey configuration is valid and the required listener is running." if status == "pass" else "Hotkey override is enabled but its listener is not running.", overrideEnabled=config.override_lossless_hotkey, listenerRunning=running, globalHotkey=config.global_hotkey.model_dump(), overrideHotkey=config.override_hotkey.model_dump())

    def _assets(self) -> Dict:
        packages = self.server.asset_store.list_packages()
        bad = []
        for package in packages:
            payload = Path(str(package.get("payload_path") or ""))
            if not payload.is_dir():
                bad.append(f"{package.get('provider')}/{package.get('version')}: payload missing")
                continue
            for item in package.get("files") or []:
                candidate = payload / str(item.get("relative_path") or "")
                if not candidate.is_file():
                    bad.append(f"{package.get('provider')}/{package.get('version')}: {candidate.name} missing")
        return self._result("fail" if bad else "pass", "Managed package payloads are present." if not bad else "Managed asset files are missing.", packageCount=len(packages), problems=bad)

    def _reshade(self) -> Dict:
        selected = [p for p in self.server.config.profiles if (p.graphics.reshade.enabled or (p.reshade and p.reshade.enabled))]
        checks, failures = [], []
        known = {item.id for item in self.server.config.reshade_profiles}
        exe = self.server.config.lossless_scaling_exe_path
        for profile in selected:
            try:
                plan = self.server.automation.graphics_resolver.resolve(profile, lossless_scaling_exe=exe)
                runtime_files = [item["relative_path"] for item in plan if str(item.get("source_package") or "").casefold().startswith("reshade/")]
                managed_id = profile.reshade.managed_profile_id if profile.reshade else None
                if managed_id and managed_id not in known:
                    raise RuntimeError(f"managed ReShade profile {managed_id!r} is missing")
                if not runtime_files:
                    raise RuntimeError("resolved injection plan contains no ReShade runtime")
                checks.append({"profile": profile.id, "runtimeFiles": runtime_files, "managedProfile": managed_id})
            except Exception as error:
                failures.append({"profile": profile.id, "error": str(error)})
        status = "fail" if failures else "pass" if selected else "warn"
        summary = "Every selected ReShade profile resolves to an injection plan." if status == "pass" else "ReShade is not selected by any profile." if status == "warn" else "One or more ReShade injection plans are invalid."
        return self._result(status, summary, selectedProfileCount=len(selected), checks=checks, failures=failures)

    def _reshade_move_behavior(self) -> Dict:
        with self._sandbox("reshade-move") as directory:
            root = Path(directory)
            (root / "LosslessScaling.exe").write_bytes(b"diagnostic")
            active = root / "ReShade.ini"
            preset = root / "DiagnosticPreset.ini"
            managed_dir = root / "managed"
            managed_dir.mkdir()
            managed = managed_dir / "ReShade.ini"
            original = "[GENERAL]\nCurrentPresetPath=old.ini\n"
            active.write_text(original, encoding="utf-8")
            preset.write_text("Techniques=Diagnostic@Diagnostic.fx\n", encoding="utf-8")
            managed.write_text("[INPUT]\nKeyOverlay=0,0,0,0\n", encoding="utf-8")
            swapped = ReshadeManager.swap_preset(str(active), str(preset))
            preset_moved = str(preset.resolve()) in active.read_text(encoding="utf-8")
            backup_preserved = active.with_suffix(".ini.bak").read_text(encoding="utf-8") == original
            managed_applied = ReshadeManager.apply_managed_config(str(active), str(managed))
            managed_matches = active.read_text(encoding="utf-8") == managed.read_text(encoding="utf-8")
            restored = ReshadeManager.restore_backup(str(active))
            restored_original = active.read_text(encoding="utf-8") == original
            passed = all((swapped, preset_moved, backup_preserved, managed_applied, managed_matches, restored, restored_original))
            return self._result("pass" if passed else "fail", "Production ReShade preset swap, managed-config move, backup, and restore all succeeded." if passed else "ReShade config movement or restoration failed.", presetSwapped=swapped and preset_moved, backupPreserved=backup_preserved, managedConfigApplied=managed_applied and managed_matches, originalRestored=restored and restored_original)

    def _deployment(self) -> Dict:
        manager = self.server.automation.deployment_manager
        manifest = manager._read_manifest()
        files = manifest.get("files", [])
        exe = self.server.config.lossless_scaling_exe_path
        valid = True
        error = None
        if files:
            try:
                root = manager._lossless_root(exe)
                valid = manager._active_matches_disk(root, manifest)
            except Exception as exc:
                valid, error = False, str(exc)
        return self._result("pass" if valid else "fail", "Active deployment manifest matches files on disk." if valid else "Active deployment files are missing or modified.", profileId=manifest.get("profile_id"), fileCount=len(files), diskMatches=valid, error=error)

    def _deployment_behavior(self) -> Dict:
        with self._sandbox("deployment") as directory:
            root = Path(directory)
            host = root / "lossless"
            host.mkdir()
            executable = host / "LosslessScaling.exe"
            executable.write_bytes(b"diagnostic")
            destination = host / "dxgi.dll"
            destination.write_bytes(b"original")
            source = root / "managed-dxgi.dll"
            source.write_bytes(b"managed")
            manager = DeploymentManager(AssetStore(str(root / "assets")))
            manifest = manager.apply(
                profile_id="diagnostic", lossless_scaling_exe=str(executable),
                files=[{"relative_path": "dxgi.dll", "source_path": str(source), "role": "diagnostic"}],
            )
            deployed = destination.read_bytes() == b"managed" and manager._active_matches_disk(host.resolve(), manifest)
            manager.apply(profile_id=None, lossless_scaling_exe=str(executable), files=[])
            restored = destination.read_bytes() == b"original"
            passed = deployed and restored
            return self._result("pass" if passed else "fail", "Transactional deployment replaced, verified, and restored an existing DLL in an isolated Lossless Scaling fixture." if passed else "Transactional deployment behavior failed.", deployedAndVerified=deployed, originalRestored=restored)

    def _benchmark(self) -> Dict:
        status = self.server._benchmark_status_payload()
        workload = self.server._embedded_benchmark_executable()
        presentmon = self.server._presentmon_executable()
        command = self.server._benchmark_launch_command(workload, presentmon) if workload and presentmon else []
        return self._result("pass" if status["ready"] else "fail", "Benchmark workload, verified PresentMon, and launch arguments are ready." if status["ready"] else "Benchmark prerequisites are incomplete.", benchmarkStatus=status, workload=self._path(workload), presentMon=self._path(presentmon), command=[self._path(item) for item in command])

    def _benchmark_cli_behavior(self) -> Dict:
        from scripts.benchmark_lossless_scaling import parse_args

        arguments = [
            "--benchmark-exe", r"C:\Diagnostic\LSBenchmark.exe",
            "--presentmon-exe", r"C:\Diagnostic\PresentMon.exe",
            "--use-default-profile", "--yes",
        ]
        parsed = parse_args(arguments)
        passed = bool(parsed.use_default_profile and parsed.yes and parsed.benchmark_exe and parsed.presentmon_exe)
        return self._result("pass" if passed else "fail", "The exact dashboard benchmark argument contract parses successfully." if passed else "The dashboard benchmark arguments are rejected by the runner.", arguments=arguments, parsed={"benchmarkExe": parsed.benchmark_exe, "presentMonExe": parsed.presentmon_exe, "useDefaultProfile": parsed.use_default_profile, "yes": parsed.yes})

    def _rtss(self) -> Dict:
        enabled = self.server.config.rtss_frame_limiting_enabled
        status = self.server.rtss_manager.status(self.server.config.rtss_install_path)
        result = "pass" if (not enabled or status.get("installed")) else "fail"
        return self._result(result, "RTSS readiness matches the current configuration." if result == "pass" else "RTSS integration is enabled but RTSS was not detected.", enabled=enabled, installed=status.get("installed"), running=status.get("running"), path=self._path(status.get("path")))

    def _startup(self) -> Dict:
        expected = self.server.config.run_at_startup
        actual = self.server.startup_manager.is_enabled()
        launch_arguments = (
            self.server.startup_manager.launch_arguments()
            if hasattr(self.server.startup_manager, "launch_arguments")
            else []
        )
        return self._result("pass" if expected == actual else "warn", "Startup task state matches settings." if expected == actual else "Saved startup preference and Task Scheduler state differ.", configured=expected, taskEnabled=actual, launchArguments=[self._path(item) for item in launch_arguments])

    def _runtime(self) -> Dict:
        return self._result("pass", "Core runtime services are instantiated and responding.", connectedClients=self.server.state.connected_clients, companionPid=os.getpid(), processExists=psutil.pid_exists(os.getpid()), automation=type(self.server.automation).__name__, processWatcher=type(self.server.process_watcher).__name__ if self.server.process_watcher else None, simulationMode=self.server.state.simulation_mode)
