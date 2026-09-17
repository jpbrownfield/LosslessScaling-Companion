import threading
import time
import unittest
from types import SimpleNamespace

from companion.core.models import HotkeyConfig
from companion.core.state import AppState
from companion.services.hotkey_listener import GlobalHotkeyListener


class GlobalHotkeyListenerTests(unittest.TestCase):
    def test_blocking_message_loop_refreshes_and_stops(self):
        manager = SimpleNamespace(config=SimpleNamespace(
            override_lossless_hotkey=False,
            override_hotkey=HotkeyConfig(activation_delay_ms=0),
        ))
        listener = GlobalHotkeyListener(manager, AppState(), lambda: None)

        listener.start()
        thread = listener._thread
        self.assertIsNotNone(listener._thread_id)
        self.assertTrue(thread and thread.is_alive())

        listener.refresh()
        listener.stop()

        self.assertFalse(thread and thread.is_alive())
        self.assertIsNone(listener._thread_id)

    def test_dispatch_coalesces_overlapping_hotkey_events(self):
        callback_started = threading.Event()
        allow_callback_to_finish = threading.Event()
        calls = []

        def callback():
            calls.append(True)
            callback_started.set()
            allow_callback_to_finish.wait(timeout=1)

        manager = SimpleNamespace(config=SimpleNamespace(
            override_lossless_hotkey=False,
            override_hotkey=HotkeyConfig(),
        ))
        listener = GlobalHotkeyListener(manager, AppState(), callback)

        listener._dispatch()
        self.assertTrue(callback_started.wait(timeout=1))
        listener._dispatch()
        allow_callback_to_finish.set()

        self.assertEqual(calls, [True])

    def test_dispatch_waits_for_override_keys_to_be_released(self):
        callback_called = threading.Event()
        manager = SimpleNamespace(config=SimpleNamespace(
            override_lossless_hotkey=True,
            override_hotkey=HotkeyConfig(
                modifiers=["alt"], key="l", activation_delay_ms=75
            ),
        ))
        listener = GlobalHotkeyListener(manager, AppState(), callback_called.set)

        started = time.monotonic()
        listener._dispatch()

        self.assertTrue(callback_called.wait(timeout=1))
        self.assertGreaterEqual(time.monotonic() - started, 0.06)


if __name__ == "__main__":
    unittest.main()
