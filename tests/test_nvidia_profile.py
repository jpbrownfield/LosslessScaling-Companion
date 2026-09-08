import ctypes
import tempfile
import unittest
from pathlib import Path

from companion.services.nvidia_profile import (
    NVAPI_EXECUTABLE_NOT_FOUND,
    NVAPI_SETTING_NOT_FOUND,
    NvidiaProfileError,
    NvidiaProfileManager,
    _decode_unicode,
    _DrsApplication,
    _DrsProfile,
    _DrsSetting,
)


class _QueryInterface:
    def __init__(self, addresses):
        self.addresses = addresses
        self.argtypes = None
        self.restype = None

    def __call__(self, identifier):
        return self.addresses.get(identifier, 0)


class _FakeNvapiLibrary:
    def __init__(self, application_exists=True, pathless_only=False):
        self.callbacks = []
        self.values = {}
        self.application_exists = application_exists
        self.pathless_only = pathless_only
        self.application_queries = []
        self.created_profiles = 0
        self.deleted_profiles = 0
        self.deleted_settings = []
        manager_ids = NvidiaProfileManager._IDS
        addresses = {}

        def register(name, signature, callback):
            function = ctypes.CFUNCTYPE(ctypes.c_int, *signature)(callback)
            self.callbacks.append(function)
            addresses[manager_ids[name]] = ctypes.cast(function, ctypes.c_void_p).value

        void_p = ctypes.c_void_p
        register("initialize", [], lambda: 0)
        register("unload", [], lambda: 0)
        register("create_session", [ctypes.POINTER(void_p)], self._create_session)
        register("destroy_session", [void_p], lambda session: 0)
        register("load_settings", [void_p], lambda session: 0)
        register("save_settings", [void_p], lambda session: 0)
        register(
            "find_application",
            [void_p, ctypes.POINTER(ctypes.c_uint16), ctypes.POINTER(void_p), ctypes.POINTER(_DrsApplication)],
            self._find_application,
        )
        register(
            "find_profile",
            [void_p, ctypes.POINTER(ctypes.c_uint16), ctypes.POINTER(void_p)],
            self._find_profile,
        )
        register(
            "enum_profiles",
            [void_p, ctypes.c_uint32, ctypes.POINTER(void_p)],
            lambda session, index, profile: -7,
        )
        register(
            "enum_applications",
            [void_p, void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(_DrsApplication)],
            lambda session, profile, index, count, application: -7,
        )
        register(
            "create_profile",
            [void_p, ctypes.POINTER(_DrsProfile), ctypes.POINTER(void_p)],
            self._create_profile,
        )
        register(
            "delete_profile", [void_p, void_p], self._delete_profile
        )
        register(
            "create_application",
            [void_p, void_p, ctypes.POINTER(_DrsApplication)],
            self._create_application,
        )
        register(
            "set_setting",
            [void_p, void_p, ctypes.POINTER(_DrsSetting)],
            self._set_setting,
        )
        register(
            "get_setting",
            [void_p, void_p, ctypes.c_uint32, ctypes.POINTER(_DrsSetting)],
            self._get_setting,
        )
        register(
            "delete_setting", [void_p, void_p, ctypes.c_uint32], self._delete_setting
        )
        self.nvapi_QueryInterface = _QueryInterface(addresses)

    @staticmethod
    def _create_session(output):
        output[0] = ctypes.c_void_p(100)
        return 0

    def _find_application(self, session, name, profile, application):
        query = _decode_unicode(name)
        self.application_queries.append(query)
        if not self.application_exists:
            return NVAPI_EXECUTABLE_NOT_FOUND
        if self.pathless_only and ("\\" in query or "/" in query):
            return NVAPI_EXECUTABLE_NOT_FOUND
        profile[0] = ctypes.c_void_p(200)
        return 0

    def _find_profile(self, session, name, profile):
        if not self.application_exists:
            return -163
        profile[0] = ctypes.c_void_p(200)
        return 0

    def _create_profile(self, session, profile, output):
        self.created_profiles += 1
        output[0] = ctypes.c_void_p(200)
        return 0

    def _delete_profile(self, session, profile):
        self.deleted_profiles += 1
        self.application_exists = False
        return 0

    def _create_application(self, session, profile, application):
        self.application_exists = True
        return 0

    def _set_setting(self, session, profile, setting):
        self.values[int(setting.contents.settingId)] = int(setting.contents.current.u32)
        return 0

    def _get_setting(self, session, profile, setting_id, output):
        setting_id = int(setting_id)
        if setting_id not in self.values:
            return NVAPI_SETTING_NOT_FOUND
        output.contents.settingLocation = 0
        output.contents.settingType = 0
        output.contents.current.u32 = self.values[setting_id]
        return 0

    def _delete_setting(self, session, profile, setting_id):
        setting_id = int(setting_id)
        self.deleted_settings.append(setting_id)
        self.values.pop(setting_id, None)
        return 0


class NvidiaProfileReceiptTests(unittest.TestCase):
    def test_pathless_user_application_profile_is_preferred_before_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "LosslessScaling.exe"
            executable.write_bytes(b"exe")
            library = _FakeNvapiLibrary(application_exists=True, pathless_only=True)
            manager = NvidiaProfileManager(
                lambda name: library, Path(directory) / "rollback.json"
            )

            self.assertTrue(manager.set_lossless_scaling_smooth_motion(str(executable), True))

            self.assertEqual(library.application_queries[-1], "LosslessScaling.exe")
            self.assertEqual(library.created_profiles, 0)

    def test_existing_application_profile_is_reused_and_its_override_is_undone(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "LosslessScaling.exe"
            executable.write_bytes(b"exe")
            receipt = Path(directory) / "rollback.json"
            library = _FakeNvapiLibrary(application_exists=True)
            manager = NvidiaProfileManager(lambda name: library, receipt)

            self.assertTrue(manager.set_lossless_scaling_smooth_motion(str(executable), True))
            self.assertEqual(library.created_profiles, 0)
            self.assertFalse(manager._read_receipt()["profileCreated"])

            self.assertTrue(manager.restore_managed_changes())
            self.assertEqual(library.deleted_profiles, 0)
            self.assertTrue(library.deleted_settings)
            self.assertFalse(receipt.exists())

    def test_companion_created_profile_is_deleted_during_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "LosslessScaling.exe"
            executable.write_bytes(b"exe")
            receipt = Path(directory) / "rollback.json"
            library = _FakeNvapiLibrary(application_exists=False)
            manager = NvidiaProfileManager(lambda name: library, receipt)

            self.assertTrue(manager.set_lossless_scaling_smooth_motion(str(executable), True))
            self.assertEqual(library.created_profiles, 1)
            self.assertTrue(manager._read_receipt()["profileCreated"])

            self.assertTrue(manager.restore_managed_changes())
            self.assertEqual(library.deleted_profiles, 1)
            self.assertFalse(receipt.exists())

    def test_receipt_round_trip_is_atomic_and_removable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nvidia-profile-rollback.json"
            manager = NvidiaProfileManager(receipt_path=path)
            receipt = {
                "version": 1,
                "executable": r"C:\Lossless Scaling\LosslessScaling.exe",
                "profileCreated": False,
                "settings": {"1": {"hadOverride": False, "managedValue": 1}},
            }

            manager._write_receipt(receipt)

            self.assertEqual(manager._read_receipt(), receipt)
            manager._delete_receipt()
            self.assertFalse(path.exists())

    def test_invalid_receipt_is_never_silently_discarded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nvidia-profile-rollback.json"
            path.write_text('{"version": 99}', encoding="utf-8")
            manager = NvidiaProfileManager(receipt_path=path)

            with self.assertRaises(NvidiaProfileError):
                manager._read_receipt()

            self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
