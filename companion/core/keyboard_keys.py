"""Shared punctuation and keypad identities for Windows shortcut settings.

Store the unshifted key separately from modifiers: '+' is Shift+'=', so the
shortcut's '+' delimiter never conflicts with a recorded punctuation key.
"""

# Canonical key -> (Lossless Scaling/.NET key name, Windows virtual-key code).
PUNCTUATION_KEYS = {
    ";": ("OemSemicolon", 0xBA),
    "=": ("OemPlus", 0xBB),
    ",": ("OemComma", 0xBC),
    "-": ("OemMinus", 0xBD),
    ".": ("OemPeriod", 0xBE),
    "/": ("OemQuestion", 0xBF),
    "`": ("OemTilde", 0xC0),
    "[": ("OemOpenBrackets", 0xDB),
    "\\": ("OemPipe", 0xDC),
    "]": ("OemCloseBrackets", 0xDD),
    "'": ("OemQuotes", 0xDE),
    "oem102": ("OemBackslash", 0xE2),
    "multiply": ("Multiply", 0x6A),
    "add": ("Add", 0x6B),
    "separator": ("Separator", 0x6C),
    "subtract": ("Subtract", 0x6D),
    "decimal": ("Decimal", 0x6E),
    "divide": ("Divide", 0x6F),
}
NATIVE_KEY_NAMES = {key: native for key, (native, _) in PUNCTUATION_KEYS.items()}
NATIVE_KEY_NAMES.update({str(number): f"D{number}" for number in range(10)})
KEY_ALIASES = {native.casefold(): key for key, native in NATIVE_KEY_NAMES.items()}
KEY_ALIASES.update({
    "oem1": ";", "oem2": "/", "oem3": "`", "oem4": "[", "oem5": "\\",
    "oem6": "]", "oem7": "'", "comma": ",", "equals": "=", "minus": "-",
    "semicolon": ";", "period": ".", "slash": "/", "backslash": "\\",
    "backquote": "`", "quote": "'", "bracketleft": "[", "bracketright": "]",
})
KEY_LABELS = {key: ("ISO key" if key == "oem102" else "Numpad " + key.title()
                   if key.isalpha() else key) for key in PUNCTUATION_KEYS}


def normalize_key_name(value: str) -> str:
    value = value.strip().casefold()
    return KEY_ALIASES.get(value, value)
