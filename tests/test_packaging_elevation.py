import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PackagingElevationTests(unittest.TestCase):
    def test_executable_manifest_requires_administrator(self):
        manifest = (ROOT / "installer" / "LosslessCompanion.exe.manifest").read_text(
            encoding="utf-8"
        )
        spec = (ROOT / "LosslessCompanion.spec").read_text(encoding="utf-8")

        self.assertIn('requestedExecutionLevel level="requireAdministrator"', manifest)
        self.assertIn('manifest="installer/LosslessCompanion.exe.manifest"', spec)
        self.assertIn("uac_admin=True", spec)
        self.assertIn("icon=str(app_icon)", spec)

    def test_executable_manifest_uses_per_monitor_dpi_coordinates(self):
        manifest = (ROOT / "installer" / "LosslessCompanion.exe.manifest").read_text(
            encoding="utf-8"
        )

        self.assertIn(">PerMonitorV2,PerMonitor</dpiAwareness>", manifest)

    def test_installer_and_postinstall_launch_are_elevated(self):
        script = (ROOT / "installer" / "LosslessCompanion.iss").read_text(encoding="utf-8")

        self.assertIn("PrivilegesRequired=admin", script)
        self.assertIn("postinstall skipifsilent runascurrentuser", script)


if __name__ == "__main__":
    unittest.main()
