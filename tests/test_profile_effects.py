import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from companion.core.models import DllOverrideConfig, Profile, ReshadeConfig
from companion.core.profile_manager import ProfileManager
from companion.core.state import AppState
from companion.services.automation import AutomationController
from companion.services.process_watcher import ProcessInfo
from companion.services.dll_manager import DllManager
from companion.services.reshade_manager import ReshadeManager


class ProfileEffectsTests(unittest.TestCase):
    class FakeProcessWatcher:
        def __init__(self, events, running=True):
            self.events = events
            self.running = running

        def check_is_lossless_scaling_running(self):
            return self.running

        def stop_lossless_scaling(self):
            self.events.append("stop")
            self.running = False
            return True

        def launch_lossless_scaling(self, *, force=False):
            self.events.append(("launch", force))
            self.running = True
            return True

    def test_override_hotkey_uses_default_profile_for_unmatched_foreground_window(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = ProfileManager(Path(directory) / "config")
            default = Profile(id="native-default", name="Default", is_default=True)
            manager.config.profiles = [default]
            manager.config.override_lossless_hotkey = True
            target = ProcessInfo(42, "Unmatched.exe", r"C:\Games\Unmatched.exe", "Game", 99)
            watcher = Mock()
            watcher.get_foreground_window_info.return_value = target
            automation = AutomationController(manager, AppState(), process_watcher=watcher)

            with (
                patch.object(automation, "activate_profile") as activate,
                patch.object(automation, "set_scaling", return_value=True) as set_scaling,
            ):
                self.assertTrue(automation.handle_override_hotkey())

            activate.assert_called_once_with(
                default,
                target.exe_path,
                force=True,
                target_pid=42,
                target_hwnd=99,
                suppress_auto_scale=True,
            )
            set_scaling.assert_called_once_with(
                True,
                default,
                reason="override_hotkey",
                force=True,
                target_pid=42,
                target_hwnd=99,
            )

    def test_reshade_swap_uses_normal_windows_path_and_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "LosslessScaling.exe").write_bytes(b"exe")
            ini = root / "ReShade.ini"
            preset = root / "Preset.ini"
            ini.write_text("[GENERAL]\nCurrentPresetPath=old.ini\n", encoding="utf-8")
            preset.write_text("[Preset]\n", encoding="utf-8")
            self.assertTrue(ReshadeManager.swap_preset(str(ini), str(preset)))
            self.assertIn(str(preset.resolve()), ini.read_text(encoding="utf-8"))
            self.assertTrue(root.joinpath("ReShade.ini.bak").exists())

    def test_dll_deployment_restores_original(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "LosslessScaling.exe").write_bytes(b"exe")
            source = root / "mod.dll"
            destination = root / "dxgi.dll"
            source.write_bytes(b"mod")
            destination.write_bytes(b"original")
            override = DllOverrideConfig(enabled=True, source_dll_path=str(source))
            self.assertTrue(DllManager.deploy_override(override, str(root)))
            self.assertEqual(destination.read_bytes(), b"mod")
            self.assertTrue(DllManager.cleanup_override(str(root), "dxgi.dll", True))
            self.assertEqual(destination.read_bytes(), b"original")

    def test_legacy_dll_manager_refuses_a_game_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game_dir = root / "game"
            game_dir.mkdir()
            (game_dir / "Game.exe").write_bytes(b"exe")
            source = root / "mod.dll"
            source.write_bytes(b"mod")
            override = DllOverrideConfig(enabled=True, source_dll_path=str(source))

            self.assertFalse(DllManager.deploy_override(override, str(game_dir)))
            self.assertFalse((game_dir / "dxgi.dll").exists())

    @patch("companion.services.automation.InputSimulator.trigger_hotkey", return_value=True)
    def test_monitor_windows_stay_minimized_when_scaling_stops(self, _trigger):
        with tempfile.TemporaryDirectory() as directory:
            manager = ProfileManager(Path(directory) / "config")
            manager.config.minimize_other_windows_on_scale = True
            state = AppState()
            state.current_foreground_hwnd = 1234
            windows = Mock()
            automation = AutomationController(manager, state, window_manager=windows)

            automation.set_scaling(True, reason="test")
            automation.set_scaling(False, reason="test")

            windows.minimize_others_on_target_monitor.assert_called_once_with(1234)
            windows.restore_managed_windows.assert_not_called()

    @patch("companion.services.automation.InputSimulator.trigger_hotkey", return_value=True)
    def test_monitor_minimizer_uses_confirmed_scaling_target_not_later_foreground(self, _trigger):
        with tempfile.TemporaryDirectory() as directory:
            manager = ProfileManager(Path(directory) / "config")
            manager.config.minimize_other_windows_on_scale = True
            state = AppState()
            state.current_foreground_hwnd = 9999
            windows = Mock()
            watcher = Mock()
            watcher.focus_window_identity.return_value = True
            automation = AutomationController(
                manager, state, process_watcher=watcher, window_manager=windows
            )

            automation.set_scaling(
                True, reason="test", target_pid=123, target_hwnd=456
            )

            windows.minimize_others_on_target_monitor.assert_called_once_with(456)

    @patch("companion.services.automation.InputSimulator.trigger_hotkey", return_value=True)
    def test_frame_generation_only_profile_snaps_near_monitor_edges(self, _trigger):
        with tempfile.TemporaryDirectory() as directory:
            manager = ProfileManager(Path(directory) / "config")
            state = AppState()
            watcher = Mock()
            watcher.focus_window_identity.return_value = True
            windows = Mock()
            profile = Profile(
                name="Frame generation only",
                native_scaling_settings={
                    "ScalingType": "Off",
                    "WindowedMode": "false",
                },
            )
            automation = AutomationController(
                manager, state, watcher, window_manager=windows
            )

            self.assertTrue(
                automation.set_scaling(
                    True,
                    profile,
                    reason="manual_profile_switch",
                    target_pid=123,
                    target_hwnd=456,
                )
            )

        windows.snap_borderless_window_to_monitor.assert_called_once_with(
            456, tolerance_px=8
        )

    @patch("companion.services.automation.InputSimulator.trigger_hotkey", return_value=True)
    def test_monitor_edge_snap_can_be_disabled_globally(self, _trigger):
        with tempfile.TemporaryDirectory() as directory:
            manager = ProfileManager(Path(directory) / "config")
            manager.config.snap_near_fullscreen_windows_to_monitor = False
            watcher = Mock()
            watcher.focus_window_identity.return_value = True
            windows = Mock()
            profile = Profile(
                name="Frame generation only",
                native_scaling_settings={
                    "ScalingType": "Off",
                    "WindowedMode": "false",
                },
            )
            automation = AutomationController(
                manager, AppState(), watcher, window_manager=windows
            )

            self.assertTrue(
                automation.set_scaling(
                    True,
                    profile,
                    reason="manual_profile_switch",
                    target_pid=123,
                    target_hwnd=456,
                )
            )

        windows.snap_borderless_window_to_monitor.assert_not_called()

    @patch("companion.services.automation.InputSimulator.trigger_hotkey", return_value=True)
    def test_spatial_scaling_profile_does_not_resize_game_window(self, _trigger):
        with tempfile.TemporaryDirectory() as directory:
            manager = ProfileManager(Path(directory) / "config")
            watcher = Mock()
            watcher.focus_window_identity.return_value = True
            windows = Mock()
            profile = Profile(
                name="Spatial scaling",
                native_scaling_settings={
                    "ScalingType": "LS1",
                    "WindowedMode": "false",
                },
            )
            automation = AutomationController(
                manager, AppState(), watcher, window_manager=windows
            )

            self.assertTrue(
                automation.set_scaling(
                    True,
                    profile,
                    reason="manual_profile_switch",
                    target_pid=123,
                    target_hwnd=456,
                )
            )

        windows.snap_borderless_window_to_monitor.assert_not_called()

    def test_profile_auto_gpu_route_inherits_global_window_route(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = ProfileManager(Path(directory) / "config")
            manager.config.auto_route_gpu_to_display = True
            profile = Profile(name="Auto", native_scaling_settings={
                "PreferredGpuId": "0", "OutputDisplayId": "0",
            })
            router = Mock()
            router.route_for_gpu.return_value = {
                "gpuDeviceId": None, "lsGpuId": 0, "lsDisplayId": 0, "deviceName": "",
            }
            expected = {
                "gpuDeviceId": "gpu-b", "lsGpuId": 2,
                "lsDisplayId": 3, "deviceName": r"\\.\DISPLAY3",
            }
            router.route_for_window.return_value = expected
            automation = AutomationController(manager, AppState(), gpu_router=router)

            self.assertEqual(automation._desired_gpu_route(profile, 456), expected)
            router.route_for_ls_ids.assert_not_called()

    def test_profile_numeric_gpu_overrides_only_that_global_dimension(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = ProfileManager(Path(directory) / "config")
            manager.config.auto_route_gpu_to_display = True
            profile = Profile(name="GPU override", native_scaling_settings={
                "PreferredGpuId": "1", "OutputDisplayId": "0",
            })
            router = Mock()
            router.route_for_gpu.return_value = {
                "gpuDeviceId": None, "lsGpuId": 0, "lsDisplayId": 0, "deviceName": "",
            }
            router.route_for_window.return_value = {
                "gpuDeviceId": "gpu-b", "lsGpuId": 2,
                "lsDisplayId": 3, "deviceName": r"\\.\DISPLAY3",
            }
            router.route_for_ls_ids.return_value = {
                "gpuDeviceId": "gpu-a", "lsGpuId": 1,
                "lsDisplayId": 0, "deviceName": "",
            }
            automation = AutomationController(manager, AppState(), gpu_router=router)

            route = automation._desired_gpu_route(profile, 456)

            self.assertEqual(route["lsGpuId"], 1)
            self.assertEqual(route["gpuDeviceId"], "gpu-a")
            self.assertEqual(route["lsDisplayId"], 3)
            self.assertEqual(route["deviceName"], r"\\.\DISPLAY3")

    def test_active_profile_new_window_updates_xml_route_without_redeployment(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = ProfileManager(Path(directory) / "config")
            manager.config.auto_route_gpu_to_display = True
            profile = Profile(
                id="game",
                name="Game",
                lossless_profile_title="Gaming",
                native_scaling_settings={"PreferredGpuId": "0", "OutputDisplayId": "0"},
            )
            state = AppState()
            state.current_active_profile = profile
            router = Mock()
            route = {
                "gpuDeviceId": "gpu-b", "lsGpuId": 2,
                "lsDisplayId": 3, "deviceName": r"\\.\DISPLAY3",
            }
            router.route_for_window.return_value = route
            settings = Mock()
            settings.gpu_route_changes_required.return_value = True
            settings.update_gpu_route.return_value = True
            watcher = Mock()
            watcher.check_is_lossless_scaling_running.return_value = True
            watcher.stop_lossless_scaling.return_value = True
            automation = AutomationController(
                manager,
                state,
                watcher,
                gpu_router=router,
                lossless_settings=settings,
            )
            automation.deployment_manager = Mock()

            automation.activate_profile(profile, target_pid=42, target_hwnd=82)

            settings.update_gpu_route.assert_called_once_with("Gaming", 2, 3)
            watcher.stop_lossless_scaling.assert_called_once_with()
            watcher.launch_lossless_scaling.assert_called_once_with(force=True)
            automation.deployment_manager.needs_change.assert_not_called()
            self.assertEqual(state.current_gpu_route_key, r"2:3:\\.\DISPLAY3")

    def test_stale_autoscale_is_cancelled_after_user_switches_windows(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = ProfileManager(Path(directory) / "config")
            profile = Profile(name="Game", auto_scale=True)
            watcher = Mock()
            watcher.target_is_foreground.return_value = False
            automation = AutomationController(manager, AppState(), process_watcher=watcher)
            automation.set_scaling = Mock()

            automation._trigger_profile_runtime_actions(
                profile,
                reshade_swapped=False,
                target_pid=123,
                target_hwnd=456,
            )

            automation.set_scaling.assert_not_called()
            watcher.invalidate_foreground_cache.assert_called_once_with()

    @patch("companion.services.automation.InputSimulator.trigger_hotkey", return_value=True)
    def test_focus_activation_rechecks_target_immediately_before_hotkey(self, trigger_hotkey):
        with tempfile.TemporaryDirectory() as directory:
            manager = ProfileManager(Path(directory) / "config")
            watcher = Mock()
            watcher.maintain_scaling_target_foreground.return_value = False
            automation = AutomationController(manager, AppState(), process_watcher=watcher)

            result = automation.set_scaling(
                True,
                Profile(name="Game"),
                reason="focus",
                target_pid=123,
                target_hwnd=456,
            )

            self.assertFalse(result)
            trigger_hotkey.assert_not_called()
            watcher.invalidate_foreground_cache.assert_called_once_with()

    def test_disabling_monitor_window_management_does_not_restore_windows(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = ProfileManager(Path(directory) / "config")
            manager.config.minimize_other_windows_on_scale = False
            state = AppState()
            state.is_scaling_active = True
            windows = Mock()
            automation = AutomationController(manager, state, window_manager=windows)

            automation.reconcile_window_management_setting()

            windows.restore_managed_windows.assert_not_called()

    def test_smooth_motion_targets_lossless_scaling_profile_and_is_disabled_on_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "LosslessScaling.exe"
            executable.write_bytes(b"exe")
            manager = ProfileManager(root / "config")
            manager.config.lossless_scaling_exe_path = str(executable)
            nvidia = Mock()
            profile = Profile.model_validate({
                "name": "Smooth Motion",
                "graphics": {"special_k": {
                    "enabled": True,
                    "experimental_smooth_motion": True,
                }},
            })
            automation = AutomationController(
                manager, AppState(), nvidia_profile_manager=nvidia
            )

            automation._apply_profile_transition(
                None, profile, False, [], park_runtime=False,
                trigger_runtime_actions=False, target_pid=None, target_hwnd=None,
                suppress_auto_scale=True,
            )
            automation._apply_profile_transition(
                profile, None, False, [], park_runtime=False,
                trigger_runtime_actions=False, target_pid=None, target_hwnd=None,
                suppress_auto_scale=True,
            )

            self.assertEqual(
                nvidia.set_lossless_scaling_smooth_motion.call_args_list,
                [
                    unittest.mock.call(str(executable), True),
                    unittest.mock.call(str(executable), False),
                ],
            )

    @patch("companion.services.automation.ReshadeManager.trigger_reshade_reload")
    @patch("companion.services.automation.InputSimulator.trigger_hotkey", return_value=True)
    def test_activation_applies_and_cleans_profile(self, trigger_hotkey, reload_reshade):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_dir = root / "config"
            lossless_dir = root / "lossless"
            lossless_dir.mkdir()
            (lossless_dir / "LosslessScaling.exe").write_bytes(b"exe")
            source = root / "mod.dll"
            source.write_bytes(b"mod")
            reshade_ini = lossless_dir / "ReShade.ini"
            preset = root / "Preset.ini"
            reshade_ini.write_text("[GENERAL]\nCurrentPresetPath=old.ini\n", encoding="utf-8")
            preset.write_text("", encoding="utf-8")

            profile = Profile(
                name="Integrated",
                target_process="game.exe",
                target_executable_path=str(root / "game" / "game.exe"),
                auto_scale=True,
                reshade=ReshadeConfig(
                    enabled=True,
                    reshade_ini_path=str(reshade_ini),
                    preset_path=str(preset),
                    reload_hotkey="Home",
                ),
                dll_overrides=[DllOverrideConfig(enabled=True, source_dll_path=str(source))],
            )
            manager = ProfileManager(config_dir)
            manager.config.lossless_scaling_exe_path = str(lossless_dir / "LosslessScaling.exe")
            state = AppState()
            automation = AutomationController(manager, state)
            automation.activate_profile(profile, profile.target_executable_path)

            self.assertTrue(state.is_scaling_active)
            self.assertEqual((lossless_dir / "dxgi.dll").read_bytes(), b"mod")
            reload_reshade.assert_called_once_with("Home")
            trigger_hotkey.assert_called_once()

            # Losing focus no longer de-scales or unloads the active profile.
            automation.activate_profile(None)
            self.assertTrue(state.is_scaling_active)
            self.assertTrue((lossless_dir / "dxgi.dll").exists())

            automation.set_scaling(False, profile, reason="test_cleanup")
            automation.activate_profile(None, force=True)
            self.assertFalse(state.is_scaling_active)
            self.assertFalse((lossless_dir / "dxgi.dll").exists())
            self.assertIn("CurrentPresetPath=old.ini", reshade_ini.read_text(encoding="utf-8"))

    @patch("companion.services.automation.InputSimulator.trigger_hotkey", return_value=True)
    def test_exact_window_is_verified_and_scaling_is_confirmed(self, trigger_hotkey):
        with tempfile.TemporaryDirectory() as directory:
            manager = ProfileManager(Path(directory) / "config")
            state = AppState()
            watcher = Mock()
            watcher.focus_window_identity.return_value = True
            observations = iter((False, False, True))
            automation = AutomationController(
                manager,
                state,
                watcher,
                scaling_state_probe=lambda: next(observations, True),
            )
            profile = Profile(name="Game", target_process="Game.exe")

            self.assertTrue(
                automation.set_scaling(
                    True,
                    profile,
                    reason="focus",
                    target_pid=123,
                    target_hwnd=456,
                )
            )

        watcher.focus_window_identity.assert_called_once_with(123, 456)
        self.assertGreaterEqual(
            watcher.maintain_scaling_target_foreground.call_count, 3
        )
        trigger_hotkey.assert_called_once()
        self.assertTrue(state.is_scaling_active)

    @patch("companion.services.automation.time.sleep")
    @patch("companion.services.automation.InputSimulator.trigger_hotkey", return_value=True)
    def test_auto_scale_retries_six_times_at_five_second_cadence(
        self, trigger_hotkey, sleep
    ):
        with tempfile.TemporaryDirectory() as directory:
            manager = ProfileManager(Path(directory) / "config")
            state = AppState()
            watcher = Mock()
            watcher.focus_window_identity.return_value = True
            watcher.maintain_scaling_target_foreground.return_value = True
            watcher.target_is_foreground.return_value = True
            automation = AutomationController(manager, state, watcher)
            automation._confirm_scaling_state = Mock(
                side_effect=[False, False, False, False, False, True]
            )

            self.assertTrue(
                automation.set_scaling(
                    True,
                    Profile(name="Game", target_process="Game.exe"),
                    reason="focus",
                    target_pid=123,
                    target_hwnd=456,
                )
            )

        self.assertEqual(trigger_hotkey.call_count, 6)
        self.assertEqual(automation._confirm_scaling_state.call_count, 6)
        self.assertEqual(sleep.call_count, 5)
        self.assertTrue(all(call.args[0] <= 5.0 for call in sleep.call_args_list))
        self.assertTrue(state.is_scaling_active)

    @patch("companion.services.automation.time.sleep")
    @patch("companion.services.automation.InputSimulator.trigger_hotkey", return_value=True)
    def test_auto_scale_retry_stops_when_target_loses_focus(
        self, trigger_hotkey, sleep
    ):
        with tempfile.TemporaryDirectory() as directory:
            manager = ProfileManager(Path(directory) / "config")
            watcher = Mock()
            watcher.focus_window_identity.return_value = True
            watcher.maintain_scaling_target_foreground.return_value = True
            watcher.target_is_foreground.return_value = False
            state = AppState()
            automation = AutomationController(manager, state, watcher)
            automation._confirm_scaling_state = Mock(return_value=False)

            self.assertFalse(
                automation.set_scaling(
                    True,
                    Profile(name="Game", target_process="Game.exe"),
                    reason="focus",
                    target_pid=123,
                    target_hwnd=456,
                )
            )

        trigger_hotkey.assert_called_once()
        sleep.assert_not_called()
        watcher.invalidate_foreground_cache.assert_called_once()
        self.assertFalse(state.is_scaling_active)

    @patch("companion.services.automation.time.sleep")
    @patch("companion.services.automation.InputSimulator.trigger_hotkey", return_value=True)
    def test_auto_scale_stops_wrong_target_then_retries_intended_window(
        self, trigger_hotkey, sleep
    ):
        with tempfile.TemporaryDirectory() as directory:
            manager = ProfileManager(Path(directory) / "config")
            state = AppState()
            watcher = Mock()
            watcher.focus_window_identity.return_value = True
            watcher.maintain_scaling_target_foreground.return_value = True
            watcher.target_is_foreground.return_value = True
            observations = iter(
                (
                    {"isActive": True, "pid": 999},
                    {"isActive": False, "pid": 999},
                    {"isActive": True, "pid": 123},
                )
            )
            automation = AutomationController(
                manager,
                state,
                watcher,
                scaling_state_probe=lambda: next(observations),
            )

            self.assertTrue(
                automation.set_scaling(
                    True,
                    Profile(name="Game", target_process="Game.exe"),
                    reason="focus",
                    target_pid=123,
                    target_hwnd=456,
                )
            )

        # Wrong activation, recovery toggle, correct activation.
        self.assertEqual(trigger_hotkey.call_count, 3)
        self.assertTrue(state.is_scaling_active)
        self.assertEqual(state.scaling_target_pid, 123)
        self.assertEqual(state.scaling_target_hwnd, 456)
        self.assertEqual(state.scaling_trigger, "focus")

    @patch("companion.services.automation.time.sleep")
    @patch("companion.services.automation.InputSimulator.trigger_hotkey", return_value=True)
    def test_manual_activation_remains_single_attempt(self, trigger_hotkey, sleep):
        with tempfile.TemporaryDirectory() as directory:
            manager = ProfileManager(Path(directory) / "config")
            state = AppState()
            automation = AutomationController(manager, state)
            automation._confirm_scaling_state = Mock(return_value=False)

            self.assertFalse(automation.set_scaling(True, reason="manual"))

        trigger_hotkey.assert_called_once()
        sleep.assert_not_called()
        self.assertFalse(state.is_scaling_active)

    @patch("companion.services.automation.InputSimulator.trigger_hotkey", return_value=True)
    def test_smart_auto_scale_master_switch_blocks_automatic_hotkey(self, trigger_hotkey):
        with tempfile.TemporaryDirectory() as directory:
            manager = ProfileManager(Path(directory) / "config")
            manager.config.disable_native_auto_scale = False
            state = AppState()
            profile = Profile(name="Game", target_process="Game.exe", auto_scale=True)

            AutomationController(manager, state).activate_profile(profile)

        trigger_hotkey.assert_not_called()
        self.assertFalse(state.is_scaling_active)

    @patch("companion.services.automation.InputSimulator.trigger_hotkey", return_value=True)
    def test_browser_profile_never_auto_scales_on_focus(self, trigger_hotkey):
        with tempfile.TemporaryDirectory() as directory:
            manager = ProfileManager(Path(directory) / "config")
            state = AppState()
            for target_process in ("chrome.exe", "msedge.exe"):
                profile = Profile(
                    name="Browser", target_process=target_process, auto_scale=True
                )
                AutomationController(manager, state).activate_profile(
                    profile, suppress_auto_scale=False
                )
            youtube = Profile(
                name="YouTube",
                target_process="chrome.exe",
                target_domain="youtube.com",
                auto_scale=True,
            )
            AutomationController(manager, state).activate_profile(
                youtube, suppress_auto_scale=False
            )

        trigger_hotkey.assert_not_called()
        self.assertFalse(state.is_scaling_active)

    @patch("companion.services.automation.InputSimulator.trigger_hotkey", return_value=True)
    def test_game_profile_still_auto_scales_on_focus(self, trigger_hotkey):
        with tempfile.TemporaryDirectory() as directory:
            manager = ProfileManager(Path(directory) / "config")
            state = AppState()
            profile = Profile(name="Game", target_process="Game.exe", auto_scale=True)

            AutomationController(manager, state).activate_profile(profile)

        trigger_hotkey.assert_called_once()
        self.assertTrue(state.is_scaling_active)

    @patch("companion.services.automation.InputSimulator.trigger_hotkey", return_value=True)
    def test_native_default_never_auto_scales_on_focus(self, trigger_hotkey):
        with tempfile.TemporaryDirectory() as directory:
            manager = ProfileManager(Path(directory) / "config")
            state = AppState()
            profile = Profile(
                name="Game Default", is_default=True, auto_scale=True
            )

            AutomationController(manager, state).activate_profile(profile)

        trigger_hotkey.assert_not_called()
        self.assertFalse(state.is_scaling_active)

    def test_observed_scaling_matches_detected_window_to_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = ProfileManager(Path(directory) / "config")
            game = Profile(name="Game", target_process="Game.exe")
            manager.add_or_update_profile(game)
            state = AppState()
            automation = AutomationController(manager, state)

            automation.reconcile_observed_scaling_state(
                True,
                {"processName": "Game.exe", "exePath": r"C:\Games\Game.exe"},
            )

            self.assertTrue(state.is_scaling_active)
            self.assertEqual(state.current_active_profile.id, game.id)

    def test_observed_scaling_ignores_unmatched_window(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = ProfileManager(Path(directory) / "config")
            state = AppState()
            automation = AutomationController(manager, state)

            automation.reconcile_observed_scaling_state(
                True,
                {"processName": "Unknown.exe", "exePath": r"C:\Games\Unknown.exe"},
            )

            self.assertTrue(state.is_scaling_active)
            self.assertIsNone(state.current_active_profile)

    @patch("companion.services.automation.InputSimulator.trigger_hotkey", return_value=True)
    def test_unverified_window_refuses_auto_scaling(self, trigger_hotkey):
        with tempfile.TemporaryDirectory() as directory:
            manager = ProfileManager(Path(directory) / "config")
            watcher = Mock()
            watcher.focus_window_identity.return_value = False
            state = AppState()
            automation = AutomationController(manager, state, watcher)

            self.assertFalse(
                automation.set_scaling(
                    True,
                    Profile(name="Game"),
                    reason="focus",
                    target_pid=123,
                    target_hwnd=456,
                )
            )

        trigger_hotkey.assert_not_called()
        self.assertFalse(state.is_scaling_active)

    @patch("companion.services.automation.InputSimulator.trigger_hotkey", return_value=True)
    def test_game_profile_takes_precedence_over_scaled_browser(self, trigger_hotkey):
        with tempfile.TemporaryDirectory() as directory:
            manager = ProfileManager(Path(directory) / "config")
            browser = Profile(
                id="browser",
                name="Browser",
                target_process="chrome.exe",
                target_domain="youtube.com",
            )
            game = Profile(
                id="game",
                name="Game",
                target_process="Game.exe",
                auto_scale=True,
                hotkey={"modifiers": ["ctrl", "alt"], "key": "s", "activation_delay_ms": 0},
            )
            manager.config.profiles = [browser, game]
            state = AppState()
            state.current_active_profile = browser
            state.is_scaling_active = True
            state.scaling_owner_profile_id = browser.id
            watcher = Mock()
            watcher.focus_window_identity.return_value = True
            automation = AutomationController(manager, state, watcher)

            automation.activate_profile(
                game,
                r"C:\Games\Game.exe",
                target_pid=123,
                target_hwnd=456,
            )

        self.assertEqual(trigger_hotkey.call_count, 2)
        self.assertEqual(state.current_active_profile.id, game.id)
        self.assertTrue(state.is_scaling_active)
        self.assertEqual(state.scaling_owner_profile_id, game.id)

    @patch("companion.services.automation.InputSimulator.trigger_hotkey", return_value=True)
    def test_browser_cannot_replace_an_active_game_profile(self, trigger_hotkey):
        with tempfile.TemporaryDirectory() as directory:
            manager = ProfileManager(Path(directory) / "config")
            game = Profile(id="game", name="Game", target_process="Game.exe")
            browser = Profile(
                id="browser",
                name="Browser",
                target_process="chrome.exe",
                target_domain="youtube.com",
            )
            state = AppState()
            state.current_active_profile = game
            state.is_scaling_active = True
            state.scaling_owner_profile_id = game.id
            automation = AutomationController(manager, state, Mock())

            automation.activate_profile(
                browser,
                r"C:\Chrome\chrome.exe",
                target_pid=123,
                target_hwnd=456,
            )

        trigger_hotkey.assert_not_called()
        self.assertEqual(state.current_active_profile.id, game.id)
        self.assertTrue(state.is_scaling_active)

    def test_dll_profile_restarts_running_lossless_scaling(self):
        events = []
        watcher = self.FakeProcessWatcher(events)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "LosslessScaling.exe").write_bytes(b"exe")
            profile = Profile(
                name="DLL profile",
                target_executable_path=str(root / "game.exe"),
                dll_overrides=[
                    DllOverrideConfig(
                        enabled=True,
                        source_dll_path=str(root / "mod.dll"),
                        deployment_target="lossless_scaling",
                    )
                ],
            )
            (root / "mod.dll").write_bytes(b"managed")
            manager = ProfileManager(root / "config")
            manager.config.lossless_scaling_exe_path = str(root / "LosslessScaling.exe")
            automation = AutomationController(manager, AppState(), watcher)
            automation.activate_profile(profile)

            self.assertEqual((root / "dxgi.dll").read_bytes(), b"managed")
        self.assertEqual(events, ["stop", ("launch", True)])

    def test_target_application_dll_is_refused(self):
        events = []
        watcher = self.FakeProcessWatcher(events)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = Profile(
                name="Application DLL profile",
                target_executable_path=str(root / "game.exe"),
                dll_overrides=[
                    DllOverrideConfig(
                        enabled=True,
                        source_dll_path=str(root / "mod.dll"),
                        deployment_target="target_application",
                    )
                ],
            )
            (root / "mod.dll").write_bytes(b"managed")
            automation = AutomationController(ProfileManager(root / "config"), AppState(), watcher)
            automation.activate_profile(profile)

        self.assertEqual(events, [])
        self.assertFalse((root / "dxgi.dll").exists())

    @patch("companion.services.automation.ReshadeManager.trigger_reshade_reload")
    def test_preset_only_profile_does_not_restart_lossless_scaling(self, reload_reshade):
        events = []
        watcher = self.FakeProcessWatcher(events)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "LosslessScaling.exe").write_bytes(b"exe")
            reshade_ini = root / "ReShade.ini"
            preset = root / "Preset.ini"
            reshade_ini.write_text("[GENERAL]\nCurrentPresetPath=old.ini\n", encoding="utf-8")
            preset.write_text("", encoding="utf-8")
            profile = Profile(
                name="Preset profile",
                reshade=ReshadeConfig(
                    enabled=True,
                    reshade_ini_path=str(reshade_ini),
                    preset_path=str(preset),
                    reload_hotkey="Home",
                ),
            )
            manager = ProfileManager(root / "config")
            manager.config.lossless_scaling_exe_path = str(root / "LosslessScaling.exe")
            automation = AutomationController(manager, AppState(), watcher)
            automation.activate_profile(profile)

        self.assertEqual(events, [])
        reload_reshade.assert_called_once_with("Home")

    def test_profile_reshade_path_cannot_redirect_writes_into_game(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lossless_dir = root / "lossless"
            game_dir = root / "game"
            lossless_dir.mkdir()
            game_dir.mkdir()
            (lossless_dir / "LosslessScaling.exe").write_bytes(b"exe")
            lossless_ini = lossless_dir / "ReShade.ini"
            game_ini = game_dir / "ReShade.ini"
            preset = root / "Preset.ini"
            lossless_ini.write_text("[GENERAL]\nCurrentPresetPath=old.ini\n", encoding="utf-8")
            game_ini.write_text("[GENERAL]\nCurrentPresetPath=game.ini\n", encoding="utf-8")
            preset.write_text("", encoding="utf-8")
            profile = Profile(
                name="LS-only preset",
                reshade=ReshadeConfig(
                    enabled=True,
                    reshade_ini_path=str(game_ini),
                    preset_path=str(preset),
                ),
            )
            manager = ProfileManager(root / "config")
            manager.config.lossless_scaling_exe_path = str(lossless_dir / "LosslessScaling.exe")

            AutomationController(manager, AppState()).activate_profile(profile)

            self.assertIn(str(preset.resolve()), lossless_ini.read_text(encoding="utf-8"))
            self.assertIn("CurrentPresetPath=game.ini", game_ini.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
