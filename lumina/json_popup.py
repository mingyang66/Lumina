"""JSON 操作专用弹窗，不改变 FlatMenu 或文本控件的焦点与选区。"""

import logging
import math
from contextlib import suppress
from tkinter import TclError
from uuid import uuid4

from PIL import Image, ImageDraw, ImageFilter, ImageTk

from . import clipboard_api


class JsonPopup:
    """items 为操作字典列表；popup 的 anchor 为提供屏幕坐标的 Tk 控件。"""

    def __init__(self, tk, parent_win, items, theme, dpi=1.0):
        try:
            dpi = float(dpi)
        except (TypeError, ValueError, OverflowError):
            dpi = 1.0
        self.dpi = max(0.5, min(3.0, dpi)) if math.isfinite(dpi) else 1.0
        self.tk = tk
        self.parent_win = parent_win
        self.items = [dict(item) for item in items]
        self.theme = dict(theme)
        self._closed = False
        self._shown = False
        self._on_close = None
        self._outside_check = None
        self._bindings = []
        self._key_tag = f'JsonPopup_{uuid4().hex}'
        self._key_owner = None
        self._key_targets = []
        self._key_bindings = []
        self._anchor = None
        self._photo = None
        self._active = None
        self._above = True
        self._bounds = None
        self._width = self._px(200)
        self._padding = self._px(4)
        self._row_height = self._px(30)
        self._tip_height = self._px(6)
        self._rows = []
        y = self._padding
        self._separators = []
        for i, item in enumerate(self.items):
            if i and item.get('kind') == 'restore':
                self._separators.append(y + self._px(2))
                y += self._px(5)
            self._rows.append((y, y + self._row_height))
            y += self._row_height
        self._height = y + self._padding + self._tip_height
        self.win = tk.Toplevel(parent_win)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self._transparent = '#010203'
        if self._transparent.lower() in {str(c).lower() for c in theme.values()}:
            self._transparent = '#030201'
        try:
            self.win.configure(bg=self._transparent)
            self.win.attributes('-transparentcolor', self._transparent)
        except TclError:
            self._transparent = None
            self.win.configure(bg=self.theme['card_bg'])
        self.canvas = tk.Canvas(
            self.win, width=self._width, height=self._height,
            bg=self._transparent or self.theme['card_bg'], bd=0,
            highlightthickness=0, takefocus=0)
        self.canvas.pack(fill='both', expand=True)
        self._bind(self.canvas, '<Motion>', self._motion)
        self._bind(self.canvas, '<Leave>', self._leave)
        self._bind(self.canvas, '<ButtonRelease-1>', self._click)

    def _px(self, value):
        return max(1, round(value * self.dpi))

    def _bind(self, widget, sequence, callback):
        binding = widget.bind(sequence, callback, add='+')
        self._bindings.append((widget, sequence, binding))

    @staticmethod
    def _widget_bounds(widget):
        return (widget.winfo_rootx(), widget.winfo_rooty(),
                widget.winfo_width(), widget.winfo_height())

    @staticmethod
    def _inside(bounds, x, y):
        if bounds is None:
            return False
        left, top, width, height = bounds
        return left <= x < left + width and top <= y < top + height

    def popup(self, anchor, on_close=None):
        """优先位于 anchor 上方，空间不足时下移；同一实例只显示一次。"""
        if self._closed or self._shown:
            return
        self._shown = True
        self._anchor = anchor
        self._on_close = on_close
        try:
            self.win.update_idletasks()
            ax, ay, aw, ah = self._widget_bounds(anchor)
            sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
            self._width = min(self._width, sw)
            self._height = min(self._height, sh)
            cx = ax + aw / 2
            x = max(0, min(round(cx - self._width / 2), sw - self._width))
            self._above = ay >= self._height
            y = ay - self._height if self._above else ay + ah
            y = max(0, min(y, sh - self._height))
            self._bounds = (x, y, self._width, self._height)
            margin = min(self._px(17), self._width / 2)
            self._tip_x = max(margin, min(cx - x, self._width - margin))
            self.win.geometry(f'{self._width}x{self._height}+{x}+{y}')
            focused = self.parent_win.focus_get()
            targets = [self.parent_win, self.canvas]
            if focused is not None and focused not in targets:
                targets.append(focused)
            # bind_class 在 Tk 根对象注册命令，删除时也必须使用同一对象。
            self._key_owner = self.parent_win._root()
            for key in ('Up', 'Down', 'Return', 'Escape'):
                sequence = f'<{key}>'
                binding = self._key_owner.bind_class(
                    self._key_tag, sequence, lambda event, k=key: self._key(k))
                self._key_bindings.append((sequence, binding))
            for target in targets:
                tags = target.bindtags()
                target.bindtags((self._key_tag, *tags))
                self._key_targets.append(target)
            self._bind(self.parent_win, '<Destroy>', self._parent_destroyed)
            self._active = next((i for i, item in enumerate(self.items)
                                 if item.get('selected') and self._enabled(i)), None)
            self._draw()
            self.win.deiconify()
            self.win.lift()
            self._outside_check = self.win.after(120, self._check_outside_click)
        except Exception:
            self.close()
            raise

    def contains_point(self, x, y):
        """判断点是否落在菜单矩形内，不把 anchor 计为菜单区域。"""
        return not self._closed and self._inside(self._bounds, x, y)

    def _enabled(self, index):
        return bool(self.items[index].get('enabled', True))

    def _row_at(self, x, y):
        if self._closed or not self._shown:
            return None
        if not (self._padding <= x < self._width - self._padding):
            return None
        offset = 0 if self._above else self._tip_height
        if not (offset <= y < self._height - (self._tip_height if self._above else 0)):
            return None
        for i, (top, bottom) in enumerate(self._rows):
            if top + offset <= y < bottom + offset:
                return i if self._enabled(i) else None
        return None

    def _motion(self, event):
        active = self._row_at(event.x, event.y)
        if active != self._active:
            self._active = active
            self._draw()

    def _leave(self, _event):
        if not self._closed and self._active is not None:
            self._active = None
            self._draw()

    def _click(self, event):
        index = self._row_at(event.x, event.y)
        if index is not None:
            self._invoke(index)
        return 'break'

    def _key(self, key):
        if self._closed:
            return None
        if key == 'Escape':
            self.close()
        elif key == 'Return':
            if self._active is not None:
                self._invoke(self._active)
        else:
            enabled = [i for i in range(len(self.items)) if self._enabled(i)]
            if enabled:
                step = 1 if key == 'Down' else -1
                if self._active in enabled:
                    self._active = enabled[(enabled.index(self._active) + step) % len(enabled)]
                else:
                    self._active = enabled[0 if step == 1 else -1]
                self._draw()
        return 'break'

    def _draw(self):
        if self._closed:
            return
        T = self.theme
        w, h, aa = self._width, self._height, 4
        offset = 0 if self._above else self._tip_height
        bottom = h - self._tip_height if self._above else h
        mask = Image.new('L', (w * aa, h * aa), 0)
        draw = ImageDraw.Draw(mask)
        draw.rounded_rectangle((0, offset * aa, w * aa - 1, bottom * aa - 1),
                               radius=self._px(8) * aa, fill=255)
        tip_y = h - 1 if self._above else 0
        base_y = bottom - self._px(2) if self._above else offset + self._px(2)
        draw.polygon([(round((self._tip_x - self._tip_height) * aa), base_y * aa),
                      (round(self._tip_x * aa), tip_y * aa),
                      (round((self._tip_x + self._tip_height) * aa), base_y * aa)], fill=255)
        image = Image.new('RGB', mask.size, T['hairline'])
        inner = mask.filter(ImageFilter.MinFilter(9))
        # 画布边缘补一像素边框，避免滤镜边缘延拓吞掉直边。
        ImageDraw.Draw(inner).rectangle((0, 0, w * aa - 1, h * aa - 1),
                                        outline=0, width=aa)
        image.paste(T['card_bg'], mask=inner)
        if self._active is not None:
            top, end = self._rows[self._active]
            ImageDraw.Draw(image).rounded_rectangle(
                (self._padding * aa, (top + offset) * aa,
                 (w - self._padding) * aa, (end + offset) * aa),
                radius=self._px(6) * aa, fill=T['accent_soft'])
        image.putalpha(mask)
        image = image.resize((w, h), Image.Resampling.LANCZOS)
        background = self._flatten_background(image, self._transparent, T['card_bg'])
        self._photo = ImageTk.PhotoImage(background, master=self.win)
        c = self.canvas
        c.delete('all')
        c.create_image(0, 0, image=self._photo, anchor='nw')
        for y in self._separators:
            c.create_line(self._px(12), y + offset, w - self._px(12), y + offset,
                          fill=T['separator'], width=self._px(1))
        for i, item in enumerate(self.items):
            top, end = self._rows[i]
            cy = (top + end) / 2 + offset
            enabled = self._enabled(i)
            color = T['label'] if enabled else T['label3']
            icon_color = T['accent'] if enabled and (i == self._active or item.get('selected')) else color
            self._icon(item.get('kind'), self._px(22), cy, icon_color)
            c.create_text(self._px(42), cy, text=item.get('label', ''),
                          anchor='w', fill=color,
                          font=('Microsoft YaHei UI', -self._px(13)))
            selected = item.get('selected', False)
            right = w - self._px(17)
            if selected:
                c.create_line(right - self._px(8), cy, right - self._px(5), cy + self._px(3),
                              right, cy - self._px(4), fill=icon_color,
                              width=self._px(1), capstyle='round', joinstyle='round')
                right -= self._px(17)
            c.create_text(right, cy, text=item.get('hint', ''), anchor='e',
                          fill=T['label2'] if enabled else T['label3'],
                          font=('Microsoft YaHei UI', -self._px(11)))
        c.configure(cursor='hand2' if self._active is not None else '')

    @staticmethod
    def _flatten_background(image, transparent, card_bg):
        if transparent:
            # Win32透明色只支持精确色键，不支持半透明。不可把AA边缘
            # 混到色键色上，否则生成非透明暗色像素，表现为圆角虚边。
            coverage = image.getchannel('A').point(lambda value: 255 if value >= 128 else 0)
            background = Image.new('RGB', image.size, transparent)
            background.paste(image.convert('RGB'), mask=coverage)
            return background
        background = Image.new('RGBA', image.size, card_bg)
        background.alpha_composite(image)
        return background.convert('RGB')

    def _icon(self, kind, x, y, color):
        s = self.dpi

        def line(*points):
            coords = [v for px, py in zip(points[::2], points[1::2])
                      for v in (x + px * s, y + py * s)]
            self.canvas.create_line(*coords, fill=color, width=max(1, s),
                                    capstyle='round', joinstyle='round')

        if kind == 'format':
            for dy, length in ((-5, 11), (0, 7), (5, 11)):
                line(-6, dy, -6 + length, dy)
        elif kind == 'compress':
            line(-7, -5, -2, 0, -7, 5)
            line(7, -5, 2, 0, 7, 5)
        elif kind == 'restore':
            line(-5, -1, -5, -6, 0, -6)
            line(-5, -5, 0, -6, 5, -3, 6, 2, 3, 6, -2, 6)

    def _parent_destroyed(self, event):
        if event.widget is self.parent_win:
            self.close()

    def _check_outside_click(self):
        self._outside_check = None
        if self._closed:
            return
        try:
            if not self._anchor.winfo_exists():
                self.close()
                return
            if clipboard_api.mouse_button_down():
                x, y = self.win.winfo_pointerxy()
                if (not self.contains_point(x, y)
                        and not self._inside(self._widget_bounds(self._anchor), x, y)):
                    self.close()
                    return
        except (TclError, OSError):
            self.close()
            return
        try:
            self._outside_check = self.win.after(100, self._check_outside_click)
        except TclError:
            self.close()

    def _invoke(self, index):
        if self._closed or not self._enabled(index):
            return
        command = self.items[index].get('command')
        self.close()
        if command is not None:
            command()

    def close(self):
        """幂等关闭：撤销定时器和专属绑定，释放引用，至多通知一次。"""
        if self._closed:
            return
        self._closed = True
        callback, self._on_close = self._on_close, None
        win, self.win = self.win, None
        if self._outside_check is not None:
            with suppress(TclError):
                win.after_cancel(self._outside_check)
            self._outside_check = None
        for target in self._key_targets:
            with suppress(TclError):
                target.bindtags(tuple(tag for tag in target.bindtags()
                                      if tag != self._key_tag))
        self._key_targets.clear()
        for sequence, binding in self._key_bindings:
            with suppress(TclError):
                self._key_owner.unbind_class(self._key_tag, sequence)
            # unbind_class 只清 Tcl 脚本，不删除 bind_class 注册的 Python 命令。
            if binding is not None:
                with suppress(TclError):
                    self._key_owner.deletecommand(binding)
        self._key_bindings.clear()
        self._key_owner = None
        self._key_tag = None
        for widget, sequence, binding in self._bindings:
            with suppress(TclError):
                widget.unbind(sequence, binding)
        self._bindings.clear()
        with suppress(TclError):
            win.destroy()
        self._photo = self.canvas = self._anchor = self.parent_win = self.tk = None
        self.items.clear()
        self.theme.clear()
        self._bounds = None
        self._active = None
        if callback is not None:
            try:
                callback()
            except Exception:
                logging.getLogger(__name__).exception('JsonPopup 关闭回调失败')
