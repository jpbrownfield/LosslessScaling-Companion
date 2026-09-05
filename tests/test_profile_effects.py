import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from companion.core.models import DllOverrideConfig, Profile, ReshadeConfig
from companion.core.profile_manager import ProfileManager
from companion.core.state import AppState
from companion.services.automation import AutomationController
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
                auto_scale_on_focus=True,
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

            automation.activate_profile(None)
            self.assertFalse(state.is_scaling_active)
            self.assertFalse((lossless_dir / "dxgi.dll").exists())
            self.assertIn("CurrentPresetPath=old.ini", reshade_ini.read_text(encoding="utf-8"))

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
