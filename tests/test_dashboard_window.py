import io
import json
import queue
import subprocess
import threading
import unittest
from unittest.mock import Mock, patch

from companion.ui import dashboard_window as dashboard
from companion.ui.dashboard_host import _dashboard_url, _read_commands


class DashboardWindowTests(unittest.TestCase):
    def setUp(self):
        self.host_patch = patch.object(dashboard, "_host", None)
        self.host_patch.start()
        self.addCleanup(self.host_patch.stop)

    @patch.object(dashboard, "DashboardHost")
    def test_repeated_and_concurrent_opens_reuse_the_host(self, host_class):
        host_class.return_value.send.return_value = True
        threads = [threading.Thread(target=dashboard.open_dashboard_window,
                                    args=("http://127.0.0.1:24892/dashboard",)) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)
        host_class.assert_called_once()
        self.assertEqual(host_class.return_value.send.call_count, 8)

    @patch.object(dashboard, "DashboardHost")
    def test_close_and_reopen_launches_new_host(self, host_class):
        first, second = Mock(), Mock()
        host_class.side_effect = [first, second]
        dashboard.open_dashboard_window("http://127.0.0.1:24892/dashboard")
        self.assertTrue(dashboard.close_dashboard_window())
        first.close.assert_called_once()
        self.assertFalse(dashboard.close_dashboard_window())
        dashboard.open_dashboard_window("http://127.0.0.1:24892/dashboard?view=settings")
        second.send.assert_called_once_with({"command": "open", "url": "http://127.0.0.1:24892/dashboard?view=settings"})

    @patch.object(dashboard, "DashboardHost")
    def test_dead_host_is_replaced(self, host_class):
        old = Mock()
        old.send.return_value = False
        dashboard._host = old
        self.assertTrue(dashboard.open_dashboard_window("http://127.0.0.1:24892/dashboard"))
        old.close.assert_called_once()
        host_class.assert_called_once()

    @patch.object(dashboard, "threading")
    @patch.object(dashboard.subprocess, "Popen")
    def test_host_uses_owned_pipes_and_sends_url_outside_command_line(self, popen, _threading):
        popen.return_value.poll.return_value = None
        host = dashboard.DashboardHost()
        self.assertIn("--dashboard-host", popen.call_args.args[0])
        self.assertTrue(popen.call_args.kwargs["close_fds"])
        host.send({"command": "open", "url": "http://127.0.0.1:24892/dashboard"})
        popen.return_value.stdin.flush.assert_called_once()
        host.close()
        popen.return_value.stdin.close.assert_called_once()

    def test_lifetime_pipe_eof_exits_even_without_a_gui(self):
        commands = queue.Queue()
        exited = Mock()
        _read_commands(io.StringIO(json.dumps({"command": "open", "url": "local"}) + "\n"), commands, exited)
        self.assertEqual(commands.get_nowait()["command"], "open")
        exited.assert_called_once_with(0)

    @patch.object(dashboard, "threading")
    @patch.object(dashboard.subprocess, "Popen")
    def test_update_close_waits_for_host_and_kills_it_if_it_hangs(self, popen, threads):
        host = dashboard.DashboardHost()
        dashboard._host = host
        popen.return_value.wait.side_effect = [subprocess.TimeoutExpired("dashboard", 3), 0]
        threads.Thread.reset_mock()

        self.assertTrue(dashboard.close_dashboard_window(wait=True))

        popen.return_value.stdin.close.assert_called_once_with()
        self.assertEqual(popen.return_value.wait.call_args_list[0].kwargs, {"timeout": 3})
        popen.return_value.kill.assert_called_once_with()
        self.assertEqual(popen.return_value.wait.call_count, 2)
        threads.Thread.assert_not_called()
        self.assertIsNone(dashboard._host)

    def test_accepts_only_local_dashboard(self):
        _dashboard_url("http://127.0.0.1:24892/dashboard?view=settings")
        for url in ["https://example.com/dashboard", "file:///dashboard", "http://127.0.0.1:24892/other"]:
            with self.assertRaises(ValueError):
                _dashboard_url(url)

    @patch.object(dashboard, "dashboard_window_handle", return_value=101)
    @patch.object(dashboard.ctypes.windll, "user32")
    def test_maps_css_rect_to_native_client_pixels(self, user32, _handle):
        def fill(_hwnd, pointer):
            pointer._obj.right, pointer._obj.bottom = 1200, 900
            return 1
        user32.GetClientRect.side_effect = fill
        self.assertEqual(dashboard.dashboard_css_rect_to_client_pixels({
            "left": 100, "top": 50, "width": 400, "height": 300,
            "viewportWidth": 1000, "viewportHeight": 750,
        }), (101, 120, 60, 480, 360))
