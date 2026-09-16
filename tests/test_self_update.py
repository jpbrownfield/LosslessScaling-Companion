import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from companion.services.asset_store import UnsafeAssetError
from companion.services.self_update import (
    CHECKSUM_NAME,
    INSTALLER_NAME,
    CompanionUpdateService,
)
from companion.version import stable_version_tuple


class FakeResponse:
    def __init__(self, url, content):
        self.url = url
        self.stream = io.BytesIO(content)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def geturl(self):
        return self.url

    def read(self, size=-1):
        return self.stream.read(size)


class CompanionUpdateServiceTests(unittest.TestCase):
    def test_dashboard_exposes_manual_companion_update_check(self):
        dashboard = (
            Path(__file__).resolve().parents[1] / "companion" / "ui" / "dashboard.html"
        ).read_text(encoding="utf-8")

        self.assertIn('id="checkCompanionUpdateBtn"', dashboard)
        self.assertIn("type: 'CHECK_COMPANION_UPDATE'", dashboard)
        self.assertIn('id="companionUpdateStatusText"', dashboard)

    def release(self, version="v2.0.0"):
        base = f"https://github.com/example/releases/download/{version}"
        return {
            "version": version,
            "html_url": f"https://github.com/example/releases/tag/{version}",
            "notes": "Release notes",
            "assets": [
                {"name": INSTALLER_NAME, "url": f"{base}/{INSTALLER_NAME}"},
                {"name": CHECKSUM_NAME, "url": f"{base}/{CHECKSUM_NAME}"},
            ],
        }

    def test_semantic_version_comparison_is_numeric(self):
        self.assertGreater(stable_version_tuple("1.10.0"), stable_version_tuple("1.9.9"))
        self.assertIsNone(stable_version_tuple("0.0.0-dev"))

    def test_status_requires_installer_and_checksum(self):
        manager = SimpleNamespace(check=lambda *args, **kwargs: [self.release()])
        with tempfile.TemporaryDirectory() as directory, patch(
            "companion.services.self_update.current_version", return_value="1.5.0"
        ):
            status = CompanionUpdateService(manager, Path(directory), enabled=True).check()

        self.assertTrue(status["updateAvailable"])
        self.assertEqual(status["latestVersion"], "2.0.0")

    def test_download_verifies_checksum_and_launch_rechecks_file(self):
        installer = b"verified installer payload"
        digest = hashlib.sha256(installer).hexdigest()
        release = self.release()
        manager = SimpleNamespace(check=lambda *args, **kwargs: [release])

        def open_url(request, timeout=60):
            url = request.full_url
            content = (
                f"{digest}  {INSTALLER_NAME}".encode("ascii")
                if url.endswith(".sha256") else installer
            )
            return FakeResponse(url, content)

        with tempfile.TemporaryDirectory() as directory, patch(
            "companion.services.self_update.urllib.request.urlopen", side_effect=open_url
        ):
            service = CompanionUpdateService(manager, Path(directory), enabled=True)
            downloaded = service.download("2.0.0")
            with patch(
                "companion.services.self_update.ctypes.windll.shell32.ShellExecuteW",
                return_value=42,
            ) as launch:
                launched = service.launch_installer("2.0.0")

        self.assertEqual(downloaded["sha256"], digest)
        self.assertEqual(launched["version"], "2.0.0")
        launch.assert_called_once()
        arguments = launch.call_args.args
        self.assertEqual(arguments[1], "runas")
        self.assertEqual(Path(arguments[2]).name, INSTALLER_NAME)
        self.assertIn("/CLOSEAPPLICATIONS", arguments[3])
        self.assertIn("/FORCECLOSEAPPLICATIONS", arguments[3])

    def test_private_download_uses_configured_github_token(self):
        installer = b"verified installer payload"
        digest = hashlib.sha256(installer).hexdigest()
        release = self.release()
        manager = SimpleNamespace(check=lambda *args, **kwargs: [release])
        authorization = []

        def open_url(request, timeout=60):
            authorization.append(request.headers.get("Authorization"))
            content = (
                f"{digest}  {INSTALLER_NAME}".encode("ascii")
                if request.full_url.endswith(".sha256") else installer
            )
            return FakeResponse(request.full_url, content)

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ", {"LOSSLESS_COMPANION_GITHUB_TOKEN": "private-test-token"}, clear=True
        ), patch(
            "companion.services.self_update.urllib.request.urlopen", side_effect=open_url
        ):
            CompanionUpdateService(manager, Path(directory), enabled=True).download("2.0.0")

        self.assertEqual(authorization, ["Bearer private-test-token"] * 2)

    def test_installer_launch_reports_a_rejected_uac_handoff(self):
        installer = b"verified installer payload"
        digest = hashlib.sha256(installer).hexdigest()
        manager = SimpleNamespace(check=lambda *args, **kwargs: [self.release()])

        def open_url(request, timeout=60):
            content = (
                f"{digest}  {INSTALLER_NAME}".encode("ascii")
                if request.full_url.endswith(".sha256") else installer
            )
            return FakeResponse(request.full_url, content)

        with tempfile.TemporaryDirectory() as directory, patch(
            "companion.services.self_update.urllib.request.urlopen", side_effect=open_url
        ):
            service = CompanionUpdateService(manager, Path(directory), enabled=True)
            service.download("2.0.0")
            with patch(
                "companion.services.self_update.ctypes.windll.shell32.ShellExecuteW",
                return_value=5,
            ):
                with self.assertRaisesRegex(OSError, "could not elevate"):
                    service.launch_installer("2.0.0")

    def test_download_rejects_checksum_mismatch(self):
        release = self.release()
        manager = SimpleNamespace(check=lambda *args, **kwargs: [release])

        def open_url(request, timeout=60):
            url = request.full_url
            content = (
                ("0" * 64 + f"  {INSTALLER_NAME}").encode("ascii")
                if url.endswith(".sha256") else b"tampered"
            )
            return FakeResponse(url, content)

        with tempfile.TemporaryDirectory() as directory, patch(
            "companion.services.self_update.urllib.request.urlopen", side_effect=open_url
        ):
            service = CompanionUpdateService(manager, Path(directory), enabled=True)
            with self.assertRaisesRegex(UnsafeAssetError, "does not match"):
                service.download("2.0.0")


if __name__ == "__main__":
    unittest.main()
