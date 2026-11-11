import io
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from companion.services.live_performance import FrameHistory, LivePerformanceGraph


class FrameHistoryTests(unittest.TestCase):
    def test_only_valid_target_frames_from_one_swapchain_are_plotted_and_history_is_bounded(self):
        history = FrameHistory(42)
        row = {"ProcessID": "42", "FrameTime": "16.6667", "SwapChainAddress": "0x1"}
        for _ in range(300):
            self.assertTrue(history.add(row, 100))
        self.assertEqual(len(history.values), 180)
        for invalid in ({**row, "ProcessID": "7"}, {**row, "FrameTime": "NaN"},
                        {**row, "FrameTime": "0"}, {**row, "SwapChainAddress": "0x2"}):
            self.assertFalse(history.add(invalid, 101))
        snapshot = history.snapshot(100.5)
        self.assertAlmostEqual(snapshot["fps"], 60, places=1)
        self.assertAlmostEqual(snapshot["frame_ms"], 16.67, places=2)
        self.assertIsNone(history.snapshot(103)["fps"])
        self.assertTrue(history.snapshot(103)["status"])

    def test_presentmon_legacy_columns_are_supported(self):
        history = FrameHistory(42)
        self.assertTrue(history.add({"ProcessID": "42", "MsBetweenPresents": "20"}, 100))
        self.assertEqual(history.snapshot(100)["fps"], 50)

    def test_missing_presentmon_does_not_start_a_capture(self):
        indicator = Mock()
        graph = LivePerformanceGraph(SimpleNamespace(scaling_target_pid=42), indicator, lambda: None)
        self.assertFalse(graph.toggle())
        indicator.log.assert_called_once()
        self.assertIsNone(graph._session)

    def test_capture_targets_application_and_cleans_up_only_its_unique_session(self):
        indicator = Mock()
        graph = LivePerformanceGraph(SimpleNamespace(scaling_target_pid=42), indicator, lambda: 'PresentMon.exe')
        process = Mock()
        process.stdout = io.StringIO('Application,ProcessID,FrameTime\nGame.exe,42,16.7\n')
        with patch('companion.services.live_performance.subprocess.Popen', return_value=process) as launch, \
             patch('companion.services.live_performance.subprocess.run') as stop:
            self.assertTrue(graph.toggle())
            graph.stop(wait=True)
        command = launch.call_args.args[0]
        self.assertEqual(command[command.index('--process_id') + 1], '42')
        self.assertIn('--output_stdout', command)
        cleanup = stop.call_args.args[0]
        self.assertEqual(cleanup[cleanup.index('--session_name') + 1], command[command.index('--session_name') + 1])
        self.assertIn('--terminate_existing_session', cleanup)
        self.assertTrue(command[command.index('--session_name') + 1].startswith('LSCompanionOverlay-'))
        self.assertIsNone(graph._session)
