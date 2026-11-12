"""用独立进程隔离真实 Tk/Win32 的致命 GIL 回归。"""
import os
import subprocess
import sys
import unittest
from pathlib import Path


def native_cycle(kind, rounds=12, repeats=6):
    import ctypes
    import tkinter as tk
    from types import SimpleNamespace

    from lumina import taskbar, vault_auth
    from lumina.database import Database
    from lumina.history_panel import HistoryPanel
    from lumina.ledger_window import LedgerWindow
    from lumina.vault_window import VaultWindow

    class FakePanel:
        FONT_FAMILY = "Microsoft YaHei UI"

        def __init__(self, root, db):
            self.tk = tk
            self.win = tk.Toplevel(root)
            self.win.withdraw()
            self.ui = SimpleNamespace(db=db, config={})
            self._vault_authenticated = True
            self._tool_windows = {}
            self.status_var = tk.StringVar(master=root)

        def _theme(self):
            return {key: value[1] for key, value in HistoryPanel.THEME.items()}, True

        def _sys(self, name):
            return HistoryPanel.SYS_COLORS.get(name, HistoryPanel.SYS_COLORS["gray"])[0]

        def _authenticate_vault(self, parent=None):
            self._vault_authenticated = True
            return True

    root = tk.Tk()
    root.withdraw()
    errors = []
    root.report_callback_exception = lambda *args: errors.append(str(args))
    root.tk.createcommand("bgerror", lambda message: errors.append(message))
    db = Database(":memory:")
    db.init_schema()
    panel = FakePanel(root, db)
    # 查询使用独立 DLL，避免项目内其他模块覆盖函数签名。
    user32 = ctypes.WinDLL("user32")
    user32.IsWindow.argtypes = [ctypes.c_void_p]
    user32.IsWindow.restype = ctypes.c_int
    user32.IsIconic.argtypes = [ctypes.c_void_p]
    user32.IsIconic.restype = ctypes.c_int
    cls = {"vault": VaultWindow, "ledger": LedgerWindow}[kind]
    print(f"START {kind} Python={sys.version.split()[0]} Tcl={root.tk.call('info', 'patchlevel')}", flush=True)
    try:
        for index in range(rounds):
            panel._vault_authenticated = True
            window = cls(panel)
            panel._tool_windows[kind] = window
            root.update()
            monitor = window._session_lock_monitor
            assert monitor._subclassed and monitor._registered and not monitor._closed
            hwnd = taskbar.top_level_hwnd(window.win)
            style = taskbar.read_style(hwnd) & taskbar.FRAME_FLAGS
            assert style == taskbar.FRAME_FLAGS
            for repeat in range(repeats):
                print(f"MINIMIZE {kind} {index}:{repeat}", flush=True)
                window._minimize()
                root.update()
                assert user32.IsIconic(hwnd)
                assert not panel._vault_authenticated
                assert not window._privacy_visible
                panel._vault_authenticated = True
                window.summon()
                root.update()
                assert not user32.IsIconic(hwnd)
                assert window.win.winfo_viewable()
                assert taskbar.top_level_hwnd(window.win) == hwnd
                assert taskbar.read_style(hwnd) & taskbar.FRAME_FLAGS == style
                # 显隐及显式还原也必须安全地同步进入 Tk 窗口过程。
                taskbar.hide_window(window.win)
                root.update()
                assert not window.win.winfo_viewable()
                taskbar.show_window(window.win, activate=False)
                root.update()
                assert window.win.winfo_viewable()
                taskbar.minimize_window(window.win)
                root.update()
                assert user32.IsIconic(hwnd)
                taskbar.restore_window(window.win)
                root.update()
                assert not user32.IsIconic(hwnd)
            window.close()
            root.update()
            assert not user32.IsWindow(hwnd)
            assert monitor._closed and not monitor._subclassed
            assert monitor._job is None
            assert id(monitor) not in vault_auth._LIVE_MONITORS
            assert not errors, errors
        # 用纯 Tk 窗口覆盖 WM_NCDESTROY；工具窗口必须走 close 取消其 UI 定时器。
        probe = tk.Toplevel(root)
        probe.update_idletasks()
        monitor = vault_auth.SessionLockMonitor(probe, lambda: errors.append("意外锁屏回调"))
        assert monitor._registered and monitor._subclassed
        probe.destroy()
        root.update()
        assert monitor._closed and not monitor._subclassed
        assert monitor._job is None
        assert not errors, errors
        assert not vault_auth._LIVE_MONITORS
        print(f"PASS {kind} rounds={rounds} repeats={repeats} minimize_restore={rounds * repeats * 2} callback_errors=0 live_monitors=0", flush=True)
    finally:
        panel.win.destroy()
        root.destroy()
        db.close()


@unittest.skipUnless(os.name == "nt", "需要真实 Windows Tk/Win32")
class NativeTaskbarTests(unittest.TestCase):
    def test_private_windows_minimize_restore_destroy_in_subprocess(self):
        for kind in ("vault", "ledger"):
            with self.subTest(kind=kind):
                result = subprocess.run(
                    [sys.executable, "-X", "faulthandler", "-m", "tests.test_taskbar_native", kind],
                    cwd=Path(__file__).resolve().parents[1], capture_output=True,
                    text=True, encoding="utf-8", errors="replace", timeout=180)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(f"PASS {kind}", result.stdout)
                # Tk 自带 PNG 的 ICC 元数据警告与 GIL/窗口回调无关。
                unexpected = [line for line in result.stderr.splitlines()
                              if line != "libpng warning: iCCP: known incorrect sRGB profile"]
                self.assertEqual(unexpected, [], result.stderr)
                print(result.stdout.splitlines()[-1])


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] in ("vault", "ledger"):
        native_cycle(sys.argv[1])
    else:
        unittest.main()
