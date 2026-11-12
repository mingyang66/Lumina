"""Windows system-tray integration for Lumina."""
import os
import threading
import traceback


def _best_effort_taskbar_filter(api):
    """Ignore access denial for pystray's optional Explorer-restart filter."""
    original = api.ChangeWindowMessageFilterEx
    warned = False

    def change_filter(hwnd, message, action, filter_info):
        nonlocal warned
        try:
            return original(hwnd, message, action, filter_info)
        except OSError as exc:
            if (getattr(exc, "winerror", None) == 5
                    and message == api.WM_TASKBARCREATED
                    and action == api.MSGFLT_ALLOW):
                if not warned:
                    print("[lumina] tray warning: Windows denied the optional "
                          "Explorer-restart message filter; the tray icon will "
                          "continue without automatic recovery after Explorer restarts.",
                          flush=True)
                    warned = True
                # pystray only needs this call to succeed to continue creating its
                # message window; the filter itself is an optional recovery feature.
                return True
            raise

    api.ChangeWindowMessageFilterEx = change_filter

    def restore():
        api.ChangeWindowMessageFilterEx = original

    return restore


class TrayIcon:
    """Owns the tray icon on its own thread and delegates actions to the app."""

    def __init__(self, app):
        self.app = app
        self._icon = None
        self._thread = None

    def start(self):
        try:
            import pystray
        except ImportError:
            print("[lumina] tray disabled: install pystray to enable it", flush=True)
            return False

        # 画法与任务栏图标**同源**：直接复用 `app_icon.draw_mark`，不再在这里
        # 复制一份绘制代码。此前两处各写一段坐标完全相同的大段代码，改一处
        # 必然导致托盘与任务栏长得不一样。托盘按 48px 渲染（Windows 通知区域
        # 大图标尺寸），`draw_mark` 内部自带超采样抗锯齿。
        from .app_icon import draw_mark
        image = draw_mark(48)

        menu = pystray.Menu(
            pystray.MenuItem("显示面板", self._toggle_panel,
                             checked=self._panel_is_visible, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出 Lumina", self._quit),
        )
        self._icon = pystray.Icon("lumina", image, "Lumina", menu)
        self._thread = threading.Thread(target=self._run, name="lumina-tray",
                                         daemon=True)
        self._thread.start()
        return True

    def _run(self):
        restore_filter = lambda: None
        try:
            if os.name == "nt":
                # Pystray uses this filter only to re-register the icon after an
                # Explorer restart. Some Windows environments reject it with
                # access denied; that should not prevent normal tray startup.
                from pystray._win32 import win32
                restore_filter = _best_effort_taskbar_filter(win32)
            self._icon.run()
        except Exception as exc:
            print(f"[lumina] tray stopped: {exc}", flush=True)
            traceback.print_exc()
        finally:
            restore_filter()

    def _show_panel(self, _icon, _item):
        self.app.ui.show_panel()

    def _toggle_panel(self, _icon, _item):
        self.app.ui.toggle_panel()
        # Panel visibility is applied asynchronously on the Tk thread. Refresh
        # after that queue has been processed so the checkmark uses new state.
        threading.Timer(0.2, self._refresh_menu).start()

    def _panel_is_visible(self, _item):
        return bool(self.app.ui.panel_visible)

    def _refresh_menu(self):
        icon = self._icon
        if icon is not None:
            try:
                icon.update_menu()
            except Exception:
                pass

    def _hide_panel(self, _icon, _item):
        self.app.ui.request_hide_panel()

    def _quit(self, _icon, _item):
        self.app.stop()

    def stop(self):
        icon = self._icon
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                pass
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2)
        self._icon = None
        self._thread = None
