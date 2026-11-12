"""Read application icon resources without launching or modifying the target."""

from __future__ import annotations

from collections import OrderedDict
import ctypes
from ctypes import wintypes
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import threading
import time
import tempfile

from PIL import Image
import psutil

from ..core.models import Profile


def executable_icon_png(path: Path, size: int = 64) -> bytes | None:
    """Extract an embedded icon; every native handle is owned and released here."""
    if os.name != "nt":
        return None

    class BitmapHeader(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("width", wintypes.LONG),
                    ("height", wintypes.LONG), ("planes", wintypes.WORD),
                    ("bits", wintypes.WORD), ("compression", wintypes.DWORD),
                    ("image_size", wintypes.DWORD), ("xppm", wintypes.LONG),
                    ("yppm", wintypes.LONG), ("used", wintypes.DWORD),
                    ("important", wintypes.DWORD)]

    user = ctypes.WinDLL("user32", use_last_error=True)
    gdi = ctypes.WinDLL("gdi32", use_last_error=True)
    extract = user.PrivateExtractIconsW
    extract.argtypes = [wintypes.LPCWSTR, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                        ctypes.POINTER(wintypes.HICON), ctypes.POINTER(wintypes.UINT),
                        wintypes.UINT, wintypes.UINT]
    extract.restype = wintypes.UINT
    user.DestroyIcon.argtypes = [wintypes.HICON]
    user.DrawIconEx.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int,
                               wintypes.HICON, ctypes.c_int, ctypes.c_int,
                               wintypes.UINT, wintypes.HBRUSH, wintypes.UINT]
    gdi.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi.CreateCompatibleDC.restype = wintypes.HDC
    gdi.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
    gdi.CreateDIBSection.restype = wintypes.HBITMAP
    gdi.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
    gdi.SelectObject.restype = wintypes.HANDLE
    gdi.DeleteObject.argtypes = [wintypes.HANDLE]
    gdi.DeleteDC.argtypes = [wintypes.HDC]
    icon = wintypes.HICON()
    icon_id = wintypes.UINT()
    dc = bitmap = previous = None
    try:
        if extract(str(path), 0, size, size, ctypes.byref(icon), ctypes.byref(icon_id), 1, 0) != 1 or not icon.value:
            return None
        dc = gdi.CreateCompatibleDC(None)
        if not dc:
            return None
        # Top-down 32-bit DIB; render on black and white to recover alpha for
        # both modern alpha icons and older AND/XOR masked resources.
        header = BitmapHeader(size=ctypes.sizeof(BitmapHeader), width=size,
                              height=-size, planes=1, bits=32)
        bits = ctypes.c_void_p()
        bitmap = gdi.CreateDIBSection(dc, ctypes.byref(header), 0, ctypes.byref(bits), None, 0)
        if not bitmap or not bits.value:
            return None
        previous = gdi.SelectObject(dc, bitmap)
        if not previous or previous == ctypes.c_void_p(-1).value:
            previous = None
            return None
        renders = []
        for background in (0, 255):
            ctypes.memset(bits, background, size * size * 4)
            if not user.DrawIconEx(dc, 0, 0, icon, size, size, 0, None, 3):
                return None
            gdi.GdiFlush()
            renders.append(Image.frombytes("RGB", (size, size), ctypes.string_at(bits, size * size * 4), "raw", "BGRX"))
        pixels = []
        dark_rgb, light_rgb = (render.tobytes() for render in renders)
        for offset in range(0, len(dark_rgb), 3):
            dark, light = dark_rgb[offset:offset + 3], light_rgb[offset:offset + 3]
            alpha = max(0, min(255, 255 - max(b - a for a, b in zip(dark, light))))
            rgb = tuple(min(255, round(channel * 255 / alpha)) for channel in dark) if alpha else (0, 0, 0)
            pixels.append((*rgb, alpha))
        image = Image.new("RGBA", (size, size))
        image.putdata(pixels)
        if image.getbbox() is None:
            return None
        output = BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()
    finally:
        if previous:
            gdi.SelectObject(dc, previous)
        if bitmap:
            gdi.DeleteObject(bitmap)
        if dc:
            gdi.DeleteDC(dc)
        if icon.value:
            user.DestroyIcon(icon)


class ProfileIconService:
    """Bounded resource cache shared by all profiles pointing at the same app."""

    def __init__(self, cache_dir: Path | None = None):
        self._lock = threading.Lock()
        self._icons = OrderedDict()
        self._names = OrderedDict()
        self._process_paths = {}
        self._process_paths_until = 0.0
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None

    def _disk_icon(self, version: str) -> bytes | None:
        if self.cache_dir is None:
            return None
        try:
            record = json.loads((self.cache_dir / f"{version}.json").read_text(encoding="utf-8"))
            source = Path(record["path"])
            if not source.is_absolute() or str(source).startswith(("\\\\", "//")):
                return None
            try:
                info = source.stat()
                if [info.st_mtime_ns, info.st_size] != record["signature"]:
                    return None
            except OSError:
                pass  # Keep the last icon when an application is unavailable.
            icon = self.cache_dir / f"{version}.png"
            if icon.stat().st_size > 1024 * 1024:
                return None
            data = icon.read_bytes()
            with Image.open(BytesIO(data)) as picture:
                if picture.format != "PNG" or picture.size != (64, 64):
                    return None
                picture.verify()
            return data
        except (OSError, ValueError, KeyError, TypeError, Image.DecompressionBombError):
            return None

    def _save_disk_icon(self, version: str, path: Path, info, data: bytes) -> None:
        if self.cache_dir is None:
            return
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            record = json.dumps({"path": str(path), "signature": [info.st_mtime_ns, info.st_size]})
            for suffix, content in (("png", data), ("json", record.encode("utf-8"))):
                handle, temporary = tempfile.mkstemp(dir=self.cache_dir, suffix=".tmp")
                try:
                    with os.fdopen(handle, "wb") as stream:
                        stream.write(content)
                    os.replace(temporary, self.cache_dir / f"{version}.{suffix}")
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
            records = sorted(self.cache_dir.glob("????????????????.json"), key=lambda item: item.stat().st_mtime)
            for old in records[:-256]:
                if len(old.stem) == 16 and all(char in "0123456789abcdef" for char in old.stem):
                    old.with_suffix(".png").unlink(missing_ok=True)
                    old.unlink(missing_ok=True)
        except OSError:
            pass  # Extraction still works if the optional cache cannot be saved.

    @staticmethod
    def targets(profile: Profile, lossless_exe: str | None) -> list[str]:
        if profile.is_default:
            return [lossless_exe] if lossless_exe else []
        if profile.target_domain:
            return []
        targets = [value for value in [profile.target_executable_path, *profile.target_executable_paths,
                                       profile.target_process, *profile.target_processes] if value]
        native = profile.lossless_profile_path
        # A reused native scaling template may still name a different game.
        # Its historical path is useful only when it matches a current target.
        names = {Path(value.strip().strip('"')).name.casefold() for value in targets}
        if native and (not targets or Path(native.strip().strip('"')).name.casefold() in names):
            targets.append(native)
        return targets

    @classmethod
    def version(cls, profile: Profile, lossless_exe: str | None) -> str:
        payload = json.dumps(cls.targets(profile, lossless_exe), ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()[:16]

    def _resolve_name(self, name: str) -> list[str]:
        now = time.monotonic()
        key = name.casefold()
        cached = self._names.get(key)
        if cached and cached[0] > now:
            self._names.move_to_end(key)
            return cached[1]
        if self._process_paths_until <= now:
            paths = {}
            for process in psutil.process_iter(["name", "exe"]):
                try:
                    info = process.info
                    if info.get("name") and info.get("exe"):
                        paths.setdefault(info["name"].casefold(), []).append(info["exe"])
                except (psutil.Error, OSError):
                    continue
            self._process_paths = paths
            self._process_paths_until = now + 30
        result = list(self._process_paths.get(key, []))
        if os.name == "nt":
            import winreg
            for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                    try:
                        with winreg.OpenKey(root, rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{name}", 0, winreg.KEY_READ | view) as entry:
                            value, _ = winreg.QueryValueEx(entry, None)
                            if isinstance(value, str):
                                result.append(value)
                    except OSError:
                        pass
        found = shutil.which(name)
        if found:
            result.append(found)
        self._names[key] = (now + 30, result)
        while len(self._names) > 128:
            self._names.popitem(last=False)
        return result

    def png(self, profile: Profile, lossless_exe: str | None = None) -> bytes | None:
        with self._lock:
            version = self.version(profile, lossless_exe)
            saved = self._disk_icon(version)
            if saved:
                return saved
            targets = self.targets(profile, lossless_exe)
            candidates, names = [], []
            for value in targets:
                value = os.path.expandvars(value.strip().strip('"'))
                path = Path(value)
                if path.suffix.casefold() != ".exe":
                    continue
                if path.is_absolute():
                    candidates.append(path)
                elif path.name == value:
                    names.append(value)
            seen = set()
            # Resolve process-only legacy profiles lazily, after explicit paths.
            def paths():
                yield from candidates
                for name in names:
                    yield from map(Path, self._resolve_name(name))
            for candidate in paths():
                path = Path(os.path.expandvars(str(candidate).strip().strip('"')))
                if not path.is_absolute() or path.suffix.casefold() != ".exe":
                    continue
                key = os.path.normcase(str(path))
                if key in seen or str(path).startswith(("\\\\", "//")):
                    continue
                seen.add(key)
                try:
                    if not path.is_file():
                        continue
                    info = path.stat()
                    signature = (key, info.st_mtime_ns, info.st_size)
                    cached = self._icons.get(signature)
                    if cached and (cached[1] is not None or cached[0] > time.monotonic()):
                        self._icons.move_to_end(signature)
                        data = cached[1]
                    else:
                        data = executable_icon_png(path)
                        self._icons[signature] = (time.monotonic() + 30, data)
                        while len(self._icons) > 128:
                            self._icons.popitem(last=False)
                    if data:
                        self._save_disk_icon(version, path, info, data)
                        return data
                except (OSError, ValueError):
                    continue
            return None
