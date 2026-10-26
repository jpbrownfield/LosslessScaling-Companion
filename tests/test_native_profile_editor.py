import json
import re
import unittest
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "companion" / "ui" / "dashboard.html"
FIXTURE = ROOT / "tests" / "fixtures" / "lossless_scaling" / "Settings.xml"
FIXTURE_322 = ROOT / "tests" / "fixtures" / "lossless_scaling" / "Settings-3.2.2.xml"
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

    def test_echo_upscalers_are_distinct_from_native_fsr_and_save_as_nis(self):
        self.assertIn('<option value="FSR">FSR (LS built-in)</option>', self.html)
        self.assertIn('<option value="EchoDLSS">DLSS (Echo add-on)</option>', self.html)
        self.assertIn('<option value="EchoFSR">FSR (Echo add-on)</option>', self.html)
        self.assertIn("values.ScalingType = serializedScalingType(values.ScalingType)", self.html)
        self.assertIn("if (addonScalingSavePending) { scheduleProfileAutosave(0); return; }", self.html)
        self.assertIn("activeProfileSaveAddonGeneration = addonGeneration", self.html)

    def test_normalized_native_fixture_fields_have_editor_mappings(self):
        fixture_keys = set()
        for fixture in (FIXTURE, FIXTURE_322):
            for profile in ET.parse(fixture).getroot().findall("./GameProfiles/Profile"):
                fixture_keys.update(
                    child.tag for child in profile
                    if child.tag not in {"Title", "Path", "AutoScale"}
                )
        native_keys = set(re.findall(r'data-native-key="([^"]+)"', self.html))
        aliases = {
            alias.strip()
            for value in re.findall(r'data-native-aliases="([^"]+)"', self.html)
            for alias in value.split(",")
            if alias.strip()
        }

        self.assertEqual(fixture_keys - native_keys - aliases, set())

    def test_lossless_scaling_322_serialized_select_values_are_options(self):
        profiles = ET.parse(FIXTURE_322).getroot().findall("./GameProfiles/Profile")
        observed = {}
        for profile in profiles:
            for child in profile:
                observed.setdefault(child.tag, set()).add(child.text or "")
        for key, values in observed.items():
            if key in {"PreferredGpuId", "OutputDisplayId"}:
                continue
            match = re.search(
                rf'<select[^>]*data-native-key="{re.escape(key)}"[^>]*>(.*?)</select>',
                self.html,
                flags=re.DOTALL,
            )
            if match is None:
                continue
            for value in values:
                self.assertIn(
                    f'value="{value}"', match.group(0),
                    f"{key} is missing serialized Lossless Scaling value {value!r}",
                )

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

    def test_advanced_and_package_maintenance_controls_are_not_shown(self):
        self.assertIn(
            'id="nativeAdvancedSettingsGroup" class="settings-group profile-settings-nested" hidden',
            self.html,
        )
        self.assertNotIn("Special K Version</label>", self.html)
        self.assertNotIn("Special K Updates</label>", self.html)
        self.assertNotIn("ReShade Version</label>", self.html)
        self.assertNotIn("ReShade Updates</label>", self.html)
        self.assertNotIn("LosslessProxy Version</label>", self.html)
        self.assertNotIn("LosslessProxy Updates</label>", self.html)

    def test_hidden_special_k_package_choices_are_preserved_on_save(self):
        self.assertIn("version: existingSpecialK.version || null", self.html)
        self.assertIn("channel: existingSpecialK.channel || 'stable'", self.html)
        self.assertIn(
            "update_policy: existingSpecialK.update_policy || 'notify'",
            self.html,
        )
        self.assertIn("version: existingLosslessProxy.version || null", self.html)
        self.assertIn("channel: existingLosslessProxy.channel || 'stable'", self.html)
        self.assertIn(
            "update_policy: existingLosslessProxy.update_policy || 'notify'",
            self.html,
        )

    def test_native_echo_panel_and_reshade_passthrough_are_wired(self):
        self.assertIn('id="addonEditorCards"', self.html)
        self.assertIn("function renderAddonEditor()", self.html)
        self.assertIn("type: 'SAVE_ADDON_CONFIG'", self.html)
        self.assertNotIn("type: 'EMBED_ADDON_MENU'", self.html)
        self.assertNotIn('id="echoConfigJson"', self.html)
        self.assertNotIn("function renderAddonConfigEditor()", self.html)
        self.assertIn("addonConfig.addons.ReShadePassthrough._enabled = true", self.html)
        self.assertNotIn('id="reshadeMenuProxyEnabled"', self.html)
        self.assertNotIn('id="losslessProxyEnabled"', self.html)
        self.assertNotIn('id="echoDlssNrEnabled"', self.html)
        self.assertNotIn('id="echoReshadePassthroughEnabled"', self.html)
        self.assertIn("Echo's passthrough is enabled automatically", self.html)
        self.assertNotIn("values.HdrSupport = 'false'", self.html)
        self.assertIn(
            "enabled: reshadeEnabled",
            self.html,
        )
        self.assertIn("implementation: dlss5Enabled ? 'lsp_neural_render' : 'disabled'", self.html)

    def test_collapsible_profile_headers_match_other_settings_groups(self):
        self.assertEqual(self.html.count('class="settings-group profile-settings-group" open>'), 2)
        self.assertIn(
            '<summary>Lossless Scaling Profile Settings <span class="chevron" aria-hidden="true">&#8250;</span></summary>',
            self.html,
        )
        self.assertIn(
            '<summary>Echo Add-on Manager Configuration <span class="chevron" aria-hidden="true">&#8250;</span></summary>',
            self.html,
        )
        self.assertIn('.settings-group > summary { display: flex; align-items: center;', self.html)
        self.assertIn('details[open] > summary .chevron { transform: rotate(90deg); }', self.html)
        self.assertNotIn('.native-settings-group > summary', self.html)

    def test_legacy_neural_render_profile_controls_are_removed(self):
        self.assertNotIn('id="dlss5Enabled"', self.html)
        self.assertNotIn('id="neuralRenderFields"', self.html)
        self.assertNotIn('name="neuralSamplingPreset"', self.html)
        self.assertNotIn("function renderNeuralRenderSettings()", self.html)
        self.assertNotIn("selectedNeuralSamplingPreset", self.html)

    def test_special_k_is_a_separate_profile_section_and_rtx_hdr_toggle_is_removed(self):
        echo_editor = self.html.index('id="addonEditorCards"')
        special_k = self.html.index('id="specialKSettings"', echo_editor)
        self.assertGreater(special_k, echo_editor)
        self.assertIn("Special K is independent of Echo LS Add-on Manager", self.html)
        self.assertGreaterEqual(self.html.count('class="profile-addon-section'), 1)
        self.assertNotIn('id="nvidiaRtxHdrEnabled"', self.html)
        self.assertNotIn("NVIDIA RTX HDR for Lossless Scaling", self.html)
        self.assertIn("nvidiaRtxHdrEnabled: false", self.html)

    def test_profile_reshade_selector_can_create_and_select_a_new_profile(self):
        self.assertIn('id="newProfileReshadeBtn"', self.html)
        self.assertIn("pendingProfileReshadeCreate = true", self.html)
        self.assertIn("renderReshadeProfileChoices(msg.profile.id)", self.html)

    def test_profile_gpu_choices_use_friendly_inventory_and_global_inheritance(self):
        self.assertIn("function renderNativeGpuChoices()", self.html)
        self.assertIn("Follow General Settings (${globalGpu})", self.html)
        self.assertIn("`${gpu.lsGpuId} — ${gpu.name}", self.html)
        self.assertIn("`${display.lsDisplayId} — ${display.name}", self.html)

    def test_auto_scale_is_highlighted_and_hidden_for_native_default(self):
        self.assertIn('id="profileAutoScaleControl" class="profile-auto-scale"', self.html)
        self.assertIn('>Auto-Scale</label>', self.html)
        self.assertIn(
            "document.getElementById('profileAutoScaleControl').hidden = nativeDefaultAlias",
            self.html,
        )
        self.assertIn("border: 1px solid rgba(56,189,248,.58)", self.html)

    def test_integrations_are_grouped_with_addons_and_benchmark_results_are_visible(self):
        addons_start = self.html.index('id="losslessAddonsPanel"')
        benchmark_start = self.html.index('id="performanceBenchmarkPanel"')
        addon_markup = self.html[addons_start:benchmark_start]
        self.assertIn('id="processLassoPerformanceModeScaling"', addon_markup)
        self.assertIn('id="rtssFrameLimitingEnabled"', addon_markup)
        self.assertIn('id="performanceBenchmarkResults"', self.html)
        self.assertIn('id="benchmarkAverageFps"', self.html)
        self.assertIn('id="benchmarkAverageLatency"', self.html)

    def test_external_runtime_downloads_auto_import_without_a_second_button(self):
        self.assertNotIn("data-addon-import=", self.html)
        self.assertNotIn("Import downloaded runtime", self.html)
        self.assertNotIn("Import installed runtime", self.html)
        self.assertIn("const addonAutoImportInFlight = new Set()", self.html)
        self.assertIn("Installing automatically", self.html)

    def test_collapsible_box_titles_match_addon_title_size_with_compact_divider_spacing(self):
        self.assertIn(
            ".settings-group > summary { display: flex; align-items: center; gap: 10px; "
            "padding: 9px 14px; color: var(--text); font-size: 15px;",
            self.html,
        )
        self.assertIn(".addon-card h3 { color: var(--text); font-size: 15px;", self.html)


if __name__ == "__main__":
    unittest.main()
