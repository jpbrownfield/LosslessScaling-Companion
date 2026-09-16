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
            )
            runner = DiagnosticRunner(server)
            runner._tests = lambda: (
                ("first", "First", lambda: runner._result("pass", "worked")),
                ("second", "Second", lambda: runner._result("warn", "inspect")),
            )

            result = runner.run()

            archive = Path(result["archivePath"])
            self.assertTrue(archive.is_file())
            self.assertEqual(result["counts"], {"pass": 1, "warn": 1, "fail": 0})
            with zipfile.ZipFile(archive) as bundle:
                names = set(bundle.namelist())
                self.assertIn("ls-companion-diagnostics/first.log", names)
                self.assertIn("ls-companion-diagnostics/second.log", names)
                summary = json.loads(bundle.read("ls-companion-diagnostics/summary.json"))
            self.assertEqual([item["id"] for item in summary["results"]], ["first", "second"])


if __name__ == "__main__":
    unittest.main()
