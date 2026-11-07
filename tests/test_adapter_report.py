"""In-LS adapter reports ground GPU routing without trial scaling."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from companion.services import adapter_report


def fixture(path, **overrides):
    payload = {
        "schema": 1,
        "pid": 1234,
        "hostVersion": 300,
        "hasDevice": True,
        "device": {"description": "NVIDIA GeForce RTX 5050",
                   "vendorId": 4318, "deviceId": 11651,
                   "subSysId": 0, "revision": 161,
                   "luidLow": 111, "luidHigh": 0, "isSoftware": False},
        "enumAdapters": [
            {"description": "NVIDIA GeForce RTX 3090 Ti",
             "vendorId": 4318, "deviceId": 8707,
             "luidLow": 222, "luidHigh": 0, "isSoftware": False},
            {"description": "NVIDIA GeForce RTX 5050",
             "vendorId": 4318, "deviceId": 11651,
             "luidLow": 111, "luidHigh": 0, "isSoftware": False},
        ],
        "enumAdapters1": [
            {"description": "NVIDIA GeForce RTX 5050",
             "vendorId": 4318, "deviceId": 11651,
             "luidLow": 111, "luidHigh": 0, "isSoftware": False},
            {"description": "NVIDIA GeForce RTX 3090 Ti",
             "vendorId": 4318, "deviceId": 8707,
             "luidLow": 222, "luidHigh": 0, "isSoftware": False},
        ],
        "deviceIndexAdapters": 1,
        "deviceIndexAdapters1": 0,
        "deviceOutputs": [],
        "hasSettingsIds": True,
        "settingsGpu": 1,
        "settingsDisplay": 2,
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


class AdapterReportTests(unittest.TestCase):
    def test_load_latest_prefers_newest_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture(root / "ls-adapter-report-1.json", pid=1)
            fixture(root / "ls-adapter-report-2.json", pid=2)
            (root / "ls-adapter-report-3.json").write_text("bogus",
                                                           encoding="utf-8")
            (root / "other.json").write_text("{}", encoding="utf-8")
            import os
            import time

            old = time.time() - 100
            os.utime(root / "ls-adapter-report-1.json", (old, old))
            report = adapter_report.load_latest(root)
            self.assertEqual(report["pid"], 2)

    def test_load_latest_filters_pid_and_stale(self):
        import os
        import time

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture(root / "ls-adapter-report-9.json", pid=9)
            self.assertIsNone(adapter_report.load_latest(root, pid=10))
            old = time.time() - 100000
            os.utime(root / "ls-adapter-report-9.json", (old, old))
            self.assertIsNone(adapter_report.load_latest(root))

    def test_device_pci(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = fixture(root / "ls-adapter-report-1.json")
            report = adapter_report.load_latest(root)
            self.assertEqual(adapter_report.device_pci(report),
                             "PCI\\VEN_10DE&DEV_2D83")
            payload["device"] = {"vendorId": "x"}
            self.assertIsNone(adapter_report.device_pci(payload))
            self.assertIsNone(adapter_report.device_pci({}))

    def test_candidate_gpu_ids_settings_first(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture(root / "ls-adapter-report-1.json")
            report = adapter_report.load_latest(root)
            # Settings-anchored 1, then enumAdapters index 1 -> 2,
            # then enumAdapters1 index 0 -> 1 (deduped).
            self.assertEqual(adapter_report.candidate_gpu_ids(report), [1, 2])

    def test_candidate_gpu_ids_without_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture(root / "ls-adapter-report-1.json", hasSettingsIds=False)
            report = adapter_report.load_latest(root)
            self.assertEqual(adapter_report.candidate_gpu_ids(report), [2, 1])

    def test_settings_route_for_title(self):
        report = {
            "settingsProfiles": [
                {"title": "Default", "gpu": 1, "display": 2},
                {"title": "Game", "gpu": 2, "display": 1},
            ],
            "rootGpuPreference": 1,
        }
        self.assertEqual(
            adapter_report.settings_route_for(report, "Default"), (1, 2))
        self.assertEqual(
            adapter_report.settings_route_for(report, "game"), (2, 1))
        self.assertEqual(
            adapter_report.settings_route_for(report, "Missing"), (None, None))
        self.assertEqual(
            adapter_report.settings_route_for({}, "Default"), (None, None))
        self.assertEqual(adapter_report.settings_route_for(
            {"hasSettingsIds": True, "settingsGpu": 3, "settingsDisplay": 4},
            "Anything"), (3, 4))

    def test_candidate_gpu_ids_prefers_title_match(self):
        report = {
            "settingsProfiles": [
                {"title": "Default", "gpu": 1, "display": 2},
                {"title": "Game", "gpu": 2, "display": 1},
            ],
            "deviceIndexAdapters": 0,
            "deviceIndexAdapters1": 1,
        }
        self.assertEqual(
            adapter_report.candidate_gpu_ids(report, "Game")[0], 2)
        self.assertEqual(
            adapter_report.candidate_gpu_ids(report, "Default")[0], 1)
        self.assertEqual(
            adapter_report.candidate_gpu_ids(report, "Missing")[0], 1)

    def test_attached_outputs(self):
        report = {"deviceOutputs": [
            {"deviceName": "A", "attached": True},
            {"deviceName": "B", "attached": False},
            "junk",
        ]}
        self.assertEqual(
            [item["deviceName"] for item in adapter_report.attached_outputs(report)],
            ["A"])

    def test_detect_reshaping_layers(self):
        from types import SimpleNamespace

        maps = [
            SimpleNamespace(path=r"C:\Windows\System32\dxgi.dll"),
            SimpleNamespace(path=r"C:\Windows\System32\kernel32.dll"),
            SimpleNamespace(path=r"J:\Steam\Lossless Scaling\dxgi.dll"),
            SimpleNamespace(path=r"J:\Steam\Lossless Scaling\SpecialK64.dll"),
            SimpleNamespace(path=""),
        ]
        proc = SimpleNamespace(memory_maps=lambda: maps)
        self.assertEqual(
            adapter_report.detect_reshaping_layers(process=proc),
            ["dxgi.dll", "specialk64.dll"])
        self.assertEqual(adapter_report.detect_reshaping_layers(process=None, pid=None), [])
        self.assertEqual(adapter_report.detect_reshaping_layers(pid=999999999), [])

    def test_same_device(self):
        full = r"PCI\VEN_10DE&DEV_2D83&SUBSYS_53811462&REV_A1"
        self.assertTrue(adapter_report.same_device(full, full))
        self.assertTrue(adapter_report.same_device(full, r"PCI\VEN_10DE&DEV_2D83"))
        self.assertTrue(adapter_report.same_device(r"pci\ven_10de&dev_2d83", full))
        self.assertFalse(adapter_report.same_device(
            full, r"PCI\VEN_10DE&DEV_2203&SUBSYS_50901462&REV_A1"))
        self.assertFalse(adapter_report.same_device(full, "nonsense"))
        self.assertFalse(adapter_report.same_device(None, full))


def make_automation(settings_profiles, gpu_map=None, disp_map=None,
                    **config_overrides):
    from companion.core.profile_manager import ProfileManager
    from companion.core.state import AppState
    from companion.services.automation import AutomationController
    from companion.services.gpu_router import GpuRouter

    adapters = [
        {"deviceName": r"\\.\DISPLAY1",
         "name": "NVIDIA GeForce RTX 3090 Ti",
         "deviceId": r"PCI\VEN_10DE&DEV_2203", "primary": True},
        {"deviceName": r"\\.\DISPLAY2", "name": "NVIDIA GeForce RTX 5050",
         "deviceId": r"PCI\VEN_10DE&DEV_2D83", "primary": False},
    ]
    manager = ProfileManager(Path(tempfile.mkdtemp()))
    if gpu_map is not None:
        manager.config.ls_gpu_id_map = dict(gpu_map)
    if disp_map is not None:
        manager.config.ls_display_id_map = dict(disp_map)
    for key, value in config_overrides.items():
        setattr(manager.config, key, value)
    router = GpuRouter(lambda: adapters, lambda hwnd: r"\\.\DISPLAY2",
                       ls_gpu_id_map=dict(gpu_map or {}),
                       ls_display_id_map=dict(disp_map or {}))
    watcher = Mock()
    watcher.check_is_lossless_scaling_running.return_value = True
    watcher.stop_lossless_scaling.return_value = True
    watcher.launch_lossless_scaling.return_value = True
    settings = Mock()
    settings.read.return_value = {"profiles": settings_profiles}
    automation = AutomationController(
        manager, AppState(), watcher, gpu_router=router,
        lossless_settings=settings)
    return automation, manager, watcher, settings


class RepairUsesReportTests(unittest.TestCase):
    def test_report_candidates_tried_first(self):
        from types import SimpleNamespace

        from companion.core.profile_manager import ProfileManager
        from companion.core.state import AppState
        from companion.services.automation import AutomationController
        from companion.services.gpu_router import GpuRouter

        adapters = [
            {"deviceName": r"\\.\DISPLAY1",
             "name": "NVIDIA GeForce RTX 3090 Ti",
             "deviceId": r"PCI\VEN_10DE&DEV_2203", "primary": True},
            {"deviceName": r"\\.\DISPLAY2", "name": "NVIDIA GeForce RTX 5050",
             "deviceId": r"PCI\VEN_10DE&DEV_2D83", "primary": False},
        ]
        with tempfile.TemporaryDirectory() as directory:
            manager = ProfileManager(Path(directory))
            router = GpuRouter(lambda: adapters, lambda hwnd: r"\\.\DISPLAY2")
            watcher = Mock()
            watcher.check_is_lossless_scaling_running.return_value = True
            watcher.stop_lossless_scaling.return_value = True
            watcher.launch_lossless_scaling.return_value = True
            settings = Mock()
            settings.read.return_value = {"profiles": [
                {"Title": "Default", "PreferredGpuId": "2",
                 "OutputDisplayId": "1"}]}
            settings.update_gpu_route.return_value = True
            automation = AutomationController(
                manager, AppState(), watcher, gpu_router=router,
                lossless_settings=settings,
                presentmon_provider=lambda: Path("pm.exe"))
            automation.set_scaling = Mock(return_value=True)
            automation._verify_active_route = Mock(return_value=True)
            profile = SimpleNamespace(lossless_profile_title="Default",
                                      name="Game", id="game")
            report = {"device": {"vendorId": 4318, "deviceId": 11651},
                      "hasSettingsIds": True, "settingsGpu": 5,
                      "settingsDisplay": 2, "deviceIndexAdapters": 1,
                      "deviceIndexAdapters1": 0}
            with patch.object(adapter_report, "load_latest",
                              return_value=report), \
                    patch.object(automation, "_adapter_report_process", return_value=(4242, 0)):
                self.assertTrue(automation._repair_route(
                    profile, "focus", target_pid=42, target_hwnd=82))
            first = settings.update_gpu_route.call_args_list[0]
            # Report-anchored GPU 5 is tried before the blind 1..N sweep.
            self.assertEqual(first.args[1:], (5, 2))
            # Attribution uses the report device (5050), not the window GPU.
            self.assertEqual(
                manager.config.ls_gpu_id_map[r"PCI\VEN_10DE&DEV_2D83"], 5)
            self.assertEqual(
                manager.config.ls_display_id_map[r"\\.\DISPLAY2"], 2)


class AdoptAdapterReportTests(unittest.TestCase):
    def _report(self, **overrides):
        payload = {
            "schema": 1, "pid": 4242,
            "device": {"description": "NVIDIA GeForce RTX 5050",
                       "vendorId": 4318, "deviceId": 11651},
            "hasSettingsIds": True, "settingsGpu": 1, "settingsDisplay": 2,
        }
        payload.update(overrides)
        return payload

    def test_adopt_confirms_matching_map(self):
        automation, manager, _, _ = make_automation(
            [{"Title": "Default", "PreferredGpuId": "1",
              "OutputDisplayId": "2"}],
            gpu_map={r"PCI\VEN_10DE&DEV_2D83": 1})
        profile = SimpleNamespace(lossless_profile_title="Default", name="G")
        with patch.object(adapter_report, "load_latest",
                          return_value=self._report()), \
             patch.object(automation, "_adapter_report_process", return_value=(4242, 0)):
            self.assertEqual(automation._adopt_adapter_report(profile),
                             "confirmed")
        self.assertEqual(
            manager.config.ls_gpu_id_map[r"PCI\VEN_10DE&DEV_2D83"], 1)
        self.assertIsNotNone(manager.config.ls_gpu_map_fingerprint)

    def test_adopt_corrects_mismatched_map(self):
        automation, manager, _, _ = make_automation(
            [{"Title": "Default", "PreferredGpuId": "1",
              "OutputDisplayId": "2"}],
            gpu_map={r"PCI\VEN_10DE&DEV_2203": 1})
        self.assertNotIn(r"PCI\VEN_10DE&DEV_2D83",
                         manager.config.ls_gpu_id_map)
        profile = SimpleNamespace(lossless_profile_title="Default", name="G")
        with patch.object(adapter_report, "load_latest",
                          return_value=self._report()), \
             patch.object(automation, "_adapter_report_process", return_value=(4242, 0)):
            self.assertEqual(automation._adopt_adapter_report(profile),
                             "corrected")
        self.assertEqual(
            manager.config.ls_gpu_id_map[r"PCI\VEN_10DE&DEV_2D83"], 1)
        self.assertEqual(
            manager.config.ls_gpu_id_map_previous[r"PCI\VEN_10DE&DEV_2203"], 1)

    def test_adopt_ignores_stale_pid(self):
        automation, manager, _, _ = make_automation(
            [{"Title": "Default", "PreferredGpuId": "1",
              "OutputDisplayId": "2"}])
        profile = SimpleNamespace(lossless_profile_title="Default", name="G")
        with patch.object(adapter_report, "load_latest",
                          return_value=self._report()), \
             patch.object(automation, "_adapter_report_process", return_value=(9999, 0)):
            self.assertIsNone(automation._adopt_adapter_report(profile))
        self.assertEqual(manager.config.ls_gpu_id_map, {})

    def test_adopt_without_report_or_profile(self):
        automation, _, _, _ = make_automation(
            [{"Title": "Default", "PreferredGpuId": "1",
              "OutputDisplayId": "2"}])
        with patch.object(adapter_report, "load_latest", return_value=None):
            self.assertIsNone(automation._adopt_adapter_report(
                SimpleNamespace(lossless_profile_title="Default", name="G")))
            self.assertIsNone(automation._adopt_adapter_report(None))

    def test_static_display_tried_first_in_repair(self):
        automation, _, _, settings = make_automation(
            [{"Title": "Default", "PreferredGpuId": "2",
              "OutputDisplayId": "1"}])
        automation.set_scaling = Mock(return_value=True)
        automation._verify_active_route = Mock(return_value=False)
        seen = []

        def record(title, gpu, disp):
            seen.append((gpu, disp))
            return True

        settings.update_gpu_route.side_effect = record
        profile = SimpleNamespace(lossless_profile_title="Default",
                                  name="Game", id="game")
        with patch.object(adapter_report, "load_latest", return_value=None):
            automation._repair_route(profile, "focus", target_pid=42,
                                     target_hwnd=82)
        # Window is on DISPLAY2 (GDI id 2): its static display leads.
        self.assertTrue(seen)
        self.assertEqual(seen[0][1], 2)
        self.assertIn(1, [disp for _, disp in seen])


class EnforceIntendedRouteTests(unittest.TestCase):
    def _controller(self, settings_numbers, **config_overrides):
        automation, manager, watcher, settings = make_automation(
            [{"Title": "Default",
              "PreferredGpuId": str(settings_numbers[0]),
              "OutputDisplayId": str(settings_numbers[1])}],
            **config_overrides)
        return automation, manager, watcher, settings

    def test_noop_when_route_matches_intent(self):
        from companion.core.models import Profile

        automation, _, watcher, settings = self._controller(
            (1, 2),
            gpu_map={r"PCI\VEN_10DE&DEV_2D83": 1},
            disp_map={r"\\.\DISPLAY1": 1, r"\\.\DISPLAY2": 2},
            preferred_scaling_gpu_device_id=r"PCI\VEN_10DE&DEV_2D83",
            preferred_scaling_gpu_name="NVIDIA GeForce RTX 5050",
            auto_route_gpu_to_display=False)
        profile = Profile(name="Game", lossless_profile_title="Default")
        self.assertTrue(automation._enforce_intended_route(
            profile, "focus", 42, 82))
        watcher.stop_lossless_scaling.assert_not_called()
        settings.update_gpu_route.assert_not_called()

    def test_handoff_when_map_correction_changed_intent(self):
        from companion.core.models import Profile

        automation, _, watcher, settings = self._controller(
            (2, 2),
            gpu_map={r"PCI\VEN_10DE&DEV_2D83": 1},
            disp_map={r"\\.\DISPLAY1": 1, r"\\.\DISPLAY2": 2},
            preferred_scaling_gpu_device_id=r"PCI\VEN_10DE&DEV_2D83",
            preferred_scaling_gpu_name="NVIDIA GeForce RTX 5050",
            auto_route_gpu_to_display=False)
        profile = Profile(name="Game", lossless_profile_title="Default")
        calls = []
        automation.set_scaling = Mock(
            side_effect=lambda *a, **k: calls.append((a, k)) or True)
        self.assertTrue(automation._enforce_intended_route(
            profile, "focus", 42, 82))
        # Unscale, rewrite (2,2) -> (1,2), relaunch, rescale.
        self.assertEqual(
            settings.update_gpu_route.call_args_list[0].args[1:], (1, 2))
        self.assertTrue(any(not a[0] for a, _ in calls))
        self.assertTrue(any(a[0] for a, _ in calls))
        watcher.launch_lossless_scaling.assert_called()

    def test_skipped_during_benchmarks_or_cooldown(self):
        from companion.core.models import Profile

        automation, _, watcher, _ = self._controller((2, 2))
        profile = Profile(name="Game", lossless_profile_title="Default")
        automation.state.benchmark_mode_active = True
        self.assertTrue(automation._enforce_intended_route(
            profile, "focus", 42, 82))
        watcher.stop_lossless_scaling.assert_not_called()
        automation.state.benchmark_mode_active = False
        automation2, _, watcher2, _ = self._controller(
            (1, 1),
            gpu_map={r"PCI\VEN_10DE&DEV_2D83": 2},
            disp_map={r"\\.\DISPLAY1": 1, r"\\.\DISPLAY2": 2},
            preferred_scaling_gpu_device_id=r"PCI\VEN_10DE&DEV_2D83",
            preferred_scaling_gpu_name="NVIDIA GeForce RTX 5050",
            auto_route_gpu_to_display=False)
        automation2._last_gpu_route_relaunch_at = time.monotonic()
        self.assertTrue(automation2._enforce_intended_route(
            profile, "focus", 42, 82))
        watcher.stop_lossless_scaling.assert_not_called()


class MigrateStoredRoutesTests(unittest.TestCase):
    def _controller(self, gpu_map, disp_map, prev_gpu=None, prev_disp=None):
        automation, manager, _, settings = make_automation(
            [{"Title": "Default", "PreferredGpuId": "1",
              "OutputDisplayId": "2"}],
            gpu_map=gpu_map, disp_map=disp_map)
        if prev_gpu is not None:
            manager.config.ls_gpu_id_map_previous = dict(prev_gpu)
        if prev_disp is not None:
            manager.config.ls_display_id_map_previous = dict(prev_disp)
        return automation, manager, settings

    def test_migrates_companion_explicit_routes(self):
        automation, manager, _ = self._controller(
            {"3080": 0}, {},
            prev_gpu={r"PCI\VEN_10DE&DEV_2203": 2},
            prev_disp={r"\\.\DISPLAY1": 1, r"\\.\DISPLAY2": 2})
        from companion.core.models import Profile

        profile = Profile(name="Game", lossless_profile_title="Game",
                          native_scaling_settings={"PreferredGpuId": "2",
                                                   "OutputDisplayId": "2"})
        manager.config.profiles.append(profile)
        manager.config.ls_gpu_id_map = {
            r"PCI\VEN_10DE&DEV_2203": 1,
            r"PCI\VEN_10DE&DEV_2D83": 2}
        summary = automation._migrate_stored_routes()
        self.assertEqual(profile.native_scaling_settings["PreferredGpuId"], "1")
        self.assertEqual(profile.native_scaling_settings["OutputDisplayId"], "2")
        self.assertIn(profile.id, summary["migrated"])

    def test_migrate_skips_unknown_and_auto(self):
        automation, manager, _ = self._controller(
            {"x": 1}, {},
            prev_gpu={r"PCI\VEN_10DE&DEV_2203": 2},
            prev_disp={})
        from companion.core.models import Profile

        unknown = Profile(name="U", lossless_profile_title="U",
                          native_scaling_settings={"PreferredGpuId": "9",
                                                   "OutputDisplayId": "0"})
        auto = Profile(name="A", lossless_profile_title="A",
                       native_scaling_settings={"PreferredGpuId": "0",
                                                "OutputDisplayId": "0"})
        manager.config.profiles.extend([unknown, auto])
        summary = automation._migrate_stored_routes()
        self.assertEqual(
            unknown.native_scaling_settings["PreferredGpuId"], "9")
        self.assertTrue(any(unknown.id in str(item)
                            for item in summary["skipped"]))

    def test_migrate_ls_titles_needs_stopped_host(self):
        automation, manager, settings = self._controller(
            {r"PCI\VEN_10DE&DEV_2203": 1, r"PCI\VEN_10DE&DEV_2D83": 2},
            {r"\\.\DISPLAY1": 1, r"\\.\DISPLAY2": 2},
            prev_gpu={r"PCI\VEN_10DE&DEV_2203": 2,
                       r"PCI\VEN_10DE&DEV_2D83": 1},
            prev_disp={r"\\.\DISPLAY1": 1, r"\\.\DISPLAY2": 2})
        settings.read.return_value = {"profiles": [
            {"Title": "Default", "PreferredGpuId": "1", "OutputDisplayId": "2"},
            {"Title": "Game", "PreferredGpuId": "2", "OutputDisplayId": "2"},
            {"Title": "Custom", "PreferredGpuId": "9", "OutputDisplayId": "9"},
        ]}
        settings.gpu_route_changes_required.side_effect = lambda t, g, d: True
        settings.update_gpu_route.return_value = True
        summary = automation._migrate_ls_profile_routes(skip_titles=["Default"])
        written = {call.args[0]: call.args[1:]
                   for call in settings.update_gpu_route.call_args_list}
        # Game (2,2) under the old map meant 3090Ti+Alienware -> rewritten (1,2).
        self.assertEqual(written.get("Game"), (1, 2))
        # Default skipped; Custom (9,9) unknown under the old map.
        self.assertNotIn("Default", written)
        self.assertNotIn("Custom", written)
        self.assertTrue(any("Custom" in str(item) for item in summary["skipped"]))

    def test_no_previous_map_migrates_nothing(self):
        automation, manager, settings = self._controller({}, {})
        self.assertEqual(automation._migrate_stored_routes(),
                         {"migrated": [], "skipped": []})
        self.assertEqual(
            automation._migrate_ls_profile_routes(),
            {"migrated": [], "skipped": []})


if __name__ == "__main__":
    unittest.main()
