import unittest
from unittest.mock import patch

from companion.ui.dashboard_window import close_dashboard_window, open_dashboard_window


class DashboardWindowTests(unittest.TestCase):
    @patch("companion.ui.dashboard_window.subprocess.Popen")
    @patch("companion.ui.dashboard_window._focus_existing_dashboard", return_value=False)
    @patch("companion.ui.dashboard_window._find_app_browser", return_value="C:/Edge/msedge.exe")
    def test_opens_dashboard_in_app_mode(self, _find_browser, _focus, popen):
        url = "http://127.0.0.1:24892/dashboard"

        self.assertTrue(open_dashboard_window(url))

        args = popen.call_args.args[0]
        self.assertEqual(args[0], "C:/Edge/msedge.exe")
        self.assertIn(f"--app={url}", args)
        self.assertTrue(any(arg.startswith("--user-data-dir=") for arg in args))

    @patch("companion.ui.dashboard_window.subprocess.Popen")
    @patch("companion.ui.dashboard_window._focus_existing_dashboard", return_value=True)
    @patch("companion.ui.dashboard_window._find_app_browser", return_value="C:/Edge/msedge.exe")
    def test_focuses_existing_dashboard_instead_of_opening_another(self, _find_browser, _focus, popen):
        self.assertTrue(open_dashboard_window("http://127.0.0.1:24892/dashboard"))
        popen.assert_not_called()

    @patch("companion.ui.dashboard_window.webbrowser.open", return_value=True)
    @patch("companion.ui.dashboard_window._find_app_browser", return_value=None)
    def test_falls_back_to_default_browser(self, _find_browser, browser_open):
        url = "http://127.0.0.1:24892/dashboard"

        self.assertTrue(open_dashboard_window(url))

        browser_open.assert_called_once_with(url, new=1)

    @patch("companion.ui.dashboard_window.ctypes.windll.user32")
    @patch("companion.ui.dashboard_window._dashboard_window_handles", return_value=[101, 202])
    @patch("companion.ui.dashboard_window.os.name", "nt")
    def test_closes_all_dedicated_dashboard_windows(self, _handles, user32):
        self.assertTrue(close_dashboard_window())

        self.assertEqual(
            user32.PostMessageW.call_args_list,
            [unittest.mock.call(101, 0x0010, 0, 0), unittest.mock.call(202, 0x0010, 0, 0)],
        )

    @patch("companion.ui.dashboard_window._dashboard_window_handles", return_value=[])
    @patch("companion.ui.dashboard_window.os.name", "nt")
    def test_close_returns_false_when_dashboard_is_not_open(self, _handles):
        self.assertFalse(close_dashboard_window())


if __name__ == "__main__":
    unittest.main()
