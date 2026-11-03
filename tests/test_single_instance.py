import unittest
import json
import os
import uuid
from pathlib import Path
import tempfile
from unittest.mock import patch

from companion.services.single_instance import SingleInstanceGuard


def _unique(prefix="test"):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


class SingleInstanceGuardTests(unittest.TestCase):
    def test_dashboard_request_is_pid_bound_and_consumed_once(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "companion.services.single_instance._lock_file_path", return_value=Path(directory) / "companion.lock"
        ):
            owner = SingleInstanceGuard("activation-test")
            self.assertTrue(owner.acquire())
            try:
                requester = SingleInstanceGuard("activation-test")
                requester.holder_pid = os.getpid()
                self.assertTrue(requester.request_dashboard())
                self.assertTrue(owner.consume_dashboard_request())
                self.assertFalse(owner.consume_dashboard_request())
                path = Path(directory) / "companion.activate"
                path.write_text(json.dumps({"pid": -1}), encoding="utf-8")
                self.assertFalse(owner.consume_dashboard_request())
            finally:
                owner.release()

    def test_second_acquire_fails_while_first_is_held(self):
        name = _unique("hold")
        first = SingleInstanceGuard(name)
        self.assertTrue(first.acquire())
        try:
            second = SingleInstanceGuard(name)
            self.assertFalse(second.acquire())
            self.assertIsNotNone(second.holder_pid)
        finally:
            first.release()

    def test_acquire_succeeds_after_release(self):
        name = _unique("release")
        guard = SingleInstanceGuard(name)
        self.assertTrue(guard.acquire())
        guard.release()
        again = SingleInstanceGuard(name)
        self.assertTrue(again.acquire())
        again.release()

    def test_simulation_and_production_use_separate_guards(self):
        production = SingleInstanceGuard("companion")
        simulation = SingleInstanceGuard("simulation-test")
        self.assertNotEqual(production.mutex_name, simulation.mutex_name)
        if not production.acquire():
            self.skipTest("LS Companion is already running; production mutex is held")
        try:
            self.assertTrue(simulation.acquire())
            simulation.release()
        finally:
            production.release()


if __name__ == "__main__":
    unittest.main()
