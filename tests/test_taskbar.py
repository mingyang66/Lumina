"""任务栏 API 的投递契约；原生崩溃验证见 test_taskbar_native。"""
import ctypes
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from lumina import taskbar


class TaskbarTests(unittest.TestCase):
    def setUp(self):
        self.user32 = SimpleNamespace(
            ShowWindow=Mock(side_effect=AssertionError("不能同步重入 Tk")),
            ShowWindowAsync=Mock(return_value=1), IsIconic=Mock(return_value=0))
        for item in (
            patch.object(taskbar.os, "name", "nt"),
            patch.object(ctypes, "windll", SimpleNamespace(user32=self.user32), create=True),
            patch.object(taskbar, "top_level_hwnd", return_value=1 << 40),
            patch.object(taskbar, "claim_taskbar", return_value=True),
        ):
            item.start()
            self.addCleanup(item.stop)
        self.window = Mock()

    def test_all_visibility_commands_use_async_native_signature(self):
        for operation, args, expected in (
            (taskbar.minimize_window, (), taskbar.SW_MINIMIZE),
            (taskbar.hide_window, (), taskbar.SW_HIDE),
            (taskbar.restore_window, (), taskbar.SW_RESTORE),
            (taskbar.show_window, (), taskbar.SW_SHOW),
            (taskbar.show_window, (False,), 8),
        ):
            with self.subTest(operation=operation.__name__, args=args):
                self.user32.ShowWindowAsync.reset_mock()
                self.assertTrue(operation(self.window, *args))
                call = self.user32.ShowWindowAsync.call_args.args
                self.assertEqual(call[0].value, 1 << 40)
                self.assertEqual(call[1], expected)
                self.assertEqual(self.user32.ShowWindowAsync.argtypes,
                                 [ctypes.c_void_p, ctypes.c_int])
                self.assertIs(self.user32.ShowWindowAsync.restype, ctypes.c_bool)
        self.user32.ShowWindow.assert_not_called()
        self.window.overrideredirect.assert_not_called()

    def test_show_restores_iconic_window_even_without_activation(self):
        self.user32.IsIconic.return_value = 1
        self.assertTrue(taskbar.show_window(self.window, activate=False))
        self.assertEqual(self.user32.ShowWindowAsync.call_args.args[1], taskbar.SW_RESTORE)

    def test_native_post_failure_is_not_reported_as_success(self):
        self.user32.ShowWindowAsync.return_value = 0
        self.assertFalse(taskbar.minimize_window(self.window))

    def test_missing_hwnd_does_not_post(self):
        with patch.object(taskbar, "top_level_hwnd", return_value=None):
            self.assertFalse(taskbar.minimize_window(self.window))
        self.user32.ShowWindowAsync.assert_not_called()


if __name__ == "__main__":
    unittest.main()
