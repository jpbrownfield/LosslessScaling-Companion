import unittest
from unittest.mock import patch

from companion.main import simulation_mode_enabled

from companion.core.models import Profile
from companion.services.simulation_adapters import (
    SimulationNvidiaProfileManager,
    SimulationRtssProfileManager,
    SimulationStartupTaskManager,
)


class SimulationAdapterTests(unittest.TestCase):
    def test_source_mode_honors_explicit_simulation_environment(self):
        with (
            patch("companion.main.sys.frozen", False, create=True),
            patch.dict("companion.main.os.environ", {"LOSSLESS_COMPANION_SIMULATION": "1"}),
        ):
            self.assertTrue(simulation_mode_enabled())

    def test_frozen_release_ignores_simulation_environment(self):
        with (
            patch("companion.main.sys.frozen", True, create=True),
            patch.dict("companion.main.os.environ", {"LOSSLESS_COMPANION_SIMULATION": "1"}),
        ):
            self.assertFalse(simulation_mode_enabled())

    def test_startup_adapter_never_creates_a_real_task(self):
        manager = SimulationStartupTaskManager()
        self.assertFalse(manager.is_enabled())
        self.assertTrue(manager.set_enabled(True)["simulated"])

    def test_nvidia_adapter_reports_simulated_changes(self):
        manager = SimulationNvidiaProfileManager()
        self.assertTrue(manager.set_lossless_scaling_rtx_hdr("fake.exe", True))
        self.assertTrue(manager.set_lossless_scaling_smooth_motion("fake.exe", True))
        self.assertTrue(manager.restore_managed_changes())

    def test_rtss_adapter_stays_unavailable_and_clears_only_metadata(self):
        manager = SimulationRtssProfileManager()
        profile = Profile(name="Game", target_executable_path=r"C:\Games\game.exe")
        profile.rtss.managed_profile_created = True
        profile.rtss.managed_target_process = "game.exe"
        self.assertFalse(manager.status()["installed"])
        self.assertEqual(manager._target_process(profile), "game.exe")
        manager.clear_metadata(profile)
        self.assertFalse(profile.rtss.managed_profile_created)
        with self.assertRaisesRegex(RuntimeError, "disabled in simulation"):
            manager.apply_profile(profile)


if __name__ == "__main__":
    unittest.main()
