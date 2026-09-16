"""Stage user-obtained graphics runtimes without executing their installers."""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path
from typing import Dict

from .asset_store import AssetStore, UnsafeAssetError
from .graphics_source_detector import GraphicsSourceDetector


class GraphicsSourceImporter:
    MAX_RUNTIME_BYTES = 256 * 1024 * 1024

    def __init__(self, store: AssetStore):
        self.store = store

    def _add_recipe(self, package: Dict, recipe: Dict) -> Dict:
        payload = Path(package["payload_path"])
        path = payload / "lossless-scaling-deployment.json"
        encoded = json.dumps(recipe, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        path.write_bytes(encoded)
        package["files"] = [
            *package.get("files", []),
            {
                "relative_path": path.name,
                "sha256": AssetStore.sha256(path),
                "size": len(encoded),
                "architecture": None,
            },
        ]
        AssetStore._atomic_json(payload.parent / "package.json", package)
        return package

    def _stage_reshade(self, source: Dict, operation_id: str) -> Dict:
        installer = Path(str(source.get("path") or "")).resolve(strict=True)
        if not GraphicsSourceDetector.RESHADE_NAME.fullmatch(installer.name):
            raise UnsafeAssetError("The detected ReShade installer name is invalid")
        staging = self.store.new_staging_directory(operation_id)
        runtime = staging / "ReShade64.dll"
        try:
            # Official ReShade Setup appends a ZIP containing ReShade32.dll and
            # ReShade64.dll to its executable. Python's ZIP reader supports the
            # self-extracting prefix, so the installer never needs to run.
            with zipfile.ZipFile(installer) as archive:
                matches = [
                    item for item in archive.infolist()
                    if not item.is_dir() and Path(item.filename).name.casefold() == "reshade64.dll"
                ]
                if len(matches) != 1:
                    raise UnsafeAssetError("ReShade installer does not contain exactly one ReShade64.dll")
                member = matches[0]
                if member.file_size <= 0 or member.file_size > self.MAX_RUNTIME_BYTES:
                    raise UnsafeAssetError("ReShade64.dll has an unsafe extracted size")
                with archive.open(member) as input_stream, runtime.open("wb") as output_stream:
                    remaining = member.file_size
                    while remaining:
                        chunk = input_stream.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise UnsafeAssetError("ReShade64.dll ended before its declared size")
                        output_stream.write(chunk)
                        remaining -= len(chunk)
                    if input_stream.read(1):
                        raise UnsafeAssetError("ReShade64.dll exceeded its declared size")
            if AssetStore.pe_architecture(runtime) != "x64":
                raise UnsafeAssetError("The extracted ReShade runtime is not a valid x64 DLL")
            package = self.store.import_release_file(
                str(runtime),
                provider="reshade",
                version=str(source.get("version") or "detected"),
                source_metadata={
                    "kind": "official-site-user-download",
                    "installer": installer.name,
                    "installer_sha256": AssetStore.sha256(installer),
                    "extracted_without_execution": True,
                },
            )
            return self._add_recipe(package, {
                "schema_version": 1,
                "files": [{
                    "source": "ReShade64.dll",
                    "destination": "ReShade64.dll",
                    "role": "injector",
                }],
            })
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _stage_special_k(self, source: Dict) -> Dict:
        runtime = Path(str(source.get("path") or "")).resolve(strict=True)
        if runtime.name.casefold() != "specialk64.dll" or AssetStore.pe_architecture(runtime) != "x64":
            raise UnsafeAssetError("The detected Special K runtime is not a valid x64 SpecialK64.dll")
        package = self.store.import_release_file(
            str(runtime),
            provider="special-k",
            version=str(source.get("version") or "detected"),
            source_metadata={"kind": "detected-local-installation"},
        )
        return self._add_recipe(package, {
            "schema_version": 1,
            "files": [{
                "source": "SpecialK64.dll",
                "destination": "dxgi.dll",
                "role": "injector",
            }],
        })

    def stage(self, provider: str, operation_id: str) -> Dict:
        sources = GraphicsSourceDetector.status()
        source = sources.get(provider)
        if not source or not source.get("detected"):
            raise ValueError(f"No detected {provider} source is available to import")
        if provider == "reshade":
            return self._stage_reshade(source, operation_id)
        if provider == "special-k":
            return self._stage_special_k(source)
        raise ValueError("Unsupported external graphics source")
