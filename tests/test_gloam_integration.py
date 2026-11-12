import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from companion.core.profile_manager import ProfileManager
from companion.core.state import AppState
from companion.services.gloam_integration import GloamIntegration, provision


MONITORS = [{"monitorId": r"\\?\DISPLAY#AAA#one", "deviceName": r"\\.\DISPLAY1", "name": "Panel A", "hdr": True, "sdrWhiteNits": 400},
            {"monitorId": r"\\?\DISPLAY#BBB#two", "deviceName": r"\\.\DISPLAY2", "name": "Panel B", "hdr": True, "sdrWhiteNits": 200}]


class GloamIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = self.root / "gloam.json"
        self.original = {"SchemaVersion": 4, "GamerModeEnabled": False, "GamerProfiles": [{"AppName": "game.exe", "Custom": 123}],
                         "MonitorProfiles": {"original": {"Gamma": 2.2}}, "Future": {"keep": True}}
        self.settings.write_text(json.dumps(self.original), encoding="utf-8")
        self.source = self.root / "LSHdrSession.exe"
        self.source.write_bytes(b"MZ-fixture-only")

    def setup_profiles(self, running=lambda: []):
        return provision(self.root / "sessions", self.settings, MONITORS, self.source, running)

    def test_provisions_unique_exact_path_profiles_preserving_preferences_and_is_idempotent(self):
        result = self.setup_profiles()
        self.assertEqual(result["state"], "ready")
        data = json.loads(self.settings.read_text())
        self.assertEqual(data["MonitorProfiles"], self.original["MonitorProfiles"])
        self.assertEqual(data["Future"], self.original["Future"])
        self.assertEqual(data["GamerProfiles"][0], self.original["GamerProfiles"][0])
        self.assertEqual(len({p["AppName"] for p in result["profiles"]}), 2)
        for monitor, profile in zip(MONITORS, result["profiles"]):
            self.assertEqual(profile["MonitorDevicePath"], monitor["monitorId"])
            self.assertEqual(profile["PictureIntent"], "CinematicHdr")
            self.assertTrue(profile["ApplyWhileRunning"])
            self.assertEqual(Path(profile["ExecutablePath"]).read_bytes(), self.source.read_bytes())
            self.assertNotIn("companion", profile["AppName"].lower())
        stamp = self.settings.stat().st_mtime_ns
        self.setup_profiles()
        self.assertEqual(self.settings.stat().st_mtime_ns, stamp)
        self.assertEqual(len(list(self.root.glob("settings.before-ls-hdr-*.json"))), 1)

    def test_running_gloam_waits_without_touching_its_settings(self):
        original = self.settings.read_bytes()
        self.assertEqual(self.setup_profiles(lambda: ["Gloam.exe"])["state"], "awaiting_gloam_exit")
        self.assertEqual(self.settings.read_bytes(), original)
        self.assertEqual(len(list((self.root / "sessions").glob("*.exe"))), 2)
        self.assertEqual(self.setup_profiles()["state"], "ready")

    def test_owned_helper_upgrades_but_external_changes_are_preserved(self):
        result = self.setup_profiles()
        self.source.write_bytes(b"MZ-new-version")
        self.assertEqual(self.setup_profiles()["state"], "ready")
        executable = Path(result["profiles"][0]["ExecutablePath"])
        self.assertEqual(executable.read_bytes(), self.source.read_bytes())
        executable.write_bytes(b"external-change")
        with self.assertRaises(ValueError):
            self.setup_profiles()
        self.assertEqual(executable.read_bytes(), b"external-change")

    def test_edited_managed_profile_and_manual_pause_are_preserved(self):
        self.setup_profiles()
        data = json.loads(self.settings.read_text())
        data["GamerModeEnabled"] = False
        self.settings.write_text(json.dumps(data))
        self.assertEqual(self.setup_profiles()["state"], "paused")
        data["GamerProfiles"][-1]["GammaMode"] = "Gamma22"
        self.settings.write_text(json.dumps(data))
        before = self.settings.read_bytes()
        self.assertEqual(self.setup_profiles()["state"], "profile_conflict")
        self.assertEqual(self.settings.read_bytes(), before)

    def test_unknown_schema_and_concurrent_edit_are_preserved(self):
        data = copy.deepcopy(self.original)
        data["SchemaVersion"] = 5
        self.settings.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            self.setup_profiles()
        self.assertEqual(json.loads(self.settings.read_text()), data)
        self.settings.write_text(json.dumps(self.original))
        running = Mock(side_effect=[[], ["Gloam.exe"]])
        self.assertEqual(self.setup_profiles(running)["state"], "retry")
        self.assertEqual(json.loads(self.settings.read_text()), self.original)

    def controller(self):
        manager = ProfileManager(self.root / "config")
        manager.config.gloam_hdr_enabled = True
        state = AppState()
        state.is_scaling_active = True
        clock = [1.0]
        tick = [10000]
        probe = {"enabled": True, "ready": True, "colorSpace": 1, "format": 10,
                 "pixels": 36864, "highlights": 100, "gamut": 0, "presentTick": 10000,
                 "sampledTick": 10000, "deviceName": MONITORS[0]["deviceName"], "scans": 1, "error": ""}
        probe.update(sampleDeviceName=MONITORS[0]["deviceName"], sampleWhiteNits=400)
        telemetry = Mock(side_effect=lambda _: {"available": True, "pid": 42, "hdrProbe": dict(probe)})
        child = Mock()
        child.poll.return_value = None
        control = Mock(mapping=None)
        integration = GloamIntegration(manager, state, monitors=Mock(return_value=copy.deepcopy(MONITORS)),
            running=Mock(return_value=["Gloam.exe"]), source=lambda: self.source, telemetry=telemetry,
            process=lambda _: (42, 1), control=control, spawn=Mock(return_value=child),
            clock=lambda: clock[0], settings_path=self.settings)
        # First-time provisioning is performed while Gloam is closed.
        integration.running.return_value = []
        integration.poll()
        integration.running.return_value = ["Gloam.exe"]
        self.addCleanup(integration.close)
        return integration, manager, probe, child, clock, tick

    def test_disabled_runs_no_discovery_telemetry_or_scans_and_keeps_files(self):
        integration, manager, probe, child, clock, tick = self.controller()
        manager.config.gloam_hdr_enabled = False
        integration.monitors.reset_mock()
        integration.telemetry.reset_mock()
        integration.poll()
        integration.monitors.assert_not_called()
        integration.telemetry.assert_not_called()
        integration.control.set.assert_not_called()
        integration.spawn.assert_not_called()
        self.assertEqual(len(list(integration.root.glob("*.exe"))), 2)
        self.assertEqual(len(json.loads(self.settings.read_text())["GamerProfiles"]), 3)

    def test_hdr_requires_repeated_evidence_then_latches_and_disables_sampling(self):
        integration, manager, probe, child, clock, tick = self.controller()
        with patch("companion.services.gloam_integration.tick_ms", side_effect=lambda: tick[0]):
            for i in range(3):
                probe["sampledTick"] = probe["presentTick"] = tick[0] = 10000 + 500 * i
                integration.poll()
            self.assertTrue(integration.snapshot()["confirmed"])
            integration.spawn.assert_called_once()
            self.assertEqual(integration.control.set.call_args.args, (42, True, False, 400))
            # A dark frame cannot undo a confirmed HDR session.
            probe["highlights"] = 0
            probe["sampledTick"] = probe["presentTick"] = tick[0] = 12000
            integration.poll()
            self.assertTrue(integration.snapshot()["helperRunning"])
            manager.config.gloam_hdr_enabled = False
            integration.poll()
            child.stdin.close.assert_called_once()
            self.assertEqual(integration.snapshot()["state"], "disabled")

    def test_stale_and_sdr_evidence_do_not_start_marker(self):
        integration, _, probe, _, _, _ = self.controller()
        with patch("companion.services.gloam_integration.tick_ms", return_value=20000):
            integration.poll()
            integration.spawn.assert_not_called()
            probe["presentTick"] = probe["sampledTick"] = 20000
            probe["highlights"] = 0
            for _ in range(4):
                integration.poll()
            integration.spawn.assert_not_called()

    def test_pq_needs_no_sdr_white_measurement_but_scrgb_does(self):
        integration, _, probe, _, _, _ = self.controller()
        integration.outputs[0]["sdrWhiteNits"] = None
        with patch("companion.services.gloam_integration.tick_ms", return_value=10000):
            integration.poll()
            integration.spawn.assert_not_called()
            self.assertFalse(integration.control.set.call_args.args[2])
            probe["colorSpace"] = 12
            integration.poll()
            integration.spawn.assert_called_once()
            self.assertTrue(integration.snapshot()["confirmed"])
            self.assertFalse(integration.control.set.call_args.args[2])

    def test_monitor_move_exits_old_marker_before_starting_new_marker(self):
        integration, _, probe, child, _, tick = self.controller()
        probe["colorSpace"] = 12
        with patch("companion.services.gloam_integration.tick_ms", return_value=10000):
            integration.poll()
            first = integration.spawn.call_args.args[0][0]
            probe["deviceName"] = MONITORS[1]["deviceName"]
            integration.poll()
            second = integration.spawn.call_args.args[0][0]
            self.assertNotEqual(first, second)
            child.stdin.close.assert_called_once()
            self.assertEqual(integration.snapshot()["monitorId"], MONITORS[1]["monitorId"])

    def test_monitor_move_discards_old_display_and_sdr_white_evidence(self):
        integration, _, probe, _, _, tick = self.controller()
        with patch("companion.services.gloam_integration.tick_ms", side_effect=lambda: tick[0]):
            for i in range(2):
                probe["sampledTick"] = probe["presentTick"] = tick[0] = 10000 + 500 * i
                integration.poll()
            probe["deviceName"] = MONITORS[1]["deviceName"]
            for i in range(3):
                probe["sampledTick"] = probe["presentTick"] = tick[0] = 11000 + 500 * i
                integration.poll()
            integration.spawn.assert_not_called()
            probe["sampleDeviceName"] = MONITORS[1]["deviceName"]
            for i in range(3):
                probe["sampledTick"] = probe["presentTick"] = tick[0] = 13000 + 500 * i
                integration.poll()
            integration.spawn.assert_not_called()  # White level still belongs to Panel A.
            probe["sampleWhiteNits"] = 200
            for i in range(3):
                probe["sampledTick"] = probe["presentTick"] = tick[0] = 15000 + 500 * i
                integration.poll()
            integration.spawn.assert_called_once()
            self.assertEqual(integration.snapshot()["monitorId"], MONITORS[1]["monitorId"])

    def test_live_diagnostic_disabled_never_starts_gpu_fixture_or_helpers(self):
        from companion.services.diagnostic_runner import DiagnosticRunner
        from types import SimpleNamespace
        integration, manager, _, _, _, _ = self.controller()
        manager.config.gloam_hdr_enabled = False
        runner = DiagnosticRunner.__new__(DiagnosticRunner)
        self.assertIn("gloam_hdr", {item["id"] for item in runner.catalog()})
        runner.server = SimpleNamespace(automation=SimpleNamespace(gloam_integration=integration), config=manager.config)
        with patch("companion.services.diagnostic_runner.subprocess.run") as run:
            self.assertEqual(runner._gloam_hdr()["status"], "pass")
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
