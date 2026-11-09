"""GPU calibration drills: identify LS GPU indices via the benchmark workload.

Covers AutomationController.calibrate_gpu_map without touching real
hardware: single-GPU short-circuit, missing-tool errors, scaling-active
refusal, and a two-GPU success path that persists the calibrated map.
"""
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from unittest.mock import Mock, patch

from companion.core.profile_manager import ProfileManager
from companion.core.state import AppState
from companion.services.automation import AutomationController
from companion.services.gpu_router import GpuRouter
from companion.services.ls_settings import LosslessSettingsXml


def bind_live_title(settings):
    """Mock settings still resolve titles like the real reader."""
    settings.resolve_live_title = LosslessSettingsXml.resolve_live_title
    return settings


RTX_3090 = r"PCI\VEN_10DE&DEV_2203&SUBSYS_50901462"
RTX_5050 = r"PCI\VEN_10DE&DEV_2D83&SUBSYS_53811462"


def two_gpu_inventory():
    return {
        "gpus": [
            {"deviceId": RTX_3090, "name": "NVIDIA GeForce RTX 3090 Ti",
             "vendor": "NVIDIA", "lsGpuId": 1,
             "connectedDisplays": ["Generic PnP Monitor"]},
            {"deviceId": RTX_5050, "name": "NVIDIA GeForce RTX 5050",
             "vendor": "NVIDIA", "lsGpuId": 2,
             "connectedDisplays": ["Alienware AW2725Q"]},
        ],
        "displays": [
            {"deviceName": r"\\.\DISPLAY1", "name": "Generic PnP Monitor",
             "deviceId": RTX_3090, "gpuDeviceId": RTX_3090,
             "lsDisplayId": 1, "primary": True},
            {"deviceName": r"\\.\DISPLAY2", "name": "Alienware AW2725Q",
             "deviceId": RTX_5050, "gpuDeviceId": RTX_5050,
             "lsDisplayId": 2, "primary": False},
        ],
    }


def one_gpu_inventory():
    inventory = two_gpu_inventory()
    return {"gpus": inventory["gpus"][:1], "displays": inventory["displays"][:1]}


def make_automation(inventory):
    manager = ProfileManager(Path(tempfile.mkdtemp()))
    router = GpuRouter(
        lambda: [{"deviceName": d["deviceName"], "name": d["name"],
                  "deviceId": d["deviceId"], "primary": d.get("primary", False)}
                 for d in inventory["displays"]],
        lambda hwnd: r"\\.\DISPLAY1",
    )
    state = AppState()
    watcher = Mock()
    watcher.check_is_lossless_scaling_running.return_value = False
    watcher.stop_lossless_scaling.return_value = True
    watcher.launch_lossless_scaling.return_value = True
    settings = bind_live_title(Mock())
    automation = AutomationController(
        manager, state, watcher, gpu_router=router, lossless_settings=settings,
    )
    automation.deployment_manager = Mock()
    automation.deployment_manager.needs_change.return_value = False
    automation.window_manager = Mock()
    automation.window_manager.move_window_to_display.return_value = True
    automation.activate_profile = Mock()
    automation.set_scaling = Mock(return_value=True)
    automation._wait_for_ls_ready = Mock(return_value=True)
    return automation, manager, state, watcher, settings


def settings_snapshot(manager):
    default = next(
        (item for item in manager.config.profiles if item.is_default),
        manager.config.profiles[0],
    )
    title = (getattr(default, "lossless_profile_title", None)
             or getattr(default, "name", None) or "Default")
    return {"profiles": [{"Title": title, "PreferredGpuId": "1",
                           "OutputDisplayId": "1"}]}


class CalibrateGpuMapTests(unittest.TestCase):
    def test_single_gpu_returns_unnecessary_without_launching(self):
        inventory = one_gpu_inventory()
        automation, manager, state, watcher, settings = make_automation(inventory)
        automation.gpu_router = GpuRouter(lambda: [], lambda hwnd: None)
        with patch.object(GpuRouter, "detect", return_value=inventory):
            with patch("subprocess.Popen") as popen:
                result = automation.calibrate_gpu_map("C:/tools/LSBenchmark.exe")
        self.assertEqual(result["state"], "unnecessary")
        popen.assert_not_called()

    def test_scaling_active_refuses_calibration(self):
        inventory = two_gpu_inventory()
        automation, manager, state, watcher, settings = make_automation(inventory)
        state.is_scaling_active = True
        with patch.object(GpuRouter, "detect", return_value=inventory):
            with self.assertRaisesRegex(RuntimeError, "Stop the current scaling"):
                automation.calibrate_gpu_map("C:/tools/LSBenchmark.exe")

    def test_missing_presentmon_raises(self):
        inventory = two_gpu_inventory()
        automation, manager, state, watcher, settings = make_automation(inventory)
        automation.presentmon_provider = lambda: None
        workload = Path(tempfile.mkdtemp()) / "LSBenchmark.exe"
        workload.write_bytes(b"fake")
        with patch.object(GpuRouter, "detect", return_value=inventory):
            with self.assertRaisesRegex(RuntimeError, "PresentMon"):
                automation.calibrate_gpu_map(workload)

    def test_two_gpu_success_persists_map_and_restores_route(self):
        inventory = two_gpu_inventory()
        automation, manager, state, watcher, settings = make_automation(inventory)
        automation.presentmon_provider = lambda: Path("C:/tools/PresentMon.exe")
        workload = Path(tempfile.mkdtemp()) / "LSBenchmark.exe"
        workload.write_bytes(b"fake")
        settings.read.return_value = settings_snapshot(manager)
        settings.update_gpu_route.return_value = True
        # Both slots produce frames, but each renders on a different GPU.
        automation._verify_active_route = Mock(return_value=True)
        def current_report():
            if not settings.update_gpu_route.call_args:
                return None
            slot = settings.update_gpu_route.call_args.args[1]
            return {"device": {"vendorId": 0x10DE,
                               "deviceId": 0x2203 if slot == 1 else 0x2D83}}
        automation._current_adapter_report = Mock(side_effect=current_report)
        progress_calls = []

        fake_process = Mock()
        fake_process.pid = 4242
        with patch.object(GpuRouter, "detect", return_value=inventory), \
                patch("subprocess.Popen", return_value=fake_process), \
                patch("companion.services.performance_benchmark.find_largest_window",
                      return_value=9876), \
                patch("companion.services.performance_benchmark.focus_window",
                      return_value=True):
            result = automation.calibrate_gpu_map(
                workload,
                progress=lambda stage, detail: progress_calls.append((stage, detail)),
            )

        self.assertEqual(result["state"], "ok")
        self.assertEqual(result["gpuCount"], 2)
        self.assertEqual(len(result["mapped"]), 2)
        self.assertTrue(progress_calls)
        stages = [stage for stage, _ in progress_calls]
        self.assertIn("starting", stages)
        self.assertIn("done", stages)
        # Calibrated map persisted keyed by stable PCI identity.
        stored = dict(manager.config.ls_gpu_id_map or {})
        self.assertIn(RTX_3090, stored)
        self.assertIn(RTX_5050, stored)
        self.assertEqual(stored[RTX_3090], 1)
        self.assertEqual(stored[RTX_5050], 2)
        self.assertIsNotNone(manager.config.ls_gpu_map_fingerprint)
        # Original Default route restored after the sweep.
        restored = [call for call in settings.update_gpu_route.call_args_list
                    if call.args[1:] == (1, 1)]
        self.assertTrue(restored, settings.update_gpu_route.call_args_list)
        # Scaling left idle and the workload cleaned up.
        self.assertFalse(state.is_scaling_active)
        fake_process.terminate.assert_called_once()

    def test_failed_sweep_reports_failure(self):
        inventory = two_gpu_inventory()
        automation, manager, state, watcher, settings = make_automation(inventory)
        automation.presentmon_provider = lambda: Path("C:/tools/PresentMon.exe")
        workload = Path(tempfile.mkdtemp()) / "LSBenchmark.exe"
        workload.write_bytes(b"fake")
        settings.read.return_value = settings_snapshot(manager)
        settings.update_gpu_route.return_value = True
        automation._verify_active_route = Mock(return_value=False)

        fake_process = Mock()
        fake_process.pid = 4242
        with patch.object(GpuRouter, "detect", return_value=inventory), \
                patch("subprocess.Popen", return_value=fake_process), \
                patch("companion.services.performance_benchmark.find_largest_window",
                      return_value=9876), \
                patch("companion.services.performance_benchmark.focus_window",
                      return_value=True):
            result = automation.calibrate_gpu_map(workload)

        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["mapped"], {})

    def run_flowing_calibration(self, report_for_slot, initial_report=None):
        inventory = two_gpu_inventory()
        automation, manager, _, _, settings = make_automation(inventory)
        automation.presentmon_provider = lambda: Path("PresentMon.exe")
        workload = manager.config_dir / "LSBenchmark.exe"
        workload.write_bytes(b"fake")
        settings.read.return_value = settings_snapshot(manager)
        settings.update_gpu_route.return_value = True
        automation._verify_active_route = Mock(return_value=True)

        def report():
            if not settings.update_gpu_route.call_args:
                return initial_report
            return report_for_slot(settings.update_gpu_route.call_args.args[1])

        automation._current_adapter_report = Mock(side_effect=report)
        with patch.object(GpuRouter, "detect", return_value=inventory), \
                patch("subprocess.Popen"), \
                patch("companion.services.performance_benchmark.find_largest_window", return_value=9876), \
                patch("companion.services.performance_benchmark.focus_window", return_value=True):
            result = automation.calibrate_gpu_map(workload)
        return result, manager

    def test_frames_on_3090_never_validate_5050(self):
        result, manager = self.run_flowing_calibration(
            lambda slot: {"device": {"vendorId": 0x10DE, "deviceId": 0x2203}})
        self.assertEqual(result["state"], "partial")
        self.assertEqual(result["mapped"], {RTX_3090: 1})
        self.assertNotIn(RTX_5050, manager.config.ls_gpu_id_map)

    def test_frames_without_device_report_do_not_change_map(self):
        result, manager = self.run_flowing_calibration(lambda slot: None)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(manager.config.ls_gpu_id_map, {})

    def test_duplicate_3090_entries_do_not_hide_5050_at_slot_six(self):
        names = ["NVIDIA GeForce RTX 3090 Ti"] * 5 + ["NVIDIA GeForce RTX 5050"]
        result, manager = self.run_flowing_calibration(
            lambda slot: {"device": {"vendorId": 0x10DE,
                                    "deviceId": 0x2D83 if slot == 6 else 0x2203}},
            initial_report={"lsAdapterNames": names})
        self.assertEqual(result["state"], "ok")
        self.assertEqual(manager.config.ls_gpu_id_map[RTX_5050], 6)


class CalibrationExclusivityTests(unittest.TestCase):
    """Calibration/benchmark runs own LS: automatic starts refuse, stops pass."""

    def make_guarded_automation(self):
        manager = ProfileManager(Path(tempfile.mkdtemp()))
        state = AppState()
        watcher = Mock()
        watcher.check_is_lossless_scaling_running.return_value = True
        watcher.stop_lossless_scaling.return_value = True
        watcher.launch_lossless_scaling.return_value = True
        settings = bind_live_title(Mock())
        trigger = Mock(return_value=False)
        automation = AutomationController(
            manager, state, watcher, lossless_settings=settings,
            hotkey_trigger=trigger,
        )
        automation.deployment_manager = Mock()
        automation.deployment_manager.needs_change.return_value = False
        state.benchmark_mode_active = True
        return automation, state, trigger

    def test_automatic_start_refused_while_flagged(self):
        automation, state, trigger = self.make_guarded_automation()
        self.assertFalse(
            automation.set_scaling(True, reason="focus", force=True))
        trigger.assert_not_called()

    def test_benchmark_and_calibration_starts_allowed(self):
        for reason in ("performance_benchmark", "gpu_calibration",
                       "gpu_calibration_probe"):
            automation, state, trigger = self.make_guarded_automation()
            automation.set_scaling(True, reason=reason, force=True)
            trigger.assert_called_once()

    def test_stops_always_allowed_while_flagged(self):
        automation, state, trigger = self.make_guarded_automation()
        state.is_scaling_active = True
        automation.set_scaling(False, reason="focus", force=True)
        # A graceful stop still emits the toggle hotkey while flagged.
        trigger.assert_called_once()


class ScaleUntilFramesTests(unittest.TestCase):
    """Lost hotkeys resend once when starved; flowing captures never resend."""

    def make_automation(self):
        manager = ProfileManager(Path(tempfile.mkdtemp()))
        state = AppState()
        watcher = Mock()
        automation = AutomationController(
            manager, state, watcher,
            lossless_settings=bind_live_title(Mock()))
        return automation

    def test_confirmed_scaling_accepted_without_resend(self):
        automation = self.make_automation()
        automation.set_scaling = Mock(return_value=True)
        automation._verify_active_route = Mock(return_value=True)
        profile = Mock()
        self.assertTrue(automation._scale_until_frames(
            profile, "gpu_calibration", 42, 82))
        self.assertEqual(automation.set_scaling.call_count, 1)

    def test_starved_scaling_resends_once_then_succeeds(self):
        automation = self.make_automation()
        automation.set_scaling = Mock(return_value=False)
        automation._verify_active_route = Mock(side_effect=[False, True])
        profile = Mock()
        self.assertTrue(automation._scale_until_frames(
            profile, "gpu_calibration", 42, 82))
        self.assertEqual(automation.set_scaling.call_count, 2)
        reasons = [call.kwargs.get("reason", call.args[1] if len(call.args) > 1 else "")
                   for call in automation.set_scaling.call_args_list]
        self.assertTrue(any("retry" in str(reason) for reason in reasons))

    def test_starved_twice_gives_up(self):
        automation = self.make_automation()
        automation.set_scaling = Mock(return_value=False)
        automation._verify_active_route = Mock(return_value=False)
        profile = Mock()
        self.assertFalse(automation._scale_until_frames(
            profile, "gpu_calibration", 42, 82))
        self.assertEqual(automation.set_scaling.call_count, 2)

    def test_unknown_verification_never_resends(self):
        automation = self.make_automation()
        automation.set_scaling = Mock(return_value=False)
        automation._verify_active_route = Mock(return_value=None)
        profile = Mock()
        self.assertFalse(automation._scale_until_frames(
            profile, "gpu_calibration", 42, 82))
        self.assertEqual(automation.set_scaling.call_count, 1)

    def test_probe_blind_but_flowing_counts_as_success(self):
        # Hotkey worked (frames flow) but the overlay probe missed it:
        # accepting must not resend, which would toggle a live session off.
        automation = self.make_automation()
        automation.set_scaling = Mock(return_value=False)
        automation._verify_active_route = Mock(return_value=True)
        profile = Mock()
        self.assertTrue(automation._scale_until_frames(
            profile, "gpu_calibration", 42, 82))
        self.assertEqual(automation.set_scaling.call_count, 1)

    def test_wait_for_ls_ready(self):
        automation = self.make_automation()
        automation.process_watcher.check_is_lossless_scaling_running.return_value = True
        self.assertTrue(
            automation._wait_for_ls_ready(min_uptime_seconds=0, timeout=2))
        automation.process_watcher.check_is_lossless_scaling_running.return_value = False
        self.assertFalse(
            automation._wait_for_ls_ready(min_uptime_seconds=0, timeout=1))


class LiveTitleTests(unittest.TestCase):
    """LS renames profiles on save; route reads must follow the live title."""

    def test_settings_route_survives_ls_rename(self):
        manager = ProfileManager(Path(tempfile.mkdtemp()))
        state = AppState()
        automation = AutomationController(
            manager, state, Mock(),
            lossless_settings=bind_live_title(Mock()))
        automation.lossless_settings.read.return_value = {"profiles": [
            {"Title": "Default", "PreferredGpuId": "2",
             "OutputDisplayId": "2"}]}
        self.assertEqual(automation._settings_route("New Game"), (2, 2))


if __name__ == "__main__":
    unittest.main()
