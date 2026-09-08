"""
DLL manager and proxy injector.
Handles copying managed DLLs into the Lossless Scaling directory
with backup and restoration. The automation layer never supplies a game directory.
"""

import shutil
import logging
from pathlib import Path
from typing import Optional, List, Dict
from ..core.models import DllOverrideConfig

logger = logging.getLogger("LSCompanion.DllManager")


class DllManager:
    @staticmethod
    def _validated_lossless_dir(value: str) -> Optional[Path]:
        directory = Path(value).resolve()
        if not directory.is_dir() or not (directory / "LosslessScaling.exe").is_file():
            logger.error("DLL destination must be the directory containing LosslessScaling.exe: %s", directory)
            return None
        return directory

    @staticmethod
    def destination_exists(override: DllOverrideConfig, target_app_dir: str) -> bool:
        directory = DllManager._validated_lossless_dir(target_app_dir)
        if directory is None:
            return False
        dest = directory / override.target_dll_name
        backup = directory / f"{override.target_dll_name}.orig.bak"
        return backup.exists() or dest.exists() or dest.is_symlink()

    @staticmethod
    def deploy_override(override: DllOverrideConfig, target_app_dir: str) -> bool:
        """
        Deploys a DLL override into the caller-validated Lossless Scaling directory.
        Creates a backup of any existing DLL before overwriting.
        """
        if not override.enabled:
            return False

        source_path = Path(override.source_dll_path)
        if not source_path.exists():
            logger.error(f"Source DLL does not exist: {source_path}")
            return False

        app_dir = DllManager._validated_lossless_dir(target_app_dir)
        if app_dir is None:
            return False

        dest_path = app_dir / override.target_dll_name
        backup_path = app_dir / f"{override.target_dll_name}.orig.bak"

        if source_path.resolve() == dest_path.resolve():
            logger.error("Source and destination DLL paths must differ")
            return False

        try:
            if dest_path.is_symlink() and not backup_path.exists():
                logger.error(f"Refusing to replace unbacked symlink: {dest_path}")
                return False
            # Create backup of existing file if not already backed up
            if dest_path.exists() and not backup_path.exists() and not dest_path.is_symlink():
                logger.info(f"Creating backup of original DLL: {dest_path} -> {backup_path}")
                shutil.copy2(dest_path, backup_path)

            # Remove current destination file/link
            if dest_path.exists() or dest_path.is_symlink():
                dest_path.unlink()

            # Production deployments are real copies. A legacy `symlink` setting
            # remains parseable but cannot create a mutable elevated code link.
            shutil.copy2(source_path, dest_path)
            logger.info(f"Copied DLL: {source_path} -> {dest_path}")

            return True

        except Exception as e:
            logger.error(f"Failed to deploy DLL override: {e}")
            if backup_path.exists() and not dest_path.exists():
                try:
                    shutil.copy2(backup_path, dest_path)
                except Exception as restore_error:
                    logger.error(f"Failed to recover original DLL: {restore_error}")
            return False

    @staticmethod
    def restore_original_dll(target_app_dir: str, target_dll_name: str) -> bool:
        """
        Restores the original DLL from the .orig.bak backup if one exists.
        """
        app_dir = DllManager._validated_lossless_dir(target_app_dir)
        if app_dir is None:
            return False
        dest_path = app_dir / target_dll_name
        backup_path = app_dir / f"{target_dll_name}.orig.bak"

        if not backup_path.exists():
            return False

        try:
            if dest_path.exists() or dest_path.is_symlink():
                dest_path.unlink()
            shutil.move(backup_path, dest_path)
            logger.info(f"Restored original DLL: {backup_path} -> {dest_path}")
            return True
        except Exception as e:
            logger.error(f"Failed to restore original DLL: {e}")
            return False

    @staticmethod
    def cleanup_override(target_app_dir: str, target_dll_name: str, had_original: bool) -> bool:
        """Restore a backup, or remove a deployed file that had no predecessor."""
        if had_original:
            return DllManager.restore_original_dll(target_app_dir, target_dll_name)
        app_dir = DllManager._validated_lossless_dir(target_app_dir)
        if app_dir is None:
            return False
        dest_path = app_dir / target_dll_name
        try:
            if dest_path.exists() or dest_path.is_symlink():
                dest_path.unlink()
            return True
        except Exception as e:
            logger.error(f"Failed to clean up DLL override: {e}")
            return False
