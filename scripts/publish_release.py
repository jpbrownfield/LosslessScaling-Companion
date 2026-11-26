"""Publish complete verified artifacts with explicit beta/full GitHub flags."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.release_common import version_tuple


ASSETS = ("LosslessCompanion-Setup-x64.exe", "LSAddonManager-Companion.zip")


def verify_artifacts(directory, channel):
    paths = []
    for name in ASSETS:
        path = directory / name
        checksum = directory / (name+".sha256")
        expected = checksum.read_text(encoding="utf-8-sig").strip().split()
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if len(expected) != 2 or expected[1] != name or digest != expected[0]:
            raise ValueError(f"Release checksum mismatch: {name}")
        paths.extend([path, checksum])
    if channel == "full":
        manifest_path = directory / "release-signing.json"
        certificate = directory / "LSCompanion-CodeSigning.cer"
        metadata = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        installer_hash = hashlib.sha256((directory / ASSETS[0]).read_bytes()).hexdigest()
        if (metadata.get("channel") != "full" or metadata.get("installerSha256") != installer_hash
                or metadata.get("certificateSha256") != hashlib.sha256(certificate.read_bytes()).hexdigest()):
            raise ValueError("Full-release signing evidence does not match its installer/certificate")
        paths.extend([manifest_path, certificate])
    return paths


def publish(directory, *, tag, channel, source, from_commit=False, run=subprocess.run):
    version = version_tuple(tag.removeprefix("v"))
    if channel not in {"beta", "full"} or (version[2] == 0) != (channel == "full"):
        raise ValueError("Release channel does not match the version")
    paths = verify_artifacts(Path(directory), channel)
    def gh(*args, **kwargs):
        return run(["gh", *args], capture_output=True, text=True, **kwargs)
    existing = gh("release", "view", tag, "--json", "tagName")
    if existing.returncode == 0 and from_commit:
        raise ValueError("Release already exists; use a new version instead of replacing a branch release")
    latest = False
    if channel == "full":
        previous = gh("release", "view", "--json", "tagName")
        latest = True
        if previous.returncode == 0:
            previous_tag = json.loads(previous.stdout)["tagName"].removeprefix("v")
            latest = version >= version_tuple(previous_tag)
    flags = ["--prerelease" if channel == "beta" else "--prerelease=false", "--latest" if latest else "--latest=false"]
    if existing.returncode != 0:
        gh("release", "create", tag, "--target", source, "--draft", "--generate-notes",
           "--title", tag + (" Beta" if channel == "beta" else ""), "--prerelease" if channel == "beta" else "--prerelease=false", check=True)
    gh("release", "upload", tag, *(str(p) for p in paths), "--clobber", check=True)
    gh("release", "edit", tag, "--draft=false", *flags, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--channel", choices=("beta", "full"), required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--from-commit", action="store_true")
    publish(**vars(parser.parse_args()))


if __name__ == "__main__":
    main()
