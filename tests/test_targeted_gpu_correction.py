"""Repair only the active application's GPU and preserve all observed indexes."""
import copy
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

from companion.core.profile_manager import ProfileManager
from companion.core.state import AppState
from companion.services.automation import AutomationController
from companion.services.gpu_router import GpuRouter
from companion.services.ls_settings import LosslessSettingsXml


GPU_A = r"PCI\VEN_10DE&DEV_2203"
GPU_B = r"PCI\VEN_10DE&DEV_2D83"
GPU_C = r"PCI\VEN_1002&DEV_9999"


def report(pid, index, wanted_index=3):
    device = {"description": "RTX 5050" if index == wanted_index else "RTX 3090",
              "vendorId": 0x10DE, "deviceId": 0x2D83 if index == wanted_index else 0x2203}
    return {"pid": pid, "schema": 1, "requestId": f"request-{pid}-{index}", "device": device,
            "deviceIndexAdapters": 0, "deviceIndexAdapters1": 0,
            "lsAdapterNames": ["RTX 3090", "RTX 3090", "RTX 5050", "Other GPU"],
            "enumAdapters": [device] * 4, "enumAdapters1": [device] * 4}


@contextmanager
def controller(wanted_index=3):
    with tempfile.TemporaryDirectory() as directory:
        manager = ProfileManager(Path(directory))
        config = manager.config
        config.preferred_scaling_gpu_device_id, config.preferred_scaling_gpu_name = GPU_B, "RTX 5050"
        config.ls_gpu_id_map = {GPU_A: 1, GPU_B: 2, GPU_C: 9}
        profile = config.profiles[0]
        profile.lossless_profile_title = "Game"
        profile.native_scaling_settings = {"PreferredGpuId": "0", "OutputDisplayId": "0"}
        state = AppState()
        state.current_active_profile, state.is_scaling_active = profile, True
        state.scaling_target_pid, state.scaling_target_hwnd = 42, 82
        router = GpuRouter(lambda: [
            {"deviceName": r"\\.\DISPLAY1", "name": "RTX 3090", "deviceId": GPU_A},
            {"deviceName": r"\\.\DISPLAY2", "name": "RTX 5050", "deviceId": GPU_B},
            {"deviceName": r"\\.\DISPLAY3", "name": "Other GPU", "deviceId": GPU_C},
        ], lambda hwnd: r"\\.\DISPLAY1", ls_gpu_id_map=lambda: config.ls_gpu_id_map)
        settings = LosslessSettingsXml(str(Path(directory) / "Settings.xml"))
        settings.path.write_text("<Settings><GameProfiles><Profile><Title>Game</Title>"
            "<PreferredGpuId>2</PreferredGpuId><OutputDisplayId>1</OutputDisplayId>"
            "</Profile><Profile><Title>Untested</Title><PreferredGpuId>9</PreferredGpuId>"
            "</Profile></GameProfiles></Settings>", encoding="utf-8")
        watcher = Mock()
        host = {"running": True, "pid": 100}
        watcher.check_is_lossless_scaling_running.side_effect = lambda: host["running"]
        def stop():
            host["running"] = False
            return True
        def launch(**_kwargs):
            host["running"] = True
            host["pid"] += 1
            return True
        watcher.stop_lossless_scaling.side_effect = stop
        watcher.launch_lossless_scaling.side_effect = launch
        watcher.maintain_scaling_target_foreground.return_value = True
        automation = AutomationController(manager, state, watcher, gpu_router=router, lossless_settings=settings)
        automation.overlay_indicator = Mock()
        automation._wait_for_ls_ready = Mock(return_value=True)
        automation._adapter_report_process = lambda: (host["pid"], float(host["pid"])) if host["running"] else None
        def scale(active, *_args, **_kwargs):
            state.mark_scaling_toggled(active)
            state.scaling_target_pid, state.scaling_target_hwnd = (42, 82) if active else (None, None)
            return True
        automation.set_scaling = Mock(side_effect=scale)
        automation._verify_active_route = Mock(side_effect=AssertionError("No PresentMon during targeted correction"))
        tested = []
        def fresh(*_args, **_kwargs):
            index = int(settings.get_profile("Game")["PreferredGpuId"])
            tested.append(index)
            return report(host["pid"], index, wanted_index)
        with patch("companion.services.adapter_report.request_runtime_report", side_effect=fresh) as request:
            yield automation, manager, state, watcher, settings, tested, request


class TargetedGpuCorrectionTests(unittest.TestCase):
    def test_trial_never_refocuses_the_application_after_focus_changes(self):
        from tests.test_scale_controls import controller as base_controller
        automation, state, watcher = base_controller()
        automation._targeted_gpu_test_active = automation._verifying_route = True
        state.benchmark_mode_active = True
        watcher.maintain_scaling_target_foreground.side_effect = [True, False]
        self.assertFalse(automation.set_scaling(True, reason="gpu_calibration_targeted", force=True,
                                               target_pid=42, target_hwnd=82))
        automation.hotkey_trigger.assert_not_called()
        watcher.focus_window_identity.assert_not_called()
        self.assertFalse(state.is_scaling_active)

    def test_activation_during_testing_keeps_the_application_window_in_place(self):
        from tests.test_scale_controls import controller as base_controller
        automation, state, _ = base_controller()
        profile = automation.profile_manager.config.profiles[0]
        profile.native_scaling_settings = {"PreferredGpuId": "99", "OutputDisplayId": "99"}
        automation.window_manager = Mock()
        automation._targeted_gpu_test_active = automation._verifying_route = True
        automation._verify_gpu_after_scaling = Mock()
        state.benchmark_mode_active = True
        self.assertTrue(automation.set_scaling(True, profile, reason="gpu_calibration_targeted", force=True,
                                              target_pid=42, target_hwnd=82))
        automation.window_manager.move_window_to_display.assert_not_called()
        automation.window_manager.snap_borderless_window_to_monitor.assert_not_called()
        automation._verify_gpu_after_scaling.assert_not_called()

    def test_first_correct_index_stays_active_and_both_wrong_and_correct_ids_persist(self):
        with controller() as (automation, manager, state, watcher, settings, tested, _):
            before = copy.deepcopy(manager.config.ls_gpu_id_map)
            result = automation._correct_current_gpu_index(state.current_active_profile, 82, GPU_B, report(100, 2))
            self.assertEqual(result, "corrected")
            self.assertEqual(tested, [1, 3])
            self.assertTrue(state.is_scaling_active)
            self.assertEqual(watcher.launch_lossless_scaling.call_count, 2)
            self.assertEqual(manager.config.ls_gpu_id_map, {GPU_A: 2, GPU_B: 3, GPU_C: 9})
            self.assertEqual(manager.config.ls_gpu_id_map_previous, before)
            evidence = manager.config.ls_gpu_index_observations
            self.assertEqual(evidence["2"]["gpuDeviceId"], GPU_A)
            self.assertEqual(evidence["3"]["gpuDeviceId"], GPU_B)
            self.assertEqual(evidence["2"]["source"], "initial_mismatch")
            self.assertEqual(evidence["3"]["source"], "correct")
            self.assertEqual(settings.get_profile("Game")["PreferredGpuId"], "3")
            self.assertEqual(settings.get_profile("Game")["OutputDisplayId"], "1")
            self.assertEqual(settings.get_profile("Untested")["PreferredGpuId"], "9")
            self.assertFalse(state.benchmark_mode_active)
            self.assertFalse(state.gpu_mapping_in_progress)
            self.assertFalse(automation._verifying_route)
            reloaded = ProfileManager(manager.config_dir).config
            self.assertEqual(reloaded.ls_gpu_id_map, manager.config.ls_gpu_id_map)
            self.assertEqual(reloaded.ls_gpu_index_observations, evidence)
            self.assertTrue(any("GPU recalibration underway" in call.args[0] for call in automation.overlay_indicator.log.call_args_list))

    def test_exhaustion_restores_xml_leaves_scaling_off_and_keeps_incorrect_index_evidence(self):
        with controller(wanted_index=99) as (automation, manager, state, _, settings, tested, _):
            self.assertEqual(automation._correct_current_gpu_index(state.current_active_profile, 82, GPU_B, report(100, 2)), "failed")
            self.assertEqual(tested, [1, 3, 4])
            self.assertFalse(state.is_scaling_active)
            self.assertEqual(settings.get_profile("Game")["PreferredGpuId"], "2")
            self.assertEqual(manager.config.ls_gpu_index_observations["2"]["gpuDeviceId"], GPU_A)
            self.assertNotIn(GPU_B, manager.config.ls_gpu_id_map)

    def test_explicit_profile_choice_updates_only_after_correct_index_is_found(self):
        with controller() as (automation, manager, state, _, _, tested, _):
            profile = state.current_active_profile
            profile.native_scaling_settings["PreferredGpuId"] = "2"
            profile.preferred_gpu_device_id, profile.preferred_gpu_name = GPU_B, "RTX 5050"
            self.assertEqual(automation._correct_current_gpu_index(profile, 82, GPU_B, report(100, 2)), "corrected")
            self.assertEqual(tested, [1, 3])
            self.assertEqual(profile.native_scaling_settings["PreferredGpuId"], "3")
            self.assertEqual(profile.preferred_gpu_device_id, GPU_B)
            self.assertEqual(manager.config.preferred_scaling_gpu_device_id, GPU_B)

    def test_progress_snapshot_does_not_remap_profiles_while_testing(self):
        with controller() as (automation, _, _, _, _, _, _):
            automation._targeted_gpu_test_active = True
            automation.gpu_selections.refresh = Mock()
            self.assertTrue(automation.refresh_gpu_selections()["gpus"])
            automation.gpu_selections.refresh.assert_not_called()

    def test_focus_change_cancels_without_scaling_another_application(self):
        with controller() as (automation, _, state, watcher, settings, tested, _):
            watcher.maintain_scaling_target_foreground.return_value = False
            self.assertEqual(automation._correct_current_gpu_index(state.current_active_profile, 82, GPU_B, report(100, 2)), "cancelled")
            self.assertEqual(tested, [])
            self.assertFalse(state.is_scaling_active)
            self.assertEqual(settings.get_profile("Game")["PreferredGpuId"], "2")
            self.assertFalse(any(call.args[0] for call in automation.set_scaling.call_args_list))

    def test_no_fresh_report_never_claims_success(self):
        with controller() as (automation, manager, state, _, _, tested, request):
            request.side_effect = None
            request.return_value = None
            self.assertEqual(automation._correct_current_gpu_index(state.current_active_profile, 82, GPU_B, report(100, 2)), "failed")
            self.assertFalse(state.is_scaling_active)
            self.assertEqual(set(manager.config.ls_gpu_index_observations), {"2"})

    def test_shutdown_failure_never_writes_live_xml(self):
        with controller() as (automation, _, state, watcher, settings, tested, _):
            watcher.stop_lossless_scaling.side_effect = lambda: False
            with patch.object(settings, "update_gpu_route") as write:
                self.assertEqual(automation._correct_current_gpu_index(state.current_active_profile, 82, GPU_B, report(100, 2)), "failed")
                write.assert_not_called()
            self.assertEqual(tested, [])


if __name__ == "__main__":
    unittest.main()
