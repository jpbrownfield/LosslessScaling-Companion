import json
import re
import unittest
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "companion" / "ui" / "dashboard.html"
FIXTURE = ROOT / "tests" / "fixtures" / "lossless_scaling" / "Settings.xml"
SCHEMA_322 = ROOT / "tests" / "fixtures" / "lossless_scaling" / "3.2.2-schema.json"


class NativeProfileEditorContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = DASHBOARD.read_text(encoding="utf-8")

    def test_dashboard_ids_and_native_setting_keys_are_unique(self):
        ids = re.findall(r'\bid="([^"]+)"', self.html)
        native_keys = re.findall(r'data-native-key="([^"]+)"', self.html)

        self.assertEqual([key for key, count in Counter(ids).items() if count > 1], [])
        self.assertEqual(
            [key for key, count in Counter(native_keys).items() if count > 1], []
        )

    def test_normalized_native_fixture_fields_have_editor_mappings(self):
        profile = ET.parse(FIXTURE).getroot().find("./GameProfiles/Profile")
        fixture_keys = {
            child.tag for child in profile
            if child.tag not in {"Title", "Path", "AutoScale"}
        }
        native_keys = set(re.findall(r'data-native-key="([^"]+)"', self.html))
        aliases = {
            alias.strip()
            for value in re.findall(r'data-native-aliases="([^"]+)"', self.html)
            for alias in value.split(",")
            if alias.strip()
        }

        self.assertEqual(fixture_keys - native_keys - aliases, set())

    def test_hidden_and_version_specific_controls_fail_closed(self):
        self.assertIn("[hidden] { display: none !important; }", self.html)
        self.assertIn("control.dataset.nativeAvailable = String(available)", self.html)
        self.assertIn("if (control.dataset.nativeAvailable !== 'true') return", self.html)
        self.assertIn("input.readOnly = true", self.html)

    def test_lossless_scaling_322_fields_values_and_ranges_are_wired(self):
        schema = json.loads(SCHEMA_322.read_text(encoding="utf-8"))
        for key, contract in schema["fields"].items():
            match = re.search(
                rf'<(?:input|select)[^>]*data-native-key="{re.escape(key)}"[^>]*>(.*?)</select>|'
                rf'<input[^>]*data-native-key="{re.escape(key)}"[^>]*>',
                self.html,
                flags=re.DOTALL,
            )
            self.assertIsNotNone(match, f"Missing Lossless Scaling 3.2.2 field {key}")
            markup = match.group(0)
            for value in contract.get("values", []):
                self.assertIn(f'value="{value}"', markup, f"{key} is missing {value}")
            for attribute in ("min", "max", "step"):
                if attribute in contract:
                    self.assertIn(
                        f'{attribute}="{contract[attribute]}"', markup,
                        f"{key} has the wrong {attribute}",
                    )

    def test_current_lsfg_fields_keep_legacy_aliases(self):
        self.assertRegex(
            self.html,
            r'data-native-key="LSFGFlowScale" data-native-aliases="[^"]*LSFGResolutionScale',
        )
        self.assertRegex(
            self.html,
            r'data-native-key="LSFGSize" data-native-aliases="[^"]*LSFGType',
        )


if __name__ == "__main__":
    unittest.main()
