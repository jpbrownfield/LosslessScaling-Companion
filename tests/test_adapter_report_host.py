"""End-to-end fixture: the real reporter DLL against a real D3D device.

Builds are required: runs build/native/LSC-AdapterReport/LSC_AdapterReportHost.exe
for adapter 0 and validates the emitted JSON. Skips when the DLL was not built
(local runs without cmake, or CI jobs without the native step).
"""
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HOST_CANDIDATES = [
    PROJECT_ROOT / "build" / "native" / "LSC-AdapterReport"
    / "LSC_AdapterReportHost.exe",
]


def find_host():
    for candidate in HOST_CANDIDATES:
        if candidate.is_file():
            return candidate
    return None


class AdapterReportHostTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows-only D3D fixture")
    def test_host_reports_real_device(self):
        host = find_host()
        if host is None:
            self.skipTest("reporter not built; run cmake build first")
        for index in (0, 1):
            completed = subprocess.run(
                [str(host), str(index)], capture_output=True, text=True,
                timeout=60)
            match = re.search(r"OK (.+\.json)", completed.stdout or "")
            if completed.returncode != 0 or not match:
                if "no adapter" in (completed.stdout or ""):
                    self.skipTest(f"adapter {index} absent on this machine")
                self.fail(f"host failed for adapter {index}: "
                          f"{completed.stdout} {completed.stderr}")
                return
            payload = json.loads(Path(match.group(1).strip()).read_text(
                encoding="utf-8"))
            self.assertEqual(payload.get("schema"), 1)
            self.assertTrue(payload.get("hasDevice"))
            device = payload["device"]
            self.assertTrue(str(device.get("description")))
            self.assertGreater(int(device.get("deviceId") or 0), 0)
            self.assertIsInstance(payload.get("settingsProfiles"), list)
            self.assertIsInstance(payload.get("rootGpuPreference"), int)
            entries = payload.get("enumAdapters") or []
            self.assertTrue(entries)
            position = int(payload.get("deviceIndexAdapters", -1))
            self.assertGreaterEqual(position, 0)
            self.assertEqual(entries[position]["deviceId"],
                             device["deviceId"])
            self.assertEqual(entries[position]["vendorId"],
                             device["vendorId"])
            for output in payload.get("deviceOutputs", []):
                left, top, right, bottom = output["rect"]
                self.assertLessEqual(left, right)
                self.assertLessEqual(top, bottom)


if __name__ == "__main__":
    unittest.main()
