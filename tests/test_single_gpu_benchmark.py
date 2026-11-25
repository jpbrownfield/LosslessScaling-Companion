import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from companion.core.models import AppConfig, Profile
from companion.core.state import AppState
from companion.services.single_gpu_benchmark import SingleGpuSetup, select_gpu, verify_gpu_context


GPU = {"name": "NVIDIA GeForce RTX 3090 Ti", "deviceId": "pci-3090", "lsGpuId": 2}


class SingleGpuBenchmarkTests(unittest.TestCase):
    def test_3090_selection_requires_unique_mapped_gpu(self):
        inventory = {"gpus": [{"name": "RTX 5050", "deviceId": "pci-5050", "lsGpuId": 1}, GPU]}
        self.assertEqual(select_gpu(inventory, "3090"), GPU)
        for gpus in ([], [GPU, GPU], [{**GPU, "lsGpuId": 0}]):
            with self.subTest(gpus=gpus), self.assertRaises(RuntimeError):
                select_gpu({"gpus": gpus}, "3090")

    def test_actual_workload_capture_and_presentation_must_share_requested_gpu(self):
        source = {"adapter": GPU["name"], "adapterLuid": "00000000:00000001"}
        host = {"capture": {"observationVersion": 2, "adapterSource": "capture_frame", "captures": 20,
                            "adapterLuid": "00000000:00000001"},
                "presentationPacing": {"presentationLuid": "0x0_0x1_0"}, "addons": []}
        self.assertTrue(verify_gpu_context(source, host, GPU["name"])["verified"])
        for field, value in (("adapterSource", "device_creation"), ("captures", 0),
                             ("adapterLuid", "00000000:00000002"), ("observationVersion", 1)):
            invalid = {**host, "capture": {**host["capture"], field: value}}
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                verify_gpu_context(source, invalid, GPU["name"])
        for invalid in ({**host, "presentationPacing": {"presentationLuid": "00000000:00000002"}},
                        {**host, "addons": [{"id": "DLSSNR02", "enabled": True}]}):
            with self.assertRaises(RuntimeError):
                verify_gpu_context(source, invalid, GPU["name"])

    def setup_session(self, directory):
        settings = Path(directory) / "Settings.xml"
        settings.write_bytes(b"original private settings")
        default = Profile(id="default", name="Default", is_default=True,
                          lossless_profile_title="Native", preferred_gpu_name="RTX 5050")
        manager = Mock(config=AppConfig(profiles=[default], active_profile_id=default.id))
        manager.get_profile_by_id.return_value = default
        automation = Mock(state=AppState())
        automation.gpu_router.detect.return_value = {"gpus": [GPU]}
        automation.lossless_settings.path = settings
        automation.lossless_settings.get_profile.return_value = {"Title": "Native"}
        watcher = Mock()
        watcher.check_is_lossless_scaling_running.return_value = False
        with patch("companion.services.single_gpu_benchmark.assert_private_settings"):
            setup = SingleGpuSetup(manager, automation, watcher, "3090", directory)
        return setup, automation, watcher, settings, default

    def test_temporary_setup_disables_nr_and_restores_xml_deployment_and_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            setup, automation, watcher, settings, previous = self.setup_session(directory)
            automation.activate_profile.side_effect = lambda profile, *a, **k: setattr(automation.state, "current_active_profile", profile)
            setup.apply(123, 456, Path("LSBenchmark.exe"))
            self.assertEqual(setup.profile.preferred_gpu_name, GPU["name"])
            self.assertFalse(setup.profile.addon_config["addons"]["DLSSNR02"]["_enabled"])
            self.assertEqual(setup.profile.native_scaling_settings["CaptureApi"], "WGC")
            self.assertEqual(setup.profile.native_scaling_settings["PreferredGpuId"], "2")
            settings.write_bytes(b"temporary benchmark settings")
            setup.restore()
            self.assertEqual(settings.read_bytes(), b"original private settings")
            self.assertIs(automation.state.current_active_profile, previous)
            self.assertEqual(previous.preferred_gpu_name, "RTX 5050")
            self.assertEqual(automation.deployment_manager.apply.call_args.kwargs["profile_id"], previous.id)
            self.assertFalse(setup.changed)

    def test_failed_setup_still_restores_and_refuses_to_restore_under_running_host(self):
        with tempfile.TemporaryDirectory() as directory:
            setup, automation, watcher, settings, previous = self.setup_session(directory)
            automation.lossless_settings.update_profile.return_value = False
            with self.assertRaises(RuntimeError):
                setup.apply(123, 456, Path("LSBenchmark.exe"))
            settings.write_bytes(b"partial edit")
            watcher.check_is_lossless_scaling_running.return_value = True
            with self.assertRaises(RuntimeError):
                setup.restore()
            self.assertEqual(settings.read_bytes(), b"partial edit")
            automation.deployment_manager.apply.assert_not_called()
            watcher.check_is_lossless_scaling_running.return_value = False
            setup.restore()
            self.assertEqual(settings.read_bytes(), b"original private settings")

    def test_deployment_restore_failure_still_restores_private_xml(self):
        with tempfile.TemporaryDirectory() as directory:
            setup, automation, watcher, settings, previous = self.setup_session(directory)
            setup.changed = True
            settings.write_bytes(b"temporary settings")
            automation.deployment_manager.apply.side_effect = RuntimeError("fixture restore error")
            with self.assertRaisesRegex(RuntimeError, "fixture restore error"):
                setup.restore()
            self.assertEqual(settings.read_bytes(), b"original private settings")


if __name__ == "__main__":
    unittest.main()
