"""Ghost DXGI adapters must not silently shift LS GPU numbering.

Lossless Scaling enumerates DXGI directly, so orphaned adapter instances
(same PCI id, fresh LUID) duplicate its GPU list and renumber its slots
while the physical topology -- and its fingerprint -- is unchanged. These
tests cover ghost detection, the re-verification rule, and the dashboard
warning.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from companion.core.profile_manager import ProfileManager
from companion.core.state import AppState
from companion.services.automation import AutomationController
from companion.services.gpu_router import (
    GpuRouter,
    dxgi_adapter_counts,
    ghost_signature,
    inventory_fingerprint,
)


RTX_3090 = r"PCI\VEN_10DE&DEV_2203&SUBSYS_50901462&REV_A1"
RTX_5050 = r"PCI\VEN_10DE&DEV_2D83&SUBSYS_53811462&REV_A1"


def report(*entries):
    return {"enumAdapters": [
        {"description": name, "vendorId": ven, "deviceId": dev,
         "subSysId": 0, "revision": 0, "isSoftware": bool(software)}
        for name, ven, dev, software in entries
    ]}


def inventory(counts=None):
    return {
        "gpus": [
            {"deviceId": RTX_3090, "name": "NVIDIA GeForce RTX 3090 Ti",
             "vendor": "NVIDIA", "lsGpuId": 1, "connectedDisplays": []},
            {"deviceId": RTX_5050, "name": "NVIDIA GeForce RTX 5050",
             "vendor": "NVIDIA", "lsGpuId": 2, "connectedDisplays": []},
        ],
        "displays": [],
        "dxgiAdapterCounts": dict(counts or {}),
    }


def make_automation(detect_inventory):
    manager = ProfileManager(Path(tempfile.mkdtemp()))
    state = AppState()
    watcher = Mock()
    router = Mock()
    router.detect.return_value = detect_inventory
    automation = AutomationController(
        manager, state, watcher, gpu_router=router,
        lossless_settings=Mock(), presentmon_provider=lambda: Path("x"),
    )
    automation.deployment_manager = Mock()
    return automation, manager, state


class DxgiCountsTests(unittest.TestCase):
    def test_counts_group_by_pci_and_skip_software(self):
        counts = dxgi_adapter_counts(report(
            ("NVIDIA GeForce RTX 3090 Ti", 0x10DE, 0x2203, False),
            ("NVIDIA GeForce RTX 3090 Ti", 0x10DE, 0x2203, False),
            ("NVIDIA GeForce RTX 5050", 0x10DE, 0x2D83, False),
            ("Microsoft Basic Render Driver", 0x1414, 0x8C, True),
            ("Microsoft Basic Render Driver", 0x1414, 0x8C, True),
        ))
        self.assertEqual(counts, {
            "VEN_10DE&DEV_2203": 2, "VEN_10DE&DEV_2D83": 1})

    def test_counts_empty_without_report(self):
        self.assertEqual(dxgi_adapter_counts(None), {})
        self.assertEqual(dxgi_adapter_counts({}), {})
        self.assertEqual(dxgi_adapter_counts({"enumAdapters": None}), {})

    def test_signature_only_covers_duplicates(self):
        self.assertEqual(ghost_signature({}), "")
        self.assertEqual(ghost_signature(
            {"VEN_10DE&DEV_2203": 1, "VEN_10DE&DEV_2D83": 1}), "")
        self.assertEqual(ghost_signature(
            {"VEN_10DE&DEV_2203": 5, "VEN_10DE&DEV_2D83": 1}),
            "VEN_10DE&DEV_2203:5")

    def test_fingerprint_ignores_multiplicity(self):
        plain = {"gpus": inventory()["gpus"], "displays": []}
        haunted = dict(plain, dxgiAdapterCounts={"VEN_10DE&DEV_2203": 5})
        self.assertEqual(inventory_fingerprint(plain),
                         inventory_fingerprint(haunted))


class RouterGhostTests(unittest.TestCase):
    def test_detect_attaches_counts_and_ghosts(self):
        adapters = [
            {"deviceName": r"\\.\DISPLAY1", "name": "NVIDIA GeForce RTX 3090 Ti",
             "deviceId": RTX_3090, "primary": True},
            {"deviceName": r"\\.\DISPLAY2", "name": "NVIDIA GeForce RTX 5050",
             "deviceId": RTX_5050, "primary": False},
        ]
        loader = lambda _directory: report(
            ("NVIDIA GeForce RTX 3090 Ti", 0x10DE, 0x2203, False),
            ("NVIDIA GeForce RTX 3090 Ti", 0x10DE, 0x2203, False),
            ("NVIDIA GeForce RTX 3090 Ti", 0x10DE, 0x2203, False),
            ("NVIDIA GeForce RTX 5050", 0x10DE, 0x2D83, False),
        )
        router = GpuRouter(lambda: adapters, lambda hwnd: None,
                           adapter_report_dir="reports",
                           adapter_report_loader=loader)
        detected = router.detect()
        self.assertEqual(detected["dxgiAdapterCounts"],
                         {"VEN_10DE&DEV_2203": 3, "VEN_10DE&DEV_2D83": 1})
        self.assertEqual(len(detected["dxgiGhosts"]), 1)
        self.assertEqual(detected["dxgiGhosts"][0]["dxgiAdapters"], 3)
        self.assertIn("3090", detected["dxgiGhosts"][0]["name"])

    def test_detect_without_report_dir_stays_ghost_blind(self):
        adapters = [{"deviceName": r"\\.\DISPLAY1",
                     "name": "NVIDIA GeForce RTX 3090 Ti",
                     "deviceId": RTX_3090, "primary": True}]
        router = GpuRouter(lambda: adapters, lambda hwnd: None)
        detected = router.detect()
        self.assertEqual(detected["dxgiAdapterCounts"], {})
        self.assertEqual(detected["dxgiGhosts"], [])


class GhostVerificationTests(unittest.TestCase):
    def test_map_status_reports_ghost_signature(self):
        inv = inventory({"VEN_10DE&DEV_2203": 5, "VEN_10DE&DEV_2D83": 1})
        automation, manager, _state = make_automation(inv)
        manager.config.ls_gpu_id_map = {"x": 1}
        recorded = inventory_fingerprint(inv)
        manager.config.ls_gpu_map_fingerprint = recorded
        status = automation.gpu_map_status()
        self.assertEqual(status["state"], "ok")
        self.assertEqual(status["ghostSignature"], "VEN_10DE&DEV_2203:5")
        self.assertEqual(len(status["ghosts"]), 1)

    def test_ghost_change_forces_reverification_once(self):
        inv = inventory({"VEN_10DE&DEV_2203": 5, "VEN_10DE&DEV_2D83": 1})
        automation, manager, state = make_automation(inv)
        manager.config.ls_gpu_id_map = {"x": 1}
        manager.config.ls_gpu_map_fingerprint = inventory_fingerprint(inv)
        state.current_gpu_route_key = "2:2:disp"
        automation._last_verified_route_key = "2:2:disp"
        automation._last_verified_ghost_signature = ""
        # New ghosts since the last proof: verify again.
        self.assertTrue(automation._route_verification_due())
        # A fresh proof quiets it until the population changes again.
        automation._note_route_verified("2:2:disp", "VEN_10DE&DEV_2203:5")
        self.assertFalse(automation._route_verification_due())

    def test_no_ghosts_no_extra_verification(self):
        automation, manager, state = make_automation(inventory())
        manager.config.ls_gpu_id_map = {"x": 1}
        manager.config.ls_gpu_map_fingerprint = inventory_fingerprint(
            inventory())
        state.current_gpu_route_key = "2:2:disp"
        automation._last_verified_route_key = "2:2:disp"
        automation._last_verified_ghost_signature = ""
        self.assertFalse(automation._route_verification_due())

    def test_ghost_warning_surfaced(self):
        inv = inventory({"VEN_10DE&DEV_2203": 5, "VEN_10DE&DEV_2D83": 1})
        automation, _manager, _state = make_automation(inv)
        automation.gpu_selections.refresh(inv)
        self.assertTrue(any("ghost" in warning.casefold()
                            for warning in automation.gpu_selections.warnings))


if __name__ == "__main__":
    unittest.main()
