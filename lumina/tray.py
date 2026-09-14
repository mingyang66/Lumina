"""Windows system-tray integration for Lumina."""
import os
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
            from PIL import Image, ImageDraw, ImageFilter, ImageFont
        except ImportError:
            print("[lumina] tray disabled: install pystray to enable it", flush=True)
            return False

        size = 96
        image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)

        # Lumina mark: a dark glass tile, a blue-violet glow, and a compact
        # clipboard silhouette that remains legible at 16px in the tray.
        for y in range(size):
            t = y / (size - 1)
            color = (round(12 + 28 * t), round(20 + 14 * t),
                     round(50 + 80 * t), 255)
            draw.line((0, y, size, y), fill=color)

        glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        glow_draw = ImageDraw.Draw(glow)
        glow_draw.ellipse((11, 0, 85, 66), fill=(76, 142, 255, 110))
        glow = glow.filter(ImageFilter.GaussianBlur(8))
        image.alpha_composite(glow)
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((5, 5, 91, 91), radius=18,
                               outline=(117, 179, 255, 220), width=2)

        # Clipboard body and top clip, leaving room for the brand name.
        draw.rounded_rectangle((28, 13, 68, 59), radius=7,
                               fill=(246, 249, 255, 255))
        draw.rounded_rectangle((37, 7, 59, 19), radius=5,
                               fill=(246, 249, 255, 255))
        draw.line((39, 31, 57, 31), fill=(68, 92, 169, 255), width=4)
        draw.line((39, 42, 57, 42), fill=(68, 92, 169, 255), width=4)
        draw.ellipse((39, 49, 43, 53), fill=(103, 81, 220, 255))
        draw.line((49, 51, 58, 51), fill=(68, 92, 169, 255), width=3)

        font_path = os.path.join(
            os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "segoeuib.ttf")
        try:
            font = ImageFont.truetype(font_path, 12)
        except OSError:
            font = ImageFont.load_default()
        label = "LUMINA"
        bbox = draw.textbbox((0, 0), label, font=font)
        text_x = (size - (bbox[2] - bbox[0])) // 2 - bbox[0]
        draw.text((text_x, 70), label, font=font, fill=(246, 249, 255, 255))
        image = image.resize((48, 48), Image.Resampling.LANCZOS)

        menu = pystray.Menu(
            pystray.MenuItem("显示面板", self._show_panel, default=True),
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
