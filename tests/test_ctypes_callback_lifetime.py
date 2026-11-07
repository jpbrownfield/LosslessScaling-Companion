"""Native-crash guard: Win32 enum callbacks must never be temporaries.

Passing ``SomeWINFUNCTYPE(python_fn)`` directly into EnumWindows /
EnumDisplayMonitors lets CPython free the thunk mid-enumeration, which
AVs the interpreter (0xc0000005 in python312.dll) with no traceback.
Every call site must hold the thunk in a named local for the call.
"""
import pathlib
import re
import unittest

TEMPORARY_THUNK = re.compile(
    r"Enum(?:Windows|DisplayMonitors)\s*\(\s*"
    r"(?:[A-Za-z_][\w]*\s*\(\s*[A-Za-z_][\w]*\s*\)|"
    r"ctypes\.WINFUNCTYPE\s*\()"
)


class CallbackLifetimeTests(unittest.TestCase):
    def test_no_temporary_enum_thunks(self):
        root = pathlib.Path(__file__).resolve().parents[1] / "companion"
        offenders = []
        for path in sorted(root.rglob("*.py")):
            text = path.read_text(encoding="utf-8", errors="replace")
            for match in TEMPORARY_THUNK.finditer(text):
                line = text[: match.start()].count("\n") + 1
                snippet = text[match.start(): text.find("\n", match.start())].strip()
                offenders.append(f"{path.name}:{line}: {snippet[:120]}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
