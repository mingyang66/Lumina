"""System tray error reporting regressions."""
import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest.mock import Mock

from lumina.system_tray import TrayIcon, _best_effort_taskbar_filter


class TrayIconTests(unittest.TestCase):
    def test_run_failure_prints_full_traceback(self):
        tray = TrayIcon(None)
        tray._icon = Mock()
        tray._icon.run.side_effect = OSError(5, "Access is denied")
        stdout, stderr = io.StringIO(), io.StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            tray._run()

        self.assertIn("[lumina] tray stopped: [Errno 5] Access is denied", stdout.getvalue())
        self.assertIn("Traceback (most recent call last)", stderr.getvalue())
        self.assertIn("OSError: [Errno 5] Access is denied", stderr.getvalue())
        tray._icon.run.assert_called_once_with()

    def test_taskbar_filter_access_denied_is_nonfatal(self):
        def denied(*_args):
            error = PermissionError(5, "Access is denied")
            error.winerror = 5
            raise error

        original = denied
        api = SimpleNamespace(ChangeWindowMessageFilterEx=original,
                              WM_TASKBARCREATED=0xC123, MSGFLT_ALLOW=1)
        restore = _best_effort_taskbar_filter(api)
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            result = api.ChangeWindowMessageFilterEx(1, api.WM_TASKBARCREATED,
                                                     api.MSGFLT_ALLOW, None)
            api.ChangeWindowMessageFilterEx(2, api.WM_TASKBARCREATED,
                                            api.MSGFLT_ALLOW, None)
        self.assertTrue(result)
        self.assertIn("without automatic recovery", stdout.getvalue())
        self.assertEqual(stdout.getvalue().count("tray warning"), 1)
        restore()
        self.assertIs(api.ChangeWindowMessageFilterEx, original)

    def test_taskbar_filter_does_not_hide_other_errors(self):
        def denied(*_args):
            error = PermissionError(5, "Access is denied")
            error.winerror = 5
            raise error

        api = SimpleNamespace(ChangeWindowMessageFilterEx=denied,
                              WM_TASKBARCREATED=0xC123, MSGFLT_ALLOW=1)
        _best_effort_taskbar_filter(api)
        with self.assertRaises(PermissionError):
            api.ChangeWindowMessageFilterEx(1, 123, api.MSGFLT_ALLOW, None)


if __name__ == "__main__":
    unittest.main()
