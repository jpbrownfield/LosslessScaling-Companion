import unittest

from companion.services.gpu_router import GpuRouter


class GpuRouterTests(unittest.TestCase):
    def setUp(self):
        self.displays = [
            {
                "deviceName": r"\\.\DISPLAY1",
                "name": "NVIDIA GeForce RTX 5080",
                "deviceId": r"PCI\VEN_10DE&DEV_2C02",
                "primary": True,
            },
            {
                "deviceName": r"\\.\DISPLAY2",
                "name": "AMD Radeon Graphics",
                "deviceId": r"PCI\VEN_1002&DEV_164E",
                "primary": False,
            },
        ]

    def test_detects_vendors_and_assigns_lossless_scaling_ids(self):
        router = GpuRouter(lambda: self.displays, lambda hwnd: None)
        inventory = router.detect()

        self.assertTrue(inventory["hasNvidia"])
        self.assertEqual([gpu["vendor"] for gpu in inventory["gpus"]], ["NVIDIA", "AMD"])
        self.assertEqual([gpu["lsGpuId"] for gpu in inventory["gpus"]], [1, 2])
        self.assertEqual([display["lsDisplayId"] for display in inventory["displays"]], [1, 2])

    def test_routes_a_window_to_its_connected_adapter(self):
        router = GpuRouter(lambda: self.displays, lambda hwnd: r"\\.\DISPLAY2")

        route = router.route_for_window(1234)

        self.assertEqual(route["gpuDeviceId"], r"PCI\VEN_1002&DEV_164E")
        self.assertEqual(route["lsGpuId"], 2)
        self.assertEqual(route["lsDisplayId"], 2)

    def test_resolves_numeric_ls_ids_to_friendly_inventory_entries(self):
        router = GpuRouter(lambda: self.displays, lambda hwnd: None)

        route = router.route_for_ls_ids(0, 2)

        self.assertEqual(route["gpuDeviceId"], r"PCI\VEN_1002&DEV_164E")
        self.assertEqual(route["lsGpuId"], 2)
        self.assertEqual(route["lsDisplayId"], 2)
        self.assertEqual(route["deviceName"], r"\\.\DISPLAY2")

    def test_window_geometry_selects_monitor_with_largest_overlap(self):
        monitors = [
            {
                "deviceName": r"\\.\DISPLAY1",
                "bounds": (-2560, 0, 0, 1440),
                "primary": False,
            },
            {
                "deviceName": r"\\.\DISPLAY2",
                "bounds": (0, 0, 1920, 1080),
                "primary": True,
            },
        ]

        selected = GpuRouter._monitor_for_window_bounds(
            (-400, 100, 1400, 900), monitors
        )

        self.assertEqual(selected, r"\\.\DISPLAY2")

    def test_window_geometry_supports_negative_coordinate_monitor_layouts(self):
        monitors = [
            {
                "deviceName": r"\\.\DISPLAY1",
                "bounds": (-2560, -300, 0, 1140),
                "primary": False,
            },
            {
                "deviceName": r"\\.\DISPLAY2",
                "bounds": (0, 0, 1920, 1080),
                "primary": True,
            },
        ]

        selected = GpuRouter._monitor_for_window_bounds(
            (-2200, -100, 300, 900), monitors
        )

        self.assertEqual(selected, r"\\.\DISPLAY1")

    def test_window_geometry_returns_none_when_window_is_off_desktop(self):
        selected = GpuRouter._monitor_for_window_bounds(
            (5000, 5000, 5500, 5500),
            [{"deviceName": r"\\.\DISPLAY1", "bounds": (0, 0, 1920, 1080)}],
        )

        self.assertIsNone(selected)


if __name__ == "__main__":
    unittest.main()
