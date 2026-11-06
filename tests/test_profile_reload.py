"""Edits reload the existing scaling session only after editing and focus permit it."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from companion.core.profile_manager import ProfileManager
from companion.core.state import AppState
from companion.services.automation import AutomationController


class ProfileReloadTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.manager = ProfileManager(Path(self.directory.name))
        self.profile = self.manager.create_profile_from_process("game.exe")
        self.profile.auto_scale = False
        self.profile.lossless_profile_title = "Game"
        self.state = AppState()
        self.state.current_active_profile = self.profile
        self.state.mark_scaling_toggled(True)
        self.state.scaling_owner_profile_id = self.profile.id
        self.state.scaling_trigger = "override_hotkey"
        self.state.scaling_target_pid = 42
        self.state.scaling_target_hwnd = 99
        self.state.auto_scale_enabled = False
        self.running = True
        self.focused = True
        self.editing = True
        self.events = []
        self.watcher = Mock()
        self.watcher.check_is_lossless_scaling_running.side_effect = lambda: self.running
        self.watcher.stop_lossless_scaling.side_effect = self.stop
        self.watcher.launch_lossless_scaling.side_effect = self.launch
        self.watcher.maintain_scaling_target_foreground.side_effect = lambda *_: self.focused
        self.watcher.focus_window_identity.side_effect = AssertionError("Reload must not grab focus")
        self.indicator = Mock()
        self.hotkey = Mock(return_value=True)
        self.automation = AutomationController(
            self.manager, self.state, self.watcher, hotkey_trigger=self.hotkey,
            overlay_indicator=self.indicator, window_manager=Mock(),
        )
        self.automation.profile_changes_blocked = lambda: self.editing
        self.automation.resolve_deployment_files = Mock(return_value=[])
        self.automation._live_deployment_stale = Mock(return_value=False)
        self.automation._runtime_providers = Mock(return_value=[])
        self.automation._adopt_adapter_report = Mock(return_value=None)
        self.automation._route_verification_due = Mock(return_value=False)
        self.automation.activate_profile = Mock(side_effect=self.activate)

    def stop(self):
        self.events.append("stop")
        self.running = False
        self.state.is_scaling_active = False
        self.state.scaling_owner_profile_id = None
        self.state.scaling_trigger = None
        return True

    def launch(self, **_kwargs):
        self.events.append("launch")
        self.running = True
        return True

    def activate(self, profile, **_kwargs):
        self.events.append(("activate", profile.name))
        self.state.current_active_profile = profile
        self.automation.addon_configs.mark_applied(profile)

    def save_edit(self, **kwargs):
        self.profile = self.profile.model_copy(deep=True)
        self.profile.name = "Edited Game"
        self.manager.add_or_update_profile(self.profile)
        self.automation.apply_saved_profile(self.profile, **kwargs)
        self.automation._pending_profile_restart["ready_at"] = 0

    def reconcile(self):
        self.automation.reconcile_pending_addon_settings()

    def test_manual_session_waits_for_editor_close_and_original_window_then_restarts_once(self):
        self.save_edit()
        self.save_edit()  # Multiple saves coalesce.
        self.reconcile()
        self.assertEqual(self.events, [])
        self.watcher.maintain_scaling_target_foreground.assert_not_called()
        self.editing = False
        self.focused = False  # Dashboard or another application still has focus.
        self.reconcile()
        self.assertEqual(self.events, [])
        self.focused = True
        self.reconcile()
        self.reconcile()
        self.assertEqual(self.events, ["stop", ("activate", "Edited Game"), "launch"])
        self.hotkey.assert_called_once()
        self.assertTrue(self.state.is_scaling_active)
        self.assertEqual((self.state.scaling_target_pid, self.state.scaling_target_hwnd), (42, 99))
        self.assertEqual(self.state.scaling_owner_profile_id, self.profile.id)
        self.assertFalse(self.profile.auto_scale)
        messages = [call.args[0] for call in self.indicator.log.call_args_list]
        self.assertTrue(any("restarting scaling" in text for text in messages))
        self.assertTrue(any("Scaling started" in text for text in messages))

    def test_native_changes_wait_without_touching_running_host_or_xml(self):
        settings = Mock()
        settings.path.is_file.return_value = True
        settings.upsert_profile.side_effect = lambda *_args, **_kwargs: self.events.append("xml")
        self.profile.native_scaling_settings = {"ScalingType": "FSR"}
        self.save_edit(native_settings=settings, template_title="Old Game")
        self.reconcile()
        settings.upsert_profile.assert_not_called()
        self.assertTrue(self.state.is_scaling_active)
        self.editing = False
        self.reconcile()
        settings.upsert_profile.assert_called_once_with(
            "Game", {"ScalingType": "FSR"}, template_title="Old Game"
        )
        self.assertEqual(self.events, ["stop", "xml", ("activate", "Edited Game"), "launch"])

    def test_addon_edits_also_queue_reload_and_keep_latest_values(self):
        for gain in ("0.5", "0.8"):
            self.profile = self.automation.save_addon_config(self.profile.id, {
                "global": {}, "addons": {"FutureAddon": {"gain": gain}},
            })
        self.automation._pending_profile_restart["ready_at"] = 0
        self.reconcile()
        self.assertEqual(self.events, [])
        self.editing = False
        self.reconcile()
        self.assertEqual(self.state.current_active_profile.addon_config["addons"]["FutureAddon"]["gain"], "0.8")
        self.hotkey.assert_called_once()
        self.assertFalse(self.automation.addon_configs.has_pending(self.profile))

    def test_identical_addon_save_does_not_requeue_a_completed_reload(self):
        profile = self.automation.save_addon_config(self.profile.id, {
            "global": {}, "addons": {"FutureAddon": {"gain": "0.8"}},
        })
        self.automation._pending_profile_restart["ready_at"] = 0
        self.editing = False
        self.reconcile()
        self.automation.save_addon_config(profile.id, profile.addon_config)
        self.assertIsNone(self.automation._pending_profile_restart)

    def test_changes_captured_from_native_echo_panel_queue_reload(self):
        self.profile.addon_config = {"global": {}, "addons": {}}

        def capture(profile, _path):
            profile.addon_config = {"global": {}, "addons": {"FutureAddon": {"gain": "0.8"}}}
            return True

        self.automation.addon_configs.capture = Mock(side_effect=capture)
        self.automation.capture_active_addon_config()
        self.assertTrue(self.automation.profile_reload_pending(self.profile.id))
        self.assertTrue(self.automation.addon_configs.has_pending(self.profile))
        self.reconcile()
        self.assertEqual(self.events, [])

    def test_reload_uses_real_profile_activation_without_auto_scale_or_parking_runtime(self):
        # Exercise deployment/activation too, rather than only the reload coordinator.
        self.automation.activate_profile = AutomationController.activate_profile.__get__(self.automation)
        self.automation._desired_gpu_route = Mock(return_value=None)
        self.automation._runtime_providers = Mock(return_value=["ls-addon-manager"])
        self.automation._cleanup_profile = Mock()
        deployment = Mock()
        deployment.needs_change.return_value = True
        deployment.runtime_packages_need_state.return_value = False
        deployment.apply.return_value = {}
        self.automation.deployment_manager = deployment
        self.manager.config.lossless_scaling_exe_path = str(Path(self.directory.name) / "LosslessScaling.exe")
        self.save_edit()
        self.editing = False
        self.reconcile()
        self.assertTrue(self.state.is_scaling_active)
        self.hotkey.assert_called_once()
        self.assertEqual(self.events, ["stop", "launch"])
        deployment.apply.assert_called_once()
        deployment.set_runtime_packages_active.assert_not_called()

    def test_explicit_stop_cancels_resumption(self):
        self.save_edit()
        self.automation.set_scaling(False, reason="test_stop", force=True)
        self.hotkey.reset_mock()
        self.editing = False
        self.reconcile()
        self.assertFalse(self.state.is_scaling_active)
        self.hotkey.assert_not_called()

    def test_target_closes_or_changes_before_editor_closes(self):
        self.save_edit()
        self.automation.handle_scaling_target_closed()
        self.editing = False
        self.reconcile()
        self.hotkey.assert_not_called()
        self.assertIsNone(self.automation._pending_profile_restart)

    def test_another_window_in_same_profile_does_not_inherit_pending_restart(self):
        self.save_edit()
        self.state.scaling_target_hwnd = 123
        self.editing = False
        self.reconcile()
        self.assertEqual(self.events, [])
        self.hotkey.assert_not_called()

    def test_editing_another_profile_keeps_current_session_running(self):
        other = self.manager.create_profile_from_process("other.exe")
        settings = Mock()
        settings.path.is_file.return_value = True
        self.automation.apply_saved_profile(other, native_settings=settings)
        self.editing = False
        self.reconcile()
        self.assertEqual(self.events, [])
        settings.upsert_profile.assert_not_called()
        self.assertIsNone(self.automation._pending_profile_restart)

    def test_focus_change_during_reload_never_emits_scaling_hotkey(self):
        self.save_edit()
        self.editing = False

        def user_switches_apps(**kwargs):
            self.focused = False
            return self.launch(**kwargs)

        self.watcher.launch_lossless_scaling.side_effect = user_switches_apps
        self.reconcile()
        self.hotkey.assert_not_called()
        self.assertFalse(self.state.is_scaling_active)
        self.assertEqual(self.state.scaling_control_status["state"], "error")
        self.reconcile()
        self.assertEqual(self.events.count("stop"), 1)

    def test_failed_stop_keeps_session_and_does_not_retry_in_a_loop(self):
        self.save_edit()
        self.editing = False
        self.watcher.stop_lossless_scaling.side_effect = None
        self.watcher.stop_lossless_scaling.return_value = False
        self.reconcile()
        self.reconcile()
        self.assertTrue(self.state.is_scaling_active)
        self.hotkey.assert_not_called()
        self.watcher.stop_lossless_scaling.assert_called_once()
        self.assertEqual(self.state.scaling_control_status["state"], "error")

    def test_unknown_scaling_target_is_never_replaced_with_dashboard_foreground(self):
        self.state.scaling_target_hwnd = None
        self.save_edit()
        self.editing = False
        self.reconcile()
        self.assertEqual(self.events, [])
        self.hotkey.assert_not_called()

    def test_failed_xml_update_restores_idle_host_without_restarting_in_a_loop(self):
        settings = Mock()
        settings.path.is_file.return_value = True
        settings.upsert_profile.side_effect = OSError("XML is locked")
        self.save_edit(native_settings=settings)
        self.editing = False
        self.reconcile()
        self.reconcile()
        self.assertTrue(self.running)
        self.assertFalse(self.state.is_scaling_active)
        self.assertEqual(self.events, ["stop", "launch"])
        settings.upsert_profile.assert_called_once()
        self.hotkey.assert_not_called()


if __name__ == "__main__":
    unittest.main()
