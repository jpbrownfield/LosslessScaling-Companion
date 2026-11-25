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
        self.profile = Profile(id="dynamic-game", name="Dynamic Game", target_process="Game.exe",
            rtss=RtssLimiterConfig(enabled=True, limit_mode="dynamic", gpu_target_percent=15,
                                  minimum_framerate_limit=30, maximum_framerate_limit=100))
        self.manager.config.profiles = [self.profile]
        self.manager.config.rtss_frame_limiting_enabled = True
        self.state = AppState()
        self.state.is_scaling_active = True
        self.state.current_active_profile = self.profile
        self.state.current_scaled_target = {"pid": 100, "hwnd": 123, "processName": "Game.exe"}
        self.telemetry = FakeTelemetry()
        self.telemetry.sample_for_scaling = lambda *_: {
            "game_gpu_percent": self.telemetry.value[100], "lossless_gpu_percent": self.telemetry.value[200],
            "gpu_headroom_percent": self.telemetry.headroom, "gpu_adapters": self.adapters}
        self.adapters = ["0x0_0x1_0"]
        self.rtss = FakeRtss()
        self.rtss.read_limit = lambda _: self.rtss.limits[-1] if self.rtss.limits else None
        self.now = 1000.
        self.sequence = 0
        self.count = 0
        self.ratio = 1.
        self.focus = True
        self.generation = 1
        self.source_identity = (100, 50., "Game.exe")
        self.instance = (200, 60.)
        self.age = 0
        self.dropped = 0
        self.controller = DynamicLimiterController(self.manager, self.state, self.rtss,
            telemetry=self.telemetry, time_fn=lambda: self.now, process_running_fn=lambda _: True,
            identity_fn=lambda *_: self.source_identity, ls_instance_fn=lambda _: self.instance,
            presentation_fn=self.host, output_window_fn=lambda _: 456)

    def tearDown(self):
        self.temp_dir.cleanup()

    def host(self, exe, instance, *, binding):
        return {"sampledAt": self.now, "capture": {"observationVersion": 2, "captures": self.count, "adapterSource": "capture_frame", "adapterLuid": "00000000:00000001"},
                "presentationPacing": {"version": 2, "enabled": True, "identityVerified": True, "focused": self.focus,
                    "sourcePid": binding[0], "sourceWindow": binding[1], "outputWindow": binding[2], "context": binding[3],
                    "generation": self.generation, "sequence": self.sequence, "startedMs": (self.now-2)*1000,
                    "endedMs": self.now*1000, "medianMs": 10., "p95Ms": 10*self.ratio,
                    "relativeStalls": 10 if self.ratio > 1.3 else 0, "intervals": 199,
                    "ageMs": self.age, "swapchain": 99, "device": 88, "presentationLuid": "00000000:00000001",
                    "dropped": self.dropped, "overflow": 0}}

    def step(self, count=1, *, responds=True):
        for _ in range(count):
            self.sequence += 1; self.now += 2
            self.count += 2*(self.rtss.limits[-1] if self.rtss.limits and responds else 100)
            result = self.controller.poll()
        return result

    def test_stable_low_ls_utilization_never_reduces_even_when_headroom_is_low(self):
        self.telemetry.value = {100: 2., 200: 1.}
        self.telemetry.headroom = 1.
        self.assertEqual(self.step(20)["state"], "stable")
        self.assertEqual(self.rtss.limits, [100])
        self.assertIsNone(self.profile.rtss.learned_framerate_limit)

    def test_game_utilization_drop_is_not_an_ls_health_signal(self):
        self.step(4)
        self.telemetry.value[100] = 0.
        self.assertEqual(self.step(5)["state"], "stable")
        self.assertEqual(self.rtss.limits, [100])

    def test_isolated_stall_never_changes_cap(self):
        self.step(4);self.ratio = 2.;self.step();self.ratio = 1.;self.step(4)
        self.assertEqual(self.rtss.limits, [100])

    def test_trial_requires_intake_response_and_ls_benefit(self):
        self.ratio = 1.7
        self.assertEqual(self.step(4)["state"], "trial_reduction")
        self.assertEqual(self.rtss.limits, [100, 98])
        self.ratio = 1.
        result = self.step(5)
        self.assertEqual(result["state"], "benefit_confirmed")
        self.assertTrue(result["benefit_confirmed"])
        self.assertEqual(result["extra_reserve_percent"], 2.)
        self.assertFalse(result["absolute_delivery_verified"])
        self.assertIsNone(self.profile.rtss.learned_framerate_limit)

    def test_no_intake_response_restores_previous_cap(self):
        self.ratio = 1.7;self.step(4);self.ratio = 1.
        result = self.step(5, responds=False)
        self.assertEqual(result["state"], "no_response")
        self.assertEqual(self.rtss.limits, [100, 98, 100])
        self.assertFalse(result["benefit_confirmed"])

    def test_headroom_improvement_without_ls_benefit_rolls_back(self):
        self.ratio = 1.7;self.step(4);self.telemetry.headroom = 90.
        result = self.step(5)
        self.assertEqual(result["state"], "no_response")
        self.assertEqual(self.rtss.limits[-1], 100)

    def test_stale_focus_loss_and_drops_cancel_trial(self):
        for invalid in ("stale", "focus", "dropped"):
            with self.subTest(invalid=invalid):
                self.controller.clear_sessions();self.age=0;self.focus=True;self.dropped=0
                self.ratio=1.7;self.step(4)
                self.assertEqual(self.rtss.limits[-1], 98)
                if invalid == "stale":self.age = 4000
                elif invalid == "focus":self.focus = False
                else:self.dropped += 1
                self.assertEqual(self.step()["state"], "unavailable")
                self.assertEqual(self.rtss.limits[-1], 100)

    def test_duplicate_snapshots_do_not_advance_policy(self):
        self.ratio=1.7;self.step(2)
        for _ in range(20):self.controller.poll()
        self.assertEqual(self.controller._session.policy.valid_windows, 2)
        self.assertEqual(self.rtss.limits, [100])

    def test_process_incarnation_change_resets_identity_and_trial(self):
        self.ratio=1.7;self.step(4)
        self.source_identity=(100, 500., "Game.exe")
        self.step()
        self.assertEqual(self.rtss.limits, [100,98,100])
        self.assertEqual(self.controller._session.policy.valid_windows, 1)
        self.instance=(200,600.)
        self.step()
        self.assertEqual(self.controller._session.ls_instance, self.instance)
        self.assertEqual(self.controller._session.policy.valid_windows, 1)

    def test_generation_and_settings_changes_discard_mixed_history(self):
        self.ratio=1.7;self.step(4)
        self.generation += 1
        self.assertEqual(self.step()["state"], "observing")
        self.assertEqual(self.rtss.limits[-1], 100)
        self.state.last_scale_toggle_time += 1
        self.step()
        self.assertEqual(self.controller._session.policy.valid_windows, 1)

    def test_cross_adapter_requires_both_luids_without_using_gpu_enumeration_order(self):
        original=self.controller.presentation_fn
        def cross(*args, **kwargs):
            host=original(*args, **kwargs)
            host["capture"]["adapterLuid"]="00000000:00000002"
            return host
        self.controller.presentation_fn=cross
        self.assertEqual(self.step()["state"], "unavailable")
        self.adapters.append("0x0_0x2_0")
        self.assertEqual(self.step(4)["state"], "stable")

    def test_explicit_manual_lock_survives_policy_upgrade(self):
        self.profile.rtss.automatic_calibration_disabled=True
        self.profile.rtss.learned_framerate_limit=60
        self.profile.rtss.calibration_policy_version=1
        self.profile.rtss.original_values={"Limit":144}
        self.assertEqual(self.step()["state"], "manual_calibration_locked")
        self.assertEqual(self.rtss.limits,[60])
        self.assertEqual(self.profile.rtss.original_values,{"Limit":144})
        self.assertEqual(self.profile.rtss.calibration_policy_version,2)

    def test_incompatible_automatic_learning_is_discarded(self):
        self.profile.rtss.learned_framerate_limit=60
        self.profile.rtss.calibration_policy_version=1
        self.step()
        self.assertEqual(self.rtss.limits,[100])
        self.assertIsNone(self.profile.rtss.learned_framerate_limit)

    def test_manual_completion_requires_fresh_evidence_then_locks(self):
        self.controller.start_manual(self.profile.id)
        self.step(4)
        self.now += 293
        # Expiry sees stale evidence, so cannot persist a supposedly good cap.
        self.step()
        self.assertFalse(self.profile.rtss.automatic_calibration_disabled)
        self.controller.start_manual(self.profile.id)
        self.step(149)
        self.step()
        self.assertTrue(self.profile.rtss.automatic_calibration_disabled)
        self.assertEqual(self.profile.rtss.learned_framerate_limit,100)
        self.controller.reset(self.profile.id)
        self.assertFalse(self.profile.rtss.automatic_calibration_disabled)

    def test_write_failure_after_profile_change_restores_unconfirmed_trial(self):
        original=self.rtss.apply_profile
        def write(profile, previous=None, configured_path=None, framerate_limit=None):
            original(profile, previous, configured_path, framerate_limit)
            if framerate_limit == 98:raise OSError("notify failed after file write")
        self.rtss.apply_profile=write
        self.ratio=1.7
        result=self.step(4)
        self.assertEqual(result["state"],"unavailable")
        self.assertEqual(self.rtss.limits,[100,98,100])

    def test_external_rtss_edits_are_not_overwritten_by_rollback(self):
        self.ratio=1.7;self.step(4)
        self.rtss.read_limit=lambda _: 77
        result=self.step()
        self.assertEqual(result["state"],"unavailable")
        self.assertIn("edited externally", result["message"])
        self.assertEqual(self.rtss.limits,[100,98])

    def test_capture_pause_never_counts_as_healthy_or_spends_reserve(self):
        self.step(4)
        self.now+=2;self.sequence+=1
        result=self.controller.poll()
        self.assertEqual(result["state"],"unavailable")
        self.assertIn("capture is paused",result["message"])
        self.assertEqual(self.rtss.limits,[100])

    def test_profile_shadow_setting_blocks_trials(self):
        self.profile.rtss.adaptive_shadow_only=True
        self.ratio=1.7
        result=self.step(10)
        self.assertEqual(result["state"],"shadow_trial")
        self.assertEqual(self.rtss.limits,[100])

    def test_missing_independent_capture_evidence_cannot_look_healthy(self):
        original = self.controller.presentation_fn
        for field, value in (("observationVersion", 1), ("adapterSource", "device_creation"),
                             ("adapterLuid", ""), ("captures", None)):
            def host(*args, **kwargs):
                snapshot = original(*args, **kwargs)
                snapshot["capture"][field] = value
                return snapshot
            self.controller.presentation_fn = host
            with self.subTest(field=field):
                self.assertEqual(self.step(4)["state"], "unavailable")
        self.assertEqual(self.rtss.limits, [100])

    def test_losing_capture_adapter_evidence_rolls_back_trial(self):
        self.ratio = 1.7
        self.step(4)
        original = self.controller.presentation_fn
        def missing(*args, **kwargs):
            snapshot = original(*args, **kwargs)
            snapshot["capture"]["adapterSource"] = "device_creation"
            return snapshot
        self.controller.presentation_fn = missing
        self.assertEqual(self.step()["state"], "unavailable")
        self.assertEqual(self.rtss.limits, [100, 98, 100])

    def test_benchmark_ownership_blocks_all_profile_writes(self):
        self.state.benchmark_mode_active=True
        self.assertEqual(self.controller.poll()["state"],"inactive")
        self.assertEqual(self.rtss.limits,[])

    def test_missing_host_never_falls_back_to_gpu_headroom(self):
        self.controller.presentation_fn=lambda *_, **__: (_ for _ in ()).throw(ValueError("old host"))
        self.ratio=2
        self.assertEqual(self.step(10)["state"],"unavailable")
        self.assertEqual(self.rtss.limits,[100])


if __name__ == "__main__":
    unittest.main()
