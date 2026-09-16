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
            with patch("companion.services.self_update.subprocess.Popen") as launch:
                launched = service.launch_installer("2.0.0")

        self.assertEqual(downloaded["sha256"], digest)
        self.assertEqual(launched["version"], "2.0.0")
        launch.assert_called_once()

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
