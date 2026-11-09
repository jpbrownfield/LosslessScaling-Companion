"""Idle mapping uses LS's own slot order without starting a scaling session."""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from companion.core.models import Profile
from companion.core.profile_manager import ProfileManager
from companion.core.state import AppState
from companion.services import adapter_report, gpu_index_refresh as refresh
from companion.services.gpu_router import GpuRouter
from companion.services.ls_settings import LosslessSettingsXml

GPU_A = r"PCI\VEN_10DE&DEV_2203&SUBSYS_12345678"
GPU_B = r"PCI\VEN_10DE&DEV_2D83&SUBSYS_87654321"
NAME_A = "NVIDIA GeForce RTX 3090"
NAME_B = "NVIDIA GeForce RTX 5050"


def inventory():
    return {"gpus": [{"deviceId": GPU_A, "name": NAME_A},
                     {"deviceId": GPU_B, "name": NAME_B}], "displays": []}


def report(count=5):
    rows = [{"description": NAME_A, "vendorId": 0x10DE, "deviceId": 0x2203,
             "luidLow": i + 1, "luidHigh": 0, "isSoftware": False} for i in range(count)]
    rows.append({"description": NAME_B, "vendorId": 0x10DE, "deviceId": 0x2D83,
                 "luidLow": 99, "luidHigh": 0, "isSoftware": False})
    return {"schema": 1, "pid": 42, "hasDevice": False, "enumAdapters": rows,
            "enumAdapters1": copy.deepcopy(rows), "lsAdapterNames": [NAME_A] * count + [NAME_B]}


class IdleRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = ProfileManager(self.root)
        self.config = self.manager.config
        self.profile = Profile(name="Game", lossless_profile_title="Game",
                               preferred_gpu_device_id=GPU_B, preferred_gpu_name=NAME_B,
                               native_scaling_settings={"PreferredGpuId": "2", "OutputDisplayId": "4"})
        self.config.profiles = [self.profile]
        self.config.ls_gpu_id_map = {GPU_A: 1, GPU_B: 2}
        self.config.lossless_control_configured = True
        self.config.disable_native_auto_scale = True
        self.exe = self.root / "LosslessScaling.exe"
        self.exe.write_bytes(b"fixture")
        self.exe.with_suffix(".dll").write_bytes(b"managed UI fixture")
        (self.root / "Lossless.dll").write_bytes(b"engine fixture")
        self.config.lossless_scaling_exe_path = str(self.exe)
        self.settings = LosslessSettingsXml(str(self.root / "Settings.xml"))
        self.settings.path.write_text("<Settings><Unknown>keep</Unknown><GameProfiles>"
                                      "<Profile><Title>Game</Title><PreferredGpuId>2</PreferredGpuId>"
                                      "<OutputDisplayId>4</OutputDisplayId><AutoScale>false</AutoScale>"
                                      "<FutureSetting>unchanged</FutureSetting></Profile>"
                                      "<Profile><Title>Auto</Title><PreferredGpuId>0</PreferredGpuId></Profile>"
                                      "</GameProfiles></Settings>", encoding="utf-8")
        self.running = False
        self.now = 1.0
        self.watcher = Mock()
        self.watcher.check_is_lossless_scaling_running.side_effect = lambda: self.running
        self.watcher.stop_lossless_scaling.side_effect = self.stop_host
        self.watcher.launch_lossless_scaling.side_effect = self.start_host
        self.server = SimpleNamespace(
            profile_manager=self.manager, state=AppState(), process_watcher=self.watcher,
            ls_settings=self.settings, _editing_clients=set(), _active_mutations=0,
            _active_requests=0, _gpu_activity_lock=threading.Lock(),
            component_install_lock=threading.Lock(), benchmark_task=None,
            diagnostics_task=None, _gpu_calibration_task=None,
            automation=SimpleNamespace(_lock=threading.RLock(), scaling_state_probe=lambda: False,
                                       gpu_router=SimpleNamespace(detect=inventory)))
        self.fast = Mock(return_value=report())
        self.service = refresh.IdleGpuIndexRefresh(self.server, fast_probe=self.fast,
                                                   process_lookup=lambda _: (42, time.time()) if self.running else None,
                                                   clock=lambda: self.now)
        self.allow_hash = patch.object(refresh, "SUPPORTED_UI_HASHES", {hashlib.sha256(b"managed UI fixture").hexdigest()})
        self.allow_hash.start()
        self.addCleanup(self.allow_hash.stop)
        self.guard = patch("ctypes.WinDLL", return_value=SimpleNamespace(LSCLaunchHiddenWithReport=True))
        self.guard.start()
        self.addCleanup(self.guard.stop)

    def stop_host(self):
        self.running = False
        return True

    def start_host(self, **_):
        self.running = True
        return True

    def launch_report(self, *_):
        self.running = True
        return report()

    def plan(self, payload=None, native=None):
        return refresh.mapping_plan(self.config, inventory(), payload or report(),
                                    self.settings.read()["profiles"] if native is None else native)

    def test_maps_5050_after_duplicate_3090_slots_and_preserves_auto_display_unknowns(self):
        plan = self.plan()
        self.assertEqual(plan["mapping"], {GPU_A: 1, GPU_B: 6})
        self.assertEqual(plan["profiles"], {self.profile.id: 6})
        self.assertEqual(plan["xml"], {"Game": 6})
        with patch.object(self.service, "_launch_probe", side_effect=self.launch_report):
            self.service._remap(self.exe, inventory())
        self.assertFalse(self.running)
        self.assertEqual(self.config.ls_gpu_id_map[GPU_B], 6)
        self.assertEqual(self.profile.native_scaling_settings["PreferredGpuId"], "6")
        native = self.settings.read()["profiles"]
        self.assertEqual(native[0]["PreferredGpuId"], "6")
        self.assertEqual(native[0]["OutputDisplayId"], "4")
        self.assertEqual(native[0]["FutureSetting"], "unchanged")
        self.assertEqual(native[1]["PreferredGpuId"], "0")
        self.assertIn("<Unknown>keep</Unknown>", self.settings.path.read_text())
        self.assertTrue(self.settings.initial_backup_path.is_file())

    def test_existing_valid_alias_is_retained(self):
        self.config.ls_gpu_id_map[GPU_A] = 4
        self.assertEqual(self.plan()["mapping"][GPU_A], 4)

    def test_runtime_mismatch_forces_refresh_even_before_periodic_deadline(self):
        self.service.pending = False
        self.service.observed = "old observation"
        self.service.next_check = self.now + refresh.CHECK_SECONDS
        self.service.retry_after = self.now + 300
        self.service.request_refresh()
        self.assertTrue(self.service.pending)
        self.assertIsNone(self.service.observed)
        self.assertEqual(self.service.retry_after, 0)
        with patch.object(self.service, "_remap") as remap:
            self.service.check()
        remap.assert_called_once()

    def test_unknown_numeric_identity_is_not_inferred_from_shifted_number(self):
        self.config.ls_gpu_id_map = {}
        self.profile.preferred_gpu_device_id = self.profile.preferred_gpu_name = None
        plan = self.plan()
        self.assertEqual(plan["profiles"], {})
        self.assertEqual(plan["xml"], {})
        self.assertIn("Game", plan["skipped"])

    def test_auto_profile_remains_auto_even_with_stable_pin(self):
        self.profile.native_scaling_settings["PreferredGpuId"] = "0"
        self.assertEqual(self.plan()["profiles"], {})

    def test_identical_physical_model_names_are_not_guessed(self):
        devices = inventory()
        devices["gpus"].append({"deviceId": GPU_B + "other", "name": NAME_B})
        plan = refresh.mapping_plan(self.config, devices, report(), self.settings.read()["profiles"])
        self.assertNotIn(GPU_B, plan["mapping"])
        self.assertEqual(plan["xml"], {})

    def test_duplicate_native_titles_are_not_remapped(self):
        native = self.settings.read()["profiles"]
        native.append(dict(native[0]))
        self.assertEqual(self.plan(native=native)["xml"], {})

    def test_layout_fingerprint_detects_reorder_luids_and_multiplicity(self):
        first = report()
        second = copy.deepcopy(first)
        second["enumAdapters"].reverse()
        third = copy.deepcopy(first)
        third["enumAdapters"][0]["luidLow"] = 100
        for changed in (second, third, report(2)):
            self.assertNotEqual(refresh.snapshot_fingerprint(first), refresh.snapshot_fingerprint(changed))

    def test_startup_then_three_hour_fast_check_only_unless_layout_changes(self):
        with patch.object(self.service, "_remap") as slow:
            self.service.check()
            slow.assert_called_once()
            self.now += 30
            self.service.check()
            self.assertEqual(self.fast.call_count, 1)
            self.now += refresh.CHECK_SECONDS
            self.service.check()
            self.assertEqual(self.fast.call_count, 2)
            self.assertEqual(slow.call_count, 1)
            self.fast.return_value = report(2)
            self.now += refresh.CHECK_SECONDS
            self.service.check()
            self.assertEqual(slow.call_count, 2)

    def test_deferred_startup_retries_when_editor_closes_without_waiting_three_hours(self):
        with patch.object(self.service, "_remap") as slow:
            self.server._editing_clients.add("editor")
            self.service.check()
            self.assertEqual(self.server.state.gpu_mapping_status["state"], "deferred")
            slow.assert_not_called()
            self.fast.assert_not_called()
            self.server._editing_clients.clear()
            self.now += 30
            self.service.check()
            slow.assert_called_once()
            self.assertEqual(self.fast.call_count, 1)

    def test_unknown_build_is_deferred_before_any_process_or_settings_changes(self):
        self.exe.with_suffix(".dll").write_bytes(b"different build")
        before = self.settings.path.read_bytes()
        self.service.check()
        self.assertEqual(self.server.state.gpu_mapping_status["state"], "needs_validation")
        self.watcher.stop_lossless_scaling.assert_not_called()
        self.assertEqual(before, self.settings.path.read_bytes())

    def test_scaling_unknown_observation_tests_updates_and_installs_all_defer(self):
        with patch.object(self.service, "_remap") as slow:
            for name in ("is_scaling_active", "benchmark_mode_active", "update_in_progress"):
                setattr(self.server.state, name, True)
                self.service.check()
                setattr(self.server.state, name, False)
            self.running = True
            self.server.automation.scaling_state_probe = lambda: None
            self.service.check()
            self.running = False
            self.server.component_install_lock.acquire()
            try:
                self.service.check()
            finally:
                self.server.component_install_lock.release()
            slow.assert_not_called()
            self.fast.assert_not_called()

    def test_due_three_hour_check_waits_for_scaling_to_end_before_any_probe(self):
        with patch.object(self.service, "_remap") as slow:
            self.service.check()
            self.now += refresh.CHECK_SECONDS
            self.server.state.is_scaling_active = True
            self.service.check()
            self.assertEqual(self.fast.call_count, 1)
            self.assertEqual(slow.call_count, 1)
            self.server.state.is_scaling_active = False
            self.now += 30
            self.service.check()
            self.assertEqual(self.fast.call_count, 2)
            self.assertEqual(slow.call_count, 1)

    def test_probe_error_backs_off_and_clears_operation_flag(self):
        with patch.object(self.service, "_remap", side_effect=RuntimeError("fixture failure")) as slow, \
                self.assertLogs(refresh.logger, level="ERROR"):
            self.service.check()
            self.assertEqual(self.server.state.gpu_mapping_status["state"], "error")
            self.assertFalse(self.server.state.gpu_mapping_in_progress)
            self.now += 30
            self.service.check()
            self.assertEqual(slow.call_count, 1)
            self.now += 300
            self.service.check()
            self.assertEqual(slow.call_count, 2)

    def test_reordered_second_ls_report_rolls_back_xml_and_helper_map(self):
        before = self.settings.path.read_bytes()
        def launch(*_):
            self.running = True
            return report(5 if launch.calls == 0 else 2)
        launch.calls = 0
        def counted(*args):
            result = launch(*args)
            launch.calls += 1
            return result
        with patch.object(self.service, "_launch_probe", side_effect=counted):
            with self.assertRaisesRegex(RuntimeError, "rolled back"):
                self.service._remap(self.exe, inventory())
        self.assertEqual(self.config.ls_gpu_id_map[GPU_B], 2)
        self.assertEqual(self.profile.native_scaling_settings["PreferredGpuId"], "2")
        self.assertEqual(self.settings.read()["profiles"][0]["PreferredGpuId"], "2")
        self.assertIn("<Unknown>keep</Unknown>", self.settings.path.read_text())
        self.assertFalse(self.running)
        self.assertFalse(self.settings.path.with_suffix(".xml.gpu-refresh-recovery").exists())

    def test_save_failure_rolls_back_and_restores_preexisting_idle_host(self):
        self.running = True
        with patch.object(self.service, "_launch_probe", side_effect=self.launch_report), \
                patch.object(self.manager, "save_config", side_effect=OSError("fixture save error")):
            with self.assertRaisesRegex(OSError, "fixture save error"):
                self.service._remap(self.exe, inventory())
        self.assertTrue(self.running)
        self.watcher.launch_lossless_scaling.assert_called_once_with(force=True)
        self.assertEqual(self.settings.read()["profiles"][0]["PreferredGpuId"], "2")
        self.assertEqual(self.config.ls_gpu_id_map[GPU_B], 2)

    def test_xml_batch_rejects_missing_duplicate_and_auto_without_partial_write(self):
        before = self.settings.path.read_bytes()
        for bad in ({"Missing": 6}, {"Game": 6, "Auto": 1}, {"Game": 0}):
            with self.assertRaises(ValueError):
                self.settings.update_gpu_indexes(bad)
            self.assertEqual(self.settings.path.read_bytes(), before)

    def test_pid_reuse_foreign_filename_and_newer_other_pid_do_not_poison_live_report(self):
        directory = self.root / "diagnostics"
        directory.mkdir()
        first = directory / "ls-adapter-report-42.json"
        first.write_text(json.dumps(report()), encoding="utf-8")
        second = directory / "ls-adapter-report-99.json"
        second.write_text(json.dumps(dict(report(2), pid=99)), encoding="utf-8")
        self.assertEqual(adapter_report.load_latest(directory, pid=42)["lsAdapterNames"], report()["lsAdapterNames"])
        old = time.time() - 20
        os.utime(first, (old, old))
        self.assertIsNone(adapter_report.load_latest(directory, pid=42, created_after=time.time() - 5))
        first.write_text(json.dumps(dict(report(), pid=99)), encoding="utf-8")
        self.assertIsNone(adapter_report.load_latest(directory, pid=42))

    def test_probe_waits_for_nonce_and_pid_from_its_own_launch(self):
        token = "ab" * 32
        wrong = dict(report(), requestId="cd" * 32)
        right = dict(report(), requestId=token)
        other_pid = dict(right, pid=99)
        self.service.process_lookup = lambda _: (42, time.time())
        with patch.object(refresh.secrets, "token_hex", return_value=token), \
                patch.object(refresh.startup_guard, "launch_hidden", return_value=42) as launch, \
                patch.object(adapter_report, "load_latest", side_effect=[wrong, other_pid, right]), \
                patch.object(self.service.stopping, "wait"):
            self.assertEqual(self.service._launch_probe(self.exe), right)
        self.assertEqual(launch.call_args.kwargs["probe_token"], token)

    def test_probe_will_not_launch_if_native_auto_scale_cannot_be_disabled(self):
        with patch.object(self.settings, "native_auto_scale_enabled", return_value=True), \
                patch.object(self.settings, "disable_native_auto_scale", return_value=False), \
                patch.object(refresh.startup_guard, "launch_hidden") as launch:
            with self.assertRaisesRegex(RuntimeError, "Auto Scale"):
                self.service._launch_probe(self.exe)
        launch.assert_not_called()

    def test_cancelled_probe_never_waits_for_full_timeout(self):
        self.service.stop()
        with patch.object(refresh.startup_guard, "launch_hidden", return_value=42):
            with self.assertRaisesRegex(RuntimeError, "shutdown"):
                self.service._launch_probe(self.exe)

    def test_router_uses_exact_pid_report_instead_of_newest_other_process(self):
        directory = self.root / "diagnostics"
        directory.mkdir()
        (directory / "ls-adapter-report-42.json").write_text(json.dumps(report()), encoding="utf-8")
        (directory / "ls-adapter-report-99.json").write_text(json.dumps(dict(report(2), pid=99)), encoding="utf-8")
        router = GpuRouter(display_enumerator=lambda: [], adapter_report_dir=directory,
                           adapter_report_process=lambda: (42, time.time() - 5))
        self.assertEqual(router._dxgi_adapter_counts()["VEN_10DE&DEV_2203"], 5)


if __name__ == "__main__":
    unittest.main()
