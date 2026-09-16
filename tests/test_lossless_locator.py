import tempfile
import unittest
from pathlib import Path

from companion.services.lossless_locator import find_lossless_scaling_executable


class LosslessScalingLocatorTests(unittest.TestCase):
    @staticmethod
    def _install(library: Path) -> Path:
        executable = (
            library / "steamapps" / "common" / "Lossless Scaling" / "LosslessScaling.exe"
        )
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_bytes(b"MZ")
        return executable

    def test_keeps_a_valid_configured_executable(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = self._install(Path(directory) / "ConfiguredLibrary")

            found = find_lossless_scaling_executable(
                str(executable), steam_roots=[Path(directory) / "UnusedSteam"]
            )

            self.assertEqual(found, executable.resolve())

    def test_discovers_executable_from_modern_libraryfolders_vdf(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            steam = root / "Steam"
            library = root / "Games" / "SteamLibrary"
            (steam / "steamapps").mkdir(parents=True)
            executable = self._install(library)
            escaped = str(library).replace("\\", "\\\\")
            (steam / "steamapps" / "libraryfolders.vdf").write_text(
                f'"libraryfolders"\n{{\n  "1"\n  {{\n    "path" "{escaped}"\n  }}\n}}\n',
                encoding="utf-8",
            )

            found = find_lossless_scaling_executable(
                r"C:\missing\LosslessScaling.exe", steam_roots=[steam]
            )

            self.assertEqual(found, executable.resolve())

    def test_rejects_simulation_fixture_as_configured_install(self):
        with tempfile.TemporaryDirectory() as directory:
            simulation = Path(directory) / "simulation" / "LosslessScaling.exe"
            simulation.parent.mkdir()
            simulation.write_bytes(b"MZ")

            found = find_lossless_scaling_executable(
                str(simulation), steam_roots=[Path(directory) / "UnusedSteam"]
            )

            self.assertIsNone(found)


if __name__ == "__main__":
    unittest.main()
