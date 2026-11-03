"""Window-to-monitor GPU alignment: every GPU must be reachable by window.

Simulates a two-GPU desktop (RTX 3090 Ti + RTX 5050 style pair), places a
window on each monitor, swaps the enumeration order (driver reorder), and
asserts routing still lands on the same physical GPU by stable identity.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from companion.core.profile_manager import ProfileManager
from companion.core.state import AppState
from companion.services.automation import AutomationController
from companion.services.gpu_router import GpuRouter


RTX_3090 = r"PCI\VEN_10DE&DEV_2203&SUBSYS_50901462"
RTX_5050 = r"PCI\VEN_10DE&DEV_2D83&SUBSYS_53811462"


def adapters(suffix_5050="REV_A1"):
    return [
        {"deviceName": r"\\.\DISPLAY1", "name": "NVIDIA GeForce RTX 3090 Ti",
         "deviceId": RTX_3090, "primary": True},
        {"deviceName": r"\\.\DISPLAY2", "name": "NVIDIA GeForce RTX 5050",
         "deviceId": f"{RTX_5050}\\{suffix_5050}", "primary": False},
    ]


def controller_for(router):
    manager = ProfileManager(Path(tempfile.mkdtemp()))
    manager.config.auto_route_gpu_to_display = True
    state = AppState()
    return AutomationController(manager, state, Mock(), gpu_router=router), manager


class GpuWindowAlignmentTests(unittest.TestCase):
    def test_window_on_each_monitor_routes_to_that_monitors_gpu(self):
        for hwnd, display, gpu_id, gpu_name in [
            (101, r"\\.\DISPLAY1", RTX_3090, "NVIDIA GeForce RTX 3090 Ti"),
            (102, r"\\.\DISPLAY2", RTX_5050, "NVIDIA GeForce RTX 5050"),
        ]:
            router = GpuRouter(lambda: adapters(), lambda h, d=display: d)
            route = router.route_for_window(hwnd)
            self.assertEqual(route["deviceName"], display)
            self.assertEqual(route["gpuName"], gpu_name)
            self.assertEqual(route["gpuDeviceId"].casefold(), gpu_id.casefold() if display.endswith("1") else f"{gpu_id}\\rev_a1".casefold())

    def test_gpu_swap_keeps_physical_identity_by_friendly_name(self):
        # Driver reorder: same cards, enumeration reversed so lsGpuIds flip.
        forward = GpuRouter(lambda: adapters(), lambda hwnd: None)
        before = {gpu["name"]: gpu["lsGpuId"] for gpu in forward.detect()["gpus"]}
        reversed_router = GpuRouter(lambda: list(reversed(adapters())), lambda hwnd: None)
        after = {gpu["name"]: gpu["lsGpuId"] for gpu in reversed_router.detect()["gpus"]}
        self.assertNotEqual(before["NVIDIA GeForce RTX 5050"], after["NVIDIA GeForce RTX 5050"])
        # Name-authoritative lookup lands on the same card despite new numbers.
        route = reversed_router.route_for_gpu(RTX_5050, "NVIDIA GeForce RTX 5050")
        self.assertEqual(route["gpuName"], "NVIDIA GeForce RTX 5050")
        self.assertEqual(route["lsGpuId"], after["NVIDIA GeForce RTX 5050"])

    def test_explicit_default_gpu_survives_device_id_suffix_change(self):
        router = GpuRouter(lambda: adapters(suffix_5050="REV_B2"), lambda hwnd: None)
        route = router.route_for_gpu(f"{RTX_5050}\\REV_A1", "NVIDIA GeForce RTX 5050")
        self.assertEqual(route["gpuName"], "NVIDIA GeForce RTX 5050")
        self.assertIn("REV_B2", route["gpuDeviceId"])

    def test_auto_route_follows_window_per_gpu(self):
        for hwnd, display, ls_id in [(101, r"\\.\DISPLAY1", 1), (202, r"\\.\DISPLAY2", 2)]:
            router = GpuRouter(lambda: adapters(), lambda h, d=display: d)
            automation, manager = controller_for(router)
            manager.config.auto_route_gpu_to_display = True
            route = automation._desired_gpu_route(None, hwnd)
            self.assertEqual(route["lsDisplayId"], ls_id)
            self.assertEqual(route["deviceName"], display)

    def test_explicit_default_gpu_ignores_window_monitor(self):
        router = GpuRouter(lambda: adapters(), lambda hwnd: r"\\.\DISPLAY2")
        automation, manager = controller_for(router)
        manager.config.auto_route_gpu_to_display = False
        manager.config.preferred_scaling_gpu_device_id = RTX_3090
        manager.config.preferred_scaling_gpu_name = "NVIDIA GeForce RTX 3090 Ti"
        route = automation._desired_gpu_route(None, 999)
        self.assertEqual(route["gpuName"], "NVIDIA GeForce RTX 3090 Ti")
        self.assertEqual(route["lsGpuId"], 1)

    def test_profile_explicit_gpu_overrides_default_on_every_gpu(self):
        router = GpuRouter(lambda: adapters(), lambda hwnd: r"\\.\DISPLAY1")
        automation, manager = controller_for(router)
        manager.config.auto_route_gpu_to_display = False
        manager.config.preferred_scaling_gpu_device_id = RTX_3090
        manager.config.preferred_scaling_gpu_name = "NVIDIA GeForce RTX 3090 Ti"
        profile = manager.config.profiles[0]
        for ls_gpu, expected in [(1, "NVIDIA GeForce RTX 3090 Ti"), (2, "NVIDIA GeForce RTX 5050")]:
            profile.native_scaling_settings["PreferredGpuId"] = str(ls_gpu)
            profile.native_scaling_settings["OutputDisplayId"] = "0"
            route = automation._desired_gpu_route(profile, 999)
            self.assertEqual(route["gpuName"], expected)
            self.assertEqual(route["lsGpuId"], ls_gpu)


if __name__ == "__main__":
    unittest.main()
