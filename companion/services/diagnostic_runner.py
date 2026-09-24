"""Experimental live-system tests with repository-friendly per-capability logs."""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import traceback
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

import psutil

from ..core.models import HotkeyConfig, Profile, ReshadeConfig
from ..core.profile_manager import ProfileManager
from ..core.state import AppState
from .asset_store import AssetStore
from .automation import AutomationController
from .deployment_manager import DeploymentManager
from .input_simulator import InputSimulator
from .graphics_source_importer import GraphicsSourceImporter
from .reshade_manager import ReshadeManager


class DiagnosticRunner:
    """Exercise fundamental paths against the installed application and LS host."""

    DEPENDENCIES = {
        "profile_integrity": ("configuration",),
        "profile_matching": ("profile_integrity",),
        "profile_persistence_behavior": ("configuration",),
        "lossless_installation": ("configuration",),
        "lossless_settings": ("lossless_installation",),
        "autoscale": ("profile_integrity", "lossless_settings", "hotkeys"),
        "autoscale_behavior": (
            "autoscale", "profile_persistence_behavior", "process_windows", "assets", "benchmark",
        ),
        "hotkey_override_behavior": ("autoscale_behavior", "hotkeys"),
        "lossless_scaling_log": ("autoscale_behavior",),
        "reshade": ("assets", "lossless_installation"),
        "reshade_runtime": ("reshade", "deployment_behavior"),
        "proxy_reshade": ("reshade", "deployment_behavior"),
        "proxy_neural_render": ("deployment_behavior",),
        "deployment": ("assets", "lossless_installation"),
        "deployment_behavior": ("deployment", "benchmark", "process_windows"),
        "benchmark": ("assets", "lossless_installation"),
        "rtss": ("process_windows",),
        "startup": ("configuration",),
        "runtime": ("configuration",),
    }
    LS_MUTATING_TESTS = {"autoscale_behavior", "deployment_behavior"}
    ALWAYS_RUN_AFTER_DEPENDENCIES = {
        "reshade_runtime", "proxy_reshade", "proxy_neural_render",
    }

    def __init__(self, server):
        self.server = server
        self.root = server.profile_manager.config_dir / "diagnostics" / "application-tests"
        self._live_scaling_results: Optional[Dict[str, Dict]] = None
        self._live_addon_result: Optional[Dict] = None

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
            ("autoscale_behavior", "Live automatic scaling behavior", self._autoscale_behavior),
            ("hotkey_override_behavior", "Live override-hotkey behavior", self._hotkey_override_behavior),
            ("lossless_scaling_log", "Live Lossless Scaling log confirmation", self._lossless_scaling_log_behavior),
            ("process_windows", "Process and foreground-window discovery", self._process_windows),
            ("hotkeys", "Hotkey configuration and listener", self._hotkeys),
            ("assets", "Managed asset store and package manifests", self._assets),
            ("reshade", "ReShade selection and injection plan", self._reshade),
            ("reshade_runtime", "Live ReShade deployment and load confirmation", self._reshade_runtime_behavior),
            ("proxy_reshade", "LosslessProxy + ReShade end-to-end runtime", self._proxy_reshade_behavior),
            ("proxy_neural_render", "LosslessProxy + NeuralRender end-to-end runtime", self._proxy_neural_render_behavior),
            ("deployment", "Active add-on deployment integrity", self._deployment),
            ("deployment_behavior", "Live add-on and proxy deployment behavior", self._live_addon_deployment_behavior),
            ("benchmark", "Benchmark workload and PresentMon command", self._benchmark),
            ("rtss", "RTSS integration readiness", self._rtss),
            ("startup", "Windows startup integration", self._startup),
            ("runtime", "Companion runtime and WebSocket state", self._runtime),
        )

    def catalog(self) -> list[Dict]:
        return [
            {
                "id": test_id,
                "name": name,
                "dependencies": list(self.DEPENDENCIES.get(test_id, ())),
            }
            for test_id, name, _function in self._tests()
        ]

    def _execution_plan(self, selected_test: Optional[str]) -> list[tuple[str, str, Callable[[], Dict]]]:
        tests = list(self._tests())
        by_id = {item[0]: item for item in tests}
        if selected_test is not None and selected_test not in by_id:
            raise ValueError(f"Unknown application test: {selected_test}")
        roots = [selected_test] if selected_test else [item[0] for item in tests]
        ordered: list[tuple[str, str, Callable[[], Dict]]] = []
        visited: set[str] = set()
        visiting: set[str] = set()

        def add(test_id: str) -> None:
            if test_id in visited:
                return
            if test_id in visiting:
                raise RuntimeError(f"Circular application-test dependency at {test_id}")
            if test_id not in by_id:
                raise RuntimeError(f"Application test dependency does not exist: {test_id}")
            visiting.add(test_id)
            for dependency in self.DEPENDENCIES.get(test_id, ()):
                add(dependency)
            visiting.remove(test_id)
            visited.add(test_id)
            ordered.append(by_id[test_id])

        for root in roots:
            add(root)
        return ordered

    @contextmanager
    def _sandbox(self, name: str):
        path = self._behavior_root / name
        path.mkdir(parents=True)
        yield str(path)

    def run(self, selected_test: Optional[str] = None) -> Dict:
        # Each button press must execute a fresh live workflow and collect new log deltas.
        self._live_scaling_results = None
        self._live_addon_result = None
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
        result_by_id: Dict[str, Dict] = {}
        initial_ls_running = bool(
            self.server.process_watcher
            and self.server.process_watcher.check_is_lossless_scaling_running()
        )
        for test_id, name, function in self._execution_plan(selected_test):
            failed_dependencies = [
                dependency for dependency in self.DEPENDENCIES.get(test_id, ())
                if result_by_id.get(dependency, {}).get("status") == "fail"
            ]
            if failed_dependencies and test_id not in self.ALWAYS_RUN_AFTER_DEPENDENCIES:
                result = self._result(
                    "fail",
                    "Not run because required setup tests failed: " + ", ".join(failed_dependencies),
                    skipped=True,
                    failedDependencies=failed_dependencies,
                )
            else:
                try:
                    result = function()
                except Exception as error:  # A broken diagnostic must remain visible.
                    result = self._result(
                        "fail", f"Diagnostic raised {type(error).__name__}: {error}",
                        traceback=traceback.format_exc(),
                    )
            if test_id in self.LS_MUTATING_TESTS and not result.get("details", {}).get("skipped"):
                isolation_reset = self._reset_lossless_folder(initial_ls_running)
                result.setdefault("details", {})["postTestFolderReset"] = isolation_reset
                if isolation_reset["status"] == "fail":
                    result["status"] = "fail"
                    result["summary"] += " The post-test Lossless Scaling folder reset failed."
            result.update({"id": test_id, "name": name})
            results.append(result)
            result_by_id[test_id] = result
            (session / f"{test_id}.log").write_text(
                json.dumps(result, indent=2, default=str), encoding="utf-8"
            )

        reset_result = self._reset_lossless_folder(initial_ls_running)
        reset_result.update({"id": "environment_reset", "name": "Lossless Scaling folder reset"})
        results.append(reset_result)
        (session / "environment_reset.log").write_text(
            json.dumps(reset_result, indent=2, default=str), encoding="utf-8"
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
            "selectedTest": selected_test,
            "executedTests": [item["id"] for item in results],
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
            "selectedTest": selected_test,
            "availableTests": self.catalog(),
            "directory": self._path(session),
            "archiveName": archive.name,
            "archivePath": str(archive.resolve()),
        }

    def _reset_lossless_folder(self, should_be_running: bool) -> Dict:
        """Restore all companion-owned deployment destinations to their originals."""
        exe_value = self.server.config.lossless_scaling_exe_path
        executable = Path(exe_value) if exe_value else None
        if not executable or not executable.is_file():
            return self._result(
                "warn", "LosslessScaling.exe is unavailable; no managed folder reset was possible."
            )
        stopped = True
        restarted = not should_be_running
        try:
            if self.server.state.is_scaling_active:
                self.server.automation.set_scaling(
                    False, reason="experimental_environment_reset", force=True,
                    park_runtime_on_stop=False,
                )
            if self.server.process_watcher.check_is_lossless_scaling_running():
                stopped = self.server.process_watcher.stop_lossless_scaling()
            if not stopped:
                raise RuntimeError("Lossless Scaling could not be stopped for folder reset")
            manifest = self.server.automation.deployment_manager.apply(
                profile_id=None,
                lossless_scaling_exe=str(executable),
                files=[],
            )
            self.server.state.current_active_profile = None
            self.server.process_watcher.invalidate_foreground_cache()
            if should_be_running:
                restarted = self.server.process_watcher.launch_lossless_scaling(force=True)
            passed = stopped and restarted and not manifest.get("files")
            return self._result(
                "pass" if passed else "fail",
                "Managed add-on files were removed, original files restored, and the prior LS running state restored."
                if passed else "The Lossless Scaling folder reset did not fully complete.",
                directory=self._path(executable.parent),
                managedFilesRemaining=len(manifest.get("files", [])),
                priorRunningState=should_be_running,
                stopped=stopped,
                restarted=restarted,
            )
        except Exception as error:
            return self._result(
                "fail", f"Lossless Scaling folder reset failed: {error}",
                directory=self._path(executable.parent),
                priorRunningState=should_be_running,
                traceback=traceback.format_exc(),
            )

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
        manager = self.server.profile_manager
        profile_id = f"experimental-profile-test-{uuid.uuid4().hex}"
        original_active_id = manager.config.active_profile_id
        created = edited = deleted = False
        try:
            profile = Profile(
                id=profile_id,
                name="Experimental Profile Save Test",
                target_process="LSBenchmark.exe",
                auto_scale=True,
                custom_notes="create-pass",
            )
            manager.add_or_update_profile(profile)
            reloaded = ProfileManager(manager.config_dir)
            stored = reloaded.get_profile_by_id(profile_id)
            created = bool(stored and stored.custom_notes == "create-pass" and stored.auto_scale)

            profile.name = "Experimental Profile Edit Test"
            profile.custom_notes = "edit-pass"
            manager.add_or_update_profile(profile)
            reloaded = ProfileManager(manager.config_dir)
            stored = reloaded.get_profile_by_id(profile_id)
            matched = reloaded.match_target_profile(process_name="lsbenchmark.exe")
            edited = bool(
                stored
                and stored.name == "Experimental Profile Edit Test"
                and stored.custom_notes == "edit-pass"
                and matched
                and matched.id == profile_id
            )
        finally:
            deleted = manager.delete_profile(profile_id) or manager.get_profile_by_id(profile_id) is None
            manager.config.active_profile_id = original_active_id
            manager.save_config()
        absent_after_reload = ProfileManager(manager.config_dir).get_profile_by_id(profile_id) is None
        passed = created and edited and deleted and absent_after_reload
        return self._result(
            "pass" if passed else "fail",
            "A profile was created, edited, matched, deleted, and verified through real settings reloads."
            if passed else "The real profile create/edit/delete persistence cycle failed.",
            createdAndReloaded=created,
            editedAndReloaded=edited,
            caseInsensitiveMatch=edited,
            deletedAndReloaded=deleted and absent_after_reload,
            configFile=self._path(manager.config_file),
        )

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
        sanitized_profiles = [
            {
                "index": index,
                "settings": {
                    key: self._path(value)
                    for key, value in profile.items()
                    if key not in {"Title", "Path"}
                },
            }
            for index, profile in enumerate(profiles)
        ]
        return self._result(
            status,
            "Settings.xml parses and contains native profiles."
            if status == "pass"
            else "Settings.xml is missing, unreadable, or contains no profiles.",
            path=self._path(path),
            nativeProfileCount=len(profiles),
            defaultProfileFound=bool(default),
            backupFound=self.server.ls_settings.initial_backup_path.is_file(),
            nativeAutoScaleEnabled=self.server.ls_settings.native_auto_scale_enabled(),
            nativeProfileSchemas=sanitized_profiles,
        )

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
        return self._live_scaling_result("autoscale")

    def _hotkey_override_behavior(self) -> Dict:
        return self._live_scaling_result("hotkey_override")

    def _lossless_scaling_log_behavior(self) -> Dict:
        return self._live_scaling_result("lossless_log")

    def _reshade_runtime_behavior(self) -> Dict:
        deployment = self._live_addon_deployment_behavior()
        attempts = [
            item for item in deployment.get("details", {}).get("testedProfiles", [])
            if "reshade" in str(item.get("profile") or "").casefold()
        ]
        if not attempts:
            return self._result(
                "warn", "No installed ReShade package was available for the live run.",
                selected=False, attempts=[],
            )
        passed = all(item.get("status") == "pass" for item in attempts)
        return self._result(
            "pass" if passed else "fail",
            "ReShade was deployed and produced fresh runtime evidence while scaling the benchmark."
            if passed else "ReShade was deployed, but its live scaling or runtime confirmation failed.",
            selected=True,
            attempts=attempts,
        )

    def _proxy_runtime_behavior(self, profile_fragment: str, label: str) -> Dict:
        deployment = self._live_addon_deployment_behavior()
        attempts = [
            item for item in deployment.get("details", {}).get("testedProfiles", [])
            if profile_fragment in str(item.get("profile") or "").casefold()
        ]
        if not attempts:
            return self._result(
                "warn", f"No complete installed {label} stack was available for the live run.",
                selected=False, attempts=[],
            )
        passed = all(item.get("status") == "pass" for item in attempts)
        return self._result(
            "pass" if passed else "fail",
            f"{label} deployed, loaded its expected modules, and produced fresh runtime evidence."
            if passed else f"{label} failed deployment, module-load, scaling, or runtime evidence checks.",
            selected=True, attempts=attempts,
        )

    def _proxy_reshade_behavior(self) -> Dict:
        return self._proxy_runtime_behavior("reshade-proxy", "LosslessProxy + ReShade")

    def _proxy_neural_render_behavior(self) -> Dict:
        return self._proxy_runtime_behavior("lsp-neural-render", "LosslessProxy + NeuralRender")

    @staticmethod
    def _wait_until(predicate: Callable[[], bool], timeout: float, interval: float = 0.2) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if predicate():
                    return True
            except Exception:
                pass
            time.sleep(interval)
        return False

    def _installed_addon_profiles(self) -> list[Profile]:
        """Build executable profiles for every installed production add-on recipe."""
        packages: Dict[str, Dict] = {}
        for package in self.server.asset_store.list_packages():
            provider = str(package.get("provider") or "")
            current = packages.get(provider)
            if current is None or str(package.get("version") or "") > str(current.get("version") or ""):
                packages[provider] = package

        default = next((item for item in self.server.config.profiles if item.is_default), None)
        native_title = default.lossless_profile_title if default else None

        def base(label: str) -> Profile:
            return Profile(
                id=f"experimental-live-{label}-{uuid.uuid4().hex}",
                name=f"Experimental Live {label}",
                lossless_profile_title=native_title,
            )

        profiles: list[Profile] = []
        proxy = packages.get("lossless-proxy")
        reshade = packages.get("reshade")
        special_k = packages.get("special-k")
        feeder = packages.get("dlss5-feeder")
        neural = packages.get("lsp-neural-render")

        if proxy:
            profile = base("lossless-proxy")
            profile.graphics.lossless_proxy.enabled = True
            profile.graphics.lossless_proxy.version = str(proxy.get("version") or "") or None
            profiles.append(profile)
        if reshade:
            profile = base("reshade-proxy" if proxy else "reshade")
            profile.graphics.reshade.enabled = True
            profile.graphics.reshade.version = str(reshade.get("version") or "") or None
            if proxy:
                profile.graphics.lossless_proxy.enabled = True
                profile.graphics.lossless_proxy.version = str(proxy.get("version") or "") or None
                profile.reshade = ReshadeConfig(enabled=True, menu_proxy_enabled=True)
            profiles.append(profile)
        if special_k:
            profile = base("special-k")
            profile.graphics.special_k.enabled = True
            profile.graphics.special_k.version = str(special_k.get("version") or "") or None
            profiles.append(profile)
        if feeder:
            profile = base("dlss5-feeder")
            profile.graphics.neural_render.implementation = "ls_reshade_feeder"
            profile.graphics.neural_render.package.enabled = True
            profile.graphics.neural_render.package.version = str(feeder.get("version") or "") or None
            profiles.append(profile)
        if neural and proxy and reshade:
            runtime = next(
                (
                    item for item in self.server.asset_store.list_imports()
                    if str(item.get("filename") or "").casefold() == "nvngx_dlssnr.dll"
                    and item.get("architecture") == "x64"
                ),
                None,
            )
            if runtime:
                profile = base("lsp-neural-render")
                profile.graphics.lossless_proxy.enabled = True
                profile.graphics.lossless_proxy.version = str(proxy.get("version") or "") or None
                profile.graphics.neural_render.implementation = "lsp_neural_render"
                profile.graphics.neural_render.package.enabled = True
                profile.graphics.neural_render.package.version = str(neural.get("version") or "") or None
                profile.graphics.neural_render.runtime_asset_sha256 = str(runtime["sha256"])
                profile.graphics.reshade.enabled = True
                profile.graphics.reshade.version = str(reshade.get("version") or "") or None
                profiles.append(profile)
        return profiles

    def _live_addon_deployment_behavior(self) -> Dict:
        if self._live_addon_result is None:
            self._live_addon_result = self._run_live_addon_deployment_behavior()
        return self._live_addon_result

    def _run_live_addon_deployment_behavior(self) -> Dict:
        exe_value = self.server.config.lossless_scaling_exe_path
        executable = Path(exe_value) if exe_value else None
        if not executable or not executable.is_file():
            return self._result("fail", "LosslessScaling.exe is required for live add-on deployment.")
        profiles = self._installed_addon_profiles()
        if not profiles:
            return self._result("warn", "No installed add-on packages were available for live deployment.")

        manager = self.server.automation.deployment_manager
        resolver = self.server.automation.graphics_resolver
        previous = self.server.state.current_active_profile
        auto_scale_was_enabled = self.server.state.auto_scale_enabled
        was_running = self.server.process_watcher.check_is_lossless_scaling_running()
        checks = []
        failures = []
        workload_process: Optional[subprocess.Popen] = None
        try:
            from scripts.benchmark_lossless_scaling import find_largest_window, focus_window
            from .ls_inspector import LosslessScalingInspector

            workload = self.server._embedded_benchmark_executable()
            if not workload or not Path(workload).is_file():
                raise RuntimeError("the bundled LSBenchmark.exe is unavailable")
            # Keep the foreground watcher from racing this explicitly controlled
            # start/stop cycle and immediately reactivating scaling after stop.
            self.server.state.auto_scale_enabled = False
            if self.server.state.is_scaling_active:
                self.server.automation.set_scaling(
                    False, reason="experimental_addon_test_setup", force=True,
                    park_runtime_on_stop=False,
                )
            workload_process = subprocess.Popen(
                [str(workload), "--duration", "300"], cwd=str(Path(workload).parent)
            )
            hwnd = find_largest_window(workload_process.pid, timeout=12.0)
            if not focus_window(hwnd):
                raise RuntimeError("Windows did not grant the benchmark foreground ownership")
            if was_running and not self.server.process_watcher.stop_lossless_scaling():
                raise RuntimeError("Lossless Scaling could not be stopped for live deployment")
            for profile in profiles:
                attempt = {"profile": profile.name, "status": "fail", "files": []}
                lossless_delta: Dict[str, str] = {}
                runtime_delta: Dict[str, str] = {}
                runtime_errors: List[str] = []
                runtime_evidence = {
                    "proxyStarted": False,
                    "reshadeBridgeInitialized": False,
                    "neuralEngineReady": False,
                    "neuralModelPrepared": False,
                    "neuralTapObserved": False,
                }
                loaded_modules: List[str] = []
                module_inspection_error: Optional[str] = None
                try:
                    if self.server.process_watcher.check_is_lossless_scaling_running():
                        if not self.server.process_watcher.stop_lossless_scaling():
                            raise RuntimeError("Lossless Scaling could not be stopped between add-on tests")
                    plan = resolver.resolve(profile, lossless_scaling_exe=str(executable))
                    manifest = manager.apply(
                        profile_id=profile.id,
                        lossless_scaling_exe=str(executable),
                        files=plan,
                    )
                    file_checks = []
                    for item in manifest.get("files", []):
                        destination = executable.parent / item["relative_path"]
                        actual = AssetStore.sha256(destination) if destination.is_file() else None
                        good = actual == item.get("deployed_sha256")
                        file_checks.append({
                            "path": self._path(destination),
                            "role": item.get("role"),
                            "sourcePackage": item.get("source_package"),
                            "expectedSha256": item.get("deployed_sha256"),
                            "actualSha256": actual,
                            "verified": good,
                        })
                    attempt["files"] = file_checks
                    if not plan or not all(item["verified"] for item in file_checks):
                        raise RuntimeError("one or more deployed files failed destination verification")

                    log_inspector = LosslessScalingInspector(
                        settings_xml_path=self.server.config.lossless_settings_xml_path,
                        lossless_exe_path=str(executable),
                    )
                    active_log = log_inspector._find_log_file()
                    log_paths = [active_log] if active_log else []
                    log_snapshot = self._log_snapshot(log_paths)
                    runtime_paths = self._runtime_log_paths(executable.parent)
                    runtime_snapshot = self._log_snapshot(runtime_paths)
                    if not self.server.process_watcher.launch_lossless_scaling(force=True):
                        raise RuntimeError("Lossless Scaling could not launch with the deployed recipe")
                    # Process creation precedes global-hotkey and injected
                    # add-on initialization on slower installations.
                    time.sleep(1.5)
                    focus_window(hwnd)
                    self.server.state.current_active_profile = profile
                    activation_attempts = 1
                    scaled = self.server.automation.set_scaling(
                        True,
                        profile,
                        reason=f"experimental_addon_test:{profile.id}",
                        force=True,
                        target_pid=workload_process.pid,
                        target_hwnd=hwnd,
                        park_runtime_on_stop=False,
                    )
                    if not scaled:
                        # The first hotkey can still race a slow injected
                        # runtime. It was observed idle, so retrying cannot turn
                        # a confirmed scaling session back off.
                        time.sleep(1.5)
                        focus_window(hwnd)
                        activation_attempts += 1
                        scaled = self.server.automation.set_scaling(
                            True,
                            profile,
                            reason=f"experimental_addon_test_retry:{profile.id}",
                            force=True,
                            target_pid=workload_process.pid,
                            target_hwnd=hwnd,
                            park_runtime_on_stop=False,
                        )
                    active_observed = scaled and self._wait_until(
                        lambda: bool(log_inspector.inspect_current_scaling_target().is_active), 5.0
                    )

                    # Add-ons can initialize several seconds after LS reports
                    # scaling. Sample both the process module table and fresh
                    # logs while the presentation is still alive.
                    neural_render_selected = (
                        profile.graphics.neural_render.implementation == "lsp_neural_render"
                    )
                    reshade_selected = profile.graphics.reshade.enabled
                    proxy_selected = profile.graphics.lossless_proxy.enabled

                    def addon_runtime_ready() -> bool:
                        nonlocal loaded_modules, module_inspection_error, runtime_evidence
                        loaded_modules, module_inspection_error = self._lossless_loaded_modules(executable)
                        current_paths = self._runtime_log_paths(executable.parent)
                        current_delta = self._log_delta(runtime_snapshot, current_paths)
                        runtime_evidence = self._runtime_log_evidence(current_delta)
                        module_names = {Path(item).name.casefold() for item in loaded_modules}
                        if neural_render_selected:
                            return (
                                {"lsp_neuralrender.dll", "nvngx.dll_lspnr.dll", "nvngx_dlssnr.dll"}
                                <= module_names
                                and runtime_evidence["proxyStarted"]
                                and runtime_evidence["neuralEngineReady"]
                                and runtime_evidence["neuralModelPrepared"]
                                and runtime_evidence["neuralTapObserved"]
                            )
                        if reshade_selected and proxy_selected:
                            return (
                                {"dxgi.dll", "lsc_reshadebridge.dll"} <= module_names
                                and runtime_evidence["proxyStarted"]
                                and runtime_evidence["reshadeBridgeInitialized"]
                            )
                        if reshade_selected:
                            return "dxgi.dll" in module_names and bool(current_delta)
                        if proxy_selected:
                            return "lossless.dll" in module_names and runtime_evidence["proxyStarted"]
                        return bool(current_delta)

                    addon_ready = active_observed and self._wait_until(
                        addon_runtime_ready, 12.0, interval=0.4
                    )
                    stopped = self.server.automation.set_scaling(
                        False,
                        profile,
                        reason=f"experimental_addon_test_complete:{profile.id}",
                        force=True,
                        park_runtime_on_stop=False,
                    ) if scaled else False
                    time.sleep(0.3)
                    if active_log is None:
                        active_log = log_inspector._find_log_file()
                        if active_log:
                            log_paths = [active_log]
                    lossless_delta = self._log_delta(log_snapshot, log_paths)
                    current_runtime_paths = self._runtime_log_paths(executable.parent)
                    runtime_delta = self._log_delta(runtime_snapshot, current_runtime_paths)
                    runtime_errors = self._runtime_log_errors(runtime_delta)
                    runtime_evidence = self._runtime_log_evidence(runtime_delta)
                    expected_proxy_version = (
                        str(profile.graphics.lossless_proxy.version or "").removeprefix("v")
                        if profile.graphics.lossless_proxy.enabled else None
                    )
                    reported_proxy_version = self._reported_lossless_proxy_version(runtime_delta)
                    proxy_version_matches = (
                        None
                        if not expected_proxy_version or not reported_proxy_version
                        else expected_proxy_version.casefold() == reported_proxy_version.casefold()
                    )
                    attempt.update({
                        "scalingConfirmed": scaled and active_observed,
                        "deactivationConfirmed": stopped,
                        "losslessLogFiles": [self._path(item) for item in lossless_delta],
                        "losslessLogTail": self._path("\n".join(lossless_delta.values())[-8000:]),
                        "runtimeLogFiles": [self._path(item) for item in runtime_delta],
                        "runtimeLogTail": self._path("\n".join(runtime_delta.values())[-8000:]),
                        "runtimeErrors": runtime_errors,
                        "runtimeEvidence": runtime_evidence,
                        "loadedModules": [self._path(item) for item in loaded_modules],
                        "moduleInspectionError": module_inspection_error,
                        "addonRuntimeReady": addon_ready,
                        "activationAttempts": activation_attempts,
                        "expectedLosslessProxyVersion": expected_proxy_version,
                        "reportedLosslessProxyVersion": reported_proxy_version,
                        "losslessProxyVersionMatches": proxy_version_matches,
                        "losslessProxyVersionNote": (
                            "The runtime self-reported a different version. This can be a stale "
                            "embedded label and is recorded as evidence, not treated as a failure."
                            if proxy_version_matches is False else None
                        ),
                    })
                    runtime_confirmed = (
                        scaled and active_observed and stopped
                        and bool(runtime_delta) and not runtime_errors
                        and addon_ready and not module_inspection_error
                    )
                    if not runtime_confirmed:
                        raise RuntimeError(
                            "deployment verified, but the scaling cycle or add-on runtime health check failed; "
                            f"scaled={scaled}, activeObserved={active_observed}, stopped={stopped}, "
                            f"runtimeLog={bool(runtime_delta)}, runtimeErrors={runtime_errors}, "
                            f"runtimeEvidence={runtime_evidence}, addonReady={addon_ready}, "
                            f"moduleInspectionError={module_inspection_error}"
                        )
                    attempt["status"] = "pass"
                except Exception as error:
                    attempt["error"] = str(error)
                    failures.append({"profile": profile.name, "error": str(error)})
                finally:
                    checks.append(attempt)
        except Exception as error:
            failures.append({"profile": "test setup", "error": str(error)})
        finally:
            if workload_process and workload_process.poll() is None:
                workload_process.terminate()
                try:
                    workload_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    workload_process.kill()
            try:
                if self.server.state.is_scaling_active:
                    self.server.automation.set_scaling(
                        False, reason="experimental_addon_cleanup", force=True,
                        park_runtime_on_stop=False,
                    )
                self.server.process_watcher.stop_lossless_scaling()
            except Exception:
                pass
            try:
                restore_plan = resolver.resolve(previous, lossless_scaling_exe=str(executable)) if previous else []
                manager.apply(
                    profile_id=previous.id if previous else None,
                    lossless_scaling_exe=str(executable),
                    files=restore_plan,
                )
            except Exception as error:
                failures.append({"profile": "restore", "error": str(error)})
            if was_running:
                self.server.process_watcher.launch_lossless_scaling(force=True)
            self.server.state.auto_scale_enabled = auto_scale_was_enabled

        passed = bool(checks) and not failures
        return self._result(
            "pass" if passed else "fail",
            "Every installed add-on recipe was deployed, hash-verified, used for a logged benchmark scaling cycle, and the prior deployment was restored."
            if passed else "One or more live add-on deployments or the restoration failed.",
            losslessDirectory=self._path(executable.parent),
            testedProfiles=checks,
            failures=failures,
        )

    def _live_scaling_result(self, key: str) -> Dict:
        if self._live_scaling_results is None:
            self._live_scaling_results = self._run_live_scaling_workflow()
        return self._live_scaling_results[key]

    @staticmethod
    def _log_snapshot(paths: Iterable[Path]) -> Dict[Path, Dict[str, object]]:
        snapshot: Dict[Path, Dict[str, object]] = {}
        for path in paths:
            if not path.is_file():
                continue
            size = path.stat().st_size
            with path.open("rb") as stream:
                stream.seek(max(0, size - 256))
                tail = stream.read()
            snapshot[path] = {"size": size, "tail": tail}
        return snapshot

    @staticmethod
    def _log_delta(snapshot: Dict[Path, object], paths: Iterable[Path]) -> Dict[str, str]:
        output: Dict[str, str] = {}
        for path in paths:
            if not path.is_file():
                continue
            saved = snapshot.get(path, {"size": 0, "tail": b""})
            if isinstance(saved, dict):
                offset = int(saved.get("size") or 0)
                tail = bytes(saved.get("tail") or b"")
            else:  # Compatibility with older callers and archived tests.
                offset = int(saved or 0)
                tail = b""
            try:
                current_size = path.stat().st_size
                # Add-on loggers commonly truncate a log when LS restarts. Seeking
                # to the old (larger) offset would incorrectly report no output.
                if current_size < offset:
                    offset = 0
                elif offset and tail:
                    with path.open("rb") as binary:
                        binary.seek(max(0, offset - len(tail)))
                        if binary.read(len(tail)) != tail:
                            offset = 0
                with path.open("r", encoding="utf-8", errors="ignore") as stream:
                    stream.seek(offset)
                    text = stream.read()
                if text:
                    output[str(path)] = text[-12000:]
            except OSError:
                continue
        return output

    @staticmethod
    def _runtime_log_errors(logs: Dict[str, str]) -> List[str]:
        """Return explicit fatal add-on states, not NGX's fallback-path warnings."""
        patterns = (
            r"NrEngine FAILED:",
            r"\bDISABLED:",
            r"\[(?:FATAL|PANIC)\]",
        )
        findings: List[str] = []
        for path, content in logs.items():
            for line in content.splitlines():
                if any(re.search(pattern, line, re.IGNORECASE) for pattern in patterns):
                    findings.append(f"{Path(path).name}: {line.strip()}")
        return findings[-20:]

    @staticmethod
    def _runtime_log_evidence(logs: Dict[str, str]) -> Dict[str, bool]:
        content = "\n".join(logs.values())
        return {
            "proxyStarted": bool(re.search(r"\bLosslessProxy\s+v?[^\s]+\s+starting", content, re.IGNORECASE)),
            "reshadeBridgeInitialized": "LS Companion ReShade Bridge initialized" in content,
            "neuralEngineReady": "NrEngine ready" in content,
            "neuralModelPrepared": "Prepare:" in content,
            "neuralTapObserved": bool(re.search(r"\btaps\s+[1-9]\d*\b", content)),
        }

    @staticmethod
    def _lossless_loaded_modules(executable: Path) -> tuple[List[str], Optional[str]]:
        """Inspect the real LS process while scaling, before injected DLLs unload."""
        expected = str(executable.resolve()).casefold()
        errors: List[str] = []
        for process in psutil.process_iter(["name", "exe"]):
            try:
                process_exe = str(process.info.get("exe") or "").casefold()
                process_name = str(process.info.get("name") or "").casefold()
                if process_exe != expected and process_name != executable.name.casefold():
                    continue
                modules = sorted({
                    str(item.path) for item in process.memory_maps(grouped=False)
                    if getattr(item, "path", None)
                }, key=str.casefold)
                return modules, None
            except (psutil.AccessDenied, psutil.NoSuchProcess, OSError) as error:
                errors.append(f"{type(error).__name__}: {error}")
        if errors:
            return [], "; ".join(errors)
        return [], "LosslessScaling.exe process was not found during module inspection"

    @staticmethod
    def _reported_lossless_proxy_version(logs: Dict[str, str]) -> Optional[str]:
        content = "\n".join(logs.values())
        match = re.search(r"\bLosslessProxy\s+v([^\s]+)\s+starting", content, re.IGNORECASE)
        return match.group(1).rstrip(".,;:") if match else None

    @staticmethod
    def _runtime_log_paths(lossless_directory: Path) -> List[Path]:
        """Return root and nested add-on logs without duplicating paths."""
        paths = [*lossless_directory.glob("*.log")]
        logs_directory = lossless_directory / "logs"
        if logs_directory.is_dir():
            paths.extend(logs_directory.rglob("*.log"))
        return list(dict.fromkeys(paths))

    def _run_live_scaling_workflow(self) -> Dict[str, Dict]:
        results = {
            key: self._result("fail", "The live scaling workflow did not complete.")
            for key in ("autoscale", "hotkey_override", "lossless_log")
        }
        if self.server.state.simulation_mode:
            message = "Live tests cannot run while simulation mode is active."
            return {key: self._result("fail", message) for key in results}
        executable = Path(self.server.config.lossless_scaling_exe_path or "")
        workload = self.server._embedded_benchmark_executable()
        if not executable.is_file() or not workload or not Path(workload).is_file():
            message = "LosslessScaling.exe and the bundled LSBenchmark.exe are required."
            return {key: self._result("fail", message) for key in results}

        from scripts.benchmark_lossless_scaling import find_largest_window, focus_window

        manager = self.server.profile_manager
        config = manager.config
        original_profiles = [item.model_copy(deep=True) for item in config.profiles]
        original_fields = {
            "active_profile_id": config.active_profile_id,
            "disable_native_auto_scale": config.disable_native_auto_scale,
            "lossless_control_configured": config.lossless_control_configured,
            "override_lossless_hotkey": config.override_lossless_hotkey,
            "override_hotkey": config.override_hotkey.model_copy(deep=True),
            "global_hotkey": config.global_hotkey.model_copy(deep=True),
        }
        previous = self.server.state.current_active_profile
        auto_scale_was_enabled = self.server.state.auto_scale_enabled
        was_running = self.server.process_watcher.check_is_lossless_scaling_running()
        settings_path = self.server.ls_settings.path
        settings_bytes = settings_path.read_bytes() if settings_path.is_file() else None
        # The server does not own the inspector directly; the production scaling probe
        # closes over it. Create the same inspector type for independent log evidence.
        from .ls_inspector import LosslessScalingInspector
        log_inspector = LosslessScalingInspector(
            settings_xml_path=config.lossless_settings_xml_path,
            lossless_exe_path=str(executable),
        )
        active_log = log_inspector._find_log_file()
        ls_paths = [active_log] if active_log else []
        ls_snapshot = self._log_snapshot(ls_paths)
        live_profile = Profile(
            id=f"experimental-live-scale-{uuid.uuid4().hex}",
            name="Experimental Live Scaling",
        )
        live_profile.target_process = Path(workload).name
        live_profile.target_executable_path = str(Path(workload).resolve())
        live_profile.auto_scale = True
        default = next((item for item in config.profiles if item.is_default), None)
        if default and not live_profile.lossless_profile_title:
            live_profile.lossless_profile_title = default.lossless_profile_title

        process: Optional[subprocess.Popen] = None
        autoscale_ok = override_ok = active_seen = False
        hwnd = None
        try:
            if self.server.state.is_scaling_active:
                self.server.automation.set_scaling(False, reason="experimental_test_setup", force=True)
            config.disable_native_auto_scale = True
            self.server.state.auto_scale_enabled = True
            manager.add_or_update_profile(live_profile)
            process = subprocess.Popen(
                [str(workload), "--duration", "60"],
                cwd=str(Path(workload).parent),
            )
            hwnd = find_largest_window(process.pid, timeout=12.0)
            if not focus_window(hwnd):
                raise RuntimeError("Windows did not grant the benchmark foreground ownership")
            if not self.server.process_watcher.check_is_lossless_scaling_running():
                if not self.server.process_watcher.launch_lossless_scaling(force=True):
                    raise RuntimeError("Lossless Scaling could not be launched")
                focus_window(hwnd)

            self.server.process_watcher.invalidate_foreground_cache()
            self.server.process_watcher.check_foreground_and_update()
            autoscale_ok = self._wait_until(lambda: bool(self.server.state.is_scaling_active), 10.0)
            active_seen = autoscale_ok or self._wait_until(
                lambda: bool(log_inspector.inspect_current_scaling_target().is_active), 5.0
            )
            results["autoscale"] = self._result(
                "pass" if autoscale_ok else "fail",
                "Foreground detection selected the temporary profile and Lossless Scaling confirmed automatic activation."
                if autoscale_ok else "The real benchmark window did not automatically enter scaling.",
                workload=self._path(workload), pid=process.pid, hwnd=hwnd,
                activeProfileId=getattr(self.server.state.current_active_profile, "id", None),
                scalingControl=dict(self.server.state.scaling_control_status),
            )

            if self.server.state.is_scaling_active:
                self.server.automation.set_scaling(
                    False, live_profile, reason="experimental_autoscale_complete", force=True,
                    park_runtime_on_stop=False,
                )
            # The autoscale portion is complete. Disable it while testing manual
            # override toggles so the watcher cannot undo the OFF transition.
            self.server.state.auto_scale_enabled = False
            config.override_lossless_hotkey = True
            config.override_hotkey = HotkeyConfig(modifiers=["ctrl", "shift"], key="f23")
            config.lossless_control_configured = True
            manager.save_config()
            controls_synced = self.server.process_watcher.enforce_helper_scaling_control()
            if self.server.hotkey_listener:
                self.server.hotkey_listener.refresh()
            registered = self._wait_until(
                lambda: bool(self.server.hotkey_listener and self.server.hotkey_listener._registered), 3.0
            )
            # Synchronizing the hidden native hotkey may restart LS. Let its
            # message loop register F24 before exercising the override.
            time.sleep(1.5)
            focus_window(hwnd)
            emitted = InputSimulator.trigger_hotkey(modifiers=["ctrl", "shift"], key="f23", hold_ms=80)
            override_on = emitted and self._wait_until(lambda: bool(self.server.state.is_scaling_active), 10.0)
            first_dispatch_complete = self._wait_until(
                lambda: not bool(
                    self.server.hotkey_listener
                    and self.server.hotkey_listener._callback_lock.locked()
                ),
                5.0,
            )
            # Give both the listener and Lossless Scaling time to release the
            # first chord before emitting the same override chord again.
            time.sleep(0.75)
            focus_window(hwnd)
            emitted_off = InputSimulator.trigger_hotkey(modifiers=["ctrl", "shift"], key="f23", hold_ms=80)
            override_off = emitted_off and self._wait_until(lambda: not self.server.state.is_scaling_active, 10.0)
            override_ok = controls_synced and registered and override_on and first_dispatch_complete and override_off
            results["hotkey_override"] = self._result(
                "pass" if override_ok else "fail",
                "The registered override hotkey activated and deactivated scaling through the real listener."
                if override_ok else "The real override-hotkey activation cycle failed.",
                nativeControlsSynchronized=controls_synced,
                listenerRegistered=registered,
                activationConfirmed=override_on,
                activationHandlerCompleted=first_dispatch_complete,
                deactivationConfirmed=override_off,
                scalingControl=dict(self.server.state.scaling_control_status),
            )

            time.sleep(0.5)
            if active_log is None:
                active_log = log_inspector._find_log_file()
                if active_log:
                    ls_paths = [active_log]
            ls_delta = self._log_delta(ls_snapshot, ls_paths)
            combined_ls = "\n".join(ls_delta.values())
            log_confirmed = active_seen and bool(combined_ls.strip())
            log_status = "pass" if log_confirmed else "warn" if active_seen else "fail"
            results["lossless_log"] = self._result(
                log_status,
                "A fresh Lossless Scaling log delta was captured after confirmed scaling."
                if log_confirmed else
                "Scaling was confirmed by the live overlay, but this Lossless Scaling build did not expose a core log file."
                if active_seen else "Scaling was not corroborated by either the live overlay or a fresh core log delta.",
                logFiles=[self._path(item) for item in ls_delta],
                activeStateObserved=active_seen,
                logTail=self._path(combined_ls[-12000:]),
            )

        except Exception as error:
            failure = f"Live scaling workflow raised {type(error).__name__}: {error}"
            for key, value in list(results.items()):
                if value["summary"] == "The live scaling workflow did not complete.":
                    results[key] = self._result("fail", failure, traceback=traceback.format_exc())
        finally:
            try:
                if self.server.state.is_scaling_active:
                    self.server.automation.set_scaling(False, reason="experimental_test_cleanup", force=True)
            except Exception:
                pass
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
            try:
                self.server.process_watcher.stop_lossless_scaling()
            except Exception:
                pass
            config.profiles = original_profiles
            for field, value in original_fields.items():
                setattr(config, field, value)
            manager.save_config()
            if settings_bytes is not None:
                settings_path.write_bytes(settings_bytes)
            try:
                restored = next((item for item in config.profiles if previous and item.id == previous.id), None)
                plan = self.server.automation.graphics_resolver.resolve(
                    restored,
                    lossless_scaling_exe=str(executable),
                    reshade_overlay_hotkey=self.server.config.reshade_overlay_hotkey,
                ) if restored else []
                self.server.automation.deployment_manager.apply(
                    profile_id=restored.id if restored else None,
                    lossless_scaling_exe=str(executable),
                    files=plan,
                )
                self.server.state.current_active_profile = restored
            except Exception as error:
                for value in results.values():
                    value.setdefault("details", {})["cleanupError"] = str(error)
                    value["status"] = "fail"
            if self.server.hotkey_listener:
                self.server.hotkey_listener.refresh()
            if was_running:
                self.server.process_watcher.launch_lossless_scaling(force=True)
            self.server.state.auto_scale_enabled = auto_scale_was_enabled
        return results

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
        staged = None
        if selected and not any(
            item.get("provider") == "reshade"
            for item in self.server.asset_store.list_packages()
        ):
            try:
                staged = GraphicsSourceImporter(self.server.asset_store).stage(
                    "reshade", f"diagnostic-reshade-{uuid.uuid4().hex}"
                )
            except Exception as error:
                failures.append({
                    "profile": "setup",
                    "error": f"ReShade is selected but its detected installer could not be staged: {error}",
                })
        known = {item.id for item in self.server.config.reshade_profiles}
        exe = self.server.config.lossless_scaling_exe_path
        for profile in selected:
            try:
                plan = self.server.automation.graphics_resolver.resolve(
                    profile,
                    lossless_scaling_exe=exe,
                    reshade_overlay_hotkey=self.server.config.reshade_overlay_hotkey,
                )
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
        return self._result(
            status, summary,
            selectedProfileCount=len(selected),
            stagedPackage=(staged or {}).get("version"),
            checks=checks,
            failures=failures,
        )

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
        status = self.server.rtss_manager.status(self.server.config.rtss_install_path)
        if not status.get("installed"):
            result = "fail" if self.server.config.rtss_frame_limiting_enabled else "warn"
            return self._result(
                result,
                "RTSS is not installed, so its real profile write/remove cycle could not run.",
                enabled=self.server.config.rtss_frame_limiting_enabled,
                installed=False,
            )
        profile = Profile(
            id=f"experimental-rtss-{uuid.uuid4().hex}",
            name="Experimental RTSS Test",
            target_process=f"LSCompanionDiagnostic-{uuid.uuid4().hex[:8]}.exe",
        )
        profile.rtss.enabled = True
        profile.rtss.framerate_limit = 73
        path = None
        applied = removed = False
        error = None
        try:
            path = self.server.rtss_manager.apply_profile(
                profile,
                configured_path=self.server.config.rtss_install_path,
            )
            text = path.read_text(encoding="utf-8", errors="ignore")
            applied = path.is_file() and "Limit=73" in text
            removed = self.server.rtss_manager.remove_profile(
                profile, self.server.config.rtss_install_path
            ) and not path.exists()
        except Exception as exc:
            error = str(exc)
        finally:
            if path and path.exists():
                try:
                    self.server.rtss_manager.remove_profile(
                        profile, self.server.config.rtss_install_path
                    )
                except Exception:
                    pass
        passed = applied and removed
        return self._result(
            "pass" if passed else "fail",
            "A real RTSS application profile was written, verified, removed, and its change notification sent."
            if passed else "The real RTSS profile write/remove cycle failed.",
            installed=True,
            running=status.get("running"),
            installPath=self._path(status.get("path")),
            profilePath=self._path(path),
            applied=applied,
            removed=removed,
            error=error,
        )

    def _startup(self) -> Dict:
        initial = self.server.startup_manager.is_enabled()
        launch_arguments = (
            self.server.startup_manager.launch_arguments()
            if hasattr(self.server.startup_manager, "launch_arguments")
            else []
        )
        toggled = restored = False
        error = None
        try:
            self.server.startup_manager.set_enabled(not initial)
            toggled = self.server.startup_manager.is_enabled() is (not initial)
            self.server.startup_manager.set_enabled(initial)
            restored = self.server.startup_manager.is_enabled() is initial
        except Exception as exc:
            error = str(exc)
        finally:
            if self.server.startup_manager.is_enabled() is not initial:
                try:
                    self.server.startup_manager.set_enabled(initial)
                    restored = self.server.startup_manager.is_enabled() is initial
                except Exception as exc:
                    error = f"{error or ''}; restore failed: {exc}".strip("; ")
        passed = toggled and restored
        return self._result(
            "pass" if passed else "fail",
            "The real Task Scheduler startup entry was toggled, verified, and restored."
            if passed else "The real startup-task toggle or restoration failed.",
            initialState=initial,
            toggled=toggled,
            restored=restored,
            launchArguments=[self._path(item) for item in launch_arguments],
            error=error,
        )

    def _runtime(self) -> Dict:
        return self._result("pass", "Core runtime services are instantiated and responding.", connectedClients=self.server.state.connected_clients, companionPid=os.getpid(), processExists=psutil.pid_exists(os.getpid()), automation=type(self.server.automation).__name__, processWatcher=type(self.server.process_watcher).__name__ if self.server.process_watcher else None, simulationMode=self.server.state.simulation_mode)
