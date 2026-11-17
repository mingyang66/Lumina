"""System tray error reporting regressions.

诊断信息从 ``print`` 改为 logging 之后，这里断言的对象也跟着变：不再看 stdout
（pythonw 下根本没有 stdout，这正是要修的 bug），而是抓 ``lumina.system_tray``
的日志记录，并检查格式化后的文本里确实带着完整堆栈。
"""
import logging
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from lumina.system_tray import TrayIcon, _best_effort_taskbar_filter


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(self.format(record))

    def text(self):
        return "\n".join(self.records)


class _captured_logs:
    """临时把某个 logger 的记录收集起来，退出时还原级别与 handler。"""

    def __init__(self, name):
        self.handler = _Capture()
        self.handler.setFormatter(logging.Formatter("%(message)s"))
        self.logger = logging.getLogger(name)
        self.name = name

    def __enter__(self):
        self.previous_level = self.logger.level
        self.logger.setLevel(logging.DEBUG)
        self.logger.addHandler(self.handler)
        return self.handler

    def __exit__(self, *_exc):
        self.logger.removeHandler(self.handler)
        self.logger.setLevel(self.previous_level)
        return False


class TrayIconTests(unittest.TestCase):
    def test_run_failure_logs_full_traceback(self):
        tray = TrayIcon(None)
        tray._icon = Mock()
        tray._icon.run.side_effect = OSError(5, "Access is denied")

        with _captured_logs("lumina.system_tray") as captured:
            tray._run()

        text = captured.text()
        self.assertIn("tray stopped: [Errno 5] Access is denied", text)
        self.assertIn("Traceback (most recent call last)", text)
        self.assertIn("OSError: [Errno 5] Access is denied", text)
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
        with _captured_logs("lumina.system_tray") as captured:
            result = api.ChangeWindowMessageFilterEx(1, api.WM_TASKBARCREATED,
                                                     api.MSGFLT_ALLOW, None)
            api.ChangeWindowMessageFilterEx(2, api.WM_TASKBARCREATED,
                                            api.MSGFLT_ALLOW, None)
        self.assertTrue(result)
        text = captured.text()
        self.assertIn("without automatic recovery", text)
        self.assertEqual(text.count("tray warning"), 1)
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
