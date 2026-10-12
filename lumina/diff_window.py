"""独立文本差异窗口；后台只比较数据，所有 Tk 操作留在 UI 线程。"""

from contextlib import suppress
from queue import Empty, Queue
from threading import Thread
from tkinter import TclError, filedialog

from . import diff_tools


class DiffWindow:
    def __init__(self, ui, left_row, right_row, theme=None):
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
        self._block_index = -1
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
        self.win = self.tk.Toplevel(ui._root)
        try:
            if dark is None:
                rgb = self.win.winfo_rgb(self.theme['card_bg'])
                dark = sum(rgb) / 3 < 32768
            self._dark = bool(dark)
            self._build()
            self.update(self._left_row, self._right_row)
        except Exception:
            self.close()
            raise

    def _build(self):
        tk, win, t = self.tk, self.win, self.theme
        win.title('文本差异比较')
        win.configure(bg=t['window_bg'])
        win.attributes('-topmost', False)
        win.resizable(True, True)
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        width, height = min(1000, max(1, sw - 40)), min(650, max(1, sh - 80))
        win.geometry(f'{width}x{height}+{max(0, (sw - width) // 2)}+{max(0, (sh - height) // 2)}')
        win.minsize(min(600, width), min(360, height))
        win.protocol('WM_DELETE_WINDOW', self.close)
        win.bind('<Destroy>', self._destroyed, add='+')
        win.bind('<Escape>', lambda event: self.close())
        win.grid_rowconfigure(2, weight=1)
        win.grid_columnconfigure(0, weight=1)
        header = tk.Frame(win, bg=t['window_bg'])
        header.grid(row=0, column=0, sticky='ew', padx=10, pady=(10, 4))
        header.grid_columnconfigure(0, weight=1, uniform='meta')
        header.grid_columnconfigure(2, weight=1, uniform='meta')
        self._meta_left = tk.Label(header, anchor='w', bg=t['window_bg'], fg=t['label'])
        self._meta_left.grid(row=0, column=0, sticky='ew')
        self._button(header, '交换左右', self._swap).grid(row=0, column=1, padx=8)
        self._meta_right = tk.Label(header, anchor='w', bg=t['window_bg'], fg=t['label'])
        self._meta_right.grid(row=0, column=2, sticky='ew')
        options = tk.Frame(win, bg=t['window_bg'])
        options.grid(row=1, column=0, sticky='ew', padx=10, pady=4)
        self._options = {}
        for key, label in (('ignore_trailing_space', '忽略行尾空格 / Tab'),
                           ('ignore_line_endings', '忽略换行符类型'),
                           ('json_format', 'JSON 格式化')):
            var = tk.BooleanVar(master=win, value=False)
            self._options[key] = var
            tk.Checkbutton(options, text=label, variable=var, command=self._request,
                           bg=t['window_bg'], fg=t['label'], selectcolor=t['field'],
                           activebackground=t['window_bg'], activeforeground=t['label']).pack(side='left')
        body = tk.Frame(win, bg=t['window_bg'])
        body.grid(row=2, column=0, sticky='nsew', padx=10, pady=4)
        body.grid_rowconfigure(0, weight=1)
        for col in (0, 1):
            body.grid_columnconfigure(col, weight=1, uniform='side')
        self._texts, self._gutters = [], []
        self._yscroll = tk.Scrollbar(body, orient='vertical', command=self._scroll_y)
        self._yscroll.grid(row=0, column=2, sticky='ns')
        for side in range(2):
            pane = tk.Frame(body, bg=t['card_bg'])
            pane.grid(row=0, column=side, sticky='nsew', padx=(0, 4) if side == 0 else (4, 0))
            pane.grid_rowconfigure(0, weight=1)
            pane.grid_columnconfigure(1, weight=1)
            common = dict(font=('Consolas', 11), wrap='none', bd=0,
                          highlightthickness=0, padx=4, pady=4, spacing1=0,
                          spacing2=0, spacing3=0, state='disabled', undo=False,
                          exportselection=False)
            gutter = tk.Text(pane, width=5, takefocus=0, bg=t['field'], fg=t['label2'], **common)
            text = tk.Text(pane, width=1, bg=t['card_bg'], fg=t['label'],
                           selectbackground=t['accent'], selectforeground='#FFFFFF', **common)
            gutter.grid(row=0, column=0, sticky='ns')
            text.grid(row=0, column=1, sticky='nsew')
            xscroll = tk.Scrollbar(pane, orient='horizontal', command=text.xview)
            xscroll.grid(row=1, column=1, sticky='ew')
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
            text.tag_configure('removed', background='#512F35' if self._dark else '#FFE8E8')
            text.tag_configure('added', background='#294637' if self._dark else '#E5F5E9')
            text.tag_configure('emphasis_removed', background='#914653' if self._dark else '#FFB8BE')
            text.tag_configure('emphasis_added', background='#397956' if self._dark else '#ADE5BC')
            text.tag_configure('block', underline=True)
        footer = tk.Frame(win, bg=t['window_bg'])
        footer.grid(row=3, column=0, sticky='ew', padx=10, pady=(4, 10))
        self._status = tk.Label(footer, anchor='w', bg=t['window_bg'], fg=t['label2'])
        self._status.pack(fill='x', pady=(0, 4))
        self._block_label = tk.Label(footer, bg=t['window_bg'], fg=t['label'])
        self._block_label.pack(side='left')
        self._previous = self._button(footer, '上一块', lambda: self._navigate(-1))
        self._next = self._button(footer, '下一块', lambda: self._navigate(1))
        self._previous.pack(side='left', padx=4)
        self._next.pack(side='left')
        self._copy_left = self._button(footer, '复制左侧', lambda: self._copy(0))
        self._copy_right = self._button(footer, '复制右侧', lambda: self._copy(1))
        self._export_button = self._button(footer, '导出统一 diff', self._export)
        for button in (self._export_button, self._copy_right, self._copy_left):
            button.pack(side='right', padx=4)
        self._refresh_actions()

    def _button(self, parent, label, command):
        t = self.theme
        return self.tk.Button(parent, text=label, command=command, bg=t['fill'],
                              fg=t['label'], activebackground=t['field'],
                              activeforeground=t['label'], relief='flat', padx=8)

    def is_alive(self):
        if self._closed:
            return False
        try:
            return bool(self.win.winfo_exists())
        except TclError:
            return False

    def update(self, left_row, right_row):
        if not self.is_alive():
            return
        self._left_row, self._right_row = dict(left_row), dict(right_row)
        self._request()

    def _swap(self):
        self.update(self._right_row, self._left_row)

    def _set_meta(self, rows):
        for label, name, row in zip((self._meta_left, self._meta_right), ('左', '右'), rows):
            label.configure(text=f"{name} ID: {row.get('id', '—')}  来源: {row.get('source') or '—'}\n"
                                 f"时间: {row.get('created_at') or '—'}")

    def _request(self):
        if not self.is_alive():
            return
        self._generation += 1
        self._pending = (self._generation, dict(self._left_row), dict(self._right_row),
                         {key: var.get() for key, var in self._options.items()})
        self._busy = True
        self._set_meta((self._left_row, self._right_row))
        self._status.configure(text='正在比较…（复制、导航和导出暂不可用）')
        self._refresh_actions()
        self._start_pending()
        self._schedule_poll()

    @staticmethod
    def _compare(request, outcomes):
        generation, left, right, options = request
        try:
            result = diff_tools.compare_text(left['content'], right['content'], **options)
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
                        self._set_meta(self._display_rows)
                    self._status.configure(text=f"比较失败：{error}" +
                                           ('；保留上次结果' if self._result is not None else ''))
                else:
                    self._result = result
                    self._display_rows = rows
                    self._set_meta(rows)
                    self._render(result)
                self._refresh_actions()
        self._start_pending()
        if self._busy or self._pending is not None or (self._worker is not None and self._worker.is_alive()):
            self._schedule_poll()

    def _render(self, result):
        self._block_index = -1
        eol_names = {'': '∅', '\n': 'LF', '\r': 'CR', '\r\n': 'CRLF'}
        self._syncing = True
        try:
            for side, text, gutter in zip(('left', 'right'), self._texts, self._gutters):
                lines, numbers = [], []
                for row in result.rows:
                    lines.append(getattr(row, side + '_text'))
                    number = getattr(row, side + '_no')
                    label = '' if number is None else str(number)
                    if number is not None and row.left_eol != row.right_eol:
                        label += ' ' + eol_names[getattr(row, side + '_eol')]
                    numbers.append(label)
                for widget, content in ((text, lines), (gutter, numbers)):
                    widget.configure(state='normal')
                    widget.delete('1.0', 'end')
                    # 每侧始终有相同数量的显示行；末尾 Tk 空行不参与有效文本复制。
                    widget.insert('1.0', '\n'.join(content) + ('\n' if content else ''))
                    widget.configure(state='disabled')
                gutter.configure(width=max(5, max(map(len, numbers), default=0) + 1))
                for index, row in enumerate(result.rows, 1):
                    present = getattr(row, side + '_no') is not None
                    tag = 'removed' if side == 'left' else 'added'
                    if present and row.kind != 'equal':
                        text.tag_add(tag, f'{index}.0', f'{index + 1}.0')
                        for start, end in getattr(row, side + '_spans'):
                            text.tag_add('emphasis_' + tag, f'{index}.{start}', f'{index}.{end}')
                text.yview_moveto(0)
                text.xview_moveto(0)
                gutter.yview_moveto(0)
        finally:
            self._syncing = False
        self._yscroll.set(*self._texts[0].yview())
        exact = '是' if result.exact_equal else '否'
        rules = '是' if result.rule_equal else '否'
        duplicates = bool(result.left_duplicate_keys or result.right_duplicate_keys)
        self._status.configure(text=f'原文完全相等：{exact}  规则相等：{rules}  '
                                    '行号旁 LF / CR / CRLF / ∅ 表示换行差异'
                                    + ('；JSON 含重复键' if duplicates else ''))

    def _refresh_actions(self):
        ready = not self._busy and self._result is not None and not self._closed
        for button in (self._copy_left, self._copy_right, self._export_button):
            button.configure(state='normal' if ready else 'disabled')
        blocks = self._result.blocks if self._result is not None else ()
        for button in (self._previous, self._next):
            button.configure(state='normal' if ready and blocks else 'disabled')
        self._block_label.configure(text=f'差异块 {self._block_index + 1}/{len(blocks)}')

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
        for text in self._texts:
            text.tag_remove('block', '1.0', 'end')
            text.tag_add('block', index, f'{blocks[self._block_index] + 2}.0')
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
            self._status.configure(text='已复制完整有效文本' if ok else '复制失败')
        except Exception as error:
            self._status.configure(text=f'复制失败：{error}')

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
            self._status.configure(text='已导出 UTF-8 统一差异')
        except (OSError, UnicodeError) as error:
            self._status.configure(text=f'导出失败：{error}')

    def _destroyed(self, event):
        if event.widget is self.win:
            self.close()

    def close(self):
        if self._closed:
            return
        self._closed = True
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
