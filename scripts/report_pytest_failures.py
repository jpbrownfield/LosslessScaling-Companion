"""Expose pytest JUnit failures as GitHub Actions annotations."""
from __future__ import annotations

from pathlib import Path
import sys
import xml.etree.ElementTree as ET


def escape(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def report(path: Path) -> None:
    if not path.is_file():
        print("::error title=Python tests::pytest failed before creating its JUnit report.")
        return
    root = ET.parse(path).getroot()
    for case in root.iter("testcase"):
        for result in case:
            if result.tag not in {"failure", "error"}:
                continue
            title = f"pytest: {case.get('classname', '')}.{case.get('name', '')}"
            title = escape(title).replace(",", "%2C").replace(":", "%3A")
            detail = result.text or result.get("message", "Python test failed")
            print(f"::error title={title}::{escape(detail[-16000:])}")


if __name__ == "__main__":
    report(Path(sys.argv[1]))
