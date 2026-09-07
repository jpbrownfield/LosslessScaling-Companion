import tempfile
import unittest
from pathlib import Path

from companion.core.models import Profile, RtssLimiterConfig
from companion.core.profile_manager import ProfileManager
from companion.core.state import AppState
from companion.services.dynamic_limiter import DynamicLimiterController


class FakeTelemetry:
    def __init__(self):
        self.value = {100: 80.0, 200: 10.0}

    def sample_by_pid(self):
        return dict(self.value)

    def close(self):
        pass


class FakeRtss:
    def __init__(self):
        self.limits = []

    def apply_profile(self, profile, previous=None, configured_path=None, framerate_limit=None):
        self.limits.append(framerate_limit)


class DynamicLimiterTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.manager = ProfileManager(Path(self.temp_dir.name))
        self.profile = Profile(
            id="dynamic-game",
            name="Dynamic Game",
            target_process="Game.exe",
            rtss=RtssLimiterConfig(
                enabled=True,
                limit_mode="dynamic",
                gpu_target_percent=15,
                minimum_framerate_limit=30,
                maximum_framerate_limit=100,
                managed_target_process="Game.exe",
            ),
        )
        self.manager.config.profiles = [self.profile]
        self.manager.config.active_profile_id = self.profile.id
        self.manager.config.rtss_frame_limiting_enabled = True
        self.manager.save_config()
        self.state = AppState()
        self.state.is_scaling_active = True
        self.state.current_active_profile = self.profile
        self.state.current_scaled_target = {"pid": 100, "processName": "Game.exe"}
        self.telemetry = FakeTelemetry()
        self.rtss = FakeRtss()
        self.now = 1000.0
        self.game_running = True
        self.controller = DynamicLimiterController(
            self.manager,
            self.state,
            self.rtss,
            telemetry=self.telemetry,
            time_fn=lambda: self.now,
            process_running_fn=lambda _pid: self.game_running,
            process_age_fn=lambda _pid: 0.0,
        )
        self.controller._find_lossless_pid = lambda: 200

    def tearDown(self):
        self.temp_dir.cleanup()

    def poll_windows(self, count):
        result = None
        for _ in range(count * self.controller.WINDOW_SAMPLES):
            result = self.controller.poll()
        return result

    def test_automatic_calibration_reduces_only_after_qualified_gameplay(self):
        first = self.poll_windows(2)
        self.assertEqual(first["state"], "waiting_for_gameplay")
        self.assertEqual(self.rtss.limits, [100])
        third = self.poll_windows(1)
        self.assertEqual(third["state"], "calibrating")
        self.assertEqual(third["current_limit"], 99)
        self.assertEqual(self.rtss.limits, [100, 99])

    def test_game_gpu_drop_freezes_limit_without_rebaselining(self):
        self.poll_windows(3)
        self.telemetry.value = {100: 20.0, 200: 5.0}
        result = self.poll_windows(3)
        self.assertEqual(result["state"], "loading")
        self.assertEqual(self.rtss.limits, [100, 99])
        self.assertIsNone(self.profile.rtss.game_gpu_high_water_percent)

    def test_target_is_a_floor_and_higher_utilization_latches(self):
        self.telemetry.value = {100: 80.0, 200: 22.0}
        result = self.poll_windows(3)
        self.assertEqual(result["state"], "calibrated")
        self.assertEqual(self.profile.rtss.learned_framerate_limit, 100)
        self.assertEqual(self.rtss.limits, [100])

    def test_manual_calibration_arms_for_five_minutes_and_reset_erases_metrics(self):
        result = self.controller.start_manual(self.profile.id)
        self.assertEqual(result["manual_remaining_seconds"], 300)
        self.assertEqual(self.rtss.limits, [100])
        self.poll_windows(1)
        self.now += 301
        expired = self.controller.poll()
        self.assertEqual(expired.get("manual_remaining_seconds", 0), 0)
        self.assertIsNotNone(self.profile.rtss.learned_framerate_limit)
        self.assertTrue(self.profile.rtss.automatic_calibration_disabled)
        self.profile.rtss.learned_framerate_limit = 90
        self.profile.rtss.game_gpu_baseline_percent = 75
        self.profile.rtss.game_gpu_high_water_percent = 85
        reset = self.controller.reset(self.profile.id)
        self.assertEqual(reset["state"], "waiting_for_gameplay")
        self.assertIsNone(self.profile.rtss.learned_framerate_limit)
        self.assertIsNone(self.profile.rtss.game_gpu_baseline_percent)
        self.assertIsNone(self.profile.rtss.game_gpu_high_water_percent)
        self.assertFalse(self.profile.rtss.automatic_calibration_disabled)

    def test_long_high_utilization_session_promotes_baseline_only_after_exit(self):
        self.telemetry.value = {100: 65.0, 200: 22.0}
        self.poll_windows(3)
        self.assertIsNone(self.profile.rtss.game_gpu_baseline_percent)

        self.now += 601
        self.game_running = False
        self.state.is_scaling_active = False
        self.controller.poll()

        self.assertEqual(self.profile.rtss.game_gpu_baseline_percent, 65.0)
        self.assertEqual(self.profile.rtss.game_gpu_high_water_percent, 65.0)

    def test_alternate_baseline_requires_more_than_ten_minutes_and_above_fifty_percent(self):
        self.telemetry.value = {100: 50.0, 200: 22.0}
        self.poll_windows(3)
        self.now += 601
        self.game_running = False
        self.state.is_scaling_active = False
        self.controller.poll()
        self.assertIsNone(self.profile.rtss.game_gpu_baseline_percent)

        self.game_running = True
        self.state.is_scaling_active = True
        self.telemetry.value = {100: 70.0, 200: 22.0}
        self.poll_windows(3)
        self.now += 600
        self.game_running = False
        self.state.is_scaling_active = False
        self.controller.poll()
        self.assertIsNone(self.profile.rtss.game_gpu_baseline_percent)

    def test_completed_manual_calibration_blocks_future_automatic_learning(self):
        self.controller.start_manual(self.profile.id)
        self.poll_windows(1)
        self.now += 301
        result = self.controller.poll()
        limits_after_manual = list(self.rtss.limits)
        self.assertEqual(result["state"], "manual_calibration_locked")

        self.telemetry.value = {100: 90.0, 200: 1.0}
        self.poll_windows(5)
        self.assertEqual(self.rtss.limits, limits_after_manual)

        self.now += 700
        self.game_running = False
        self.state.is_scaling_active = False
        self.controller.poll()
        self.assertNotEqual(self.profile.rtss.game_gpu_baseline_percent, 90.0)


if __name__ == "__main__":
    unittest.main()
