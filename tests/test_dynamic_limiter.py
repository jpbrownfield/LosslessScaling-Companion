import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from companion.core.models import Profile, RtssLimiterConfig
from companion.core.profile_manager import ProfileManager
from companion.core.state import AppState
from companion.services.dynamic_limiter import DynamicLimiterController
from companion.services.gpu_telemetry import WindowsGpuTelemetry


class GpuHeadroomTelemetryTests(unittest.TestCase):
    def test_ls_on_a_separate_gpu_uses_only_its_own_adapter_capacity(self):
        snapshot = WindowsGpuTelemetry.aggregate_engines([
            ('pid_100_luid_0x0_0x1_phys_0_eng_0_engtype_3D', 100.0),
            ('pid_200_luid_0x0_0x2_phys_0_eng_0_engtype_3D', 5.0),
        ])
        telemetry = WindowsGpuTelemetry.__new__(WindowsGpuTelemetry)
        telemetry._lock = threading.RLock()
        telemetry._adapter_utilization = snapshot['adapters']
        telemetry._process_adapters = snapshot['process_adapters']
        with patch.object(telemetry, 'sample_by_pid', return_value=snapshot['processes']):
            result = telemetry.sample_for_scaling(100, 200)
        self.assertEqual(result['gpu_headroom_percent'], 95)
        self.assertEqual(result['gpu_adapters'], ['0x0_0x2_0'])

    def test_total_is_summed_per_engine_but_not_across_engines_or_gpus(self):
        entries = [
            ('pid_100_luid_0x00000000_0x00000001_phys_0_eng_0_engtype_3D', 80.0),
            ('pid_200_luid_0x00000000_0x00000001_phys_0_eng_0_engtype_3D', 5.0),
            ('pid_100_luid_0x00000000_0x00000001_phys_0_eng_1_engtype_Copy', 30.0),
            ('pid_300_luid_0x00000000_0x00000002_phys_0_eng_0_engtype_3D', 100.0),
            ('pid_999_luid_0x00000000_0x00000001_phys_0_eng_0_engtype_3D', float('nan')),
        ]
        result = WindowsGpuTelemetry.aggregate_engines(entries)
        self.assertEqual(result['processes'][200], 5.0)
        self.assertEqual(result['adapters']['0x00000000_0x00000001_0'], 85.0)
        self.assertEqual(result['adapters']['0x00000000_0x00000002_0'], 100.0)
        self.assertEqual(list(result['process_adapters'][200]), ['0x00000000_0x00000001_0'])
        self.assertNotIn(999, result['processes'])


class FakeTelemetry:
    def __init__(self):
        self.value = {100: 80.0, 200: 10.0}
        self.headroom = 5.0

    def sample_by_pid(self):
        return dict(self.value)

    def sample_for_scaling(self, target_pid, lossless_pid):
        if target_pid not in self.value or lossless_pid not in self.value or self.headroom is None:
            return {}
        return {'game_gpu_percent': self.value[target_pid], 'lossless_gpu_percent': self.value[lossless_pid],
                'gpu_headroom_percent': self.headroom, 'gpu_adapters': ['gpu-a']}

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

    def test_clean_install_discards_all_volatile_calibration_and_maintenance_does_not_sample(self):
        self.controller.poll()
        self.assertIsNotNone(self.controller._session)
        self.controller._pending_sessions.append(self.controller._session)
        self.controller._manual_profile_id = self.profile.id
        self.controller._manual_until = self.now + 300
        self.controller.clear_sessions()
        self.assertIsNone(self.controller._session)
        self.assertEqual(self.controller._pending_sessions, [])
        self.assertIsNone(self.controller._manual_profile_id)
        self.assertEqual(self.controller._manual_until, 0)
        self.state.update_in_progress = True
        self.assertEqual(self.controller.poll()['state'], 'inactive')
        self.assertIsNone(self.controller._session)

    def poll_windows(self, count):
        result = None
        for _ in range(count * self.controller.WINDOW_SAMPLES):
            result = self.controller.poll()
        return result

    def test_dedicated_benchmark_does_not_calibrate_the_saved_profile(self):
        self.state.benchmark_mode_active = True
        self.assertEqual(self.controller.poll()['state'], 'inactive')
        self.assertEqual(self.rtss.limits, [])
        self.assertIsNone(self.controller._session)
        self.assertIsNone(self.profile.rtss.learned_framerate_limit)

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

    def test_low_ls_utilization_is_healthy_when_headroom_is_available(self):
        self.telemetry.value = {100: 3.0, 200: 5.0}
        self.telemetry.headroom = 30.0
        result = self.poll_windows(3)
        self.assertEqual(result["state"], "calibrated")
        self.assertEqual(self.profile.rtss.learned_framerate_limit, 100)
        self.assertEqual(self.rtss.limits, [100])

    def test_high_ls_utilization_does_not_mask_a_saturated_adapter(self):
        self.telemetry.value = {100: 80.0, 200: 30.0}
        self.telemetry.headroom = 2.0
        self.assertEqual(self.poll_windows(3)['state'], 'calibrating')
        self.telemetry.headroom = 20.0
        result = self.poll_windows(1)
        self.assertEqual(result['state'], 'calibrated')
        self.assertEqual(result['current_limit'], 99)

    def test_missing_adapter_telemetry_never_infers_free_capacity(self):
        self.telemetry.headroom = None
        result = self.poll_windows(4)
        self.assertEqual(result['state'], 'telemetry_unavailable')
        self.assertEqual(self.rtss.limits, [100])

    def test_old_floor_based_calibration_is_discarded_without_changing_user_bounds(self):
        self.profile.rtss.learned_framerate_limit = 60
        self.profile.rtss.automatic_calibration_disabled = True
        self.profile.rtss.original_values = {'Limit': 144}
        self.telemetry.headroom = 30
        result = self.poll_windows(3)
        self.assertEqual(result['current_limit'], 100)
        self.assertEqual(self.profile.rtss.minimum_framerate_limit, 30)
        self.assertEqual(self.profile.rtss.calibration_policy_version, 1)
        self.assertEqual(self.profile.rtss.original_values, {'Limit': 144})

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
