"""Narrow NVAPI DRS integration for LS Companion's NVIDIA app settings."""

from __future__ import annotations

import ctypes
import json
import logging
import os
import tempfile
import uuid
from pathlib import Path
from typing import Callable, Dict, Optional

logger = logging.getLogger("LSCompanion.NvidiaProfile")

NVAPI_OK = 0
NVAPI_END_ENUMERATION = -7
NVAPI_SETTING_NOT_FOUND = -160
NVAPI_PROFILE_NOT_FOUND = -163
NVAPI_EXECUTABLE_NOT_FOUND = -166
SMOOTH_MOTION_ENABLE_ID = 0xB0D384C0
RTX_HDR_DRIVER_FLAGS_ID = 0x00432F84
RTX_HDR_REQUIRED_ID = 0x1077A11A
RTX_HDR_GAME_FILTERS_ID = 0x00980896
RTX_HDR_ENABLE_ID = 0x00DD48FB
NVDRS_DWORD_TYPE = 0
UNICODE_LENGTH = 2048
BINARY_LENGTH = 4096
CURRENT_PROFILE_LOCATION = 0
MANAGED_SETTING_IDS = (
    SMOOTH_MOTION_ENABLE_ID,
    RTX_HDR_DRIVER_FLAGS_ID,
    RTX_HDR_REQUIRED_ID,
    RTX_HDR_GAME_FILTERS_ID,
    RTX_HDR_ENABLE_ID,
)


class NvidiaProfileError(RuntimeError):
    pass


class _BinaryValue(ctypes.Structure):
    _fields_ = [("valueLength", ctypes.c_uint32), ("valueData", ctypes.c_uint8 * BINARY_LENGTH)]


class _SettingValue(ctypes.Union):
    _fields_ = [
        ("u32", ctypes.c_uint32),
        ("binary", _BinaryValue),
        ("text", ctypes.c_uint16 * UNICODE_LENGTH),
        ("u64", ctypes.c_uint64),
    ]


class _DrsSetting(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("version", ctypes.c_uint32),
        ("settingName", ctypes.c_uint16 * UNICODE_LENGTH),
        ("settingId", ctypes.c_uint32),
        ("settingType", ctypes.c_uint32),
        ("settingLocation", ctypes.c_uint32),
        ("isCurrentPredefined", ctypes.c_uint32),
        ("isPredefinedValid", ctypes.c_uint32),
        ("predefined", _SettingValue),
        ("current", _SettingValue),
    ]


class _DrsApplication(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_uint32),
        ("isPredefined", ctypes.c_uint32),
        ("appName", ctypes.c_uint16 * UNICODE_LENGTH),
        ("userFriendlyName", ctypes.c_uint16 * UNICODE_LENGTH),
        ("launcher", ctypes.c_uint16 * UNICODE_LENGTH),
        ("fileInFolder", ctypes.c_uint16 * UNICODE_LENGTH),
        ("flags", ctypes.c_uint32),
        ("commandLine", ctypes.c_uint16 * UNICODE_LENGTH),
    ]


class _DrsProfile(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_uint32),
        ("profileName", ctypes.c_uint16 * UNICODE_LENGTH),
        ("gpuSupport", ctypes.c_uint32),
        ("isPredefined", ctypes.c_uint32),
        ("numOfApps", ctypes.c_uint32),
        ("numOfSettings", ctypes.c_uint32),
    ]


def _version(structure: type[ctypes.Structure], revision: int) -> int:
    return ctypes.sizeof(structure) | (revision << 16)


def _unicode(value: str):
    result = (ctypes.c_uint16 * UNICODE_LENGTH)()
    encoded = value.encode("utf-16-le")
    units = len(encoded) // 2
    if units >= UNICODE_LENGTH:
        raise NvidiaProfileError("NVIDIA profile string is too long")
    ctypes.memmove(result, encoded, len(encoded))
    return result


def _decode_unicode(value) -> str:
    units = []
    for item in value:
        if not item:
            break
        units.append(int(item))
    raw = b"".join(item.to_bytes(2, "little") for item in units)
    return raw.decode("utf-16-le")


class NvidiaProfileManager:
    """Manage selected settings on LosslessScaling.exe's application profile."""

    _IDS: Dict[str, int] = {
        "initialize": 0x0150E828,
        "unload": 0xD22BDD7E,
        "create_session": 0x0694D52E,
        "destroy_session": 0xDAD9CFF8,
        "load_settings": 0x375DBD6B,
        "save_settings": 0xFCBC7E14,
        "create_profile": 0xCC176068,
        "delete_profile": 0x17093206,
        "find_profile": 0x7E4A9A0B,
        "enum_profiles": 0xBC371EE0,
        "enum_applications": 0x7FA2173A,
        "create_application": 0x4347A9DE,
        "find_application": 0xEEE566B2,
        "set_setting": 0x577DD202,
        "set_setting_ex": 0x8A2CF5F5,
        "get_setting": 0x73BF8338,
        "delete_setting": 0xE4A26362,
    }

    def __init__(
        self,
        library_loader: Optional[Callable[[str], object]] = None,
        receipt_path: Optional[Path] = None,
    ):
        self._library_loader = library_loader or ctypes.WinDLL
        self.receipt_path = Path(receipt_path) if receipt_path else None

    def _read_receipt(self) -> Optional[Dict]:
        if not self.receipt_path or not self.receipt_path.is_file():
            return None
        try:
            value = json.loads(self.receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise NvidiaProfileError(f"NVIDIA rollback receipt is unreadable: {error}") from error
        if not isinstance(value, dict) or value.get("version") != 1:
            raise NvidiaProfileError("NVIDIA rollback receipt has an unsupported format")
        return value

    def _write_receipt(self, receipt: Dict) -> None:
        if not self.receipt_path:
            raise NvidiaProfileError("NVIDIA settings cannot be changed without a rollback receipt path")
        self.receipt_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.receipt_path.name}.", suffix=".tmp", dir=self.receipt_path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(receipt, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(temp_name, self.receipt_path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def _delete_receipt(self) -> None:
        if self.receipt_path:
            self.receipt_path.unlink(missing_ok=True)

    @staticmethod
    def _check(status: int, operation: str) -> None:
        if status != NVAPI_OK:
            raise NvidiaProfileError(f"{operation} failed with NVAPI status {status}")

    @staticmethod
    def _function(query, identifier: int, *arguments):
        address = query(identifier)
        if not address:
            return None
        return ctypes.CFUNCTYPE(ctypes.c_int, *arguments)(address)

    def _find_lossless_application(
        self,
        session,
        path: Path,
        find_application,
        enum_profiles,
        enum_applications,
        profile_handle,
        application,
    ) -> int:
        """Prefer an existing full-path or explicitly pathless LS application entry."""
        status = find_application(
            session,
            _unicode(str(path)),
            ctypes.byref(profile_handle),
            ctypes.byref(application),
        )
        if status == NVAPI_OK:
            return status

        # Older/user-created profiles commonly store only the executable name.
        # Ask NVAPI directly first, then enumerate to resolve an exact pathless
        # entry even when basename lookup reports an ambiguous executable.
        application = _DrsApplication()
        application.version = _version(_DrsApplication, 4)
        status = find_application(
            session,
            _unicode(path.name),
            ctypes.byref(profile_handle),
            ctypes.byref(application),
        )
        if status == NVAPI_OK:
            return status

        profile_index = 0
        while True:
            candidate_profile = ctypes.c_void_p()
            profile_status = enum_profiles(
                session, profile_index, ctypes.byref(candidate_profile)
            )
            if profile_status == NVAPI_END_ENUMERATION:
                break
            self._check(profile_status, "NvAPI_DRS_EnumProfiles")
            application_index = 0
            while True:
                count = ctypes.c_uint32(1)
                candidate = _DrsApplication()
                candidate.version = _version(_DrsApplication, 4)
                app_status = enum_applications(
                    session,
                    candidate_profile,
                    application_index,
                    ctypes.byref(count),
                    ctypes.byref(candidate),
                )
                if app_status == NVAPI_END_ENUMERATION:
                    break
                self._check(app_status, "NvAPI_DRS_EnumApplications")
                if _decode_unicode(candidate.appName).casefold() == path.name.casefold():
                    profile_handle.value = candidate_profile.value
                    ctypes.memmove(
                        ctypes.byref(application), ctypes.byref(candidate), ctypes.sizeof(candidate)
                    )
                    return NVAPI_OK
                application_index += max(1, int(count.value))
            profile_index += 1
        return status

    def set_lossless_scaling_smooth_motion(self, executable: str, enabled: bool) -> bool:
        return self._set_lossless_scaling_dwords(
            executable,
            {SMOOTH_MOTION_ENABLE_ID: 1 if enabled else 0},
            create_if_missing=enabled,
            feature="Smooth Motion",
        )

    def set_lossless_scaling_rtx_hdr(self, executable: str, enabled: bool) -> bool:
        """Set experimental RTX HDR flags on Lossless Scaling's app profile."""
        value = 1 if enabled else 0
        return self._set_lossless_scaling_dwords(
            executable,
            {
                RTX_HDR_DRIVER_FLAGS_ID: 0x6 if enabled else 0,
                RTX_HDR_REQUIRED_ID: value,
                RTX_HDR_GAME_FILTERS_ID: value,
                RTX_HDR_ENABLE_ID: value,
            },
            create_if_missing=enabled,
            feature="RTX HDR",
        )

    def _set_lossless_scaling_dwords(
        self,
        executable: str,
        settings: Dict[int, int],
        *,
        create_if_missing: bool,
        feature: str,
    ) -> bool:
        """Set per-executable DRS DWORDs; create the profile only when enabling."""
        if os.name != "nt":
            return False
        path = Path(executable).resolve()
        if path.name.casefold() != "losslessscaling.exe":
            raise NvidiaProfileError("Smooth Motion may only target LosslessScaling.exe")
        try:
            library = self._library_loader("nvapi64.dll")
        except OSError:
            if create_if_missing:
                raise NvidiaProfileError(f"NVIDIA NVAPI is unavailable; {feature} requires an NVIDIA driver")
            return False
        query = getattr(library, "nvapi_QueryInterface")
        query.argtypes = [ctypes.c_uint32]
        query.restype = ctypes.c_void_p

        void_p = ctypes.c_void_p
        initialize = self._function(query, self._IDS["initialize"])
        unload = self._function(query, self._IDS["unload"])
        create_session = self._function(query, self._IDS["create_session"], ctypes.POINTER(void_p))
        destroy_session = self._function(query, self._IDS["destroy_session"], void_p)
        load_settings = self._function(query, self._IDS["load_settings"], void_p)
        save_settings = self._function(query, self._IDS["save_settings"], void_p)
        find_application = self._function(
            query, self._IDS["find_application"], void_p, ctypes.POINTER(ctypes.c_uint16),
            ctypes.POINTER(void_p), ctypes.POINTER(_DrsApplication)
        )
        enum_profiles = self._function(
            query, self._IDS["enum_profiles"], void_p, ctypes.c_uint32, ctypes.POINTER(void_p)
        )
        enum_applications = self._function(
            query, self._IDS["enum_applications"], void_p, void_p, ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(_DrsApplication)
        )
        create_profile = self._function(
            query, self._IDS["create_profile"], void_p, ctypes.POINTER(_DrsProfile), ctypes.POINTER(void_p)
        )
        create_application = self._function(
            query, self._IDS["create_application"], void_p, void_p, ctypes.POINTER(_DrsApplication)
        )
        set_setting_ex = self._function(
            query, self._IDS["set_setting_ex"], void_p, void_p, ctypes.POINTER(_DrsSetting),
            ctypes.c_size_t, ctypes.c_size_t
        )
        set_setting = set_setting_ex or self._function(
            query, self._IDS["set_setting"], void_p, void_p, ctypes.POINTER(_DrsSetting)
        )
        get_setting = self._function(
            query, self._IDS["get_setting"], void_p, void_p, ctypes.c_uint32,
            ctypes.POINTER(_DrsSetting)
        )
        required = {
            "initialize": initialize, "unload": unload, "create session": create_session,
            "destroy session": destroy_session, "load settings": load_settings,
            "save settings": save_settings, "find application": find_application,
            "enum profiles": enum_profiles, "enum applications": enum_applications,
            "create profile": create_profile, "create application": create_application,
            "set setting": set_setting, "get setting": get_setting,
        }
        missing = [name for name, function in required.items() if function is None]
        if missing:
            raise NvidiaProfileError(f"NVIDIA driver is missing NVAPI functions: {', '.join(missing)}")

        self._check(initialize(), "NvAPI_Initialize")
        session = void_p()
        try:
            self._check(create_session(ctypes.byref(session)), "NvAPI_DRS_CreateSession")
            try:
                self._check(load_settings(session), "NvAPI_DRS_LoadSettings")
                application = _DrsApplication()
                application.version = _version(_DrsApplication, 4)
                application_path = _unicode(str(path))
                profile_handle = void_p()
                found = self._find_lossless_application(
                    session, path, find_application, enum_profiles, enum_applications,
                    profile_handle, application,
                )
                receipt = self._read_receipt()
                if receipt and str(receipt.get("executable") or "").casefold() != str(path).casefold():
                    raise NvidiaProfileError(
                        "Restore LS Companion's previous NVIDIA profile changes before changing the Lossless Scaling path"
                    )
                profile_created = False
                profile_name_text = ""
                if found != NVAPI_OK:
                    if not create_if_missing:
                        return False
                    if receipt:
                        raise NvidiaProfileError(
                            "The NVIDIA rollback receipt exists, but its Lossless Scaling application entry is missing"
                        )
                    profile_name_text = f"LS Companion - Lossless Scaling - {uuid.uuid4().hex[:8]}"
                    profile = _DrsProfile()
                    profile.version = _version(_DrsProfile, 1)
                    profile.profileName = _unicode(profile_name_text)
                    self._check(
                        create_profile(session, ctypes.byref(profile), ctypes.byref(profile_handle)),
                        "NvAPI_DRS_CreateProfile",
                    )
                    application = _DrsApplication()
                    application.version = _version(_DrsApplication, 4)
                    application.appName = application_path
                    application.userFriendlyName = _unicode("Lossless Scaling")
                    self._check(
                        create_application(session, profile_handle, ctypes.byref(application)),
                        "NvAPI_DRS_CreateApplication",
                    )
                    profile_created = True

                if receipt is None:
                    originals = {}
                    if not profile_created:
                        for setting_id in MANAGED_SETTING_IDS:
                            original = _DrsSetting()
                            original.version = _version(_DrsSetting, 1)
                            status = get_setting(
                                session, profile_handle, setting_id, ctypes.byref(original)
                            )
                            if status == NVAPI_OK and original.settingLocation == CURRENT_PROFILE_LOCATION:
                                originals[str(setting_id)] = {
                                    "hadOverride": True,
                                    "settingType": int(original.settingType),
                                    "value": int(original.current.u32),
                                }
                            elif status in (NVAPI_OK, NVAPI_SETTING_NOT_FOUND):
                                originals[str(setting_id)] = {"hadOverride": False}
                            else:
                                self._check(status, "NvAPI_DRS_GetSetting")
                    receipt = {
                        "version": 1,
                        "executable": str(path),
                        "profileCreated": profile_created,
                        "profileName": profile_name_text,
                        "settings": originals,
                    }
                receipt_settings = receipt.setdefault("settings", {})
                for setting_id, value in settings.items():
                    entry = receipt_settings.setdefault(str(setting_id), {"hadOverride": False})
                    entry["managedValue"] = int(value)
                self._write_receipt(receipt)

                for setting_id, value in settings.items():
                    setting = _DrsSetting()
                    setting.version = _version(_DrsSetting, 1)
                    setting.settingId = setting_id
                    setting.settingType = NVDRS_DWORD_TYPE
                    setting.settingLocation = CURRENT_PROFILE_LOCATION
                    setting.current.u32 = value
                    if set_setting_ex:
                        status = set_setting(session, profile_handle, ctypes.byref(setting), 0, 0)
                    else:
                        status = set_setting(session, profile_handle, ctypes.byref(setting))
                    self._check(status, "NvAPI_DRS_SetSetting")
                self._check(save_settings(session), "NvAPI_DRS_SaveSettings")
                logger.info("Set Lossless Scaling %s in its NVIDIA application profile", feature)
                return True
            finally:
                destroy_session(session)
        finally:
            unload()

    def restore_managed_changes(self) -> bool:
        """Delete our profile or restore only the values recorded before our edits."""
        receipt = self._read_receipt()
        if receipt is None:
            return False
        if os.name != "nt":
            return False
        path = Path(str(receipt.get("executable") or "")).resolve()
        if path.name.casefold() != "losslessscaling.exe":
            raise NvidiaProfileError("NVIDIA rollback receipt does not target LosslessScaling.exe")
        try:
            library = self._library_loader("nvapi64.dll")
        except OSError as error:
            raise NvidiaProfileError("NVIDIA NVAPI is unavailable; rollback was retained") from error
        query = getattr(library, "nvapi_QueryInterface")
        query.argtypes = [ctypes.c_uint32]
        query.restype = ctypes.c_void_p
        void_p = ctypes.c_void_p
        initialize = self._function(query, self._IDS["initialize"])
        unload = self._function(query, self._IDS["unload"])
        create_session = self._function(query, self._IDS["create_session"], ctypes.POINTER(void_p))
        destroy_session = self._function(query, self._IDS["destroy_session"], void_p)
        load_settings = self._function(query, self._IDS["load_settings"], void_p)
        save_settings = self._function(query, self._IDS["save_settings"], void_p)
        find_application = self._function(
            query, self._IDS["find_application"], void_p, ctypes.POINTER(ctypes.c_uint16),
            ctypes.POINTER(void_p), ctypes.POINTER(_DrsApplication)
        )
        enum_profiles = self._function(
            query, self._IDS["enum_profiles"], void_p, ctypes.c_uint32, ctypes.POINTER(void_p)
        )
        enum_applications = self._function(
            query, self._IDS["enum_applications"], void_p, void_p, ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(_DrsApplication)
        )
        find_profile = self._function(
            query, self._IDS["find_profile"], void_p, ctypes.POINTER(ctypes.c_uint16), ctypes.POINTER(void_p)
        )
        delete_profile = self._function(
            query, self._IDS["delete_profile"], void_p, void_p
        )
        set_setting = self._function(
            query, self._IDS["set_setting"], void_p, void_p, ctypes.POINTER(_DrsSetting)
        )
        get_setting = self._function(
            query, self._IDS["get_setting"], void_p, void_p, ctypes.c_uint32,
            ctypes.POINTER(_DrsSetting)
        )
        delete_setting = self._function(
            query, self._IDS["delete_setting"], void_p, void_p, ctypes.c_uint32
        )
        required = {
            "initialize": initialize, "unload": unload, "create session": create_session,
            "destroy session": destroy_session, "load settings": load_settings,
            "save settings": save_settings, "find application": find_application,
            "enum profiles": enum_profiles, "enum applications": enum_applications,
            "find profile": find_profile, "delete profile": delete_profile,
            "set setting": set_setting, "get setting": get_setting,
            "delete setting": delete_setting,
        }
        missing = [name for name, function in required.items() if function is None]
        if missing:
            raise NvidiaProfileError(f"NVIDIA driver is missing rollback functions: {', '.join(missing)}")

        self._check(initialize(), "NvAPI_Initialize")
        session = void_p()
        try:
            self._check(create_session(ctypes.byref(session)), "NvAPI_DRS_CreateSession")
            try:
                self._check(load_settings(session), "NvAPI_DRS_LoadSettings")
                profile_handle = void_p()
                application = _DrsApplication()
                application.version = _version(_DrsApplication, 4)
                if receipt.get("profileCreated"):
                    profile_name = str(receipt.get("profileName") or "")
                    found = find_profile(session, _unicode(profile_name), ctypes.byref(profile_handle))
                    if found == NVAPI_OK:
                        self._check(delete_profile(session, profile_handle), "NvAPI_DRS_DeleteProfile")
                        self._check(save_settings(session), "NvAPI_DRS_SaveSettings")
                    elif found != NVAPI_PROFILE_NOT_FOUND:
                        self._check(found, "NvAPI_DRS_FindProfileByName")
                    self._delete_receipt()
                    logger.info("Removed LS Companion's NVIDIA application profile")
                    return found == NVAPI_OK

                found = self._find_lossless_application(
                    session, path, find_application, enum_profiles, enum_applications,
                    profile_handle, application,
                )
                if found != NVAPI_OK:
                    if found == NVAPI_EXECUTABLE_NOT_FOUND:
                        self._delete_receipt()
                        return False
                    self._check(found, "NvAPI_DRS_FindApplicationByName")
                changed = False
                for setting_id_text, original in dict(receipt.get("settings") or {}).items():
                    if "managedValue" not in original:
                        continue
                    setting_id = int(setting_id_text)
                    current = _DrsSetting()
                    current.version = _version(_DrsSetting, 1)
                    status = get_setting(session, profile_handle, setting_id, ctypes.byref(current))
                    if (
                        status != NVAPI_OK
                        or current.settingLocation != CURRENT_PROFILE_LOCATION
                        or int(current.current.u32) != int(original["managedValue"])
                    ):
                        continue
                    if original.get("hadOverride"):
                        restored = _DrsSetting()
                        restored.version = _version(_DrsSetting, 1)
                        restored.settingId = setting_id
                        restored.settingType = int(original.get("settingType", NVDRS_DWORD_TYPE))
                        restored.settingLocation = CURRENT_PROFILE_LOCATION
                        restored.current.u32 = int(original["value"])
                        self._check(
                            set_setting(session, profile_handle, ctypes.byref(restored)),
                            "NvAPI_DRS_SetSetting",
                        )
                    else:
                        status = delete_setting(session, profile_handle, setting_id)
                        if status not in (NVAPI_OK, NVAPI_SETTING_NOT_FOUND):
                            self._check(status, "NvAPI_DRS_DeleteProfileSetting")
                    changed = True
                if changed:
                    self._check(save_settings(session), "NvAPI_DRS_SaveSettings")
                self._delete_receipt()
                logger.info("Restored NVIDIA values on the existing Lossless Scaling profile")
                return changed
            finally:
                destroy_session(session)
        finally:
            unload()
