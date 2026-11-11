import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

from companion.core.profile_manager import ProfileManager
from companion.core.state import AppState
from companion.services.automation import AutomationController
from companion.services.gpu_router import GpuRouter
from companion.services.sunshine_session import SessionLog, SunshineSessionMonitor, host_processes


GPU_A = r"PCI\VEN_10DE&DEV_2203"
GPU_B = r"PCI\VEN_10DE&DEV_2D83"
STAMP = "[2026-11-10 10:00:00.000]: Info: "


def session(name="Encoder GPU", encoder="h264_nvenc"):
    return (STAMP + "New streaming session started [active sessions: 1]\n"
            + STAMP + "CLIENT CONNECTED\n"
            + STAMP + "\nDevice Description : " + name + "\n"
            + STAMP + f"Creating encoder [{encoder}]\n")


class SunshineLogTests(unittest.TestCase):
    def monitor(self, directory, text):
        path = Path(directory) / "sunshine.log"
        path.write_text(text, encoding="utf-8")
        now = [1.0]
        hosts = [(42, 1.0, path)]
        monitor = SunshineSessionMonitor(processes=lambda: hosts, clock=lambda: now[0])
        return monitor, path, now, hosts

    def test_connect_reconnect_and_last_client_disconnect(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, path, now, _ = self.monitor(directory, session())
            self.assertEqual(monitor.gpu_name(), "Encoder GPU")
            with path.open("a") as stream:
                stream.write(STAMP + "New streaming session started [active sessions: 2]\n"
                             + STAMP + "CLIENT CONNECTED\n" + STAMP + "CLIENT DISCONNECTED\n")
            now[0] += 2
            self.assertEqual(monitor.gpu_name(), "Encoder GPU")
            with path.open("a") as stream:
                stream.write(STAMP + "CLIENT DISCONNECTED\n")
            now[0] += 2
            self.assertIsNone(monitor.gpu_name())
            with path.open("a") as stream:
                stream.write(session("Other GPU", "hevc_amf"))
            now[0] += 2
            self.assertEqual(monitor.gpu_name(), "Other GPU")

    def test_startup_probes_and_software_encoding_are_not_hardware_sessions(self):
        with tempfile.TemporaryDirectory() as directory:
            for text in (session().replace(STAMP + "CLIENT CONNECTED\n", ""),
                         session(encoder="libx264"), STAMP + "CLIENT CONNECTED\n"):
                monitor, _, _, _ = self.monitor(directory, text)
                self.assertIsNone(monitor.gpu_name())

    def test_process_exit_pid_reuse_missing_log_and_rotation_drop_old_session(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, path, now, hosts = self.monitor(directory, session())
            self.assertEqual(monitor.gpu_name(), "Encoder GPU")
            hosts[:] = [(42, datetime(2026, 11, 10, 11).timestamp(), path)]
            now[0] += 2
            self.assertIsNone(monitor.gpu_name())
            hosts[:] = [(42, 1.0, path)]
            now[0] += 2
            self.assertEqual(monitor.gpu_name(), "Encoder GPU")
            path.write_text(STAMP + "Host restarted\n", encoding="utf-8")
            now[0] += 2
            self.assertIsNone(monitor.gpu_name())
            path.write_text(session(), encoding="utf-8")
            now[0] += 2
            self.assertEqual(monitor.gpu_name(), "Encoder GPU")
            path.unlink()
            now[0] += 2
            self.assertIsNone(monitor.gpu_name())
            hosts.clear()
            now[0] += 2
            self.assertIsNone(monitor.gpu_name())
            self.assertEqual(monitor._logs, {})

    def test_partial_lines_and_encoder_gpu_change(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, path, now, _ = self.monitor(directory, session().rstrip("\n"))
            self.assertIsNone(monitor.gpu_name())
            with path.open("a") as stream:
                stream.write("\n")
            now[0] += 2
            self.assertEqual(monitor.gpu_name(), "Encoder GPU")
            with path.open("a") as stream:
                stream.write(STAMP + "\nDevice Description : Intel GPU\n")
            now[0] += 2
            self.assertIsNone(monitor.gpu_name())
            with path.open("a") as stream:
                stream.write(STAMP + "Creating encoder [av1_qsv]\n")
            now[0] += 2
            self.assertEqual(monitor.gpu_name(), "Intel GPU")

    def test_legacy_timestamp_and_adapter_before_client_connect(self):
        log = SessionLog(datetime(2026, 11, 10, 9).timestamp())
        text = session().replace(STAMP + "CLIENT CONNECTED\n", "")
        text += STAMP + "CLIENT CONNECTED\n"
        for line in text.replace("2026-11-10 10", "2026:11:10:10").splitlines():
            log.feed(line)
        self.assertEqual(log.clients, 1)
        self.assertTrue(log.hardware_encoder)
        self.assertEqual(log.gpu_name, "Encoder GPU")

    def test_conflicting_host_sessions_do_not_select_arbitrary_gpu(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor, _, _, hosts = self.monitor(directory, session())
            other = Path(directory) / "apollo.log"
            other.write_text(session("Other GPU"), encoding="utf-8")
            hosts.append((43, 1.0, other))
            self.assertIsNone(monitor.gpu_name())

    def test_host_discovery_uses_process_config_and_relative_log_override(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            custom = root / "custom.conf"
            custom.write_text("log_path = logs/stream.log\n", encoding="utf-8")
            for name in ("sunshine.exe", "apollo.exe"):
                host = Mock(pid=42)
                host.info = {"name": name, "exe": str(root / name), "create_time": 12.0}
                host.cmdline.return_value = [str(root / name), str(custom)]
                with patch("companion.services.sunshine_session.psutil.process_iter", return_value=[host]):
                    self.assertEqual(list(host_processes()), [(42, 12.0, root / "config/logs/stream.log")])
                    host.cmdline.return_value.append("log_path=override.log")
                    self.assertEqual(list(host_processes()), [(42, 12.0, root / "config/override.log")])


class SunshineRoutingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.manager = ProfileManager(Path(self.directory.name))
        self.manager.config.preferred_scaling_gpu_device_id = GPU_A
        self.manager.config.preferred_scaling_gpu_name = "Game GPU"
        rows = [{"deviceName": rf"\\.\DISPLAY{index}", "deviceId": device,
                 "name": name, "primary": index == 1}
                for index, device, name in [(1, GPU_A, "Game GPU"), (2, GPU_B, "Encoder GPU")]]
        self.router = GpuRouter(lambda: rows, lambda hwnd: r"\\.\DISPLAY1",
                                ls_gpu_id_map={GPU_A: 3, GPU_B: 6})
        self.state = AppState()
        self.watcher = Mock()
        self.settings = Mock()
        self.automation = AutomationController(self.manager, self.state, self.watcher,
                                              gpu_router=self.router, lossless_settings=self.settings)
        self.automation.sunshine_sessions = Mock()
        self.automation.sunshine_sessions.gpu_name.return_value = "Encoder GPU"
        self.profile = self.manager.config.profiles[0]
        self.profile.native_scaling_settings = {"PreferredGpuId": "3", "OutputDisplayId": "1"}
        self.profile.preferred_gpu_device_id = GPU_A
        self.profile.preferred_gpu_name = "Game GPU"
        self.automation.refresh_gpu_selections()

    def test_session_overrides_gpu_pins_preserves_output_and_saved_intent(self):
        saved = self.profile.model_dump()
        route = self.automation._desired_gpu_route(self.profile, 84)
        self.assertEqual((route["lsGpuId"], route["lsDisplayId"]), (6, 1))
        self.assertEqual(route["deviceName"], r"\\.\DISPLAY1")
        self.assertEqual(self.automation._intended_rendering_gpu(self.profile, GPU_A), GPU_B)
        self.assertEqual(self.profile.model_dump(), saved)
        self.assertEqual(self.manager.config.preferred_scaling_gpu_device_id, GPU_A)
        self.profile.native_scaling_settings["OutputDisplayId"] = "0"
        self.assertEqual(self.automation._desired_gpu_route(self.profile, 84)["lsDisplayId"], 1)

    def test_disconnect_disable_missing_gpu_and_calibration_use_normal_route(self):
        for reason in ("disconnect", "disable", "missing", "benchmark"):
            with self.subTest(reason=reason):
                self.manager.config.sunshine_match_encoder_gpu = reason != "disable"
                self.state.benchmark_mode_active = reason == "benchmark"
                self.automation.sunshine_sessions.gpu_name.return_value = (
                    None if reason == "disconnect" else "Absent GPU" if reason == "missing" else "Encoder GPU")
                route = self.automation._desired_gpu_route(self.profile, 84)
                self.assertEqual(route["lsGpuId"], 3)
                self.assertEqual(self.automation._intended_rendering_gpu(self.profile, GPU_A), GPU_A)

    def test_duplicate_gpu_names_do_not_override_normal_routing(self):
        duplicate = {"deviceName": r"\\.\DISPLAY3", "deviceId": r"PCI\VEN_10DE&DEV_2D84",
                     "name": "Encoder GPU"}
        enumerate_displays = self.router._display_enumerator
        self.router._display_enumerator = lambda: [*enumerate_displays(), duplicate]
        self.assertEqual(self.automation._desired_gpu_route(self.profile, 84)["lsGpuId"], 3)

    def test_targeted_index_correction_preserves_profile_gpu_during_stream(self):
        from tests.test_targeted_gpu_correction import controller, report
        with controller() as (automation, manager, state, _, _, _, _):
            profile = state.current_active_profile
            profile.native_scaling_settings["PreferredGpuId"] = "1"
            profile.preferred_gpu_device_id, profile.preferred_gpu_name = GPU_A, "RTX 3090"
            automation.sunshine_sessions = Mock()
            automation.sunshine_sessions.gpu_name.return_value = "RTX 5050"
            result = automation._correct_current_gpu_index(profile, 82, GPU_B, report(100, 2))
            self.assertEqual(result, "corrected")
            self.assertEqual(profile.native_scaling_settings["PreferredGpuId"], "1")
            self.assertEqual(profile.preferred_gpu_device_id, GPU_A)
            self.assertEqual(manager.config.ls_gpu_id_map[GPU_B], 3)

    def test_active_connect_disconnect_and_disable_handoff_once_without_cooldown(self):
        self.state.current_active_profile = self.profile
        self.state.is_scaling_active = True
        self.state.scaling_target_pid, self.state.scaling_target_hwnd = 42, 84
        self.automation.set_scaling = Mock(return_value=True)
        self.settings.gpu_route_changes_required.return_value = True
        self.settings.update_gpu_route.return_value = True
        # Use real monotonic time; a new session bypasses a recent relaunch.
        import time
        self.automation._last_gpu_route_relaunch_at = time.monotonic()
        self.assertTrue(self.automation.reconcile_gpu_route())
        self.settings.update_gpu_route.assert_called_with(self.profile.lossless_profile_title, 6, 1)
        self.settings.gpu_route_changes_required.return_value = False
        self.assertFalse(self.automation.reconcile_gpu_route())
        self.assertEqual(self.watcher.stop_lossless_scaling.call_count, 1)
        self.settings.gpu_route_changes_required.return_value = True
        self.automation.sunshine_sessions.gpu_name.return_value = None
        self.assertTrue(self.automation.reconcile_gpu_route())
        self.settings.update_gpu_route.assert_called_with(self.profile.lossless_profile_title, 3, 1)
        self.automation.sunshine_sessions.gpu_name.return_value = "Encoder GPU"
        self.assertTrue(self.automation.reconcile_gpu_route())
        self.manager.config.sunshine_match_encoder_gpu = False
        self.assertTrue(self.automation.reconcile_gpu_route())
        self.settings.update_gpu_route.assert_called_with(self.profile.lossless_profile_title, 3, 1)

    def test_active_handoff_waits_until_scaled_target_has_focus(self):
        self.state.current_active_profile = self.profile
        self.state.is_scaling_active = True
        self.state.scaling_target_pid, self.state.scaling_target_hwnd = 42, 84
        self.automation.set_scaling = Mock(return_value=True)
        self.settings.gpu_route_changes_required.return_value = True
        self.watcher.maintain_scaling_target_foreground.return_value = False
        self.assertFalse(self.automation.reconcile_gpu_route())
        self.watcher.stop_lossless_scaling.assert_not_called()
        self.automation.set_scaling.assert_not_called()
        self.watcher.maintain_scaling_target_foreground.return_value = True
        self.assertTrue(self.automation.reconcile_gpu_route())
