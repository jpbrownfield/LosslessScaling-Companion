import importlib.util
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from companion.core.models import Profile, ReshadeConfig


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "evaluate_graphics_stack.py"
SPEC = importlib.util.spec_from_file_location("evaluate_graphics_stack", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class GraphicsStackEvaluatorTests(unittest.TestCase):
    def test_builds_baseline_and_all_seven_combinations(self):
        profile = Profile(name="Base")
        names = [name for name, _profile in MODULE.build_cases(profile)]
        self.assertEqual(
            names,
            [
                "baseline",
                "lsp",
                "reshade",
                "special-k",
                "lsp+reshade",
                "lsp+special-k",
                "reshade+special-k",
                "lsp+reshade+special-k",
            ],
        )

    def test_cases_isolate_only_the_requested_components(self):
        base = Profile(name="Base", reshade=ReshadeConfig(
            enabled=True, managed_profile_id="hdr"
        ))
        cases = dict(MODULE.build_cases(base))
        combined = cases["lsp+reshade"]
        self.assertEqual(combined.graphics.neural_render.implementation, "lsp_neural_render")
        self.assertTrue(combined.graphics.lossless_proxy.enabled)
        self.assertTrue(combined.graphics.neural_render.package.enabled)
        self.assertTrue(combined.graphics.reshade.enabled)
        self.assertFalse(combined.graphics.special_k.enabled)
        self.assertEqual(combined.dll_overrides, [])
        self.assertEqual(combined.reshade.managed_profile_id, "hdr")
        self.assertIsNone(cases["baseline"].reshade)

    def test_selects_an_explicit_subset_in_requested_order(self):
        cases = MODULE.build_cases(Profile(name="Base"))
        selected = MODULE.select_cases(cases, "lsp+reshade,baseline")
        self.assertEqual([name for name, _profile in selected], ["lsp+reshade", "baseline"])

    def test_evidence_reports_modules_and_diagnostic_markers(self):
        evidence = MODULE.analyze_evidence(
            [
                {"relative_path": "addons/LSP-NeuralRender/LSP_NeuralRender.dll"},
                {"relative_path": "ReShade.ini"},
            ],
            [{"modules": [r"C:\LS\LSP_NeuralRender.dll"], "module_error": None}],
            {r"C:\LS\LSP_NeuralRender.log": "running\nengine FAILED: test\n"},
        )
        self.assertEqual(evidence["observed_expected_modules"], ["lsp_neuralrender.dll"])
        self.assertEqual(evidence["unobserved_expected_modules"], [])
        self.assertEqual(len(evidence["diagnostic_log_markers"]), 1)

    def test_choose_profile_defaults_to_active_profile(self):
        first = Profile(id="first", name="First")
        active = Profile(id="active", name="Active")
        manager = Mock()
        manager.config.profiles = [first, active]
        manager.config.active_profile_id = active.id
        manager.get_profile_by_id.side_effect = lambda value: {
            first.id: first,
            active.id: active,
        }.get(value)
        with patch("builtins.input", return_value=""):
            selected = MODULE.choose_profile(manager, None)
        self.assertIs(selected, active)

    def test_no_arguments_execute_by_default(self):
        args = MODULE.parse_args([])
        self.assertFalse(args.preflight_only)
        self.assertIsNone(args.profile_id)


if __name__ == "__main__":
    unittest.main()
