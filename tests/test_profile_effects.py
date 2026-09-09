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
    def test_monitor_windows_are_minimized_and_restored_with_scaling(self, _trigger):
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
            windows.restore_managed_windows.assert_called_once_with()

    def test_disabling_monitor_window_management_restores_immediately(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = ProfileManager(Path(directory) / "config")
            manager.config.minimize_other_windows_on_scale = False
            state = AppState()
            state.is_scaling_active = True
            windows = Mock()
            automation = AutomationController(manager, state, window_manager=windows)

            automation.reconcile_window_management_setting()

            windows.restore_managed_windows.assert_called_once_with()

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
        trigger_hotkey.assert_called_once()
        self.assertTrue(state.is_scaling_active)

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
