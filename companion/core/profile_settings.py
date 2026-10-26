"""Human-readable, Companion-owned profile snapshots with stable device choices."""
import hashlib
import json
import logging
import os
from pathlib import Path
import re

logger = logging.getLogger("LSCompanion.ProfileSettings")
OWNER = "LSCompanion.ProfileSettings"


class ProfileSettingsStore:
    def __init__(self, config_dir):
        self.directory = Path(config_dir) / "profile-settings"
        self._written = {}

    def path(self, profile):
        return self.directory / f"{profile.id}.json"

    @staticmethod
    def fingerprint(profile):
        raw = json.dumps({"profileId": profile.id, "nativeSettings": profile.native_scaling_settings}, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def prepare(self, profiles):
        for profile in profiles:
            intent = dict(nativeFingerprint=self.fingerprint(profile),
                          gpuId=profile.preferred_gpu_device_id, gpuName=profile.preferred_gpu_name,
                          displayId=profile.output_display_device_id, displayName=profile.output_display_name)
            profile.profile_settings_revision = hashlib.sha256(json.dumps(intent, sort_keys=True).encode("utf-8")).hexdigest()

    @staticmethod
    def _choice(profile, key, id_field, name_field, label, default):
        device_id, name = getattr(profile, id_field), getattr(profile, name_field)
        try:
            numeric = int(profile.native_scaling_settings.get(key) or 0)
        except (TypeError, ValueError):
            numeric = 0
        mode = ("explicit" if numeric else "unavailable") if device_id or name else ("unresolved" if numeric else default)
        return dict(label=label, nativeKey=key, mode=mode, friendlyName=name,
                    deviceId=device_id, resolvedNativeId=numeric)

    def document(self, profile):
        return {
            "owner": OWNER, "schemaVersion": 1,
            "profileId": profile.id, "profileName": profile.name,
            "settingsFingerprint": self.fingerprint(profile),
            "revision": profile.profile_settings_revision,
            "settings": {
                "scalingGpu": self._choice(profile, "PreferredGpuId", "preferred_gpu_device_id", "preferred_gpu_name", "Scaling GPU", "general"),
                "outputDisplay": self._choice(profile, "OutputDisplayId", "output_display_device_id", "output_display_name", "Output display", "automatic"),
            },
            "nativeSettings": {
                key: {"label": re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", key), "value": value}
                for key, value in profile.native_scaling_settings.items()
            },
        }

    def restore(self, profiles):
        """Recover missing identity metadata only from a matching profile revision.

        settings.json remains authoritative. A stale snapshot must never undo a
        newer explicit 'follow General Settings' selection after an interrupted save.
        """
        for profile in profiles:
            path = self.path(profile)
            if not path.is_file():
                continue
            try:
                if path.stat().st_size > 4 * 1024 * 1024:
                    raise ValueError("profile snapshot exceeds size limit")
                data = json.loads(path.read_text(encoding="utf-8"))
                if (not isinstance(data, dict) or data.get("owner") != OWNER
                        or data.get("schemaVersion") != 1 or data.get("profileId") != profile.id
                        or not profile.profile_settings_revision
                        or data.get("revision") != profile.profile_settings_revision
                        or data.get("settingsFingerprint") != self.fingerprint(profile)):
                    continue
                settings = data.get("settings")
                if not isinstance(settings, dict):
                    continue
                for section, id_field, name_field in (
                    ("scalingGpu", "preferred_gpu_device_id", "preferred_gpu_name"),
                    ("outputDisplay", "output_display_device_id", "output_display_name"),
                ):
                    choice = settings.get(section)
                    if not isinstance(choice, dict) or choice.get("mode") not in {"explicit", "unavailable"}:
                        continue
                    for field, key in ((id_field, "deviceId"), (name_field, "friendlyName")):
                        value = choice.get(key)
                        if not getattr(profile, field) and isinstance(value, str) and value:
                            setattr(profile, field, value)
            except (OSError, ValueError, TypeError):
                logger.warning("Ignoring unreadable profile settings snapshot: %s", path, exc_info=True)

    def sync(self, profiles):
        self.directory.mkdir(parents=True, exist_ok=True)
        for profile in profiles:
            text = json.dumps(self.document(profile), indent=2, ensure_ascii=False) + "\n"
            path = self.path(profile)
            if self._written.get(profile.id) == text and path.is_file():
                continue
            if not path.is_file() or path.read_text(encoding="utf-8") != text:
                temporary = path.with_suffix(".json.tmp")
                temporary.write_text(text, encoding="utf-8")
                os.replace(temporary, path)
            self._written[profile.id] = text

    def remove(self, profile):
        path = self.path(profile)
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("owner") == OWNER and data.get("profileId") == profile.id:
                path.unlink()
        self._written.pop(profile.id, None)
