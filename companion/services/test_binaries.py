"""Sync CI-built native binaries into gitignored test_binaries/ for source runs."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

logger = logging.getLogger("LSCompanion.TestBinaries")

WORKSPACE = Path(__file__).resolve().parents[2]
TEST_BINARIES_ROOT = WORKSPACE / "test_binaries"
SOURCE_FILE = TEST_BINARIES_ROOT / ".source.json"

# Relative path inside test_binaries -> relative path inside the CI artifact.
# dist/LosslessCompanion mirrors the installed layout (tools/, addons/).
WANTED = {
    "tools/LSBenchmark.exe": "tools/LSBenchmark.exe",
    "tools/LSC_StartupGuard.dll": "tools/LSC_StartupGuard.dll",
    "addons/LSP-ReShade/LSC_ReShadeBridge.dll": "addons/LSP-ReShade/LSC_ReShadeBridge.dll",
    "addons/LSP-ReShade/addon.json": "addons/LSP-ReShade/addon.json",
}
NEURAL_ARTIFACT_PREFIX = "LSP-NeuralRender-windows-x64-"
APP_ARTIFACT_PREFIX = "LosslessCompanion-windows-x64-"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_matches() -> bool:
    """True when every wanted file exists (checksums recorded on last sync)."""
    source = {}
    if SOURCE_FILE.is_file():
        try:
            source = json.loads(SOURCE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
    checksums = source.get("sha256", {})
    for relative in WANTED:
        local = TEST_BINARIES_ROOT / relative.replace("/", os.sep)
        if not local.is_file():
            return False
        expected = checksums.get(relative)
        if expected and _sha256(local) != expected:
            return False
    return bool(checksums)


def _run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError(f"{' '.join(cmd)} failed: {(result.stderr or result.stdout).strip()}")
    return result.stdout.strip()


def _latest_successful_run() -> dict | None:
    try:
        out = _run(["gh", "run", "list", "--workflow", "build-windows.yml",
                    "--status", "success", "--limit", "1", "--json", "databaseId,headSha"])
        runs = json.loads(out or "[]")
        return runs[0] if runs else None
    except Exception as error:
        logger.warning("Could not query CI runs (%s); using cached test binaries.", error)
        return None


def sync(skip_if_current: bool = True) -> Path | None:
    """Download latest CI binaries into test_binaries/, skipping when current."""
    if os.environ.get("LS_COMPANION_NO_SYNC") == "1" or "--no-sync" in sys.argv:
        if "--no-sync" in sys.argv:
            sys.argv.remove("--no-sync")
        return TEST_BINARIES_ROOT if manifest_matches() else None
    TEST_BINARIES_ROOT.mkdir(parents=True, exist_ok=True)
    if skip_if_current and manifest_matches():
        logger.info("Test binaries up to date; skipping download.")
        return TEST_BINARIES_ROOT
    run = _latest_successful_run()
    if run is None:
        logger.warning("No CI run metadata; continuing with cached test_binaries.")
        return TEST_BINARIES_ROOT if manifest_matches() else None
    run_id = str(run.get("databaseId", ""))
    head_sha = str(run.get("headSha", ""))
    cached = {}
    if SOURCE_FILE.is_file():
        try:
            cached = json.loads(SOURCE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cached = {}
    if cached.get("run_id") == run_id and manifest_matches():
        logger.info("Test binaries already at CI run %s; skipping download.", run_id)
        return TEST_BINARIES_ROOT
    tmp = Path(tempfile.mkdtemp(prefix="test_binaries_"))
    try:
        _run(["gh", "run", "download", run_id, "--dir", str(tmp)])
    except Exception as error:
        logger.warning("Test binary download failed (%s); using cache.", error)
        shutil.rmtree(tmp, ignore_errors=True)
        return TEST_BINARIES_ROOT if manifest_matches() else None
    checksums: dict[str, str] = {}
    missing: list[str] = []
    for relative, artifact_relative in WANTED.items():
        # gh lays out <artifact-name>/<files>; search both artifact prefixes.
        found = None
        for candidate in tmp.rglob(artifact_relative.split("/")[-1]):
            # Keep the first match under the expected artifact folder.
            if APP_ARTIFACT_PREFIX in str(candidate) or "LosslessCompanion" in str(candidate):
                found = candidate
                break
        if found is None:
            # Fall back to any matching filename (covers layout changes).
            matches = [p for p in tmp.rglob(artifact_relative.split("/")[-1]) if p.is_file()]
            found = matches[0] if matches else None
        if found is None or not found.is_file():
            missing.append(relative)
            continue
        dest = TEST_BINARIES_ROOT / relative.replace("/", os.sep)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(found, dest)
        checksums[relative] = _sha256(dest)
    shutil.rmtree(tmp, ignore_errors=True)
    if missing:
        logger.warning("CI artifact missing %s; cached files kept.", ", ".join(missing))
        if not checksums and not manifest_matches():
            return None
    SOURCE_FILE.write_text(json.dumps({"run_id": run_id, "head_sha": head_sha,
                                        "sha256": checksums}, indent=2), encoding="utf-8")
    logger.info("Synced test binaries from CI run %s.", run_id)
    return TEST_BINARIES_ROOT


def candidate(relative: str) -> Path:
    return TEST_BINARIES_ROOT / relative.replace("/", os.sep)
