import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from companion.services.diagnostic_runner import DiagnosticRunner


class DiagnosticRunnerTests(unittest.TestCase):
    def test_isolated_component_helpers_execute_production_code(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = DiagnosticRunner.__new__(DiagnosticRunner)
            runner.root = Path(directory)
            runner._behavior_root = runner.root / "behavior"
            runner._behavior_root.mkdir()

            results = [
                runner._reshade_move_behavior(),
                runner._deployment_behavior(),
                runner._benchmark_cli_behavior(),
            ]

            self.assertEqual([result["status"] for result in results], ["pass"] * 3)

    def test_live_scaling_results_run_once_and_are_returned_individually(self):
        runner = DiagnosticRunner.__new__(DiagnosticRunner)
        runner._live_scaling_results = None
        expected = {
            key: runner._result("pass", key)
            for key in ("autoscale", "hotkey_override", "lossless_log", "reshade_runtime")
        }
        runner._run_live_scaling_workflow = Mock(return_value=expected)

        self.assertIs(runner._autoscale_behavior(), expected["autoscale"])
        self.assertIs(runner._hotkey_override_behavior(), expected["hotkey_override"])
        self.assertIs(runner._lossless_scaling_log_behavior(), expected["lossless_log"])
        self.assertIs(runner._reshade_runtime_behavior(), expected["reshade_runtime"])
        runner._run_live_scaling_workflow.assert_called_once_with()

    def test_run_writes_one_log_per_test_and_downloadable_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory) / "config"
            server = SimpleNamespace(
                profile_manager=SimpleNamespace(config_dir=config_dir),
                state=SimpleNamespace(simulation_mode=True),
                process_watcher=None,
            )
            runner = DiagnosticRunner(server)
            runner._tests = lambda: (
                ("first", "First", lambda: runner._result("pass", "worked")),
                ("second", "Second", lambda: runner._result("warn", "inspect")),
            )
            runner._reset_lossless_folder = lambda _running: runner._result("pass", "reset")

            result = runner.run()

            archive = Path(result["archivePath"])
            self.assertTrue(archive.is_file())
            self.assertEqual(result["counts"], {"pass": 2, "warn": 1, "fail": 0})
            with zipfile.ZipFile(archive) as bundle:
                names = set(bundle.namelist())
                self.assertIn("ls-companion-diagnostics/first.log", names)
                self.assertIn("ls-companion-diagnostics/second.log", names)
                self.assertIn("ls-companion-diagnostics/environment_reset.log", names)
                summary = json.loads(bundle.read("ls-companion-diagnostics/summary.json"))
            self.assertEqual(
                [item["id"] for item in summary["results"]],
                ["first", "second", "environment_reset"],
            )

    def test_individual_execution_plan_includes_dependencies_first(self):
        runner = DiagnosticRunner.__new__(DiagnosticRunner)
        runner._tests = lambda: (
            ("configuration", "Configuration", lambda: {}),
            ("profile_integrity", "Profiles", lambda: {}),
            ("profile_matching", "Matching", lambda: {}),
        )

        plan = runner._execution_plan("profile_matching")

        self.assertEqual([item[0] for item in plan], [
            "configuration", "profile_integrity", "profile_matching",
        ])

    def test_log_delta_reads_restarted_truncated_log_from_the_beginning(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "LosslessProxy.log"
            path.write_text("old output that is considerably longer", encoding="utf-8")
            snapshot = DiagnosticRunner._log_snapshot([path])
            path.write_text("new output", encoding="utf-8")

            delta = DiagnosticRunner._log_delta(snapshot, [path])

            self.assertEqual(delta[str(path)], "new output")

    def test_runtime_log_errors_ignores_ngx_fallback_warnings_after_ready(self):
        errors = DiagnosticRunner._runtime_log_errors({
            "LSP-NeuralRender.log": (
                "error: failed to load NGXCore: 126 (_nvngx.dll)\n"
                "nvLoadSignedLibraryW() failed: missing or corrupted\n"
                "NrEngine ready (float slot 6)\n"
            )
        })

        self.assertEqual(errors, [])

    def test_runtime_log_errors_reports_explicit_neural_engine_failure(self):
        errors = DiagnosticRunner._runtime_log_errors({
            "LSP-NeuralRender.log": "NrEngine FAILED: CreateFeature(18): FeatureNotSupported\n"
        })

        self.assertEqual(len(errors), 1)
        self.assertIn("FeatureNotSupported", errors[0])

    def test_runtime_log_evidence_requires_model_work_beyond_engine_ready(self):
        ready = DiagnosticRunner._runtime_log_evidence({
            "LSP-NeuralRender.log": "NrEngine ready (float slot 6)\nticks 0, taps 0\n"
        })
        exercised = DiagnosticRunner._runtime_log_evidence({
            "LSP-NeuralRender.log": "NrEngine ready (float slot 6)\nPrepare: frame 1280x720\ntaps 1\n"
        })

        self.assertTrue(ready["neuralEngineReady"])
        self.assertFalse(ready["neuralModelPrepared"])
        self.assertFalse(ready["neuralTapObserved"])
        self.assertTrue(exercised["neuralModelPrepared"])
        self.assertTrue(exercised["neuralTapObserved"])


if __name__ == "__main__":
    unittest.main()
