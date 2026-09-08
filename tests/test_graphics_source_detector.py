import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from companion.services.graphics_source_detector import GraphicsSourceDetector


class GraphicsSourceDetectorTests(unittest.TestCase):
    def test_finds_newest_full_addon_reshade_download(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "ReShade_Setup_6.7.0_Addon.exe"
            new = root / "ReShade_Setup_6.8.0_Addon.exe"
            old.write_bytes(b"old")
            new.write_bytes(b"new")
            os.utime(old, (1, 1))
            os.utime(new, (2, 2))

            with patch.object(GraphicsSourceDetector, "_download_directories", return_value=[root]):
                status = GraphicsSourceDetector.reshade()

            self.assertTrue(status["detected"])
            self.assertEqual(Path(status["path"]), new.resolve())
            self.assertEqual(status["version"], "6.8.0")

    def test_ignores_standard_reshade_download(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ReShade_Setup_6.8.0.exe").write_bytes(b"standard")
            with patch.object(GraphicsSourceDetector, "_download_directories", return_value=[root]):
                status = GraphicsSourceDetector.reshade()
            self.assertFalse(status["detected"])

    def test_finds_special_k_payload_from_registry_install_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / "SpecialK64.dll"
            payload.write_bytes(b"special-k")
            (root / "SKIF.exe").write_bytes(b"frontend")
            with patch.object(GraphicsSourceDetector, "_registry_value", return_value=str(root)):
                status = GraphicsSourceDetector.special_k()

            self.assertTrue(status["detected"])
            self.assertEqual(Path(status["path"]), payload.resolve())
            self.assertEqual(Path(status["frontendPath"]), (root / "SKIF.exe").resolve())


if __name__ == "__main__":
    unittest.main()
