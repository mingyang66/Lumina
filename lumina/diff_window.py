"""独立文本差异窗口；后台只比较数据，所有 Tk 操作留在 UI 线程。"""

from contextlib import suppress
from queue import Empty, Queue
from threading import Thread
from tkinter import TclError, filedialog

from PIL import Image, ImageDraw, ImageTk

from . import diff_tools


class _ToolbarControl:
    """仅用于差异工具栏的像素尺寸圆角控件。"""

    def __init__(self, owner, parent, text='', command=None, width=112, edge=None):
        self.owner = owner
        self.parent = parent
        self._command = command
        self._closed = False
        self._edge = edge
        self._hover = self._focused = self._pressed = False
        self._mouse_focus = False
        self._press_after = None
        self._tooltip_after = self._tooltip = None
        self._tooltip_text = ''
        self._photo = None
        self._width = round(width * owner._scale)
        self._height = round(30 * owner._scale)
        self._radius = round(7 * owner._scale)
        self._segments = []
        self._group = None
        self._segment_start = 0
        self.options = dict(text=text, state='normal', bg=owner.theme['toolbar_bg'],
                            fg=owner.theme['label'], command=self.invoke,
                            font=owner._ui_font, marker=None, icon=None)
        self.canvas = owner.tk.Canvas(
            parent, width=self._width, height=self._height,
            bg=owner.theme['window_bg'], bd=0, highlightthickness=0,
            takefocus=1 if command else 0)
        self.canvas.bind('<Enter>', self._enter)
        self.canvas.bind('<Leave>', self._leave)
        self.canvas.bind('<ButtonPress-1>', self._press)
        self.canvas.bind('<ButtonRelease-1>', self._click)
        self.canvas.bind('<FocusIn>', lambda event: self._focus(True))
        self.canvas.bind('<FocusOut>', lambda event: self._focus(False))
        for key in ('<space>', '<Return>'):
            self.canvas.bind(key, lambda event: self._activate())
        self.canvas.bind('<Destroy>', lambda event: self.close())
        self._draw()

    def __getattr__(self, name):
        return getattr(self.canvas, name)

    def pack(self, **kwargs):
        self.options['pack'] = kwargs
        self.canvas.pack(**kwargs)

    def place(self, **kwargs):
        self.options['place'] = kwargs
        self.canvas.place(**kwargs)

    def configure(self, **kwargs):
        if self._closed or self.owner._closed:
            return
        self.options.update(kwargs)
        self.canvas.configure(takefocus=1 if self._command and self.options['state'] != 'disabled' else 0)
        if self.options['state'] == 'disabled':
            self._hover = self._focused = self._mouse_focus = False
            self._clear_press()
            self._hide_tooltip()
        self._draw()

    def _draw(self):
        if self._closed or self.owner._closed:
            return
        if self._group is not None:
            self._group._draw()
            return
        aa, s = 4, self.owner._scale
        rgb = lambda color: tuple(value // 257 for value in self.canvas.winfo_rgb(color))
        bg, border, fg = self._colors()
        image = Image.new('RGB', (self._width * aa, self._height * aa),
                          rgb(self.owner.theme['window_bg']))
        draw = ImageDraw.Draw(image)
        inset, stroke = round(2 * s * aa), max(aa, round(s * aa))
        bounds = (inset, inset, image.width - 1 - inset, image.height - 1 - inset)
        draw.rounded_rectangle(bounds, radius=self._radius * aa, fill=rgb(bg))
        for segment in self._segments:
            segment_bg, _, _ = segment._colors()
            x = segment._segment_start * aa
            mask = Image.new('L', image.size)
            ImageDraw.Draw(mask).rounded_rectangle(bounds, radius=self._radius * aa, fill=255)
            mask_draw = ImageDraw.Draw(mask)
            if x:
                mask_draw.rectangle((0, 0, x - 1, image.height - 1), fill=0)
            end = x + segment._width * aa
            if end < image.width:
                mask_draw.rectangle((end, 0, image.width - 1, image.height - 1), fill=0)
            image.paste(rgb(segment_bg), (0, 0, image.width, image.height), mask)
        focus_stroke = max(stroke, round(1.75 * s * aa))
        draw.rounded_rectangle(bounds, radius=self._radius * aa, outline=rgb(border),
                               width=focus_stroke if self._focused else stroke)
        for segment in self._segments:
            x = segment._segment_start * aa
            _, segment_border, _ = segment._colors()
            focused = segment._focused and segment.options['state'] != 'disabled'
            segment_stroke = focus_stroke if focused else stroke
            contour = Image.new('RGBA', image.size)
            contour_draw = ImageDraw.Draw(contour)
            contour_draw.rounded_rectangle(
                bounds, radius=self._radius * aa, outline=rgb(segment_border) + (255,),
                width=segment_stroke)
            if x:
                contour_draw.rectangle((x, inset + stroke, x + segment_stroke - 1,
                                        image.height - 1 - inset - stroke),
                                       fill=rgb(segment_border if focused else border) + (255,))
            end = x + segment._width * aa
            if focused and end < bounds[2]:
                # 共享分隔线在焦点段内侧加粗，不改变相邻段的像素。
                contour_draw.rectangle((end - segment_stroke, inset + stroke, end - 1,
                                        image.height - 1 - inset - stroke),
                                       fill=rgb(segment_border) + (255,))
            region = (x, 0, end, image.height)
            image.paste(contour.crop(region), (x, 0), contour.crop(region))
        surface = image.resize((self._width, self._height), Image.Resampling.LANCZOS)
        # 每段独立降采样，LANCZOS 不把相邻段的 hover/focus 颜色扩散过去。
        for segment in self._segments:
            x, width = segment._segment_start, segment._width
            region = image.crop((x * aa, 0, (x + width) * aa, image.height))
            surface.paste(region.resize((width, self._height), Image.Resampling.LANCZOS), (x, 0))
        self._set_surface(surface, bg, border, fg)
        # 子 Canvas 使用完整外框的对应切片，矩形窗口不会盖掉圆角和分隔线。
        for segment in self._segments:
            x = segment._segment_start
            segment._set_surface(surface.crop((x, 0, x + segment._width, self._height)),
                                 *segment._colors())

    def _colors(self):
        t = self.owner.theme
        if self.options['state'] == 'disabled':
            return t['toolbar_bg'], t['toolbar_disabled_border'], t['label2']
        if self._pressed:
            bg, border, fg = t['toolbar_press_bg'], t['accent'], '#FFFFFF'
        elif self.options['marker'] in ('check', 'dot'):
            bg, border, fg = self.options['bg'], t['accent'], self.options['fg']
        elif self._hover:
            bg, border, fg = t['toolbar_hover_bg'], t['toolbar_hover_border'], t['accent']
        else:
            bg, border, fg = self.options['bg'], t['toolbar_border'], self.options['fg']
        return bg, t['toolbar_focus'] if self._focused else border, fg

    def _set_surface(self, image, bg, border, fg):
        if self._closed or self.owner._closed:
            return
        self._paint_bg, self._paint_border, self._paint_fg = bg, border, fg
        self.canvas.delete('surface')
        marker = self.options['marker']
        shift = round(5 * self.owner._scale) if marker else 0
        text_x = self._width / 2 + shift
        if self.options['icon'] == 'swap':
            s, aa = self.owner._scale, 4
            # 用实际字体宽度将图标、间距与文字作为整体居中。
            probe = self.canvas.create_text(0, 0, text=self.options['text'],
                                            font=self.options['font'], anchor='w')
            bounds = self.canvas.bbox(probe)
            self.canvas.delete(probe)
            text_width = bounds[2] - bounds[0]
            icon_width, gap = 16 * s, 7 * s
            left = (self._width - icon_width - gap - text_width) / 2
            text_x = left + icon_width + gap + text_width / 2
            icon = Image.new('RGBA', (self._width * aa, self._height * aa))
            draw = ImageDraw.Draw(icon)
            color = tuple(value // 257 for value in self.canvas.winfo_rgb(fg)) + (255,)
            stroke = max(1, round(1.25 * s * aa))
            x1, x2, y = left * aa, (left + icon_width) * aa, self._height * aa / 2
            for start, end, row in ((x1, x2, y - 3 * s * aa),
                                    (x2, x1, y + 3 * s * aa)):
                back = end - (3 * s * aa if end > start else -3 * s * aa)
                draw.line(((start, row), (end, row)), fill=color, width=stroke)
                draw.line(((back, row - 3 * s * aa), (end, row),
                           (back, row + 3 * s * aa)), fill=color, width=stroke, joint='curve')
            icon = icon.resize(image.size, Image.Resampling.LANCZOS)
            image = image.copy()
            image.paste(icon, (0, 0), icon)
        self._photo = ImageTk.PhotoImage(image, master=self.canvas)
        self.canvas.create_image(0, 0, anchor='nw', image=self._photo, tags='surface')
        self.canvas.create_text(text_x, self._height / 2,
                                text=self.options['text'], font=self.options['font'],
                                fill=fg, tags='surface')
        if marker == 'dot':
            x, y, r = round(11 * self.owner._scale), self._height / 2, 2 * self.owner._scale
            self.canvas.create_oval(x - r, y - r, x + r, y + r,
                                    fill=fg, outline='', tags='surface')
        elif marker == 'check':
            s, y = self.owner._scale, self._height / 2
            self.canvas.create_line(8 * s, y, 11 * s, y + 3 * s, 16 * s, y - 4 * s,
                                    fill=fg, width=max(1, s), capstyle='round', tags='surface')
        # 分段导航的子控件始终位于背景图之上。
        self.canvas.tag_lower('surface')

    def invoke(self):
        if self.options['state'] != 'disabled' and not self._closed and not self.owner._closed and self._command:
            self._hide_tooltip()
            self._command()

    def _clear_press(self):
        if self._press_after is not None:
            with suppress(TclError):
                self.owner.win.after_cancel(self._press_after)
            self._press_after = None
        self._pressed = False

    def _release_feedback(self):
        self._press_after = None
        self._pressed = False
        self._draw()

    def _activate(self):
        if not self._closed and not self.owner._closed and self.options['state'] != 'disabled' and self._command:
            self._mouse_focus = False
            self._focused = True
            self._clear_press()
            self._pressed = True
            self._draw()
            self._press_after = self.owner.win.after(120, self._release_feedback)
            self.invoke()
        return 'break'

    def _press(self, event):
        if not self._closed and not self.owner._closed:
            # break 会阻止顶层绑定，禁用控件的外点也需主动关闭弹层。
            self.owner._outside_rules(event, self.canvas)
        if (not self._closed and not self.owner._closed and self.options['state'] != 'disabled' and self._command
                and 0 <= event.x < self._width and 0 <= event.y < self._height):
            self._clear_press()
            self._pressed = True
            # focus_set 的 FocusIn 可能延后到 release，来源标记需保留到 FocusOut。
            self._mouse_focus = True
            self._focused = False
            self.canvas.focus_set()
            self._hide_tooltip()
            self._draw()
        return 'break'

    def _click(self, event):
        pressed = self._pressed
        self._clear_press()
        self._draw()
        if (pressed and not self._closed and not self.owner._closed
                and 0 <= event.x < self._width and 0 <= event.y < self._height
                and self.options['state'] != 'disabled' and self._command):
            self.invoke()
        return 'break'

    def _focus(self, focused):
        self._focused = (focused and not self._mouse_focus and not self._closed and not self.owner._closed
                         and bool(self._command) and self.options['state'] != 'disabled')
        if not focused:
            self._mouse_focus = False
            self._clear_press()
            self._hide_tooltip()
        self._draw()

    def _enter(self, event):
        self._hover = (not self._closed and not self.owner._closed
                       and bool(self._command) and self.options['state'] != 'disabled')
        self._draw()
        self._hide_tooltip()
        if self._hover and self._tooltip_text and not self.owner._closed:
            self._tooltip_after = self.owner.win.after(500, self._show_tooltip)

    def _leave(self, event):
        self._clear_press()
        self._hover = False
        self._hide_tooltip()
        self._draw()

    def set_tooltip(self, text):
        if text != self._tooltip_text:
            self._hide_tooltip()
        self._tooltip_text = text

    def _show_tooltip(self):
        self._tooltip_after = None
        if (self._closed or self.owner._closed or self.options['state'] == 'disabled'
                or not self._hover or not self.canvas.winfo_exists()):
            return
        owner, t = self.owner, self.owner.theme
        self._tooltip = owner.tk.Label(owner.win, text=self._tooltip_text,
                                       bg=t['card_bg'], fg=t['label'], font=owner._ui_font,
                                       padx=8, pady=4, bd=0, takefocus=0)
        self._tooltip.place(x=max(0, min(self.winfo_rootx() - owner.win.winfo_rootx(),
                                        owner.win.winfo_width() - self._width)),
                            y=self.winfo_rooty() - owner.win.winfo_rooty() + self._height + 4)
        self._tooltip.lift()

    def _hide_tooltip(self):
        if self._tooltip_after is not None:
            with suppress(TclError):
                self.owner.win.after_cancel(self._tooltip_after)
            self._tooltip_after = None
        if self._tooltip is not None:
            with suppress(TclError):
                self._tooltip.destroy()
            self._tooltip = None

    def close(self):
        self._closed = True
        self._hover = self._focused = self._mouse_focus = False
        self._clear_press()
        self._hide_tooltip()
        self._photo = None


class _PopupMenu:
    """单 Canvas 面板；背景和细勾以 4 倍 RGB 合成，文字交给 Tk。"""

    def __init__(self, owner, trigger, rows, rules=False):
        self.owner, self.trigger, self.rows, self.rules = owner, trigger, rows, rules
        self._closed = False
        self._photo = None
        self._hover = self._pressed = None
        self._keyboard = not trigger._mouse_focus
        s = owner._scale
        self._width = round(240 * s)
        self._row_height = round(48 * s)
        self._pad = round(4 * s)
        self._height = self._pad * 2 + len(rows) * self._row_height
        self._view_width, self._view_height = self._width, self._height
        self._active = None
        self.canvas = owner.tk.Canvas(owner.win, width=self._width, height=self._height,
                                      bg=owner.theme['window_bg'], bd=0,
                                      highlightthickness=0, takefocus=1)
        self.canvas.bind('<Motion>', self._motion)
        self.canvas.bind('<Leave>', self._leave)
        self.canvas.bind('<ButtonPress-1>', self._press)
        self.canvas.bind('<ButtonRelease-1>', self._release)
        self.canvas.bind('<Up>', lambda event: self._move(-1))
        self.canvas.bind('<Down>', lambda event: self._move(1))
        for key in ('<Return>', '<space>'):
            self.canvas.bind(key, self._enter)
        self.canvas.bind('<Escape>', self._escape)
        self.canvas.bind('<FocusOut>', owner._rules_focus_out)
        self.canvas.bind('<Destroy>', self._destroy)
        self.refresh()

    def __getattr__(self, name):
        return getattr(self.canvas, name)

    def _enabled(self, index):
        return (index is not None and 0 <= index < len(self.rows)
                and not (self.rules and self.owner._mode == 'json_fields'))

    def _selected(self, index):
        key = self.rows[index][0]
        return self.owner._options[key].get() if self.rules else key == self.owner._mode

    def refresh(self):
        if self._closed or self.owner._closed:
            return
        if not self._enabled(self._active):
            enabled = [i for i in range(len(self.rows)) if self._enabled(i)]
            self._active = next((i for i in enabled if self._selected(i)),
                                enabled[0] if enabled else None)
        if not self._enabled(self._hover):
            self._hover = None
        self._draw()

    def resize(self, width, height):
        self._view_width, self._view_height = width, height
        self.canvas.configure(width=width, height=height)
        self._draw()

    def _draw(self):
        if self._closed or self.owner._closed:
            return
        t, s, aa = self.owner.theme, self.owner._scale, 4
        rgb = lambda color: tuple(value // 257 for value in self.canvas.winfo_rgb(color))
        w, h = self._view_width, self._view_height
        image = Image.new('RGB', (w * aa, h * aa), rgb(t['window_bg']))
        draw = ImageDraw.Draw(image)
        bounds = (0, 0, image.width - 1, image.height - 1)
        radius, stroke = round(8 * s * aa), max(1, round(s * aa))
        draw.rounded_rectangle(bounds, radius=radius, fill=rgb(t['card_bg']))
        mask = Image.new('L', image.size)
        ImageDraw.Draw(mask).rounded_rectangle(bounds, radius=radius, fill=255)
        rows_image = image.copy()
        rows_draw = ImageDraw.Draw(rows_image)
        self._row_colors = []
        for i, (_, title, description) in enumerate(self.rows):
            enabled, selected = self._enabled(i), self._selected(i)
            active = i == self._hover or (self._keyboard and i == self._active)
            bg = t['accent_soft'] if selected and enabled else (
                t['toolbar_hover_bg'] if active and enabled else t['card_bg'])
            fg = t['label2'] if not enabled else t['accent'] if selected else t['label']
            self._row_colors.append((bg, fg))
            y = self._pad + i * self._row_height
            rows_draw.rectangle((stroke, y * aa, image.width - 1 - stroke,
                                 (y + self._row_height) * aa - 1), fill=rgb(bg))
            cy = (y + self._row_height / 2) * aa
            if self.rules:
                x, r = 18 * s * aa, 6 * s * aa
                rows_draw.rounded_rectangle((x - r, cy - r, x + r, cy + r),
                                            radius=2 * s * aa,
                                            fill=rgb(t['accent'] if selected and enabled else bg),
                                            outline=rgb(fg if selected else t['label2']), width=stroke)
            if selected:
                x = (18 * s if self.rules else w - 22 * s) * aa
                color = '#FFFFFF' if self.rules and enabled else fg
                rows_draw.line(((x - 4 * s * aa, cy), (x - s * aa, cy + 3 * s * aa),
                                (x + 5 * s * aa, cy - 4 * s * aa)),
                               fill=rgb(color), width=max(1, round(1.25 * s * aa)), joint='curve')
        image.paste(rows_image, (0, 0), mask)
        draw.rounded_rectangle(bounds, radius=radius, outline=rgb(t['card_border']), width=stroke)
        self.canvas.delete('all')
        self._photo = ImageTk.PhotoImage(image.resize((w, h), Image.Resampling.LANCZOS), master=self.canvas)
        self.canvas.create_image(0, 0, anchor='nw', image=self._photo)
        for i, (_, title, description) in enumerate(self.rows):
            y = self._pad + i * self._row_height
            if self.rules and not self._enabled(i):
                description = '不适用于字段比较；设置已保留'
            x = (34 if self.rules else 12) * s
            self.canvas.create_text(x, y + 14 * s, anchor='w', text=title,
                                    font=self.owner._ui_font, fill=self._row_colors[i][1])
            self.canvas.create_text(x, y + 33 * s, anchor='w', text=description,
                                    font=('Microsoft YaHei UI', 8), fill=t['label2'])

    def _hit(self, event):
        if self._closed or self.owner._closed:
            return None
        if not (0 <= event.x < self._view_width and 0 <= event.y < self._view_height):
            return None
        index = (int(event.y) - self._pad) // self._row_height
        return index if self._enabled(index) else None

    def _motion(self, event):
        self._keyboard = False
        self._hover = self._hit(event)
        self._draw()

    def _leave(self, event):
        self._hover = self._pressed = None
        self._draw()

    def _press(self, event):
        if not self._closed and not self.owner._closed:
            self._keyboard = False
            self._pressed = self._hit(event)
            self.canvas.focus_set()
            self._motion(event)
        return 'break'

    def _release(self, event):
        pressed, self._pressed = self._pressed, None
        if pressed is not None and pressed == self._hit(event):
            self._choose(pressed)
        return 'break'

    def _move(self, direction):
        if self._closed or self.owner._closed:
            return 'break'
        self._keyboard = True
        self._hover = None
        start = self._active if self._active is not None else (-1 if direction > 0 else 0)
        for step in range(1, len(self.rows) + 1):
            index = (start + step * direction) % len(self.rows)
            if self._enabled(index):
                self._active = index
                break
        self._draw()
        return 'break'

    def _enter(self, event):
        self._keyboard = True
        self._choose(self._active)
        return 'break'

    def _choose(self, index):
        if self._closed or self.owner._closed or not self._enabled(index):
            return
        key = self.rows[index][0]
        if self.rules:
            var = self.owner._options[key]
            var.set(not var.get())
            self.owner._request()
            self.refresh()
        else:
            self.owner._select_mode(key)

    def _escape(self, event):
        if not self._closed and not self.owner._closed:
            self._keyboard = True
            self.owner._close_rules()
        return 'break'

    def _destroy(self, event):
        if event.widget is self.canvas and not self._closed:
            self.owner._close_rules(restore_focus=False)

    def destroy(self):
        self._closed = True
        self._photo = None
        self._hover = self._active = self._pressed = None
        with suppress(TclError):
            self.canvas.delete('all')
            self.canvas.destroy()


class _BlockLabel:
    """框外纯文本计数，仅当前序号使用强调色。"""

    def __init__(self, owner, parent):
        self.owner = owner
        self.frame = owner.tk.Frame(parent, bg=owner.theme['window_bg'],
                                    bd=0, highlightthickness=0, takefocus=0)
        self.options = dict(text='', fg=owner.theme['label2'])
        self.labels = []
        for _ in range(3):
            label = owner.tk.Label(self.frame, text='', font=owner._ui_font,
                                   bg=owner.theme['window_bg'], fg=owner.theme['label2'],
                                   bd=0, highlightthickness=0, padx=0, pady=0, takefocus=0)
            label.pack(side='left')
            self.labels.append(label)

    def __getattr__(self, name):
        return getattr(self.frame, name)

    def configure(self, *, text, current=''):
        if self.owner._closed:
            return
        before, number, after = text.partition(current) if current else (text, '', '')
        self.options.update(text=text, fg=self.owner.theme['accent'] if number else self.owner.theme['label2'])
        for label, part, color in zip(self.labels, (before, number, after),
                                      ('label2', 'accent', 'label2')):
            label.configure(text=part, fg=self.owner.theme[color])


class DiffWindow:
    def __init__(self, ui, left_row, right_row, theme=None, session=None):
        self.ui = ui
        self.tk = ui._tk
        self._closed = False
        self._generation = 0
        self._pending = None
        self._worker = None
        self._outcomes = Queue(maxsize=1)
        self._after_id = None
        self._busy = False
        self._result = None
        self._display_rows = None
        self._display_options = None
        self._roles = ('A', 'B')
        self._display_roles = None
        self._badges = []
        self._rules_popup = None
        self._mode_popup = None
        self._popup_after = None
        self._mode = 'text'
        self._display_mode = None
        self._mode_labels = {'text': '原文比较', 'json_format': 'JSON格式化比较',
                             'json_fields': 'JSON按字段比较'}
        self._retained = False
        self._block_index = -1
        self._marker_after = None
        self._syncing = False
        self._wheel_remainder = 0
        self._left_row = dict(left_row)
        self._right_row = dict(right_row)
        if theme is None:
            provider = getattr(getattr(ui, '_panel', None), '_theme', None)
            theme = provider() if callable(provider) else provider
        dark = None
        if isinstance(theme, tuple):
            theme, dark = theme
        self.theme = {
            'window_bg': '#F2F2F7', 'card_bg': '#FFFFFF',
            'field': '#E7E7EA', 'fill': '#E9E9EB', 'label': '#1C1C1E',
            'label2': '#6C6C70', 'accent': '#007AFF',
        }
        self.theme.update(theme or {})
        self._theme_overrides = theme or {}
        self.win = self.tk.Toplevel(ui._root)
        try:
            if dark is None:
                rgb = self.win.winfo_rgb(self.theme['card_bg'])
                dark = sum(rgb) / 3 < 32768
            self._dark = bool(dark)
            self._build()
            if session is None:
                self.update(self._left_row, self._right_row)
            else:
                self.restore_session(session)
        except Exception:
            self.close()
            raise

    def _build(self):
        tk, win, t = self.tk, self.win, self.theme
        if self._dark:
            t.update({key: value for key, value in {
                'window_bg': '#202024', 'card_bg': '#2C2C2E', 'field': '#38383C',
                'fill': '#333338', 'label': '#F5F5F7', 'label2': '#AAAAB2',
                'accent': '#69A9FF',
            }.items() if key not in self._theme_overrides})
        t.update({key: value for key, value in ({
            'toolbar_bg': '#2C2C32', 'toolbar_border': '#60606B',
            'toolbar_hover_bg': '#263D59', 'toolbar_hover_border': '#7193BE',
            'toolbar_disabled_border': '#43434C', 'toolbar_focus': '#B9D7FF',
            'toolbar_press_bg': '#245BA8',
            'accent_soft': '#253C59', 'card_border': '#3D3D42',
        } if self._dark else {
            'toolbar_bg': '#FFFFFF', 'toolbar_border': '#B8BEC8',
            'toolbar_hover_bg': '#EDF5FF', 'toolbar_hover_border': '#799BC7',
            'toolbar_disabled_border': '#D8DCE3', 'toolbar_focus': '#245BA8',
            'toolbar_press_bg': '#245BA8',
            'accent_soft': '#E5F0FF', 'card_border': '#D8D8DE',
        }).items() if key not in self._theme_overrides})
        try:
            self._scale = max(1.0, float(win.winfo_fpixels('1i')) / 96)
        except (AttributeError, TclError):
            self._scale = 1.0
        self._ui_font = ('Microsoft YaHei UI', 9)
        pad = round(8 * self._scale)
        win.title('差异比较 · 只读预览')
        win.configure(bg=t['window_bg'])
        win.attributes('-topmost', False)
        win.resizable(True, True)
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        width = min(round(1280 * self._scale), max(1, sw - 40))
        height = min(round(840 * self._scale), max(1, sh - 80))
        win.geometry(f'{width}x{height}+{max(0, (sw - width) // 2)}+{max(0, (sh - height) // 2)}')
        win.minsize(min(round(760 * self._scale), width), min(round(420 * self._scale), height))
        win.protocol('WM_DELETE_WINDOW', self.close)
        win.bind('<Destroy>', self._destroyed, add='+')
        win.bind('<Escape>', self._escape)
        win.bind('<ButtonPress-1>', self._outside_rules, add='+')
        win.bind('<FocusOut>', self._rules_focus_out, add='+')
        win.bind('<Configure>', self._popup_configure, add='+')
        win.grid_rowconfigure(2, weight=1)
        win.grid_columnconfigure(0, weight=1)
        # 标题与只读说明合并到原生标题栏，正文不再占用重复标题行。
        options = tk.Frame(win, bg=t['window_bg'])
        options.grid(row=1, column=0, sticky='ew', padx=pad,
                     pady=(round(4 * self._scale), round(4 * self._scale)))
        self._options = {key: tk.BooleanVar(master=win, value=False) for key in
                         ('ignore_trailing_space', 'ignore_line_endings')}
        self._toolbar_controls = []
        gap = round(6 * self._scale)
        self._rules_button = self._toolbar_control(options, '比较规则 ▾', self._toggle_rules, 116)
        self._rules_button.pack(side='left')
        self._mode_button = self._toolbar_control(options, '原文比较 ▾', self._toggle_mode, 172)
        self._mode_button.pack(side='left', padx=(gap, 0))
        self._block_label = _BlockLabel(self, options)
        self._block_label.pack(side='right', padx=(pad, 0))
        self._navigation = self._toolbar_control(options, width=152)
        self._navigation.pack(side='right')
        self._previous = self._toolbar_control(self._navigation.canvas, '↑ 上一个', lambda: self._navigate(-1),
                                               76, edge='left')
        self._previous.place(x=0, y=0)
        self._previous.set_tooltip('上一处')
        self._next = self._toolbar_control(self._navigation.canvas, '下一个 ↓', lambda: self._navigate(1),
                                           76)
        self._next.place(x=round(76 * self._scale), y=0)
        self._next.set_tooltip('下一处')
        self._navigation._segments = [self._previous, self._next]
        for control, start in zip(self._navigation._segments, (0, 76)):
            control._group = self._navigation
            control._segment_start = round(start * self._scale)
        self._navigation._draw()
        self._swap_button = self._toolbar_control(options, '交换 A/B', self._swap, 108)
        self._swap_button.configure(bg=t['window_bg'], fg=t['label'], icon='swap')
        self._swap_button.pack(side='right', padx=(0, round(12 * self._scale)))
        self._swap_button.set_tooltip('交换左右')
        body = tk.Frame(win, bg=t['window_bg'])
        body.grid(row=2, column=0, sticky='nsew', padx=pad, pady=0)
        body.grid_rowconfigure(0, weight=1)
        for col in (0, 1):
            body.grid_columnconfigure(col, weight=1, uniform='side')
        self._texts, self._gutters, self._block_markers = [], [], []
        self._yscroll = tk.Scrollbar(body, orient='vertical', command=self._scroll_y)
        self._yscroll.grid(row=0, column=2, sticky='ns')
        for side in range(2):
            pane = tk.Frame(body, bg=t['card_bg'], bd=0, highlightthickness=1,
                            highlightbackground=t['card_border'], highlightcolor=t['card_border'])
            pane.grid(row=0, column=side, sticky='nsew',
                      padx=(0, round(3 * self._scale)) if side == 0 else (round(3 * self._scale), 0))
            pane.grid_rowconfigure(1, weight=1)
            pane.grid_columnconfigure(1, weight=1)
            card_header = tk.Frame(pane, bg=t['card_bg'])
            card_header.grid(row=0, column=0, columnspan=2, sticky='ew',
                             padx=round(4 * self._scale), pady=round(5 * self._scale))
            card_header.grid_columnconfigure(1, weight=1)
            badge_color = ('#69AAFF' if self._dark else '#007AFF') if side == 0 else (
                '#C39AFF' if self._dark else '#8050CC')
            badge = tk.Label(card_header, text='A' if side == 0 else 'B',
                             font=('Microsoft YaHei UI', 11, 'bold'), fg=badge_color,
                             bg=t['field'], padx=8, pady=3)
            badge.grid(row=0, column=0, rowspan=2, padx=(0, 8))
            self._badges.append(badge)
            summary = tk.Label(card_header, anchor='w', width=1, font=self._ui_font,
                               bg=t['card_bg'], fg=t['label'])
            summary.grid(row=0, column=1, sticky='ew')
            meta = tk.Label(card_header, anchor='w', width=1, font=('Microsoft YaHei UI', 8),
                            bg=t['card_bg'], fg=t['label2'])
            meta.grid(row=1, column=1, sticky='ew')
            copy_button = self._button(card_header, '复制 A' if side == 0 else '复制 B',
                                       lambda s=side: self._copy(s))
            copy_button.grid(row=0, column=2, rowspan=2, padx=(8, 0))
            if side == 0:
                self._meta_left, self._summary_left, self._copy_left = meta, summary, copy_button
            else:
                self._meta_right, self._summary_right, self._copy_right = meta, summary, copy_button
            common = dict(font=('Consolas', 11), wrap='none', bd=0,
                          highlightthickness=0, padx=4, pady=4, spacing1=0,
                          spacing2=0, spacing3=0, state='disabled', undo=False,
                          exportselection=False)
            gutter_frame = tk.Frame(pane, bg=t['field'], bd=0)
            gutter_frame.grid(row=1, column=0, sticky='ns')
            gutter_frame.grid_rowconfigure(0, weight=1)
            gutter = tk.Text(gutter_frame, width=5, takefocus=0, bg=t['field'], fg=t['label2'], **common)
            marker = tk.Canvas(gutter_frame, width=2, bg=t['field'], bd=0,
                               highlightthickness=0, takefocus=0)
            gutter.grid(row=0, column=0, sticky='ns')
            marker.grid(row=0, column=1, sticky='ns')
            self._block_markers.append(marker)
            text = tk.Text(pane, width=1, bg=t['card_bg'], fg=t['label'],
                           selectbackground=t['accent'], selectforeground='#FFFFFF', **common)
            text.grid(row=1, column=1, sticky='nsew')
            for widget in (text, gutter, marker):
                widget.bind('<Configure>', self._queue_marker_redraw, add='+')
            gutter.tag_configure('numbers', justify='right')
            gutter.tag_configure('block', background='#253C59' if self._dark else '#E5F0FF',
                                 foreground=t['accent'], font=('Consolas', 11, 'bold'))
            xscroll = tk.Scrollbar(pane, orient='horizontal', command=text.xview)
            xscroll.grid(row=2, column=1, sticky='ew')
            text.configure(xscrollcommand=xscroll.set)
            self._texts.append(text)
            self._gutters.append(gutter)
            for widget in (text, gutter):
                widget.configure(yscrollcommand=lambda first, last, w=widget: self._on_y(w, first, last))
                widget.bind('<MouseWheel>', self._wheel)
                widget.bind('<Button-4>', self._wheel)
                widget.bind('<Button-5>', self._wheel)
                for event in ('<<Paste>>', '<<Cut>>', '<Control-v>', '<Control-V>',
                              '<Control-x>', '<Control-X>', '<Shift-Insert>', '<Button-2>'):
                    widget.bind(event, lambda event: 'break')
                for event in ('<<Copy>>', '<Control-c>', '<Control-C>', '<Control-Insert>'):
                    widget.bind(event, lambda event, s=side, w=widget, g=gutter:
                                self._copy_event(s) if w is not g else 'break')
        self._configure_diff_tags()
        self._footer = footer = tk.Frame(win, bg=t['window_bg'])
        footer.grid(row=3, column=0, sticky='ew', padx=pad,
                    pady=(round(4 * self._scale), round(4 * self._scale)))
        footer.grid_columnconfigure(0, weight=1)
        self._status = tk.Label(footer, anchor='w', width=1, font=self._ui_font,
                                bg=t['window_bg'], fg=t['label'])
        self._status.grid(row=0, column=0, sticky='ew')
        self._hint = tk.Label(footer, anchor='w', width=1, font=('Microsoft YaHei UI', 8),
                              bg=t['window_bg'], fg=t['label2'])
        self._hint.grid(row=1, column=0, sticky='ew', pady=(round(3 * self._scale), 0))
        self._status_text = self._hint_text = ''
        self._sync_footer()
        self._refresh_rules()
        self._refresh_actions()

    def _configure_diff_tags(self):
        # 两侧都存在但值不同的行统一琥珀底，不再左红右绿；红底只留给单侧存在的行。
        palette = ({'missing': '#38383C', 'current': '#304F73',
                    'changed': '#514126', 'emphasis_changed': '#715321',
                    'one_sided': '#512F3C', 'emphasis_one_sided': '#784052',
                    'missing_field': '#48484E'} if self._dark else
                   {'missing': '#E7E7EA', 'current': '#CFE2FF',
                    'changed': '#F5D99B', 'emphasis_changed': '#E8BC60',
                    'one_sided': '#FBE9EF', 'emphasis_one_sided': '#F1B8CA',
                    'missing_field': '#D8D8DC'})
        for text in self._texts:
            for tag, color in palette.items():
                text.tag_configure(tag, background=color)
            text.tag_configure('missing_field', foreground='#C4C4CC' if self._dark else '#55555C')
            # 普通行底/空占位 < 当前块 < 单侧存在红底 < 字符强调/字段缺失灰提示 < 原生选择。
            for tag in ('changed', 'missing', 'current', 'one_sided',
                        'emphasis_changed', 'emphasis_one_sided', 'missing_field', 'sel'):
                text.tag_raise(tag)

    def _set_status(self, text):
        self._status_text = text
        self._status.configure(text=text)
        self._sync_footer()

    def _set_hint(self, text):
        self._hint_text = text
        self._hint.configure(text=text)
        self._sync_footer()

    def _sync_footer(self):
        if self._status_text:
            self._status.grid()
        else:
            self._status.grid_remove()
        if self._hint_text:
            self._hint.grid(row=1 if self._status_text else 0,
                            pady=(round(3 * self._scale) if self._status_text else 0, 0))
        else:
            self._hint.grid_remove()
        if self._status_text or self._hint_text:
            self._footer.grid()
        else:
            self._footer.grid_remove()

    def _button(self, parent, label, command):
        t = self.theme
        return self.tk.Button(parent, text=label, command=command, bg=t['fill'],
                              fg=t['label'], activebackground=t['field'],
                              activeforeground=t['label'], font=self._ui_font,
                              relief='flat', bd=0, padx=8, pady=4)

    def _toolbar_control(self, parent, text='', command=None, width=112, edge=None):
        control = _ToolbarControl(self, parent, text, command, width, edge)
        self._toolbar_controls.append(control)
        return control

    def _select_mode(self, mode):
        if not self.is_alive():
            return
        self._close_rules()
        if mode != self._mode:
            self._mode = mode
            self._request()

    def _toggle_mode(self):
        if self._mode_popup is not None:
            self._mode_popup._keyboard = not self._mode_button._mouse_focus
            self._close_rules()
            return
        self._close_rules(restore_focus=False)
        if not self.is_alive():
            return
        descriptions = ('逐行比较，保留空白与换行差异', '统一排版后比较文本',
                        '按字段路径、类型和值比较')
        popup = _PopupMenu(self, self._mode_button,
                           [(mode, label, description) for (mode, label), description in
                            zip(self._mode_labels.items(), descriptions)])
        self._mode_popup = popup
        self._place_popup(popup, self._mode_button)

    def _place_popup(self, popup, button, focus=True):
        self.win.update_idletasks()
        width = min(popup._width, max(1, self.win.winfo_width()))
        height = min(popup._height, max(1, self.win.winfo_height()))
        x = button.winfo_rootx() - self.win.winfo_rootx()
        y = button.winfo_rooty() - self.win.winfo_rooty() + button.winfo_height() + round(4 * self._scale)
        x = max(0, min(x, self.win.winfo_width() - width))
        y = max(0, min(y, self.win.winfo_height() - height))
        popup.resize(width, height)
        popup.place(x=x, y=y)
        self.tk.Misc.lift(popup.canvas)
        if focus:
            button._hide_tooltip()
            button._clear_press()
            popup.canvas.focus_set()

    def _refresh_rules(self):
        labels = [('ignore_trailing_space', '忽略行尾空白'),
                  ('ignore_line_endings', '忽略换行符')]
        fields = self._mode == 'json_fields'
        enabled = [label for key, label in labels if self._options[key].get() and not fields]
        t = self.theme
        self._rules_button.configure(state='disabled' if fields else 'normal',
                                     marker='dot' if enabled else None,
                                     bg=t['accent_soft'] if enabled else t['toolbar_bg'],
                                     fg=t['accent'] if enabled else t['label'])
        self._rules_button.set_tooltip('已启用：' + '、'.join(enabled) if enabled else '未启用比较规则')
        if fields:
            self._rules_button.set_tooltip('JSON按字段比较不适用文本忽略规则；已保留规则设置')
        selected = self._mode != 'text'
        self._mode_button.configure(text=self._mode_labels[self._mode] + ' ▾',
                                    bg=t['accent_soft'] if selected else t['toolbar_bg'],
                                    fg=t['accent'] if selected else t['label'],
                                    marker='check' if selected else None)
        self._mode_button.set_tooltip('三种模式互斥；JSON按字段比较复制原文')
        for popup in (self._rules_popup, self._mode_popup):
            if popup is not None:
                popup.refresh()

    def _toggle_rules(self):
        if self._rules_popup is not None:
            self._rules_popup._keyboard = not self._rules_button._mouse_focus
            self._close_rules()
            return
        self._close_rules(restore_focus=False)
        if not self.is_alive():
            return
        popup = _PopupMenu(self, self._rules_button, [
            ('ignore_trailing_space', '忽略行尾空格 / Tab', '比较时忽略行尾空格与 Tab'),
            ('ignore_line_endings', '忽略换行符类型', '比较时忽略 LF / CR / CRLF 差异'),
        ], rules=True)
        self._rules_popup = popup
        self._place_popup(popup, self._rules_button)

    def _close_rules(self, restore_focus=True):
        if self._popup_after is not None:
            with suppress(TclError):
                self.win.after_cancel(self._popup_after)
            self._popup_after = None
        popups = (self._rules_popup, self._mode_popup)
        self._rules_popup = self._mode_popup = None
        for popup in popups:
            if popup is not None:
                with suppress(TclError):
                    popup.destroy()
                    trigger = popup.trigger
                    if restore_focus and not self._closed and trigger.options['state'] != 'disabled':
                        trigger._mouse_focus = not popup._keyboard
                        trigger.canvas.focus_set()
                        trigger._focus(True)

    def _outside_rules(self, event, fallback_widget=None):
        widget = getattr(event, 'widget', fallback_widget)
        while widget is not None:
            if (widget in (self._rules_button.canvas, self._mode_button.canvas)
                    or any(popup is not None and widget is popup.canvas
                           for popup in (self._rules_popup, self._mode_popup))):
                return
            widget = getattr(widget, 'master', getattr(widget, 'parent', None))
        self._close_rules(restore_focus=False)

    def _popup_configure(self, event):
        if event.widget is self.win and not self._closed:
            for popup in (self._rules_popup, self._mode_popup):
                if popup is not None:
                    self._place_popup(popup, popup.trigger, focus=False)

    def _rules_focus_out(self, event):
        if (self._rules_popup is not None or self._mode_popup is not None) and self._popup_after is None:
            self._popup_after = self.win.after(0, self._check_rules_focus)

    def _check_rules_focus(self):
        self._popup_after = None
        if not self.is_alive():
            return
        focus = self.win.focus_get()
        if focus is None or focus.winfo_toplevel() is not self.win:
            self._close_rules(restore_focus=False)

    def _escape(self, event):
        if self._rules_popup is not None or self._mode_popup is not None:
            self._close_rules()
        else:
            self.close()
        return 'break'

    def is_alive(self):
        if self._closed:
            return False
        try:
            return bool(self.win.winfo_exists())
        except TclError:
            return False

    def export_session(self):
        """UI 线程读取当前请求配置，不保存结果或滚动位置。"""
        return {'rows': (dict(self._left_row), dict(self._right_row)),
                'roles': tuple(self._roles), 'mode': self._mode,
                'options': {key: var.get() for key, var in self._options.items()}}

    def restore_session(self, session):
        if not self.is_alive():
            return
        self._left_row, self._right_row = map(dict, session['rows'])
        self._roles = tuple(session['roles'])
        self._mode = session['mode']
        for key, value in session['options'].items():
            self._options[key].set(value)
        self._request()

    def _save_session(self):
        save = getattr(self.ui, '_save_diff_session', None)
        if callable(save):
            # 父窗口销毁时 Tk 变量可能已不可读，保留最近一次 UI 快照。
            with suppress(TclError):
                save(self)

    def update(self, left_row, right_row):
        if not self.is_alive():
            return
        self._left_row, self._right_row = dict(left_row), dict(right_row)
        self._roles = ('A', 'B')
        self._request()

    def _swap(self):
        if not self.is_alive():
            return
        self._left_row, self._right_row = self._right_row, self._left_row
        self._roles = self._roles[::-1]
        self._request()

    def _refresh_roles(self, roles):
        for badge, button, role in zip(self._badges, (self._copy_left, self._copy_right), roles):
            color = ('#69AAFF' if self._dark else '#007AFF') if role == 'A' else (
                '#C39AFF' if self._dark else '#8050CC')
            badge.configure(text=role, fg=color)
            button.configure(text='复制 ' + role)

    def _set_meta(self, rows):
        for label, summary, row in zip((self._meta_left, self._meta_right),
                                       (self._summary_left, self._summary_right), rows):
            content = row.get('content')
            preview = ' '.join(content[:100].split()) if isinstance(content, str) else ''
            summary.configure(text=(preview[:48] + '…' if len(preview) > 48 else preview) or '空文本')
            label.configure(text=f"ID: {row.get('id', '—')} · {row.get('source') or '—'} · "
                                 f"{row.get('created_at') or '—'}")

    def _request(self):
        if not self.is_alive():
            return
        self._generation += 1
        self._pending = (self._generation, dict(self._left_row), dict(self._right_row),
                         dict(mode=self._mode, **{key: var.get() for key, var in self._options.items()}))
        self._save_session()
        self._busy = True
        self._retained = False
        self._refresh_rules()
        self._refresh_roles(self._roles)
        self._set_meta((self._left_row, self._right_row))
        self._set_status('正在比较…')
        self._set_hint('复制和导航暂不可用')
        self._refresh_actions()
        self._start_pending()
        self._schedule_poll()

    @staticmethod
    def _compare(request, outcomes):
        generation, left, right, options = request
        try:
            options = dict(options)
            mode = options.pop('mode')
            if mode == 'json_fields':
                result = diff_tools.compare_json_fields(left['content'], right['content'])
            else:
                result = diff_tools.compare_text(left['content'], right['content'],
                                                 json_format=mode == 'json_format', **options)
            outcome = (generation, result, None, (left, right))
        except Exception as error:
            outcome = (generation, None, str(error), (left, right))
        outcomes.put(outcome)

    def _start_pending(self):
        # 只有 UI 线程启动 worker；旧线程真正退出后才启动最新请求。
        if self._pending is None or (self._worker is not None and self._worker.is_alive()):
            return
        request, self._pending = self._pending, None
        self._worker = Thread(target=DiffWindow._compare, args=(request, self._outcomes), daemon=True)
        self._worker.start()

    def _schedule_poll(self):
        if not self._closed and self._after_id is None:
            self._after_id = self.win.after(40, self._poll)

    def _poll(self):
        self._after_id = None
        if not self.is_alive():
            return
        try:
            generation, result, error, rows = self._outcomes.get_nowait()
        except Empty:
            pass
        else:
            if generation == self._generation:
                self._busy = False
                if error is not None:
                    if self._display_rows is not None:
                        self._left_row, self._right_row = map(dict, self._display_rows)
                        self._roles = self._display_roles
                        self._refresh_roles(self._roles)
                        self._set_meta(self._display_rows)
                    self._retained = self._result is not None
                    if self._display_options is not None:
                        for key, value in self._display_options.items():
                            self._options[key].set(value)
                        self._mode = self._display_mode
                        self._refresh_rules()
                    self._set_status('比较失败' +
                                     ('；保留上次结果' if self._retained else '') + f'：{error}')
                    self._set_hint('规则已恢复为上次结果；复制使用上次结果'
                                   if self._retained else '请检查输入文本或比较规则')
                else:
                    self._retained = False
                    self._result = result
                    self._display_options = {key: var.get() for key, var in self._options.items()}
                    self._display_mode = self._mode
                    self._display_rows = rows
                    self._display_roles = self._roles
                    self._set_meta(rows)
                    self._render(result)
                self._save_session()
                self._refresh_actions()
        self._start_pending()
        if self._busy or self._pending is not None or (self._worker is not None and self._worker.is_alive()):
            self._schedule_poll()

    def _render(self, result):
        self._configure_diff_tags()
        self._block_index = -1
        if self._marker_after is not None:
            with suppress(TclError):
                self.win.after_cancel(self._marker_after)
            self._marker_after = None
        self._redraw_markers()
        eol_names = {'': '∅', '\n': 'LF', '\r': 'CR', '\r\n': 'CRLF'}
        self._syncing = True
        try:
            for side, text, gutter in zip(('left', 'right'), self._texts, self._gutters):
                for tag in ('changed', 'one_sided', 'missing', 'current',
                            'emphasis_changed', 'emphasis_one_sided', 'missing_field'):
                    text.tag_remove(tag, '1.0', 'end')
                gutter.tag_remove('block', '1.0', 'end')
                lines, numbers = [], []
                for row in result.rows:
                    value = getattr(row, side + '_text')
                    if result.mode == 'json_fields' and getattr(row, side + '_no') is None:
                        value = '字段不存在'
                    lines.append(value)
                    number = getattr(row, side + '_no')
                    label = '' if number is None else str(number)
                    if result.mode != 'json_fields' and number is not None and row.left_eol != row.right_eol:
                        label += ' ' + eol_names[getattr(row, side + '_eol')]
                    numbers.append(label)
                for widget, content in ((text, lines), (gutter, numbers)):
                    widget.configure(state='normal')
                    widget.delete('1.0', 'end')
                    # 每侧始终有相同数量的显示行；末尾 Tk 空行不参与有效文本复制。
                    widget.insert('1.0', '\n'.join(content) + ('\n' if content else ''))
                    widget.configure(state='disabled')
                gutter.configure(width=max(5, max(map(len, numbers), default=0) + 1))
                gutter.tag_add('numbers', '1.0', 'end')
                for index, row in enumerate(result.rows, 1):
                    present = getattr(row, side + '_no') is not None
                    if row.kind in ('insert', 'delete'):
                        tag = 'one_sided'
                    elif row.kind == 'neutral':
                        # 同名字段值相同、仅位置错开：不标任何底色，空侧也不涂灰。
                        tag = None
                    else:
                        # 两侧都存在（replace / shared_change）统一琥珀底与同色字符强调。
                        tag = 'changed'
                    if not present and row.kind not in ('neutral', 'shared_change'):
                        text.tag_add('missing', f'{index}.0', f'{index + 1}.0')
                        if result.mode == 'json_fields':
                            text.tag_add('missing_field', f'{index}.0', f'{index}.{len(lines[index - 1])}')
                    if present and tag is not None and row.kind != 'equal':
                        text.tag_add(tag, f'{index}.0', f'{index + 1}.0')
                        for start, end in getattr(row, side + '_spans'):
                            text.tag_add('emphasis_' + tag, f'{index}.{start}', f'{index}.{end}')
                text.yview_moveto(0)
                text.xview_moveto(0)
                gutter.yview_moveto(0)
        finally:
            self._syncing = False
        self._yscroll.set(*self._texts[0].yview())
        if result.exact_equal:
            status = '原文一致'
        elif result.rule_equal:
            json_only_equal = (self._display_mode == 'json_format' and result.left_text == result.right_text)
            status = ('按字段一致，原文有差异' if result.mode == 'json_fields' else
                      '格式化后一致，原文有差异' if json_only_equal else '按规则一致，原文有差异')
        else:
            status = ''
        self._set_status(status)
        hints = []
        if result.mode != 'json_fields' and any(row.left_eol != row.right_eol for row in result.rows):
            hints.append('行号旁 LF / CR / CRLF / ∅ 表示换行差异')
        if result.left_duplicate_keys or result.right_duplicate_keys:
            hints.append('JSON 含重复键')
        if self._display_mode == 'json_format':
            hints.append('已格式化显示，原始记录不变')
        if result.mode == 'json_fields':
            hints.append('字段模式：行号为侧项目序号，非原文行号；文本忽略规则不适用；复制保留原文')
        self._set_hint('；'.join(hints))

    def _refresh_actions(self):
        ready = not self._busy and self._result is not None and not self._closed
        for button in (self._copy_left, self._copy_right):
            button.configure(state='normal' if ready else 'disabled')
        blocks = self._result.blocks if self._result is not None else ()
        for button in (self._previous, self._next):
            button.configure(state='normal' if ready and blocks else 'disabled')
        current = ''
        unit = '项' if self._result is not None and self._result.mode == 'json_fields' else '处'
        if self._busy:
            text = '比较中…'
        elif not blocks:
            text = '无差异'
        elif self._block_index < 0:
            text = f'待定位 / {len(blocks)} {unit}'
        else:
            current = str(self._block_index + 1)
            text = f'第 {current} / {len(blocks)} {unit}'
        self._block_label.configure(text=text, current=current)

    def _queue_marker_redraw(self, event=None):
        if not self._closed and self._block_index >= 0 and self._marker_after is None:
            self._marker_after = self.win.after(0, self._flush_marker_redraw)

    def _flush_marker_redraw(self):
        self._marker_after = None
        self._redraw_markers()

    def _redraw_markers(self):
        for text, marker in zip(self._texts, self._block_markers):
            with suppress(TclError, AttributeError):
                marker.delete('block')
                if self._closed or self._result is None or self._block_index < 0:
                    continue
                start = self._result.blocks[self._block_index]
                stop = start + 1
                while (self._result.mode != 'json_fields' and stop < len(self._result.rows)
                       and self._result.rows[stop].kind not in ('equal', 'neutral')):
                    stop += 1
                # dlineinfo 对不可见行返回 None；只查询视口内的行，避免扫描长区段。
                first = max(start + 1, int(text.index('@0,0').split('.')[0]))
                last = min(stop, int(text.index(f'@0,{text.winfo_height()}').split('.')[0]))
                top = bottom = None
                for line in range(first, last + 1):
                    info = text.dlineinfo(f'{line}.0')
                    if info is not None:
                        y, height = info[1], info[3]
                        top = y if top is None else min(top, y)
                        bottom = y + height if bottom is None else max(bottom, y + height)
                if top is not None:
                    top, bottom = max(0, top), min(marker.winfo_height(), bottom)
                    if bottom > top:
                        marker.create_rectangle(0, top, 2, bottom, fill=self.theme['accent'],
                                                outline='', tags='block')

    def _scroll_y(self, *args):
        if self._closed:
            return
        self._syncing = True
        try:
            for widget in (*self._texts, *self._gutters):
                widget.yview(*args)
        finally:
            self._syncing = False
        self._yscroll.set(*self._texts[0].yview())
        self._redraw_markers()
        self._queue_marker_redraw()

    def _on_y(self, source, first, last):
        if self._closed or self._syncing:
            return
        self._syncing = True
        try:
            for widget in (*self._texts, *self._gutters):
                if widget is not source and abs(widget.yview()[0] - float(first)) > 0.000001:
                    widget.yview_moveto(first)
            self._yscroll.set(first, last)
        finally:
            self._syncing = False
        self._redraw_markers()
        self._queue_marker_redraw()

    def _wheel(self, event):
        num = getattr(event, 'num', None)
        if num in (4, 5):
            steps = -3 if num == 4 else 3
        else:
            self._wheel_remainder += getattr(event, 'delta', 0)
            notches = int(self._wheel_remainder / 120)
            self._wheel_remainder -= notches * 120
            steps = -3 * notches
        if steps:
            self._scroll_y('scroll', steps, 'units')
        return 'break'

    def _navigate(self, direction):
        if self._closed or self._busy or self._result is None or not self._result.blocks:
            return
        blocks = self._result.blocks
        self._block_index = ((0 if direction > 0 else len(blocks) - 1)
                             if self._block_index < 0 else (self._block_index + direction) % len(blocks))
        index = f'{blocks[self._block_index] + 1}.0'
        self._scroll_y(index)
        stop = blocks[self._block_index] + 1
        while (self._result.mode != 'json_fields' and stop < len(self._result.rows)
               and self._result.rows[stop].kind not in ('equal', 'neutral')):
            stop += 1
        for text, gutter in zip(self._texts, self._gutters):
            text.tag_remove('current', '1.0', 'end')
            text.tag_add('current', index, f'{stop + 1}.0')
            gutter.tag_remove('block', '1.0', 'end')
            gutter.tag_add('block', index, f'{stop + 1}.0')
        self._redraw_markers()
        self._queue_marker_redraw()
        self._refresh_actions()

    def _copy_event(self, side):
        self._copy(side)
        return 'break'

    def _copy(self, side):
        if self._closed or self._busy or self._result is None:
            return
        content = self._result.left_text if side == 0 else self._result.right_text
        # id 必须为空，防止动作层按记录 ID 取回未格式化的旧原文。
        payload = {'id': None, 'kind': 'text', 'category': 'text', 'content': content,
                   'data': None, 'file_path': ''}
        try:
            ok = self.ui.actions.copy_to_clipboard(payload)
            success = ('已复制完整原文（非字段列表）' if self._result.mode == 'json_fields'
                       else '已复制完整有效文本')
            self._set_status(success if ok else '复制失败')
        except Exception as error:
            self._set_status(f'复制失败：{error}')

    def _export(self):
        if self._closed or self._busy or self._result is None:
            return
        generation, result = self._generation, self._result
        path = filedialog.asksaveasfilename(parent=self.win, title='导出统一差异',
                                          defaultextension='.diff',
                                          filetypes=[('统一差异', '*.diff'), ('所有文件', '*.*')])
        # 原生对话框可嵌套处理事件；期间更新或关闭就不再导出旧结果。
        if not path or self._closed or self._busy or generation != self._generation:
            return
        try:
            with open(path, 'w', encoding='utf-8', newline='') as stream:
                stream.write(result.unified_diff)
            self._set_status('已导出 UTF-8 统一差异')
        except (OSError, UnicodeError) as error:
            self._set_status(f'导出失败：{error}')

    def _destroyed(self, event):
        if event.widget is self.win:
            self.close()

    def close(self):
        if self._closed:
            return
        save = getattr(self, '_save_session', None)
        if callable(save):
            save()
        if (getattr(self, '_rules_popup', None) is not None
                or getattr(self, '_mode_popup', None) is not None
                or getattr(self, '_popup_after', None) is not None):
            self._close_rules(restore_focus=False)
        self._closed = True
        if getattr(self, '_marker_after', None) is not None:
            with suppress(TclError):
                self.win.after_cancel(self._marker_after)
            self._marker_after = None
        for marker in getattr(self, '_block_markers', ()):
            with suppress(TclError):
                marker.delete('block')
        for control in getattr(self, '_toolbar_controls', ()):
            control.close()
        self._generation += 1
        self._pending = None
        if self._after_id is not None:
            with suppress(TclError):
                self.win.after_cancel(self._after_id)
            self._after_id = None
        if getattr(self.ui, '_diff_window', None) is self:
            self.ui._diff_window = None
        with suppress(TclError):
            self.win.destroy()
