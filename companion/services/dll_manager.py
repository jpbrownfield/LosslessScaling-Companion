"""
DLL manager and proxy injector.
Handles copying or symlinking proxy DLLs (dxgi.dll, d3d11.dll, ReShade, OptiScaler)
into target application folders per profile with backup and restoration.
"""

import os
import shutil
import logging
from pathlib import Path
from typing import Optional, List, Dict
from ..core.models import DllOverrideConfig

logger = logging.getLogger("LosslessCompanion.DllManager")


class DllManager:
    @staticmethod
    def deploy_override(override: DllOverrideConfig, target_app_dir: str) -> bool:
        """
        Deploys a DLL override into the target application's directory.
        Creates a backup of any existing DLL before overwriting.
        """
        if not override.enabled:
            return False

        source_path = Path(override.source_dll_path)
        if not source_path.exists():
            logger.error(f"Source DLL does not exist: {source_path}")
            return False

        app_dir = Path(target_app_dir)
        if not app_dir.is_dir():
            logger.error(f"Target application directory is invalid: {app_dir}")
            return False

        dest_path = app_dir / override.target_dll_name
        backup_path = app_dir / f"{override.target_dll_name}.orig.bak"

        try:
            # Create backup of existing file if not already backed up
            if dest_path.exists() and not backup_path.exists() and not dest_path.is_symlink():
                logger.info(f"Creating backup of original DLL: {dest_path} -> {backup_path}")
                shutil.copy2(dest_path, backup_path)

            # Remove current destination file/link
            if dest_path.exists() or dest_path.is_symlink():
                dest_path.unlink()

            # Deploy
            if override.deployment_mode == "symlink":
                try:
                    os.symlink(source_path, dest_path)
                    logger.info(f"Created symlink: {dest_path} -> {source_path}")
                except OSError:
                    logger.warning("Symlink creation failed (requires Developer Mode/Admin). Falling back to copy.")
                    shutil.copy2(source_path, dest_path)
            else:
                shutil.copy2(source_path, dest_path)
                logger.info(f"Copied DLL: {source_path} -> {dest_path}")

            return True

        except Exception as e:
            logger.error(f"Failed to deploy DLL override: {e}")
            return False

    @staticmethod
    def restore_original_dll(target_app_dir: str, target_dll_name: str) -> bool:
        """
        Restores the original DLL from the .orig.bak backup if one exists.
        """
        app_dir = Path(target_app_dir)
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
