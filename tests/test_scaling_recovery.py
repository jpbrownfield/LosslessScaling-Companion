"""Confirmation failures must not become repeated toggle or profile-restart loops."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from companion.core.models import Profile
from companion.core.profile_manager import ProfileManager
from companion.core.state import AppState
from companion.services.automation import AutomationController
from companion.services.overlay_indicator import OverlayIndicator, null_presenter
from companion.services.process_watcher import ProcessInfo


class ScalingRecoveryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.manager = ProfileManager(Path(directory.name))
        self.default = Profile(id="native-default", name="Default", is_default=True)
        self.effects = Profile(id="effects", name="ReShade and DLSS 5", target_process="old-game.exe")
        self.effects.graphics.reshade.enabled = True
        self.effects.addon_config = {"addons": {"DLSSNR02": {"_enabled": True}}}
        self.manager.config.profiles = [self.default, self.effects]
        self.manager.config.override_lossless_hotkey = True
        self.manager.config.lossless_scaling_exe_path = "C:/LS/LosslessScaling.exe"
        self.state = AppState()
        self.state.current_active_profile = self.effects
        self.running = True
        self.events = []
        self.watcher = Mock(lossless_launch_generation=0)
        self.watcher.check_is_lossless_scaling_running.side_effect = lambda: self.running
        self.watcher.stop_lossless_scaling.side_effect = self.stop
        self.watcher.launch_lossless_scaling.side_effect = self.launch
        self.watcher.get_foreground_window_info.return_value = ProcessInfo(42, "game.exe", "C:/game.exe", "Game", 82)
        self.automation = AutomationController(self.manager, self.state, self.watcher,
            hotkey_trigger=Mock(side_effect=lambda **_: self.events.append("key") or True),
            gpu_router=Mock(), overlay_indicator=OverlayIndicator(presenter=null_presenter()))
        self.automation._live_deployment_stale = Mock(return_value=False)
        self.automation._runtime_providers = Mock(return_value=set())
        self.automation._verify_gpu_after_scaling = Mock(return_value="confirmed")
        self.automation._confirm_scaling_state = Mock(return_value=False)
        self.automation._wait_for_ls_ready = Mock(side_effect=lambda: self.events.append("ready") or True)

    def stop(self):
        self.events.append("stop")
        self.running = False
        return True

    def launch(self, **_):
        self.events.append("launch")
        self.running = True
        self.watcher.lossless_launch_generation += 1
        return True

    def fail_start(self, reason="manual"):
        self.assertFalse(self.automation.set_scaling(True, self.effects, reason=reason,
            target_pid=42, target_hwnd=82))
        self.assertTrue(self.automation._scaling_confirmation_failed)
        self.assertEqual(self.events, ["key"])

    def test_failure_pauses_later_automatic_requests_across_profiles_and_targets(self):
        self.fail_start("focus")
        for reason in ("focus", "browser_video", "process_lasso_performance_mode:game"):
            for profile, pid in ((self.effects, 42), (self.default, 43)):
                with self.subTest(reason=reason, profile=profile.id):
                    self.assertFalse(self.automation.set_scaling(True, profile, reason=reason,
                        target_pid=pid, target_hwnd=82, force=True))
        self.assertEqual(self.events, ["key"])
        self.assertEqual(self.state.scaling_control_status["state"], "error")
        for reason in ("display_gpu_handoff", "display_gpu_handoff_recovery", "profile_edit"):
            self.assertFalse(self.automation.set_scaling(True, self.default, reason=reason, force=True))
        self.assertEqual(self.events, ["key"])

    def test_default_hotkey_after_effects_failure_stops_before_applying_and_restarting(self):
        self.fail_start()
        self.events.clear()

        def apply(profile, *_args, **_kwargs):
            self.assertFalse(self.running, "the ambiguous host must be stopped before Default is applied")
            self.events.append("apply Default")
            self.state.current_active_profile = profile

        self.automation.activate_profile = Mock(side_effect=apply)
        self.automation._confirm_scaling_state.return_value = True
        self.assertTrue(self.automation.handle_override_hotkey())
        self.assertEqual(self.events, ["stop", "apply Default", "launch", "ready", "key"])
        self.assertEqual(self.automation.activate_profile.call_args.args[0].id, self.default.id)
        self.assertTrue(self.state.is_scaling_active)
        self.assertFalse(self.automation._scaling_confirmation_failed)

    def test_second_failed_explicit_attempt_pauses_again_without_recursive_recovery(self):
        self.fail_start()
        self.events.clear()
        self.assertFalse(self.automation.set_scaling(True, self.default, reason="manual", force=True))
        self.assertEqual(self.events, ["stop", "launch", "ready", "key"])
        self.assertTrue(self.automation._scaling_confirmation_failed)
        self.assertFalse(self.automation.set_scaling(True, self.default, reason="focus", force=True))
        self.assertEqual(self.events.count("key"), 1)

    def configure_rebuild(self):
        self.manager.config.lossless_source_exe_path = "C:/Steam/LS/LosslessScaling.exe"
        self.manager.managed_runtime = Mock()
        self.manager.managed_runtime.rebuild.side_effect = lambda *_: self.events.append("rebuild")
        self.deployment_ready = False

        def apply(*_args, **_kwargs):
            self.events.append("apply")
            self.deployment_ready = True

        self.automation.activate_profile = Mock(side_effect=apply)
        self.automation.resolve_deployment_files = Mock(return_value=[])
        self.automation.deployment_manager = Mock()
        self.automation.deployment_manager.needs_change.side_effect = lambda *_a, **_k: not self.deployment_ready

    def three_failed_starts(self):
        for _ in range(3):
            self.assertFalse(self.automation.set_scaling(True, self.default, reason="manual", force=True))
        self.assertEqual(self.automation._consecutive_failed_starts, 3)

    def test_three_failed_starts_rebuild_on_next_explicit_request_once_per_streak(self):
        self.configure_rebuild()
        self.three_failed_starts()
        self.manager.managed_runtime.rebuild.assert_not_called()
        self.events.clear()
        self.assertFalse(self.automation.set_scaling(True, self.default, reason="focus", force=True))
        self.assertEqual(self.events, [])
        self.assertFalse(self.automation.set_scaling(True, self.default, reason="manual", force=True))
        self.assertEqual(self.events, ["stop", "rebuild", "apply", "launch", "ready", "key"])
        self.manager.managed_runtime.rebuild.assert_called_once()
        self.events.clear()
        self.assertFalse(self.automation.set_scaling(True, self.default, reason="manual", force=True))
        self.assertEqual(self.events, ["stop", "launch", "ready", "key"])
        self.manager.managed_runtime.rebuild.assert_called_once()
        self.automation._confirm_scaling_state.return_value = True
        self.assertTrue(self.automation.set_scaling(True, self.default, reason="manual", force=True))
        self.assertEqual(self.automation._consecutive_failed_starts, 0)
        self.assertFalse(self.automation._clone_rebuild_attempted)

    def test_stopping_does_not_rebuild_or_erase_three_failed_starts(self):
        self.configure_rebuild()
        self.three_failed_starts()
        self.events.clear()
        self.assertTrue(self.automation.set_scaling(False, reason="manual"))
        self.assertEqual(self.events, ["stop"])
        self.assertEqual(self.automation._consecutive_failed_starts, 3)
        self.events.clear()
        self.assertFalse(self.automation.set_scaling(True, self.default, reason="manual", force=True))
        self.assertEqual(self.events, ["rebuild", "apply", "launch", "ready", "key"])

    def test_failed_stop_or_rebuild_never_launches_or_emits_a_key(self):
        self.configure_rebuild()
        self.three_failed_starts()
        self.events.clear()
        self.watcher.stop_lossless_scaling.side_effect = lambda: False
        self.assertFalse(self.automation.set_scaling(True, self.default, reason="manual", force=True))
        self.manager.managed_runtime.rebuild.assert_not_called()
        self.assertEqual(self.events, [])
        self.watcher.stop_lossless_scaling.side_effect = self.stop
        self.manager.managed_runtime.rebuild.side_effect = OSError("clone locked")
        self.assertFalse(self.automation.set_scaling(True, self.default, reason="manual", force=True))
        self.assertEqual(self.events, ["stop"])
        self.assertTrue(self.automation._scaling_confirmation_failed)
        self.assertTrue(self.automation._clone_rebuild_attempted)

    def test_rebuild_requires_profile_deployment_before_any_launch(self):
        self.configure_rebuild()
        self.three_failed_starts()
        self.events.clear()
        self.automation.deployment_manager.needs_change.side_effect = None
        self.automation.deployment_manager.needs_change.return_value = True
        self.assertFalse(self.automation.set_scaling(True, self.default, reason="manual", force=True))
        self.assertEqual(self.events, ["stop", "rebuild", "apply"])
        self.assertTrue(self.automation._clone_rebuild_pending)

    def test_foreground_recovery_applies_selected_profile_once(self):
        self.configure_rebuild()
        self.three_failed_starts()
        self.events.clear()
        self.automation._confirm_scaling_state.return_value = True
        self.assertTrue(self.automation.handle_override_hotkey())
        self.assertEqual(self.events, ["stop", "rebuild", "apply", "launch", "ready", "key"])
        self.automation.activate_profile.assert_called_once()
        self.assertEqual(self.automation.activate_profile.call_args.args[0].id, self.default.id)

    def test_success_resets_streak_and_controlled_tests_do_not_count(self):
        self.fail_start()
        self.automation._confirm_scaling_state.return_value = True
        self.assertTrue(self.automation.set_scaling(True, self.default, reason="manual", force=True))
        self.assertEqual(self.automation._consecutive_failed_starts, 0)
        self.automation._consecutive_failed_starts = 2
        self.automation._clone_rebuild_attempted = True
        self.automation.reconcile_observed_scaling_state(True)
        self.assertEqual(self.automation._consecutive_failed_starts, 0)
        self.assertFalse(self.automation._clone_rebuild_attempted)
        self.automation._verifying_route = True
        self.automation._record_scaling_failure(True)
        self.assertEqual(self.automation._consecutive_failed_starts, 0)

    def test_startup_failure_counts_without_sending_a_toggle(self):
        self.running = False
        self.watcher.launch_lossless_scaling.side_effect = lambda **_: False
        for index in range(3):
            self.assertFalse(self.automation.set_scaling(True, self.default, reason="manual", force=True))
            self.assertEqual(self.automation._consecutive_failed_starts, index + 1)
        self.automation.hotkey_trigger.assert_not_called()

    def test_controlled_test_cannot_trigger_a_clone_rebuild(self):
        self.configure_rebuild()
        self.three_failed_starts()
        self.events.clear()
        self.automation._verifying_route = True
        self.assertFalse(self.automation.set_scaling(True, self.default, reason="route_test", force=True))
        self.manager.managed_runtime.rebuild.assert_not_called()
        self.assertEqual(self.events, ["stop", "launch", "ready", "key"])
        self.assertEqual(self.automation._consecutive_failed_starts, 3)

    def test_debug_rebuild_stops_active_scaling_and_leaves_profile_ready_for_next_start(self):
        self.configure_rebuild()
        self.state.is_scaling_active = True
        self.state.scaling_target_pid, self.state.scaling_target_hwnd = 42, 82
        saved = self.manager.config.model_dump()
        result = self.automation.rebuild_cloned_installation()
        self.assertTrue(result["settingsPreserved"])
        self.assertEqual(self.events, ["stop", "rebuild"])
        self.assertFalse(self.state.is_scaling_active)
        self.assertIsNone(self.state.scaling_target_pid)
        self.assertTrue(self.automation._clone_rebuild_pending)
        self.assertEqual(self.manager.config.model_dump(), saved)
        self.assertFalse(self.automation._scaling_confirmation_failed)
        self.events.clear()
        self.automation._confirm_scaling_state.return_value = True
        self.assertTrue(self.automation.set_scaling(True, self.default, reason="manual", force=True))
        self.assertEqual(self.events, ["apply", "launch", "ready", "key"])

    def test_debug_rebuild_refuses_busy_work_and_preserves_files_if_stop_fails(self):
        self.configure_rebuild()
        for flag in ("update_in_progress", "benchmark_mode_active", "gpu_mapping_in_progress"):
            setattr(self.state, flag, True)
            with self.subTest(flag=flag), self.assertRaises(RuntimeError):
                self.automation.rebuild_cloned_installation()
            setattr(self.state, flag, False)
        self.assertEqual(self.events, [])
        self.watcher.stop_lossless_scaling.side_effect = lambda: False
        with self.assertRaisesRegex(RuntimeError, "Could not stop"):
            self.automation.rebuild_cloned_installation()
        self.manager.managed_runtime.rebuild.assert_not_called()
        self.assertTrue(self.automation._scaling_confirmation_failed)

    def test_confirmation_timeout_is_written_to_live_and_persistent_logs_with_evidence(self):
        import logging
        from companion.services.runtime_logging import configure_runtime_logging, runtime_log_buffer
        root = logging.getLogger()
        previous = list(root.handlers)
        path = configure_runtime_logging(self.manager.config_dir)
        try:
            before = runtime_log_buffer().snapshot()
            self.automation.scaling_state_probe = lambda: None
            self.automation.scaling_confirmation_diagnostics = lambda: {"requiresFreshLog": True, "currentSequence": 0}
            self.automation._confirm_scaling_state.side_effect = lambda *_args, **_kwargs: AutomationController._confirm_scaling_state(
                self.automation, True, timeout=0.001)
            self.assertFalse(self.automation.set_scaling(True, self.default, reason="manual", force=True))
            entries = runtime_log_buffer().snapshot(before["cursor"], before["session"])["entries"]
            errors = [item["message"] for item in entries if item["level"] == "ERROR"]
            self.assertTrue(any("Scaling not confirmed" in item and "last observation=None" in item for item in errors))
            self.assertTrue(any("requiresFreshLog" in item for item in errors))
            self.assertIn("Scaling not confirmed", path.read_text(encoding="utf-8"))
            self.assertEqual(self.state.scaling_control_status["state"], "error")
        finally:
            for handler in list(root.handlers):
                if handler not in previous and getattr(handler, "baseFilename", None) == str(path):
                    root.removeHandler(handler)
                    handler.close()

    def test_failed_or_exceptional_recovery_stop_never_emits_another_toggle(self):
        for outcome in (False, OSError("process unavailable")):
            with self.subTest(outcome=outcome):
                self.automation._scaling_confirmation_failed = True
                self.watcher.stop_lossless_scaling.side_effect = outcome if isinstance(outcome, Exception) else None
                self.watcher.stop_lossless_scaling.return_value = False
                self.assertFalse(self.automation.set_scaling(True, self.default, reason="manual"))
                self.automation.hotkey_trigger.assert_not_called()
                self.assertTrue(self.automation._scaling_confirmation_failed)
                self.assertEqual(self.state.scaling_control_status["state"], "error")

    def test_stop_after_ambiguous_start_does_not_send_a_toggle_even_when_state_says_idle(self):
        self.automation._scaling_confirmation_failed = True
        self.assertTrue(self.automation.set_scaling(False, reason="manual"))
        self.assertEqual(self.events, ["stop"])
        self.assertFalse(self.automation._scaling_confirmation_failed)
        self.automation.hotkey_trigger.assert_not_called()

    def test_profile_change_launch_waits_once_and_uses_cold_start_confirmation_timeout(self):
        self.watcher.lossless_launch_generation = 1
        self.automation._confirm_scaling_state.return_value = True
        self.assertTrue(self.automation.set_scaling(True, self.default, reason="manual"))
        self.assertEqual(self.events, ["ready", "key"])
        self.assertEqual(self.automation._confirm_scaling_state.call_args.kwargs["timeout"], 15)
        self.state.mark_scaling_toggled(False)
        self.events.clear()
        self.assertTrue(self.automation.set_scaling(True, self.default, reason="manual"))
        self.assertEqual(self.events, ["key"])
        self.assertEqual(self.automation._confirm_scaling_state.call_args.kwargs["timeout"], 5)

    def test_profile_restart_that_does_not_become_ready_cannot_send_the_scale_key(self):
        self.watcher.lossless_launch_generation = 1
        self.automation._wait_for_ls_ready.return_value = False
        self.automation._wait_for_ls_ready.side_effect = None
        self.assertFalse(self.automation.set_scaling(True, self.default, reason="manual"))
        self.automation.hotkey_trigger.assert_not_called()
        self.automation._confirm_scaling_state.assert_not_called()

    def test_fresh_native_activation_resolves_the_failure_without_forcing_a_restart(self):
        self.automation._scaling_confirmation_failed = True
        self.automation._failed_scaling_expected_active = True
        self.automation.reconcile_observed_scaling_state(True, {"pid": 42, "hwnd": 82,
            "processName": "game.exe", "detectionSource": "log_file"})
        self.assertFalse(self.automation._scaling_confirmation_failed)
        self.assertTrue(self.state.is_scaling_active)
        self.assertEqual(self.events, [])

    def test_failed_deactivation_also_pauses_automatic_handoffs_until_idle_is_observed(self):
        self.state.is_scaling_active = True
        self.state.scaling_target_pid, self.state.scaling_target_hwnd = 42, 82
        self.assertFalse(self.automation.set_scaling(False, self.effects,
            reason="display_gpu_handoff", force=True))
        self.assertTrue(self.automation._scaling_confirmation_failed)
        self.assertFalse(self.automation.set_scaling(False, self.default,
            reason="display_gpu_handoff", force=True))
        self.assertEqual(self.events, ["key"])
        self.automation.reconcile_observed_scaling_state(True, {"pid": 42, "hwnd": 82,
            "processName": "game.exe", "detectionSource": "log_file"})
        self.assertTrue(self.automation._scaling_confirmation_failed)
        self.automation.reconcile_observed_scaling_state(False)
        self.assertFalse(self.automation._scaling_confirmation_failed)
