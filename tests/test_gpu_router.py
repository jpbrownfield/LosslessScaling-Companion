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


if __name__ == "__main__":
    unittest.main()
