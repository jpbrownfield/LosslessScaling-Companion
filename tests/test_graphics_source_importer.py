import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from companion.core.models import Profile
from companion.services.asset_store import AssetStore
from companion.services.graphics_resolver import GraphicsResolver
from companion.services.graphics_source_importer import GraphicsSourceImporter


def x64_pe(payload: bytes = b"") -> bytes:
    data = bytearray(0x88)
    data[0:2] = b"MZ"
    data[0x3C:0x40] = (0x80).to_bytes(4, "little")
    data[0x80:0x84] = b"PE\0\0"
    data[0x84:0x86] = (0x8664).to_bytes(2, "little")
    return bytes(data) + payload


class GraphicsSourceImporterTests(unittest.TestCase):
    def test_extracts_reshade_runtime_without_executing_installer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
                bundle.writestr("ReShade32.dll", b"x86")
                bundle.writestr("ReShade64.dll", x64_pe(b"reshade"))
            installer = root / "ReShade_Setup_6.8.0_Addon.exe"
            installer.write_bytes(b"MZ-fake-setup-prefix" + archive.getvalue())
            store = AssetStore(str(root / "store"))
            importer = GraphicsSourceImporter(store)

            with patch(
                "companion.services.graphics_source_importer.GraphicsSourceDetector.status",
                return_value={"reshade": {
                    "detected": True,
                    "path": str(installer),
                    "version": "6.8.0",
                }},
            ):
                package = importer.stage("reshade", "operation-1")

            self.assertEqual(package["provider"], "reshade")
            self.assertTrue(Path(package["payload_path"], "ReShade64.dll").is_file())
            self.assertTrue(Path(package["payload_path"], "lossless-scaling-deployment.json").is_file())
            profile = Profile.model_validate({
                "id": "reshade-test",
                "name": "ReShade",
                "graphics": {"reshade": {"enabled": True, "version": "6.8.0"}},
            })
            plan = GraphicsResolver(store).resolve(profile)
            self.assertEqual([item["relative_path"] for item in plan], ["dxgi.dll"])

    def test_imports_detected_special_k_with_lossless_recipe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "SpecialK64.dll"
            runtime.write_bytes(x64_pe(b"special-k"))
            store = AssetStore(str(root / "store"))
            importer = GraphicsSourceImporter(store)

            with patch(
                "companion.services.graphics_source_importer.GraphicsSourceDetector.status",
                return_value={"special-k": {
                    "detected": True,
                    "path": str(runtime),
                    "version": "24.1",
                }},
            ):
                package = importer.stage("special-k", "unused-operation")

            recipe = Path(package["payload_path"], "lossless-scaling-deployment.json")
            self.assertTrue(recipe.is_file())
            self.assertIn('"destination": "dxgi.dll"', recipe.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
