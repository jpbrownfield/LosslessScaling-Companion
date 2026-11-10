"""Route-only GPU changes must not restart Lossless Scaling in a loop.

Regression tests for the FPS collapse where every foreground switch between
windows on different displays stopped LS, rewrote Settings.xml, and relaunched
LS (~15 restarts in 4 minutes), dropping scaling and forcing WGC re-capture.
"""
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path
from unittest.mock import Mock

from companion.core.profile_manager import ProfileManager
from companion.core.state import AppState
from companion.services.automation import AutomationController
from companion.services.gpu_router import GpuRouter


RTX_3090 = r"PCI\VEN_10DE&DEV_2203&SUBSYS_50901462"
RTX_5050 = r"PCI\VEN_10DE&DEV_2D83&SUBSYS_53811462"


def adapters():
    return [
        {"deviceName": r"\\.\DISPLAY1", "name": "NVIDIA GeForce RTX 3090 Ti",
         "deviceId": RTX_3090, "primary": True},
        {"deviceName": r"\\.\DISPLAY2", "name": "NVIDIA GeForce RTX 5050",
         "deviceId": RTX_5050, "primary": False},
    ]


def make_automation(**config_overrides):
    manager = ProfileManager(Path(tempfile.mkdtemp()))
    for key, value in config_overrides.items():
        setattr(manager.config, key, value)
    state = AppState()
    watcher = Mock()
    watcher.check_is_lossless_scaling_running.return_value = True
    watcher.stop_lossless_scaling.return_value = True
    watcher.launch_lossless_scaling.return_value = True
    settings = Mock()
    settings.gpu_route_changes_required.return_value = True
    settings.update_gpu_route.return_value = True
    automation = AutomationController(
        manager,
        state,
        watcher,
        lossless_settings=settings,
    )
    automation.deployment_manager = Mock()
    automation.deployment_manager.needs_change.return_value = False
    automation.deployment_manager.apply.return_value = {"files": []}
    automation.deployment_manager.runtime_packages_need_state.return_value = False
    return automation, manager, state, watcher, settings


def game_profile(manager, title="Gaming"):
    profile = manager.config.profiles[0].model_copy(deep=True)
    profile.id = "game-test"
    profile.name = "Game Test"
    profile.lossless_profile_title = title
    profile.auto_scale = False
    profile.native_scaling_settings = {"PreferredGpuId": "0", "OutputDisplayId": "0"}
    return profile


class PinnedGpuTests(unittest.TestCase):
    def test_pinned_gpu_wins_over_auto_route_window_detection(self):
        router = GpuRouter(lambda: adapters(), lambda hwnd: r"\\.\DISPLAY1")
        manager = ProfileManager(Path(tempfile.mkdtemp()))
        manager.config.auto_route_gpu_to_display = True
        manager.config.preferred_scaling_gpu_device_id = RTX_5050
        manager.config.preferred_scaling_gpu_name = "NVIDIA GeForce RTX 5050"
        automation = AutomationController(manager, AppState(), gpu_router=router)

        route = automation._desired_gpu_route(None, 999)

        self.assertEqual(route["gpuName"], "NVIDIA GeForce RTX 5050")
        self.assertEqual(route["lsGpuId"], 2)

    def test_unpinned_auto_route_still_follows_window(self):
        router = GpuRouter(lambda: adapters(), lambda hwnd: r"\\.\DISPLAY1")
        manager = ProfileManager(Path(tempfile.mkdtemp()))
        manager.config.auto_route_gpu_to_display = True
        automation = AutomationController(manager, AppState(), gpu_router=router)

        route = automation._desired_gpu_route(None, 999)

        self.assertEqual(route["deviceName"], r"\\.\DISPLAY1")
        self.assertEqual(route["lsGpuId"], 1)


class RouteOnlyRestartTests(unittest.TestCase):
    def test_route_only_change_while_scaling_never_restarts(self):
        automation, manager, state, watcher, settings = make_automation(
            auto_route_gpu_to_display=True)
        state.is_scaling_active = True
        profile = game_profile(manager)

        automation.activate_profile(profile, target_pid=42, target_hwnd=82)

        watcher.stop_lossless_scaling.assert_not_called()
        watcher.launch_lossless_scaling.assert_not_called()
        settings.update_gpu_route.assert_not_called()
        self.assertEqual(state.current_active_profile.id, "game-test")

    def test_route_only_change_within_cooldown_is_deferred(self):
        automation, manager, state, watcher, settings = make_automation(
            auto_route_gpu_to_display=True)
        automation._last_gpu_route_relaunch_at = time.monotonic()
        profile = game_profile(manager)

        automation.activate_profile(profile, target_pid=42, target_hwnd=82)

        watcher.stop_lossless_scaling.assert_not_called()
        watcher.launch_lossless_scaling.assert_not_called()
        settings.update_gpu_route.assert_not_called()

    def test_route_only_change_after_cooldown_restarts_once(self):
        automation, manager, state, watcher, settings = make_automation(
            auto_route_gpu_to_display=True)
        automation._last_gpu_route_relaunch_at = 0.0
        profile = game_profile(manager)

        automation.activate_profile(profile, target_pid=42, target_hwnd=82)

        watcher.stop_lossless_scaling.assert_called_once_with()
        settings.update_gpu_route.assert_called_once()
        watcher.launch_lossless_scaling.assert_called_once_with(force=True)

    def test_file_change_still_restarts_despite_active_scaling(self):
        automation, manager, state, watcher, settings = make_automation(
            auto_route_gpu_to_display=True)
        automation.deployment_manager.needs_change.return_value = True
        state.is_scaling_active = True
        state.current_active_profile = None
        profile = game_profile(manager)

        automation.activate_profile(profile, target_pid=42, target_hwnd=82)

        watcher.stop_lossless_scaling.assert_called_once_with()
        watcher.launch_lossless_scaling.assert_called_once_with(force=True)


class CalibratedGpuIdTests(unittest.TestCase):
    """LS GPU IDs follow the calibrated PCI map, not enumeration order."""

    def test_calibrated_map_reorders_ls_ids(self):
        router = GpuRouter(
            lambda: adapters(), lambda hwnd: None,
            ls_gpu_id_map={RTX_5050: 1, RTX_3090: 2},
        )
        inventory = router.detect()
        by_name = {gpu["name"]: gpu["lsGpuId"] for gpu in inventory["gpus"]}
        self.assertEqual(by_name["NVIDIA GeForce RTX 5050"], 1)
        self.assertEqual(by_name["NVIDIA GeForce RTX 3090 Ti"], 2)
        by_display = {d["deviceName"]: d["lsGpuId"] for d in inventory["displays"]}
        self.assertEqual(by_display[r"\\.\DISPLAY2"], 1)
        self.assertEqual(by_display[r"\\.\DISPLAY1"], 2)

    def test_calibrated_map_matches_ven_dev_prefix(self):
        router = GpuRouter(
            lambda: adapters(), lambda hwnd: None,
            ls_gpu_id_map={r"PCI\VEN_10DE&DEV_2D83": 1},
        )
        by_name = {g["name"]: g["lsGpuId"] for g in router.detect()["gpus"]}
        self.assertEqual(by_name["NVIDIA GeForce RTX 5050"], 1)
        # Unmapped GPUs fill the remaining IDs in GDI order without collision.
        self.assertEqual(by_name["NVIDIA GeForce RTX 3090 Ti"], 2)

    def test_no_map_keeps_gdi_order(self):
        router = GpuRouter(lambda: adapters(), lambda hwnd: None)
        by_name = {g["name"]: g["lsGpuId"] for g in router.detect()["gpus"]}
        self.assertEqual(by_name["NVIDIA GeForce RTX 3090 Ti"], 1)
        self.assertEqual(by_name["NVIDIA GeForce RTX 5050"], 2)

    def test_route_for_ls_ids_uses_calibrated_map(self):
        router = GpuRouter(
            lambda: adapters(), lambda hwnd: r"\\.\DISPLAY2",
            ls_gpu_id_map={RTX_5050: 1, RTX_3090: 2},
        )
        route = router.route_for_ls_ids(1, 2)
        self.assertEqual(route["gpuName"], "NVIDIA GeForce RTX 5050")
        self.assertEqual(route["deviceName"], r"\\.\DISPLAY2")

    def test_calibrated_display_map_reorders_ls_outputs(self):
        router = GpuRouter(
            lambda: adapters(), lambda hwnd: None,
            ls_display_id_map={r"\\.\DISPLAY2": 5},
        )
        by_display = {d["deviceName"]: d["lsDisplayId"]
                      for d in router.detect()["displays"]}
        self.assertEqual(by_display[r"\\.\DISPLAY2"], 5)
        # Unmapped displays fill the remaining IDs without colliding.
        self.assertEqual(by_display[r"\\.\DISPLAY1"], 1)

    def test_window_route_uses_calibrated_display_id(self):
        router = GpuRouter(
            lambda: adapters(), lambda hwnd: r"\\.\DISPLAY2",
            ls_display_id_map={r"\\.\DISPLAY2": 5},
        )
        route = router.route_for_window(999)
        self.assertEqual(route["deviceName"], r"\\.\DISPLAY2")
        self.assertEqual(route["lsDisplayId"], 5)

    def test_duplicate_recipes_survive_for_every_window(self):
        # Both monitors validated against the same working LS route.
        router = GpuRouter(
            lambda: adapters(), lambda hwnd: r"\\.\DISPLAY1",
            ls_gpu_id_map={RTX_5050: 1, RTX_3090: 1},
            ls_display_id_map={r"\\.\DISPLAY1": 2, r"\\.\DISPLAY2": 2},
        )
        inventory = router.detect()
        by_display = {d["deviceName"]: (d["lsGpuId"], d["lsDisplayId"])
                      for d in inventory["displays"]}
        self.assertEqual(by_display[r"\\.\DISPLAY1"], (1, 2))
        self.assertEqual(by_display[r"\\.\DISPLAY2"], (1, 2))

    def test_pin_resolves_output_display_never_zero(self):
        router = GpuRouter(
            lambda: adapters(), lambda hwnd: r"\\.\DISPLAY1",
            ls_gpu_id_map={RTX_5050: 1, RTX_3090: 2},
            ls_display_id_map={r"\\.\DISPLAY1": 1, r"\\.\DISPLAY2": 2},
        )
        with tempfile.TemporaryDirectory() as directory:
            manager = ProfileManager(Path(directory))
            manager.config.auto_route_gpu_to_display = False
            manager.config.preferred_scaling_gpu_device_id = RTX_5050
            manager.config.preferred_scaling_gpu_name = "NVIDIA GeForce RTX 5050"
            automation = AutomationController(
                manager, AppState(), gpu_router=router)
            route = automation._desired_gpu_route(None, None)
        self.assertEqual(route["lsGpuId"], 1)
        self.assertEqual(route["lsDisplayId"], 2)

    def test_pin_resolves_through_calibrated_map(self):
        router = GpuRouter(
            lambda: adapters(), lambda hwnd: r"\\.\DISPLAY1",
            ls_gpu_id_map={RTX_5050: 1, RTX_3090: 2},
        )
        with tempfile.TemporaryDirectory() as directory:
            manager = ProfileManager(Path(directory))
            manager.config.auto_route_gpu_to_display = False
            manager.config.preferred_scaling_gpu_device_id = RTX_5050
            manager.config.preferred_scaling_gpu_name = "NVIDIA GeForce RTX 5050"
            automation = AutomationController(
                manager, AppState(), gpu_router=router)
            route = automation._desired_gpu_route(None, None)
        self.assertEqual(route["lsGpuId"], 1)
        self.assertEqual(route["gpuName"], "NVIDIA GeForce RTX 5050")

    def test_pin_survives_config_reload_with_auto_route(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = ProfileManager(Path(directory))
            manager.config.auto_route_gpu_to_display = True
            manager.config.preferred_scaling_gpu_device_id = RTX_5050
            manager.save_config()
            reloaded = ProfileManager(Path(directory))
            reloaded.load_config()
        self.assertTrue(reloaded.config.auto_route_gpu_to_display)
        self.assertEqual(
            reloaded.config.preferred_scaling_gpu_device_id, RTX_5050)


class GpuMapFreshnessTests(unittest.TestCase):
    def test_fingerprint_ignores_enumeration_order(self):
        from companion.services.gpu_router import inventory_fingerprint

        router = GpuRouter(lambda: adapters(), lambda hwnd: None)
        forward = inventory_fingerprint(router.detect())
        router = GpuRouter(lambda: list(reversed(adapters())), lambda hwnd: None)
        self.assertEqual(inventory_fingerprint(router.detect()), forward)

    def test_fingerprint_changes_with_topology(self):
        from companion.services.gpu_router import inventory_fingerprint

        router = GpuRouter(lambda: adapters(), lambda hwnd: None)
        before = inventory_fingerprint(router.detect())
        extra = adapters() + [
            {"deviceName": r"\\.\DISPLAY3", "name": "Virtual Display",
             "deviceId": r"PCI\VEN_10DE&DEV_2203&SUBSYS_50901462", "primary": False},
        ]
        router = GpuRouter(lambda: extra, lambda hwnd: None)
        self.assertNotEqual(inventory_fingerprint(router.detect()), before)

    def _controller(self, **config_overrides):
        manager = ProfileManager(Path(tempfile.mkdtemp()))
        for key, value in config_overrides.items():
            setattr(manager.config, key, value)
        router = GpuRouter(lambda: adapters(), lambda hwnd: None)
        automation = AutomationController(
            manager, AppState(), gpu_router=router)
        return automation, manager

    def test_status_unnecessary_on_single_gpu(self):
        router = GpuRouter(lambda: adapters()[:1], lambda hwnd: None)
        with tempfile.TemporaryDirectory() as directory:
            manager = ProfileManager(Path(directory))
            automation = AutomationController(
                manager, AppState(), gpu_router=router)
            self.assertEqual(
                automation.gpu_map_status()["state"], "unnecessary")

    def test_status_missing_without_map(self):
        automation, _ = self._controller()
        self.assertEqual(automation.gpu_map_status()["state"], "missing")

    def test_status_ok_with_matching_fingerprint(self):
        from companion.services.gpu_router import inventory_fingerprint

        live = inventory_fingerprint(
            GpuRouter(lambda: adapters(), lambda hwnd: None).detect())
        automation, _ = self._controller(
            ls_gpu_id_map={RTX_3090: 2, RTX_5050: 1}, ls_gpu_map_fingerprint=live)
        status = automation.gpu_map_status()
        self.assertEqual(status["state"], "ok")
        self.assertEqual(status["mappedGpus"], 2)

    def test_partial_map_is_not_reported_as_fully_valid(self):
        from companion.services.gpu_router import inventory_fingerprint
        live = inventory_fingerprint(GpuRouter(lambda: adapters(), lambda hwnd: None).detect())
        automation, _ = self._controller(
            ls_gpu_id_map={RTX_5050: 6}, ls_gpu_map_fingerprint=live)
        status = automation.gpu_map_status()
        self.assertEqual(status["state"], "missing")
        self.assertEqual(status["mappedGpus"], 1)

    def test_aliases_do_not_inflate_mapped_gpu_count(self):
        from companion.services.gpu_router import inventory_fingerprint
        live = inventory_fingerprint(GpuRouter(lambda: adapters(), lambda hwnd: None).detect())
        automation, _ = self._controller(
            ls_gpu_id_map={RTX_5050: 6, r"PCI\VEN_10DE&DEV_2D83": 6},
            ls_gpu_map_fingerprint=live)
        self.assertEqual(automation.gpu_map_status()["mappedGpus"], 1)
        self.assertEqual(automation.gpu_map_status()["state"], "missing")

    def test_map_without_fingerprint_or_with_conflicting_slots_is_stale(self):
        from companion.services.gpu_router import inventory_fingerprint
        live = inventory_fingerprint(GpuRouter(lambda: adapters(), lambda hwnd: None).detect())
        for mapping, fingerprint in [({RTX_3090: 1, RTX_5050: 2}, None),
                                     ({RTX_3090: 2, RTX_5050: 2}, live)]:
            with self.subTest(mapping=mapping, fingerprint=fingerprint):
                automation, _ = self._controller(ls_gpu_id_map=mapping, ls_gpu_map_fingerprint=fingerprint)
                self.assertEqual(automation.gpu_map_status()["state"], "stale")

    def test_status_stale_on_topology_drift(self):
        automation, _ = self._controller(
            ls_gpu_id_map={RTX_5050: 1}, ls_gpu_map_fingerprint="deadbeef")
        status = automation.gpu_map_status()
        self.assertEqual(status["state"], "stale")
        self.assertNotEqual(status["liveFingerprint"], "deadbeef")

    def test_rollback_restores_previous_map(self):
        automation, manager = self._controller(
            ls_gpu_id_map={RTX_5050: 1},
            ls_gpu_id_map_previous={RTX_5050: 2},
            ls_display_id_map={r"\\.\DISPLAY2": 5},
            ls_display_id_map_previous={r"\\.\DISPLAY2": 3})
        self.assertTrue(automation.rollback_gpu_map())
        self.assertEqual(manager.config.ls_gpu_id_map, {RTX_5050: 2})
        self.assertEqual(manager.config.ls_display_id_map,
                         {r"\\.\DISPLAY2": 3})

    def test_rollback_without_previous_returns_false(self):
        automation, _ = self._controller(ls_gpu_id_map={RTX_5050: 1})
        self.assertFalse(automation.rollback_gpu_map())

    def test_gpu_routing_diagnostic_fails_stale_passes_ok(self):
        from types import SimpleNamespace

        from companion.services.diagnostic_runner import DiagnosticRunner

        from companion.services.gpu_router import inventory_fingerprint

        live = inventory_fingerprint(
            GpuRouter(lambda: adapters(), lambda hwnd: None).detect())
        automation, _ = self._controller(
            ls_gpu_id_map={RTX_5050: 1}, ls_gpu_map_fingerprint="deadbeef")
        runner = DiagnosticRunner.__new__(DiagnosticRunner)
        runner.server = SimpleNamespace(config=automation.profile_manager.config,
                                        automation=automation, state=automation.state)
        runner._path = str
        stale = runner._gpu_routing()
        self.assertEqual(stale["status"], "fail")
        automation.profile_manager.config.ls_gpu_id_map[RTX_3090] = 2
        automation.profile_manager.config.ls_gpu_map_fingerprint = live
        self.assertEqual(runner._gpu_routing()["status"], "pass")


class RouteVerifyRepairTests(unittest.TestCase):
    def _controller(self, **config_overrides):
        manager = ProfileManager(Path(tempfile.mkdtemp()))
        for key, value in config_overrides.items():
            setattr(manager.config, key, value)
        router = GpuRouter(lambda: adapters(), lambda hwnd: r"\\.\DISPLAY2")
        watcher = Mock()
        watcher.check_is_lossless_scaling_running.return_value = True
        watcher.stop_lossless_scaling.return_value = True
        watcher.launch_lossless_scaling.return_value = True
        settings = Mock()
        from companion.services.ls_settings import LosslessSettingsXml
        settings.resolve_live_title = LosslessSettingsXml.resolve_live_title
        settings.read.return_value = {"profiles": [
            {"Title": "Default", "PreferredGpuId": "2", "OutputDisplayId": "1"}]}
        settings.update_gpu_route.return_value = True
        automation = AutomationController(
            manager, AppState(), watcher, gpu_router=router,
            lossless_settings=settings,
            presentmon_provider=lambda: Path(tempfile.mkdtemp()) / "pm.exe",
        )
        automation._wait_for_ls_ready = Mock(return_value=True)
        automation._current_adapter_report = Mock(return_value={
            "device": {"vendorId": 0x10DE, "deviceId": 0x2D83}})
        return automation, manager, watcher, settings

    def test_verification_skipped_without_provider(self):
        automation, _, _, _ = self._controller()
        automation.presentmon_provider = None
        self.assertFalse(automation._route_verification_due())

    def test_verification_due_when_map_missing(self):
        automation, _, _, _ = self._controller()
        self.assertTrue(automation._route_verification_due())

    def test_verification_not_due_after_proof(self):
        automation, manager, _, _ = self._controller(
            ls_gpu_id_map={RTX_5050: 1},
            ls_gpu_map_fingerprint="fp")
        with unittest.mock.patch.object(
                AutomationController, "gpu_map_status",
                return_value={"state": "ok"}):
            automation.state.current_gpu_route_key = "1:2:x"
            automation._last_verified_route_key = "1:2:x"
            self.assertFalse(automation._route_verification_due())
            automation._last_verified_route_key = "other"
            self.assertTrue(automation._route_verification_due())

    def test_verify_reports_health(self):
        from unittest.mock import patch

        automation, _, _, _ = self._controller()
        automation._adapter_report_process = Mock(return_value=(1, 100))
        summary = {"application": "LosslessScaling.exe", "process_id": 1,
                   "rows": 100,
                   "metrics": {"displayed_fps": {"median": 120.0}}}
        with patch("companion.services.performance_benchmark.PresentMonCapture"), \
             patch("companion.services.benchmark_metrics.summarize_presentmon_files",
                   return_value=[summary]):
            self.assertTrue(automation._verify_active_route())
        summary["metrics"]["displayed_fps"]["median"] = 2.0
        with patch("companion.services.performance_benchmark.PresentMonCapture"), \
             patch("companion.services.benchmark_metrics.summarize_presentmon_files",
                   return_value=[summary]):
            self.assertFalse(automation._verify_active_route())

    def test_verification_ignores_output_from_another_ls_process(self):
        automation, _, _, _ = self._controller()
        automation._adapter_report_process = Mock(return_value=(1, 100))
        summaries = [
            {"application": "LosslessScaling.exe", "process_id": 99,
             "metrics": {"displayed_fps": {"median": 120}}},
            {"application": "LosslessScaling.exe", "process_id": 1,
             "metrics": {"displayed_fps": {"median": 2}}}]
        with unittest.mock.patch("companion.services.performance_benchmark.PresentMonCapture"), \
                unittest.mock.patch("companion.services.benchmark_metrics.summarize_presentmon_files", return_value=summaries):
            self.assertFalse(automation._verify_active_route())

    def test_verification_discards_capture_if_ls_restarts_or_pid_is_reused(self):
        automation, _, _, _ = self._controller()
        automation._adapter_report_process = Mock(side_effect=[(1, 100), (1, 200)])
        with unittest.mock.patch("companion.services.performance_benchmark.PresentMonCapture"), \
                unittest.mock.patch("companion.services.benchmark_metrics.summarize_presentmon_files", return_value=[{
                    "application": "LosslessScaling.exe", "process_id": 1,
                    "metrics": {"displayed_fps": {"median": 120}}}]):
            self.assertIsNone(automation._verify_active_route())

    def test_reshaping_check_inspects_ls_process_instead_of_game(self):
        automation, _, _, _ = self._controller()
        automation._adapter_report_process = Mock(return_value=(123, 100))
        automation._verify_active_route = Mock(return_value=True)
        with unittest.mock.patch("companion.services.adapter_report.detect_reshaping_layers", return_value=[]) as detect:
            automation._verify_and_repair_route(None, "test", 42, 82)
        detect.assert_called_once_with(pid=123)

    def test_verify_returns_none_without_presentmon(self):
        automation, _, _, _ = self._controller()
        automation.presentmon_provider = lambda: None
        self.assertIsNone(automation._verify_active_route())

    def test_repair_persists_winning_recipe(self):
        from types import SimpleNamespace

        automation, manager, watcher, settings = self._controller()
        profile = SimpleNamespace(lossless_profile_title="Default", name="Game",
                                  id="game")
        automation.set_scaling = Mock(return_value=True)
        automation._verify_active_route = Mock(side_effect=[False, True])
        automation._current_adapter_report = Mock(return_value={
            "device": {"vendorId": 0x10DE, "deviceId": 0x2D83}})
        automation._wait_for_ls_ready = Mock(return_value=True)
        self.assertTrue(automation._repair_route(
            profile, "focus", target_pid=42, target_hwnd=82))
        # First candidate is GPU 1 on the window's display; a lost hotkey
        # looks identical to a wrong route, so one resend is attempted
        # before moving on. The resend proves frames, persisting (1, 2).
        self.assertEqual(manager.config.ls_gpu_id_map[r"PCI\VEN_10DE&DEV_2D83"], 1)
        self.assertEqual(manager.config.ls_display_id_map[r"\\.\DISPLAY2"], 2)
        self.assertTrue(settings.update_gpu_route.called)
        self.assertIsNotNone(manager.config.ls_gpu_map_fingerprint)

    def test_repair_moves_on_after_resend_still_starved(self):
        from types import SimpleNamespace

        automation, manager, watcher, settings = self._controller()
        profile = SimpleNamespace(lossless_profile_title="Default", name="Game",
                                  id="game")
        automation.set_scaling = Mock(return_value=True)
        automation._verify_active_route = Mock(
            side_effect=[False, False, False, True])
        self.assertTrue(automation._repair_route(
            profile, "focus", target_pid=42, target_hwnd=82))
        # First candidate stays starved through its resend, so the sweep
        # moves on; the second candidate proves frames on its resend.
        self.assertEqual(manager.config.ls_gpu_id_map[r"PCI\VEN_10DE&DEV_2D83"], 2)
        self.assertEqual(manager.config.ls_display_id_map[r"\\.\DISPLAY2"], 2)
        self.assertTrue(settings.update_gpu_route.called)
        self.assertIsNotNone(manager.config.ls_gpu_map_fingerprint)

    def test_repair_restore_on_total_failure(self):
        from types import SimpleNamespace

        automation, manager, watcher, settings = self._controller()
        profile = SimpleNamespace(lossless_profile_title="Default", name="Game",
                                  id="game")
        automation.set_scaling = Mock(return_value=True)
        automation._verify_active_route = Mock(return_value=False)
        self.assertFalse(automation._repair_route(
            profile, "focus", target_pid=None, target_hwnd=None))
        # Original route (2, 1) is written back after exhaustion.
        self.assertIn(unittest.mock.call("Default", 2, 1),
                      settings.update_gpu_route.call_args_list)

    def test_no_repair_during_benchmarks(self):
        automation, _, watcher, _ = self._controller()
        automation.state.benchmark_mode_active = True
        automation._verify_active_route = Mock(return_value=False)
        self.assertFalse(automation._verify_and_repair_route(
            None, "test", None, None))
        watcher.stop_lossless_scaling.assert_not_called()

    def test_healthy_frames_on_wrong_gpu_trigger_repair(self):
        automation, _, _, _ = self._controller(
            preferred_scaling_gpu_device_id=RTX_5050,
            preferred_scaling_gpu_name="NVIDIA GeForce RTX 5050")
        automation._verify_active_route = Mock(return_value=True)
        automation._current_adapter_report = Mock(return_value={
            "device": {"vendorId": 0x10DE, "deviceId": 0x2203}})
        automation._repair_route = Mock(return_value=True)
        self.assertTrue(automation._verify_and_repair_route(None, "test", 42, 82))
        automation._repair_route.assert_called_once_with(None, "test", 42, 82)

    def test_repair_does_not_attribute_previous_process_gpu_to_new_candidate(self):
        automation, manager, _, _ = self._controller()
        automation._wait_for_ls_ready = Mock(return_value=True)
        automation.set_scaling = Mock(return_value=True)
        automation._verify_active_route = Mock(return_value=True)
        reports = iter([{"device": {"vendorId": 0x10DE, "deviceId": 0x2D83}}])
        automation._current_adapter_report = Mock(side_effect=lambda: next(
            reports, {"device": {"vendorId": 0x10DE, "deviceId": 0x2203}}))
        self.assertFalse(automation._repair_route(None, "test", 42, 82))
        self.assertEqual(manager.config.ls_gpu_id_map, {})

    def test_profile_gpu_choice_takes_precedence_over_global_pin(self):
        automation, manager, _, _ = self._controller(
            preferred_scaling_gpu_device_id=RTX_5050,
            preferred_scaling_gpu_name="NVIDIA GeForce RTX 5050")
        profile = manager.config.profiles[0]
        profile.native_scaling_settings["PreferredGpuId"] = "1"
        profile.preferred_gpu_device_id = RTX_3090
        profile.preferred_gpu_name = "NVIDIA GeForce RTX 3090 Ti"
        self.assertEqual(automation._intended_rendering_gpu(profile, RTX_5050), RTX_3090)

    def test_verified_short_identity_updates_full_identity_used_for_routing(self):
        automation, manager, _, _ = self._controller(ls_gpu_id_map={RTX_5050: 2})
        automation._persist_verified_route(r"PCI\VEN_10DE&DEV_2D83", r"\\.\DISPLAY2", 6, 2)
        from companion.services.gpu_router import _ls_id_for_device, _normalize_gpu_map
        self.assertEqual(_ls_id_for_device(RTX_5050, _normalize_gpu_map(manager.config.ls_gpu_id_map)), 6)


if __name__ == "__main__":
    unittest.main()
