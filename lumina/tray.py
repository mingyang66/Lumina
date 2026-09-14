"""Windows system-tray integration for Lumina."""
import threading


class TrayIcon:
    """Owns the tray icon on its own thread and delegates actions to the app."""

    def __init__(self, app):
        self.app = app
        self._icon = None
        self._thread = None

    def start(self):
        try:
            import pystray
            from PIL import Image, ImageDraw
        except ImportError:
            print("[lumina] tray disabled: install pystray to enable it", flush=True)
            return False

        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((6, 6, 58, 58), radius=12,
                               fill="#007aff", outline="#ffffff", width=3)
        draw.rectangle((18, 16, 46, 50), fill="#ffffff")
        draw.rectangle((23, 11, 41, 19), fill="#ffffff")
        draw.line((24, 29, 40, 29), fill="#007aff", width=3)
        draw.line((24, 37, 40, 37), fill="#007aff", width=3)

        menu = pystray.Menu(
            pystray.MenuItem("显示面板", self._show_panel),
            pystray.MenuItem("最小化面板", self._hide_panel),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出 Lumina", self._quit),
        )
        self._icon = pystray.Icon("lumina", image, "Lumina", menu)
        self._thread = threading.Thread(target=self._run, name="lumina-tray",
                                         daemon=True)
        self._thread.start()
        return True

    def _run(self):
        try:
            self._icon.run()
        except Exception as exc:
            print(f"[lumina] tray stopped: {exc}", flush=True)

    def _show_panel(self, _icon, _item):
        self.app.ui.show_panel()

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
