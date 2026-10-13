import ast
import importlib.util
import sys
from pathlib import Path
from threading import Event, get_ident
from types import SimpleNamespace
import unittest
from unittest.mock import mock_open, patch

from lumina import diff_tools
from test_diff_ui_regressions import UiServer, NAMESPACE as UI_GLOBALS


# 隔离加载，不依赖 Tcl/Tk 安装，也不污染其他测试的 tkinter 模块。
SOURCE = Path(__file__).parent / 'lumina' / 'diff_window.py'
spec = importlib.util.spec_from_file_location('lumina._test_diff_window', SOURCE)
diff_window = importlib.util.module_from_spec(spec)
fake_tkinter = SimpleNamespace(TclError=type('TclError', (Exception,), {}),
                               filedialog=SimpleNamespace(asksaveasfilename=lambda **kwargs: ''))
with patch.dict(sys.modules, {'tkinter': fake_tkinter}):
    spec.loader.exec_module(diff_window)


class FakeWidget:
    def __init__(self, parent=None, **kwargs):
        self.parent = parent
        self.children = []
        if parent is not None:
            parent.children.append(self)
        self.row_weights = {}
        self.column_weights = {}
        self.options = dict(kwargs)
        self.bindings = {}
        self.tags = []
        self.content = ''
        self.exists = True
        self.grid_visible = False
        self.view = (0.0, 1.0)
        self.xcalls = []
        self.ycalls = []
        self.callbacks = {}
        self.cancelled = []
        self.attributes_calls = []
        self.protocols = {}
        self.owner = get_ident()

    def check_thread(self):
        if self.owner != get_ident():
            raise AssertionError('后台线程不可调用 Tk')

    def configure(self, **kwargs):
        self.check_thread()
        self.options.update(kwargs)

    def grid(self, **kwargs):
        self.check_thread()
        self.options.setdefault('grid', {}).update(kwargs)
        self.grid_visible = True

    def grid_remove(self):
        self.check_thread()
        self.grid_visible = False

    def pack(self, **kwargs):
        self.options['pack'] = kwargs

    def grid_rowconfigure(self, index, **kwargs):
        self.row_weights[index] = kwargs

    def grid_columnconfigure(self, index, **kwargs):
        self.column_weights[index] = kwargs

    def place(self, **kwargs):
        self.options['place'] = kwargs

    def deiconify(self):
        self.check_thread()
        self.options['deiconified'] = True

    def focus_force(self):
        self.check_thread()
        self.options['focused'] = True

    def lift(self):
        self.check_thread()
        self.options['lifted'] = True

    def update_idletasks(self):
        self.check_thread()

    def winfo_rootx(self):
        return 20

    def winfo_rooty(self):
        return 40

    def winfo_height(self):
        return self.options.get('height', 28)

    def winfo_width(self):
        return self.options.get('width', 860)

    def focus_set(self):
        self.options['focused'] = True

    def create_image(self, *args, **kwargs):
        self.options['image'] = kwargs

    def create_text(self, *args, **kwargs):
        self.options['canvas_text'] = kwargs

    def create_oval(self, *args, **kwargs):
        self.options['oval'] = kwargs

    def create_line(self, *args, **kwargs):
        self.options['line'] = kwargs

    def winfo_fpixels(self, value):
        return 96

    def focus_get(self):
        return getattr(self, 'focus', self)

    def winfo_toplevel(self):
        return self if self.parent is None or 'title' in self.options else self.parent.winfo_toplevel()

    def title(self, value):
        self.options['title'] = value

    def attributes(self, *args):
        self.attributes_calls.append(args)

    def resizable(self, *args):
        self.options['resizable'] = args

    def geometry(self, value):
        self.options['geometry'] = value

    def minsize(self, *args):
        self.options['minsize'] = args

    def protocol(self, name, callback):
        self.protocols[name] = callback

    def bind(self, event, callback, **kwargs):
        self.bindings[event] = callback

    def winfo_rgb(self, color):
        return tuple(int(color[i:i + 2], 16) * 257 for i in (1, 3, 5))

    def winfo_screenwidth(self):
        return 900

    def winfo_screenheight(self):
        return 600

    def winfo_exists(self):
        self.check_thread()
        return self.exists

    def after(self, delay, callback):
        self.check_thread()
        identifier = f'after-{len(self.callbacks) + len(self.cancelled)}'
        self.callbacks[identifier] = callback
        return identifier

    def after_cancel(self, identifier):
        self.check_thread()
        self.cancelled.append(identifier)
        self.callbacks.pop(identifier, None)

    def destroy(self):
        self.check_thread()
        self.exists = False
        callback = self.bindings.get('<Destroy>')
        if callback:
            callback(SimpleNamespace(widget=self))

    def delete(self, *args):
        self.check_thread()
        self.content = ''
        self.tags = []

    def insert(self, index, content):
        self.check_thread()
        self.content += content

    def tag_configure(self, tag, **kwargs):
        self.options[tag] = kwargs

    def tag_lower(self, tag):
        self.options['lowered_tag'] = tag

    def tag_add(self, *args):
        self.tags.append(args)

    def tag_remove(self, tag, *args):
        self.tags = [item for item in self.tags if item[0] != tag]

    def yview(self, *args):
        self.check_thread()
        if args:
            self.ycalls.append(args)
            if args[0] == 'moveto':
                self.view = (float(args[1]), min(1, float(args[1]) + 0.2))
        return self.view

    def yview_moveto(self, value):
        self.yview('moveto', value)

    def xview(self, *args):
        self.xcalls.append(args)

    def xview_moveto(self, value):
        self.xview('moveto', value)

    def set(self, *args):
        self.check_thread()
        self.options['set'] = args


class FakeVar:
    def __init__(self, master, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class ControlledThread:
    instances = []

    def __init__(self, target, args, daemon):
        self.target, self.args = target, args
        self.alive = False
        self.daemon = daemon
        self.instances.append(self)

    def start(self):
        self.alive = True

    def is_alive(self):
        return self.alive

    def finish(self):
        self.target(*self.args)
        self.alive = False


class DiffWindowTests(unittest.TestCase):
    def setUp(self):
        ControlledThread.instances = []
        self.thread_patch = patch.object(diff_window, 'Thread', ControlledThread)
        self.thread_patch.start()
        self.addCleanup(self.thread_patch.stop)
        self.photo_patch = patch.object(diff_window.ImageTk, 'PhotoImage',
                                        side_effect=lambda image, **kwargs: image)
        self.photo_patch.start()
        self.addCleanup(self.photo_patch.stop)
        self.copies = []
        self.tk = SimpleNamespace(**{name: FakeWidget for name in
                                    ('Toplevel', 'Frame', 'Text', 'Label', 'Button',
                                     'Scrollbar', 'Checkbutton', 'Canvas')}, BooleanVar=FakeVar)
        self.ui = SimpleNamespace(_root=FakeWidget(), _tk=self.tk,
                                  actions=SimpleNamespace(copy_to_clipboard=self.copy))
        self.left = self.row('a\r\nb\n最后', 1)
        self.right = self.row('a\nx\n最后\n新增', 2)
        self.window = diff_window.DiffWindow(self.ui, self.left, self.right)
        self.ui._diff_window = self.window
        self.addCleanup(self.window.close)

    def row(self, content, identifier=1):
        return {'id': identifier, 'kind': 'text', 'category': 'text', 'content': content,
                'source': 'editor.exe', 'created_at': '2026-01-01 12:00:00'}

    def copy(self, row):
        self.copies.append(row)
        return True

    def poll(self):
        identifier = self.window._after_id
        callback = self.window.win.callbacks.pop(identifier)
        callback()

    def finish(self):
        self.window._worker.finish()
        self.poll()

    def test_constructor_no_copy_normal_window_and_screen_fit(self):
        window = self.window
        self.assertEqual(self.copies, [])
        self.assertTrue(window.is_alive())
        self.assertEqual(window.win.attributes_calls, [('-topmost', False)])
        self.assertEqual(window.win.options['resizable'], (True, True))
        self.assertEqual(window.win.options['geometry'], '860x520+20+40')
        self.assertIn('ID: 1', window._meta_left.options['text'])
        self.assertIn('editor.exe', window._meta_right.options['text'])
        self.assertEqual(window._copy_left.options['state'], 'disabled')
        self.assert_roles(('A', 'B'))
        self.assertIsNone(window._display_roles)
        self.finish()
        self.assertEqual(self.copies, [])
        self.assertEqual(window._copy_left.options['state'], 'normal')
        self.assertEqual(window._display_roles, ('A', 'B'))

    def test_render_alignment_highlights_numbers_and_eol(self):
        self.finish()
        window, result = self.window, self.window._result
        for side, text, gutter in zip(('left', 'right'), window._texts, window._gutters):
            expected = '\n'.join(getattr(row, side + '_text') for row in result.rows) + '\n'
            self.assertEqual(text.content, expected)
            self.assertEqual(text.options['state'], 'disabled')
            self.assertEqual(text.options['wrap'], 'none')
            self.assertFalse(text.options['exportselection'])
            self.assertEqual(text.options['font'], gutter.options['font'])
            self.assertEqual(text.content.count('\n'), gutter.content.count('\n'))
        self.assertIn('1 CRLF', window._gutters[0].content)
        self.assertIn('1 LF', window._gutters[1].content)
        self.assertIn('removed', [tag[0] for tag in window._texts[0].tags])
        self.assertIn('emphasis_added', [tag[0] for tag in window._texts[1].tags])
        self.assertEqual(window._texts[0].content.split('\n')[-2], '')
        self.assertEqual(window._status.options['text'], '')
        self.assertIn('换行差异', window._hint.options['text'])

    def test_no_export_button_is_created_even_when_rules_popup_is_open(self):
        window = self.window
        self.assertFalse(hasattr(window, '_export_button'))
        window._toggle_rules()
        widgets = [window.win]
        while widgets:
            widget = widgets.pop()
            self.assertNotEqual(widget.options.get('text'), '导出差异')
            self.assertNotEqual(widget.options.get('command'), window._export)
            widgets.extend(widget.children)
        self.finish()
        self.assertFalse(hasattr(window, '_export_button'))

    def test_difference_count_only_in_navigation_and_clears_previous_status(self):
        self.finish()
        window = self.window
        for previous_status in ('原文一致', '已复制完整有效文本'):
            with self.subTest(previous_status=previous_status):
                if previous_status == '原文一致':
                    window.update(self.row('same'), self.row('same'))
                    self.finish()
                else:
                    window._copy(0)
                self.assertEqual(window._status.options['text'], previous_status)
                window.update(self.row('a\nkeep\nb'), self.row('x\nkeep\ny'))
                self.assertEqual(window._status.options['text'], '正在比较…')
                self.assertEqual(window._hint.options['text'], '复制和导航暂不可用')
                self.finish()
                self.assertFalse(window._result.rule_equal)
                self.assertEqual(window._result.blocks, (0, 2))
                self.assertEqual(window._status.options['text'], '')
                self.assertEqual(window._hint.options['text'], '')
                self.assertEqual(window._block_label.options['text'], '0/2')
                window._next.invoke()
                self.assertEqual(window._block_label.options['text'], '1/2')
                self.assertEqual(window._status.options['text'], '')
                self.assertEqual(window._hint.options['text'], '')

    def test_only_latest_pending_and_fixed_snapshots(self):
        window = self.window
        self.left['content'] = '外部修改不应生效'
        for value in range(100):
            row = self.row(str(value))
            window.update(row, self.row('最新'))
            row['content'] = '再次外部修改'
        self.assertEqual(len(ControlledThread.instances), 1)
        self.assertEqual(window._pending[1]['content'], '99')
        self.finish()
        self.assertIsNone(window._result)
        self.assertTrue(window._busy)
        self.assertEqual(len(ControlledThread.instances), 2)
        self.assertEqual(ControlledThread.instances[0].args[0][1]['content'], self.row('a\r\nb\n最后')['content'])
        self.finish()
        self.assertEqual(window._result.left_text, '99')
        self.assertEqual(window._result.right_text, '最新')
        self.assertFalse(window._busy)
        self.assertIsNone(window._after_id)

    def test_busy_blocks_programmatic_and_keyboard_actions(self):
        self.finish()
        window = self.window
        window.update(self.row('next'), self.row('new'))
        with patch.object(diff_window.filedialog, 'asksaveasfilename') as dialog:
            window._copy(0)
            window._export()
            window._navigate(1)
            self.assertEqual(window._texts[0].bindings['<<Copy>>'](None), 'break')
            dialog.assert_not_called()
        self.assertEqual(self.copies, [])
        self.assertEqual(window._block_index, -1)
        for widget in (*window._texts, *window._gutters):
            for event in ('<<Paste>>', '<<Cut>>', '<Shift-Insert>', '<Button-2>'):
                self.assertEqual(widget.bindings[event](None), 'break')

    def test_copy_full_effective_text_not_display_or_original_id(self):
        self.finish()
        window = self.window
        window._copy(0)
        self.assertEqual(self.copies[-1]['content'], self.left['content'])
        self.assertIsNone(self.copies[-1]['id'])
        self.assertEqual(self.copies[-1]['kind'], 'text')
        window.update(self.row('{"b":2,"a":1}'), self.row('{"a":3}'))
        window._options['json_format'].set(True)
        window._request()
        self.finish()
        self.finish()
        window._texts[1].bindings['<Control-c>'](None)
        self.assertEqual(self.copies[-1]['content'], window._result.right_text)
        self.assertNotEqual(self.copies[-1]['content'], '{"a":3}')
        window.update(self.row(''), self.row(''))
        self.finish()
        # JSON 空文本失败会恢复旧行，关闭格式化后重新提交空文本。
        window._options['json_format'].set(False)
        window.update(self.row(''), self.row(''))
        self.finish()
        window._copy(0)
        self.assertEqual(self.copies[-1]['content'], '')

    def test_json_both_errors_preserve_previous_result_and_metadata(self):
        self.finish()
        window, previous = self.window, self.window._result
        metadata = window._meta_left.options['text']
        window._options['json_format'].set(True)
        window.update(self.row('bad', 8), self.row('also bad', 9))
        self.finish()
        self.assertIs(window._result, previous)
        self.assertIn('左侧 JSON', window._status.options['text'])
        self.assertIn('右侧 JSON', window._status.options['text'])
        self.assertIn('保留上次结果', window._status.options['text'])
        self.assertEqual(window._meta_left.options['text'], metadata)
        window._copy(1)
        self.assertEqual(self.copies[-1]['content'], previous.right_text)

    def test_options_and_swap(self):
        self.finish()
        window = self.window
        window.update(self.row('a \r\n'), self.row('a\n'))
        window._options['ignore_trailing_space'].set(True)
        window._options['ignore_line_endings'].set(True)
        window._request()
        self.finish()
        self.finish()
        self.assertTrue(window._result.rule_equal)
        self.assertFalse(window._result.exact_equal)
        window._swap()
        self.finish()
        self.assertEqual(window._result.left_text, 'a\n')
        self.assertEqual(window._result.right_text, 'a \r\n')

    def test_scrollbar_wheel_native_y_and_independent_x(self):
        self.finish()
        window = self.window
        widgets = (*window._texts, *window._gutters)
        window._scroll_y('moveto', '0.4')
        self.assertTrue(all(widget.view[0] == 0.4 for widget in widgets))
        window._on_y(window._texts[1], '0.7', '0.9')
        self.assertEqual(window._gutters[0].view[0], 0.7)
        for event in (SimpleNamespace(delta=60), SimpleNamespace(delta=60),
                      SimpleNamespace(num=5), SimpleNamespace(num=4)):
            self.assertEqual(window._wheel(event), 'break')
        self.assertTrue(all(('scroll', -3, 'units') in widget.ycalls for widget in widgets))
        self.assertTrue(all(('scroll', 3, 'units') in widget.ycalls for widget in widgets))
        before = list(window._texts[1].xcalls)
        window._texts[0].xview('moveto', '0.5')
        self.assertEqual(window._texts[1].xcalls, before)
        window._syncing = True
        before = list(window._gutters[0].ycalls)
        window._on_y(window._texts[0], '0.1', '0.2')
        self.assertEqual(window._gutters[0].ycalls, before)

    def test_navigation_blocks_are_zero_based_and_wrap(self):
        window = self.window
        window.update(self.row('a\nb\nc\nd'), self.row('a\nx\nc\ny'))
        self.finish()
        self.finish()
        self.assertEqual(window._result.blocks, (1, 3))
        window._navigate(-1)
        self.assertEqual(window._block_index, 1)
        self.assertEqual(window._texts[0].ycalls[-1], ('4.0',))
        window._navigate(1)
        self.assertEqual(window._texts[0].ycalls[-1], ('2.0',))
        self.assertEqual(window._block_label.options['text'], '1/2')
        window.update(self.row('same'), self.row('same'))
        self.finish()
        self.assertEqual(window._next.options['state'], 'disabled')
        self.assertEqual(window._block_label.options['text'], '无差异')

    def test_export_utf8_no_newline_conversion_cancel_and_error(self):
        self.finish()
        window = self.window
        writer = mock_open()
        with patch.object(diff_window.filedialog, 'asksaveasfilename', return_value='result.diff') as dialog:
            with patch('builtins.open', writer):
                window._export()
            writer.assert_called_once_with('result.diff', 'w', encoding='utf-8', newline='')
            writer().write.assert_called_once_with(window._result.unified_diff)
            self.assertIs(dialog.call_args.kwargs['parent'], window.win)
            with patch('builtins.open', side_effect=OSError('不可写')):
                window._export()
            self.assertIn('不可写', window._status.options['text'])
        with patch.object(diff_window.filedialog, 'asksaveasfilename', return_value=''):
            with patch('builtins.open') as writer:
                window._export()
                writer.assert_not_called()

    def test_export_nested_update_or_close_does_not_write_stale_result(self):
        self.finish()
        window = self.window

        def update_during_dialog(**kwargs):
            window.update(self.row('changed'), self.row('new'))
            return 'stale.diff'

        with patch.object(diff_window.filedialog, 'asksaveasfilename', side_effect=update_during_dialog):
            with patch('builtins.open') as writer:
                window._export()
                writer.assert_not_called()
        self.finish()
        with patch.object(diff_window.filedialog, 'asksaveasfilename',
                          side_effect=lambda **kwargs: (window.close() or 'closed.diff')):
            with patch('builtins.open') as writer:
                window._export()
                writer.assert_not_called()

    def test_close_cancels_after_discards_pending_and_late_outcome(self):
        window = self.window
        worker = window._worker
        identifier = window._after_id
        callback = window.win.callbacks[identifier]
        window.update(self.row('pending'), self.row('new'))
        window.close()
        window.close()
        self.assertFalse(window.is_alive())
        self.assertIsNone(self.ui._diff_window)
        self.assertEqual(window.win.cancelled, [identifier])
        self.assertIsNone(window._pending)
        worker.finish()
        callback()
        window.update(self.left, self.right)
        self.assertIsNone(window._result)
        self.assertEqual(len(ControlledThread.instances), 1)
        self.assertEqual(self.copies, [])

    def test_destroy_only_self_and_preserve_replacement_reference(self):
        window = self.window
        window._destroyed(SimpleNamespace(widget=window._texts[0]))
        self.assertTrue(window.is_alive())
        replacement = object()
        self.ui._diff_window = replacement
        window.win.destroy()
        self.assertFalse(window.is_alive())
        self.assertIs(self.ui._diff_window, replacement)

    def test_theme_provider_tuple_and_explicit_dark_palette(self):
        self.ui._panel = SimpleNamespace(_theme=lambda: ({'card_bg': '#2C2C2E', 'label': '#F5F5F7'}, True))
        dark = diff_window.DiffWindow(self.ui, self.left, self.right)
        self.addCleanup(dark.close)
        self.assertEqual(dark._texts[0].options['removed']['background'], '#512F35')
        explicit = diff_window.DiffWindow(self.ui, self.left, self.right,
                                         theme={'card_bg': '#101010', 'label': '#EEEEEE'})
        self.addCleanup(explicit.close)
        self.assertEqual(explicit.theme['card_bg'], '#101010')
        self.assertTrue(explicit._dark)
        self.assertNotEqual(explicit._texts[0].options['added']['background'],
                            self.window._texts[0].options['added']['background'])

    def test_initial_failure_no_result_actions_remain_disabled(self):
        window = self.window
        window.update(self.row(None), self.row('valid'))
        self.finish()
        self.finish()
        self.assertIsNone(window._result)
        self.assertIn('必须是字符串', window._status.options['text'])
        self.assert_footer(True, True)
        self.assertEqual(window._hint.options['text'], '请检查输入文本或比较规则')
        self.assertEqual(window._copy_left.options['state'], 'disabled')

    def test_finished_outcome_before_thread_exit_delays_next_worker(self):
        window, worker = self.window, self.window._worker
        window.update(self.row('latest'), self.row('right'))
        worker.target(*worker.args)
        self.poll()
        self.assertEqual(len(ControlledThread.instances), 1)
        self.assertIsNotNone(window._after_id)
        worker.alive = False
        self.poll()
        self.assertEqual(len(ControlledThread.instances), 2)
        self.finish()
        self.assertEqual(window._result.left_text, 'latest')

    def test_real_thread_compare_has_no_tk_calls_and_ui_collects(self):
        self.finish()
        self.thread_patch.stop()
        entered, release = Event(), Event()
        owner = get_ident()
        compare = diff_tools.compare_text

        def gated(left, right, **kwargs):
            self.assertNotEqual(get_ident(), owner)
            entered.set()
            if not release.wait(5):
                raise AssertionError('测试未释放比较线程')
            return compare(left, right, **kwargs)

        with patch.object(diff_tools, 'compare_text', side_effect=gated):
            self.window.update(self.row('thread'), self.row('right'))
            self.assertTrue(entered.wait(5))
            worker = self.window._worker
            try:
                self.window.update(self.row('newest'), self.row('right'))
                self.assertIs(self.window._worker, worker)
            finally:
                release.set()
                worker.join(5)
            self.assertFalse(worker.is_alive())
            self.poll()
            self.window._worker.join(5)
            self.poll()
        self.assertEqual(self.window._result.left_text, 'newest')
        self.assertFalse(self.window._busy)

    def test_json_toggle_concise_labels_colors_and_formatted_equality(self):
        window = self.window
        self.assertEqual(window._json_toggle.options['text'], '格式化 JSON')
        self.assertEqual(window._rules_button._tooltip_text, '未启用比较规则')
        self.assertEqual(window._json_toggle.options['fg'], window.theme['label'])
        self.finish()
        window._options['json_format'].set(True)
        window.update(self.row('{"a":1}'), self.row('{ "a" : 1 }'))
        self.assertEqual(window._rules_button._tooltip_text, '未启用比较规则')
        self.assertEqual(window._json_toggle.options['fg'], window.theme['accent'])
        self.finish()
        self.assertEqual(window._status.options['text'], '格式化后一致，原文有差异')
        self.assertIn('原始记录不变', window._hint.options['text'])
        window._options['ignore_line_endings'].set(True)
        window._refresh_rules()
        self.assertEqual(window._rules_button._tooltip_text, '已启用：忽略换行符')
        window._options['json_format'].set(False)
        window._refresh_rules()
        self.assertEqual(window._json_toggle.options['fg'], window.theme['label'])
        self.assertEqual(window._json_toggle.options['bg'], window.theme['toolbar_bg'])

    def assert_footer(self, status, hint):
        window = self.window
        self.assertEqual(window._status.grid_visible, status)
        self.assertEqual(window._hint.grid_visible, hint)
        self.assertEqual(window._footer.grid_visible, status or hint)
        if hint:
            self.assertEqual(window._hint.options['grid']['row'], 1 if status else 0)
            self.assertEqual(window._hint.options['grid']['pady'],
                             (round(3 * window._scale) if status else 0, 0))

    def show_plain_difference(self):
        self.window.update(self.row('left'), self.row('right'))
        self.finish()
        if self.window._busy:
            self.finish()
        self.assert_footer(False, False)

    def test_narrow_card_layout_scales_without_changing_text_or_toolbar(self):
        for dpi in (96, 120, 144, 192):
            for dark in (False, True):
                with patch.object(FakeWidget, 'winfo_fpixels', return_value=dpi):
                    window = diff_window.DiffWindow(self.ui, self.left, self.right, theme=({}, dark))
                self.addCleanup(window.close)
                with self.subTest(dpi=dpi, dark=dark):
                    scale = window._scale
                    body = window._texts[0].parent.parent
                    toolbar = window._rules_button.parent
                    for outer in (body, toolbar, window._footer):
                        self.assertEqual(outer.options['grid']['padx'], round(8 * scale))
                    self.assertEqual(toolbar.options['grid']['pady'][1], round(4 * scale))
                    self.assertEqual(body.options['grid']['pady'], 0)
                    for side, text, gutter in zip(range(2), window._texts, window._gutters):
                        pane = text.parent
                        self.assertEqual(pane.options['grid']['padx'],
                                         (0, round(3 * scale)) if side == 0 else (round(3 * scale), 0))
                        self.assertEqual(pane.options['highlightthickness'], 1)
                        self.assertEqual(pane.options['bd'], 0)
                        border = '#3D3D42' if dark else '#D8D8DE'
                        self.assertEqual(pane.options['highlightbackground'], border)
                        self.assertEqual(pane.options['highlightcolor'], border)
                        header = pane.children[0]
                        self.assertEqual(header.options['grid']['padx'], round(4 * scale))
                        self.assertEqual(header.options['grid']['pady'], round(5 * scale))
                        for widget in (text, gutter):
                            self.assertEqual(widget.options['font'], ('Consolas', 11))
                            self.assertEqual(widget.options['padx'], 4)
                            self.assertEqual(widget.options['pady'], 4)
                        self.assertEqual(gutter.options['bg'], window.theme['field'])
                    self.assertEqual(window._navigation._width, round(244 * scale))
                    self.assertEqual(window._json_toggle._height, round(30 * scale))
                    self.assertNotIn('overrideredirect', window.win.options)

    def test_empty_footer_collapses_and_single_rows_have_no_blank_space(self):
        self.show_plain_difference()
        window = self.window
        footer_grid = dict(window._footer.options['grid'])
        window._next.invoke()
        self.assert_footer(False, False)
        window.update(self.row('same'), self.row('same'))
        self.finish()
        self.assert_footer(True, False)
        self.assertEqual(window._status.options['text'], '原文一致')
        window.update(self.row('a\r\n'), self.row('b\n'))
        self.finish()
        self.assert_footer(False, True)
        self.assertIn('换行差异', window._hint.options['text'])
        window._copy(0)
        self.assert_footer(True, True)
        self.show_plain_difference()
        self.assertEqual(window._footer.options['grid'], footer_grid)

    def test_request_and_error_restore_hidden_footer_without_render(self):
        self.show_plain_difference()
        window = self.window
        window.update(self.row('next'), self.row('new'))
        self.assert_footer(True, True)
        self.assertEqual(window._status.options['text'], '正在比较…')
        self.assertEqual(window._hint.options['text'], '复制和导航暂不可用')
        window._set_status('')
        window._set_hint('')
        self.assert_footer(False, False)
        with patch.object(diff_tools, 'compare_text', side_effect=ValueError('比较出错')), \
                patch.object(window, '_render') as render:
            self.finish()
        render.assert_not_called()
        self.assert_footer(True, True)
        self.assertIn('比较出错', window._status.options['text'])
        self.assertEqual(window._hint.options['text'], '规则已恢复为上次结果；复制使用上次结果')

    def test_copy_success_false_and_exception_restore_hidden_status(self):
        for outcome in (True, False, RuntimeError('剪贴板不可用')):
            with self.subTest(outcome=outcome):
                self.show_plain_difference()
                kwargs = {'side_effect': outcome} if isinstance(outcome, Exception) else {'return_value': outcome}
                with patch.object(self.ui.actions, 'copy_to_clipboard', **kwargs):
                    self.window._copy(0)
                self.assert_footer(True, False)
                expected = ('复制失败：剪贴板不可用' if isinstance(outcome, Exception) else
                            '已复制完整有效文本' if outcome else '复制失败')
                self.assertEqual(self.window._status.options['text'], expected)

    def test_internal_export_success_and_error_restore_hidden_status(self):
        for error in (None, OSError('不可写')):
            with self.subTest(error=error):
                self.show_plain_difference()
                writer = mock_open() if error is None else unittest.mock.Mock(side_effect=error)
                with patch.object(diff_window.filedialog, 'asksaveasfilename', return_value='result.diff'), \
                        patch('builtins.open', writer):
                    self.window._export()
                self.assert_footer(True, False)
                self.assertEqual(self.window._status.options['text'],
                                 '已导出 UTF-8 统一差异' if error is None else '导出失败：不可写')

    def test_json_session_restores_hint_only_and_format_equal_footer(self):
        self.show_plain_difference()
        window = self.window
        session = window.export_session()
        session['rows'] = (self.row('{"a":1}'), self.row('{"a":2}'))
        session['options']['json_format'] = True
        window.restore_session(session)
        self.assert_footer(True, True)
        self.finish()
        self.assert_footer(False, True)
        self.assertEqual(window._hint.options['text'], '已格式化显示，原始记录不变')
        window.update(self.row('{"a":1}'), self.row('{ "a" : 1 }'))
        self.finish()
        self.assert_footer(True, True)
        self.assertEqual(window._status.options['text'], '格式化后一致，原文有差异')
        window._options['json_format'].set(False)
        self.show_plain_difference()

    def test_compact_vertical_spacing_and_native_title(self):
        window = self.window
        self.assertEqual(window.win.options['title'], '差异比较 · 只读预览')
        self.assertFalse(any(child.options.get('grid', {}).get('row') == 0
                             for child in window.win.children))
        toolbar = window._rules_button.parent
        footer = window._status.parent
        pad = round(4 * window._scale)
        self.assertEqual(toolbar.options['grid']['pady'], (pad, pad))
        self.assertEqual(footer.options['grid']['pady'], (pad, pad))
        self.assertEqual(toolbar.options['grid']['padx'], round(8 * window._scale))

    def test_card_layout_toolbar_fonts_and_balanced_columns(self):
        window = self.window
        body = window._texts[0].parent.parent
        self.assertEqual(body.column_weights[0], body.column_weights[1])
        self.assertEqual(body.column_weights[0], {'weight': 1, 'uniform': 'side'})
        self.assertEqual(window.win.row_weights[2], {'weight': 1})
        for side, text, gutter, copy_button in zip(
                ('A', 'B'), window._texts, window._gutters,
                (window._copy_left, window._copy_right)):
            pane = text.parent
            self.assertEqual(pane.row_weights[1], {'weight': 1})
            self.assertEqual(copy_button.parent.parent, pane)
            self.assertEqual(copy_button.options['text'], '复制 ' + side)
            self.assertEqual(copy_button.options['font'][0], 'Microsoft YaHei UI')
            self.assertEqual(text.options['font'][0], 'Consolas')
            self.assertEqual(gutter.options['numbers']['justify'], 'right')
            self.assertNotIn('underline', text.options['block'])
        self.assertIs(window._previous.parent, window._navigation.canvas)
        self.assertIs(window._navigation.parent, window._rules_button.parent)
        self.assertIsNone(window._json_toggle.options['marker'])
        footer = window._status.parent
        self.assertEqual(footer.children, [window._status, window._hint])
        self.assertEqual(footer.column_weights, {0: {'weight': 1}})
        self.assertEqual(window._status.options['grid'], {'row': 0, 'column': 0, 'sticky': 'ew'})
        self.assertEqual(window._hint.options['grid'],
                         {'row': 1, 'column': 0, 'sticky': 'ew', 'pady': (3, 0)})
        badges = [child for pane in (window._texts[0].parent, window._texts[1].parent)
                  for child in pane.children[0].children if child.options.get('text') in ('A', 'B')]
        self.assertEqual(len(badges), 2)
        self.assertNotEqual(badges[0].options['fg'], badges[1].options['fg'])

    def test_rules_popup_toggle_outside_escape_close_and_option_request(self):
        window = self.window
        window._toggle_rules()
        popup = window._rules_popup
        self.assertEqual(popup.options['place'], {'x': 0, 'y': 34})
        self.assertTrue(popup.options['lifted'])
        checks = [child for child in popup.children if 'variable' in child.options]
        self.assertEqual(len(checks), 2)
        self.assertIs(checks[0].options['variable'], window._options['ignore_trailing_space'])
        window._outside_rules(SimpleNamespace(widget=checks[0]))
        self.assertIs(window._rules_popup, popup)
        window._outside_rules(SimpleNamespace(widget=window._rules_button.canvas))
        self.assertIs(window._rules_popup, popup)
        generation = window._generation
        checks[0].options['variable'].set(True)
        checks[0].options['command']()
        self.assertEqual(window._generation, generation + 1)
        self.assertIn('忽略行尾空白', window._rules_button._tooltip_text)
        self.assertEqual(window._block_label.options['text'], '比较中')
        window._outside_rules(SimpleNamespace(widget=window._texts[0]))
        self.assertIsNone(window._rules_popup)
        self.assertFalse(popup.exists)
        window._toggle_rules()
        window._toggle_rules()
        self.assertIsNone(window._rules_popup)
        window._toggle_rules()
        self.assertEqual(window._escape(None), 'break')
        self.assertTrue(window.is_alive())
        self.assertIsNone(window._rules_popup)
        window._toggle_rules()
        window._rules_popup.children[-1].options['command']()
        self.assertIsNone(window._rules_popup)
        window._toggle_rules()
        popup = window._rules_popup
        window.close()
        self.assertFalse(popup.exists)
        self.assertFalse(window.is_alive())

    def test_rules_popup_focus_loss_and_escape_window(self):
        window = self.window
        window._toggle_rules()
        window.win.focus = window.win
        window._rules_focus_out(None)
        identifier = window._popup_after
        window.win.callbacks.pop(identifier)()
        self.assertIsNotNone(window._rules_popup)
        window.win.focus = None
        window._rules_focus_out(None)
        identifier = window._popup_after
        window.win.callbacks.pop(identifier)()
        self.assertIsNone(window._rules_popup)
        window._toggle_rules()
        window._rules_focus_out(None)
        identifier = window._popup_after
        window._close_rules()
        self.assertIn(identifier, window.win.cancelled)
        self.assertEqual(window._escape(None), 'break')
        self.assertFalse(window.is_alive())

    def test_status_exact_rule_equal_duplicates_and_json_copy_hint(self):
        window = self.window
        window.update(self.row('same'), self.row('same'))
        self.finish()
        self.finish()
        self.assertEqual(window._status.options['text'], '原文一致')
        self.assertEqual(window._hint.options['text'], '')
        window._options['ignore_trailing_space'].set(True)
        window.update(self.row('a \n'), self.row('a\n'))
        self.finish()
        self.assertEqual(window._status.options['text'], '按规则一致，原文有差异')
        window._options['json_format'].set(True)
        window.update(self.row('{"a":1,"a":2}'), self.row('{"a":2}'))
        self.finish()
        self.assertIn('JSON 含重复键', window._hint.options['text'])
        self.assertIn('已格式化显示，原始记录不变', window._hint.options['text'])
        self.assertEqual(window._rules_button._tooltip_text, '已启用：忽略行尾空白')

    def test_failure_restores_result_options_count_and_metadata_then_recovers(self):
        self.finish()
        window = self.window
        previous = window._result
        summary = window._summary_left.options['text']
        window._options['json_format'].set(True)
        window._options['ignore_line_endings'].set(True)
        window.update(self.row('bad', 8), self.row('bad too', 9))
        self.assertEqual(window._block_label.options['text'], '比较中')
        for button in (window._copy_left, window._copy_right, window._previous,
                       window._next):
            self.assertEqual(button.options['state'], 'disabled')
        self.finish()
        self.assertIs(window._result, previous)
        self.assertEqual(window._summary_left.options['text'], summary)
        self.assertFalse(window._options['json_format'].get())
        self.assertFalse(window._options['ignore_line_endings'].get())
        self.assertEqual(window._rules_button._tooltip_text, '未启用比较规则')
        self.assertEqual(window._block_label.options['text'], f'0/{len(previous.blocks)}')
        self.assertIn('规则已恢复', window._hint.options['text'])
        window._copy(0)
        self.assertIn('上次结果', window._hint.options['text'])
        window.update(self.row('new'), self.row('new'))
        self.finish()
        self.assertFalse(window._retained)
        self.assertNotIn('上次结果', window._block_label.options['text'])
        self.assertEqual(window._status.options['text'], '原文一致')

    def test_missing_rows_are_not_real_blank_lines_and_whole_block_is_marked(self):
        window = self.window
        window.update(self.row('head\n\nold\nend'), self.row('head\n\nnew\nextra\nend'))
        self.finish()
        self.finish()
        result = window._result
        for side, text in zip(('left', 'right'), window._texts):
            missing = {int(tag[1].split('.')[0]) - 1 for tag in text.tags if tag[0] == 'missing'}
            self.assertEqual(missing, {i for i, row in enumerate(result.rows)
                                      if getattr(row, side + '_no') is None})
            self.assertNotIn(1, missing)
        window._navigate(1)
        start = result.blocks[0]
        stop = start + 1
        while stop < len(result.rows) and result.rows[stop].kind != 'equal':
            stop += 1
        self.assertGreater(stop - start, 1)
        for widget in (*window._texts, *window._gutters):
            self.assertIn(('block', f'{start + 1}.0', f'{stop + 1}.0'), widget.tags)
        self.assertIn('emphasis_added', [tag[0] for tag in window._texts[1].tags])

    def assert_roles(self, roles):
        window = self.window
        self.assertEqual(window._roles, roles)
        colors = {'A': '#69AAFF' if window._dark else '#007AFF',
                  'B': '#C39AFF' if window._dark else '#8050CC'}
        self.assertEqual(len(window._badges), 2)
        for role, badge, button in zip(roles, window._badges,
                                       (window._copy_left, window._copy_right)):
            self.assertEqual(badge.options['text'], role)
            self.assertEqual(badge.options['fg'], colors[role])
            self.assertEqual(button.options['text'], '复制 ' + role)

    def assert_display(self, rows, roles, **options):
        window = self.window
        expected = diff_tools.compare_text(*(row['content'] for row in rows), **options)
        self.assertFalse(window._busy)
        self.assertEqual(window._result, expected)
        self.assertEqual((window._left_row, window._right_row), rows)
        self.assertEqual(window._display_rows, rows)
        self.assertEqual(window._display_roles, roles)
        self.assert_roles(roles)
        eol_names = {'': '∅', '\n': 'LF', '\r': 'CR', '\r\n': 'CRLF'}
        for side, row, text, gutter, meta, summary, button in zip(
                ('left', 'right'), rows, window._texts, window._gutters,
                (window._meta_left, window._meta_right),
                (window._summary_left, window._summary_right),
                (window._copy_left, window._copy_right)):
            self.assertEqual(meta.options['text'],
                             f"ID: {row['id']} · {row['source']} · {row['created_at']}")
            preview = ' '.join(row['content'][:100].split())
            self.assertEqual(summary.options['text'],
                             (preview[:48] + '…' if len(preview) > 48 else preview) or '空文本')
            lines, numbers = [], []
            for aligned in expected.rows:
                lines.append(getattr(aligned, side + '_text'))
                number = getattr(aligned, side + '_no')
                label = '' if number is None else str(number)
                if number is not None and aligned.left_eol != aligned.right_eol:
                    label += ' ' + eol_names[getattr(aligned, side + '_eol')]
                numbers.append(label)
            self.assertEqual(text.content, '\n'.join(lines) + ('\n' if lines else ''))
            self.assertEqual(gutter.content, '\n'.join(numbers) + ('\n' if numbers else ''))
            self.assertEqual(text.options['state'], 'disabled')
            self.assertEqual(gutter.options['state'], 'disabled')
            self.assertEqual(button.options['state'], 'normal')
            copy_count = len(self.copies)
            button.options['command']()
            self.assertEqual(len(self.copies), copy_count + 1)
            self.assertEqual(self.copies[-1], {
                'id': None, 'kind': 'text', 'category': 'text',
                'content': getattr(expected, side + '_text'), 'data': None, 'file_path': ''})
        writer = mock_open()
        with patch.object(diff_window.filedialog, 'asksaveasfilename',
                          return_value='swapped.diff'), patch('builtins.open', writer):
            window._export()
        writer.assert_called_once_with('swapped.diff', 'w', encoding='utf-8', newline='')
        writer().write.assert_called_once_with(expected.unified_diff)

    def test_swap_moves_complete_text_metadata_summaries_roles_copy_numbers_and_export(self):
        self.finish()
        window = self.window
        rows = (dict(self.left), dict(self.right))
        self.assert_display(rows, ('A', 'B'))
        original_diff = window._result.unified_diff
        original_meta = (window._meta_left.options['text'], window._meta_right.options['text'])
        original_summary = (window._summary_left.options['text'], window._summary_right.options['text'])
        original_badges = [dict(badge.options) for badge in window._badges]
        left, right = window._left_row, window._right_row
        swap = window._swap_button
        generation = window._generation
        with patch.object(window, 'update', wraps=window.update) as update:
            swap.options['command']()
            update.assert_not_called()
        self.assertIs(window._left_row, right)
        self.assertIs(window._right_row, left)
        self.assertEqual(window._generation, generation + 1)
        self.assert_roles(('B', 'A'))
        self.assertEqual(window._display_roles, ('A', 'B'))
        self.assertTrue(window._busy)
        for button in (window._copy_left, window._copy_right):
            self.assertEqual(button.options['state'], 'disabled')
        self.finish()
        self.assert_display(rows[::-1], ('B', 'A'))
        self.assertEqual((window._meta_left.options['text'], window._meta_right.options['text']),
                         original_meta[::-1])
        self.assertEqual((window._summary_left.options['text'], window._summary_right.options['text']),
                         original_summary[::-1])
        for badge, original in zip(window._badges, original_badges[::-1]):
            self.assertEqual(badge.options['text'], original['text'])
            self.assertEqual(badge.options['fg'], original['fg'])
        self.assertNotEqual(window._result.unified_diff, original_diff)
        swap.options['command']()
        self.finish()
        self.assert_display(rows, ('A', 'B'))
        self.assertEqual(window._result.unified_diff, original_diff)

    def test_swap_json_buttons_copy_effective_text_and_dark_badges_follow_identity(self):
        self.finish()
        self.window.close()
        rows = (self.row('{"b":2,"a":1}', 11), self.row('{"a":3,"extra":[1,2]}', 12))
        self.window = diff_window.DiffWindow(self.ui, *rows, theme=({}, True))
        self.ui._diff_window = self.window
        self.addCleanup(self.window.close)
        self.finish()
        window = self.window
        window._options['json_format'].set(True)
        window._request()
        self.finish()
        window._swap()
        self.finish()
        self.assert_display(rows[::-1], ('B', 'A'), json_format=True)
        self.assertNotEqual(self.copies[-2]['content'], rows[1]['content'])
        self.assertNotEqual(self.copies[-1]['content'], rows[0]['content'])

    def test_repeated_swap_discards_stale_success_and_error_without_relabeling_display(self):
        self.finish()
        window = self.window
        rows = (dict(self.left), dict(self.right))
        for stale_error in (False, True):
            with self.subTest(stale_error=stale_error):
                window.update(*rows)
                self.finish()
                previous = window._result
                texts = [text.content for text in window._texts]
                gutters = [gutter.content for gutter in window._gutters]
                generation = window._generation
                for _ in range(3):
                    window._swap()
                self.assertEqual(window._generation, generation + 3)
                self.assertEqual(window._pending[1:3], rows[::-1])
                self.assert_roles(('B', 'A'))
                self.assertEqual(window._display_roles, ('A', 'B'))
                if stale_error:
                    with patch.object(diff_tools, 'compare_text', side_effect=ValueError('过期错误')):
                        self.finish()
                else:
                    self.finish()
                self.assertIs(window._result, previous)
                self.assertTrue(window._busy)
                self.assertEqual(window._display_rows, rows)
                self.assertEqual(window._display_roles, ('A', 'B'))
                self.assert_roles(('B', 'A'))
                self.assertNotIn('过期错误', window._status.options['text'])
                self.assertEqual([text.content for text in window._texts], texts)
                self.assertEqual([gutter.content for gutter in window._gutters], gutters)
                worker = window._worker
                for _ in range(3):
                    window._swap()
                self.assertIs(window._worker, worker)
                self.assert_roles(('A', 'B'))
                self.finish()
                self.assertIs(window._result, previous)
                self.assertTrue(window._busy)
                self.assert_roles(('A', 'B'))
                self.finish()
                self.assert_display(rows, ('A', 'B'))

    def test_reused_update_resets_roles_and_snapshots_while_swap_is_pending(self):
        self.finish()
        window = self.window
        window._swap()
        self.finish()
        self.assertEqual(window._display_roles, ('B', 'A'))
        window._swap()
        window._swap()
        left = self.row('新 A\r\n末行 A', 31)
        right = self.row('新 B\n额外 B\n末行 B', 32)
        rows = (dict(left), dict(right))
        window.update(left, right)
        left['content'] = '外部修改 A'
        right['content'] = '外部修改 B'
        self.assert_roles(('A', 'B'))
        self.assertEqual(window._display_roles, ('B', 'A'))
        self.finish()
        self.assertTrue(window._busy)
        self.assert_roles(('A', 'B'))
        self.assertEqual(window._display_roles, ('B', 'A'))
        self.finish()
        self.assert_display(rows, ('A', 'B'))
        window._swap()
        self.finish()
        self.assert_display(rows[::-1], ('B', 'A'))

    def test_failed_update_restores_display_rows_and_roles_then_swap_uses_restored_rows(self):
        self.finish()
        window = self.window
        window._swap()
        self.finish()
        rows = (dict(self.right), dict(self.left))
        previous = window._result
        texts = [text.content for text in window._texts]
        gutters = [gutter.content for gutter in window._gutters]
        window._options['json_format'].set(True)
        window.update(self.row('bad', 81), self.row('also bad', 82))
        self.assert_roles(('A', 'B'))
        self.assertEqual(window._display_roles, ('B', 'A'))
        self.finish()
        self.assertIs(window._result, previous)
        self.assertTrue(window._retained)
        self.assertFalse(window._options['json_format'].get())
        self.assertIn('保留上次结果', window._status.options['text'])
        self.assertEqual([text.content for text in window._texts], texts)
        self.assertEqual([gutter.content for gutter in window._gutters], gutters)
        self.assert_display(rows, ('B', 'A'))
        window._swap()
        self.finish()
        self.assertFalse(window._retained)
        self.assert_display(rows[::-1], ('A', 'B'))

    def test_failed_swap_restores_both_role_orders_then_swaps_again(self):
        self.finish()
        window = self.window
        for roles in (('A', 'B'), ('B', 'A')):
            with self.subTest(roles=roles):
                window.update(self.left, self.right)
                self.finish()
                if roles == ('B', 'A'):
                    window._swap()
                    self.finish()
                rows = tuple(dict(row) for row in (window._left_row, window._right_row))
                previous = window._result
                texts = [text.content for text in window._texts]
                gutters = [gutter.content for gutter in window._gutters]
                window._swap()
                self.assert_roles(roles[::-1])
                self.assertEqual(window._display_roles, roles)
                with patch.object(diff_tools, 'compare_text', side_effect=ValueError('交换比较失败')):
                    self.finish()
                self.assertIs(window._result, previous)
                self.assertTrue(window._retained)
                self.assertIn('交换比较失败', window._status.options['text'])
                self.assertEqual([text.content for text in window._texts], texts)
                self.assertEqual([gutter.content for gutter in window._gutters], gutters)
                self.assert_display(rows, roles)
                window._swap()
                self.finish()
                self.assertFalse(window._retained)
                self.assert_display(rows[::-1], roles[::-1])

    def test_dpi_scales_geometry_spacing_and_dark_defaults(self):
        with patch.object(FakeWidget, 'winfo_fpixels', return_value=144), \
                patch.object(FakeWidget, 'winfo_screenwidth', return_value=2400), \
                patch.object(FakeWidget, 'winfo_screenheight', return_value=1600):
            window = diff_window.DiffWindow(self.ui, self.left, self.right,
                                            theme=({'card_bg': '#202020'}, True))
        self.addCleanup(window.close)
        self.assertEqual(window._scale, 1.5)
        self.assertEqual(window.win.options['geometry'], '1620x1050+390+275')
        self.assertEqual(window.win.options['minsize'], (1140, 630))
        self.assertEqual(window._texts[0].parent.parent.options['grid']['padx'], 12)
        self.assertEqual(window.theme['window_bg'], '#202024')
        self.assertEqual(window.theme['label'], '#F5F5F7')
        self.assertEqual(window.theme['card_bg'], '#202020')
        self.assertNotEqual(window._texts[0].options['missing']['background'],
                            self.window._texts[0].options['missing']['background'])

    def test_toolbar_group_layout_and_scaled_sizes(self):
        window = self.window
        toolbar = window._rules_button.parent
        self.assertFalse(hasattr(window, '_rules_label'))
        self.assertEqual(window._json_toggle.options['pack']['padx'], (6, 0))
        self.assertEqual(window._separator.options['pack']['padx'], 6)
        self.assertEqual(window._separator.options['height'], 18)
        self.assertEqual(window._navigation._width, 244)
        self.assertEqual(window._previous.options['text'], '↑ 上一个')
        self.assertEqual(window._next.options['text'], '下一个 ↓')
        self.assertEqual(window._previous.canvas.options['place'], {'x': 0, 'y': 0})
        self.assertEqual(window._next.canvas.options['place'], {'x': 76, 'y': 0})
        self.assertEqual(window._block_label.options['place'],
                         {'x': 152, 'y': 0, 'width': 84, 'height': 30})
        for control in window._toolbar_controls:
            self.assertEqual(control._height, 30)
            self.assertEqual(control._radius, 7)
            self.assertEqual(control.canvas.options['highlightthickness'], 0)
            self.assertEqual(control._photo.mode, 'RGB')
            if control._group is None:
                self.assertEqual(control._photo.getpixel((0, 0)),
                                 tuple(value // 257 for value in toolbar.winfo_rgb(window.theme['window_bg'])))
            else:
                x = control._segment_start
                self.assertEqual(control._photo.tobytes(),
                                 window._navigation._photo.crop(
                                     (x, 0, x + control._width, control._height)).tobytes())
        for button in (window._copy_left, window._copy_right):
            self.assertIsInstance(button, FakeWidget)
        with patch.object(FakeWidget, 'winfo_fpixels', return_value=192):
            scaled = diff_window.DiffWindow(self.ui, self.left, self.right)
        self.addCleanup(scaled.close)
        self.assertEqual(scaled._json_toggle._height, 60)
        self.assertEqual(scaled._json_toggle._radius, 14)
        self.assertEqual(scaled._json_toggle.options['pack']['padx'], (12, 0))
        self.assertEqual(scaled._navigation._width, 488)
        self.assertEqual(scaled._next.canvas.options['place']['x'], 152)
        self.assertEqual(scaled._rules_button.parent.options['grid']['pady'], (8, 8))

    def test_toolbar_hover_focus_keyboard_and_disabled_click(self):
        window, button = self.window, self.window._next
        self.assertEqual(button.canvas.options['takefocus'], 0)
        with patch.object(window, '_navigate') as navigate:
            button.canvas.bindings['<Enter>'](None)
            self.assertFalse(button._hover)
            self.assertEqual(button._paint_bg, window.theme['toolbar_bg'])
            button.canvas.bindings['<ButtonRelease-1>'](SimpleNamespace(x=10, y=10))
            for key in ('<space>', '<Return>'):
                self.assertEqual(button.canvas.bindings[key](None), 'break')
            button.invoke()
            navigate.assert_not_called()
            self.finish()
            self.assertEqual(button.canvas.options['takefocus'], 1)
            button.canvas.bindings['<Enter>'](None)
            self.assertTrue(button._hover)
            self.assertEqual(button._paint_bg, window.theme['toolbar_hover_bg'])
            button.canvas.bindings['<ButtonRelease-1>'](SimpleNamespace(x=button._width + 5, y=10))
            navigate.assert_not_called()
            button.canvas.bindings['<ButtonRelease-1>'](SimpleNamespace(x=10, y=10))
            button.canvas.bindings['<space>'](None)
            button.canvas.bindings['<Return>'](None)
            self.assertEqual(navigate.call_args_list, [unittest.mock.call(1)] * 3)
            button.canvas.bindings['<Leave>'](None)
            self.assertEqual(button._paint_bg, window.theme['toolbar_bg'])
            button.canvas.bindings['<FocusIn>'](None)
            self.assertEqual(button._paint_bg, window.theme['toolbar_bg'])
            button.configure(state='disabled')
            self.assertEqual(button._paint_bg, window.theme['toolbar_bg'])
            self.assertEqual(button.canvas.options['takefocus'], 0)
            button.canvas.bindings['<FocusOut>'](None)
        swap = window._swap_button
        self.assertEqual(swap._paint_bg, window.theme['window_bg'])
        swap._enter(None)
        self.assertEqual(swap._paint_bg, window.theme['toolbar_hover_bg'])
        swap._leave(None)
        self.assertEqual(swap._paint_bg, window.theme['window_bg'])
        self.assertFalse(any('<Tab>' in control.canvas.bindings for control in window._toolbar_controls))

    def test_rule_summary_marker_and_json_keyboard_toggle(self):
        window = self.window
        self.assertIsNone(window._rules_button.options['marker'])
        for key in ('ignore_trailing_space', 'ignore_line_endings'):
            window._options[key].set(True)
        window._refresh_rules()
        self.assertEqual(window._rules_button.options['marker'], 'dot')
        self.assertEqual(window._rules_button._tooltip_text, '已启用：忽略行尾空白、忽略换行符')
        with patch.object(window, '_request') as request:
            window._json_toggle.canvas.bindings['<space>'](None)
            self.assertTrue(window._options['json_format'].get())
            request.assert_called_once_with()
            window._refresh_rules()
            self.assertEqual(window._json_toggle.options['marker'], 'check')
            self.assertEqual(window._json_toggle.options['fg'], window.theme['accent'])
            self.assertEqual(window._json_toggle.options['bg'], '#E5F0FF')
            window._json_toggle.canvas.bindings['<Return>'](None)
            self.assertFalse(window._options['json_format'].get())
        window._refresh_rules()
        self.assertIsNone(window._json_toggle.options['marker'])

    def test_tooltip_leave_disable_change_and_close_cancel_timers(self):
        window, button = self.window, self.window._rules_button
        button._enter(None)
        identifier = button._tooltip_after
        button._leave(None)
        self.assertIn(identifier, window.win.cancelled)
        button._enter(None)
        identifier = button._tooltip_after
        window.win.callbacks.pop(identifier)()
        tooltip = button._tooltip
        self.assertEqual(tooltip.options['text'], '未启用比较规则')
        button.set_tooltip('新摘要')
        self.assertFalse(tooltip.exists)
        button._enter(None)
        identifier = button._tooltip_after
        button.configure(state='disabled')
        self.assertIn(identifier, window.win.cancelled)
        button.configure(state='normal')
        button._enter(None)
        window.win.callbacks.pop(button._tooltip_after)()
        tooltip = button._tooltip
        window._swap_button._enter(None)
        pending = window._swap_button._tooltip_after
        late = window.win.callbacks[pending]
        window.close()
        self.assertFalse(tooltip.exists)
        self.assertIn(pending, window.win.cancelled)
        self.assertTrue(all(control._photo is None for control in window._toolbar_controls))
        late()
        self.assertIsNone(window._swap_button._tooltip)
        self.assertEqual(window.win.callbacks, {})

    def test_navigation_compact_count_busy_and_no_difference(self):
        window = self.window
        self.assertEqual(window._block_label.options['text'], '比较中')
        self.finish()
        window.update(self.row('\n'.join(['a', 'keep'] * 8)),
                      self.row('\n'.join(['b', 'keep'] * 8)))
        self.assertEqual(window._block_label.options['text'], '比较中')
        self.finish()
        self.assertEqual(window._block_label.options['text'], '0/8')
        window._next.invoke()
        window._next.invoke()
        self.assertEqual(window._block_label.options['text'], '2/8')
        window.update(self.row('same'), self.row('same'))
        self.finish()
        self.assertEqual(window._block_label.options['text'], '无差异')
        self.assertEqual(window._previous.options['state'], 'disabled')
        self.assertEqual(window._next.options['state'], 'disabled')

    def test_toolbar_dark_custom_colors_and_configure_compatibility(self):
        theme = {'window_bg': '#161820', 'fill': '#303844', 'field': '#405060',
                 'accent': '#80B0FF', 'accent_soft': '#243854'}
        window = diff_window.DiffWindow(self.ui, self.left, self.right, theme=(theme, True))
        self.addCleanup(window.close)
        window._options['json_format'].set(True)
        window._refresh_rules()
        button = window._json_toggle
        self.assertEqual(button.options['bg'], theme['accent_soft'])
        self.assertEqual(button.options['fg'], theme['accent'])
        self.assertEqual(button._photo.getpixel((0, 0)), (22, 24, 32))
        self.assertEqual(button._photo.getpixel((60, 15)), (36, 56, 84))
        button.configure(state='normal', text='JSON', bg='#112233', fg='#CCDDEE')
        self.assertEqual(button.canvas.options['canvas_text']['text'], 'JSON')
        self.assertEqual(button.canvas.options['canvas_text']['fill'], '#CCDDEE')
        self.assertEqual(button._photo.getpixel((60, 15)), (17, 34, 51))
        self.assertEqual(button._photo.mode, 'RGB')

    def assert_aa_pixel(self, image, point, color, tolerance=30):
        expected = tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))
        self.assertLessEqual(max(abs(a - b) for a, b in zip(image.getpixel(point), expected)), tolerance)

    def test_toolbar_state_palette_selected_hover_focus_and_disabled_pixels(self):
        for dark in (False, True):
            window = diff_window.DiffWindow(self.ui, self.left, self.right, theme=({}, dark))
            self.addCleanup(window.close)
            t, button = window.theme, window._rules_button
            rgb = lambda color: tuple(value // 257 for value in button.winfo_rgb(color))
            with self.subTest(dark=dark):
                self.assertNotEqual(t['toolbar_bg'], t['accent_soft'])
                self.assertNotEqual(t['toolbar_bg'], t['toolbar_border'])
                self.assertEqual(button._paint_bg, t['toolbar_bg'])
                self.assertEqual(button._paint_border, t['toolbar_border'])
                normal = button._photo.copy()
                button._enter(None)
                self.assertEqual(button._paint_bg, t['toolbar_hover_bg'])
                self.assertEqual(button._paint_border, t['toolbar_hover_border'])
                hover = button._photo.copy()
                button._leave(None)
                button._focus(True)
                self.assertEqual(button._paint_bg, t['toolbar_bg'])
                self.assertEqual(button._paint_border, t['toolbar_border'])
                self.assertNotEqual(button._photo.tobytes(), normal.tobytes())
                self.assertNotEqual(button._photo.tobytes(), hover.tobytes())
                self.assert_aa_pixel(button._photo, (button._width // 2, 0), t['toolbar_focus'])
                button._focus(False)
                window._options['ignore_line_endings'].set(True)
                window._options['json_format'].set(True)
                window._refresh_rules()
                for selected in (button, window._json_toggle):
                    self.assertEqual(selected._paint_bg, t['accent_soft'])
                    self.assertEqual(selected._paint_border, t['accent'])
                    self.assertEqual(selected._paint_fg, t['accent'])
                    selected._enter(None)
                    self.assertEqual(selected._paint_bg, t['accent_soft'])
                    selected._leave(None)
                    before = selected._photo.copy()
                    selected._focus(True)
                    self.assertEqual(selected._paint_border, t['accent'])
                    self.assertNotEqual(selected._photo.tobytes(), before.tobytes())
                    selected.configure(state='disabled')
                    disabled = selected._photo.copy()
                    selected._enter(None)
                    selected._focus(True)
                    self.assertFalse(selected._hover)
                    self.assertEqual(selected._photo.tobytes(), disabled.tobytes())
                    self.assertEqual(selected._paint_bg, t['toolbar_bg'])
                    self.assertEqual(selected._paint_border, t['toolbar_disabled_border'])
                    self.assertEqual(selected._paint_fg, t['label2'])
                    self.assertEqual(selected.canvas.options['takefocus'], 0)
                self.assertEqual(button.canvas.options['oval']['fill'], t['label2'])
                self.assertEqual(window._swap_button._paint_bg, t['window_bg'])
                self.assertEqual(window._swap_button._paint_border, t['toolbar_border'])

    def test_toolbar_4x_aa_border_pixels_bounds_and_fractional_dpi(self):
        for dpi in (96, 120, 144, 192):
            with patch.object(FakeWidget, 'winfo_fpixels', return_value=dpi):
                window = diff_window.DiffWindow(self.ui, self.left, self.right)
            self.addCleanup(window.close)
            button, s = window._json_toggle, window._scale
            t = window.theme
            rgb = lambda color: tuple(value // 257 for value in button.winfo_rgb(color))
            with self.subTest(dpi=dpi):
                self.assertEqual(button._photo.size, (round(126 * s), round(30 * s)))
                self.assertEqual(button._radius, round(7 * s))
                self.assertEqual(button._photo.getpixel((0, 0)), rgb(t['window_bg']))
                self.assert_aa_pixel(button._photo, (button._width // 2, round(2 * s)),
                                     t['toolbar_border'])
                self.assertEqual(button._photo.getpixel((button._width // 2, button._height // 2)),
                                 rgb(t['toolbar_bg']))
                colors = {button._photo.getpixel((x, y)) for x in range(button._width)
                          for y in range(button._height)}
                self.assertGreater(len(colors), 10)
                self.assertTrue(colors - {rgb(t[key]) for key in
                                         ('window_bg', 'toolbar_border', 'toolbar_bg')})
                with patch.object(diff_window.Image, 'new', wraps=diff_window.Image.new) as new:
                    button._draw()
                self.assertEqual(new.call_args_list[0].args[1],
                                 (button._width * 4, button._height * 4))
                with patch.object(window, '_toggle_json') as toggle:
                    # 原始命令已绑定，改用控件的回调验证全部四条边界。
                    with patch.object(button, '_command', toggle):
                        for x, y in ((-1, 10), (button._width, 10),
                                     (10, -1), (10, button._height)):
                            button._click(SimpleNamespace(x=x, y=y))
                        toggle.assert_not_called()
                        button._click(SimpleNamespace(x=button._width - 1, y=button._height - 1))
                        toggle.assert_called_once_with()

    def test_navigation_shared_outline_separators_segment_hover_focus_and_dpi(self):
        for dpi in (96, 120, 144, 192):
            for dark in (False, True):
                with patch.object(FakeWidget, 'winfo_fpixels', return_value=dpi):
                    window = diff_window.DiffWindow(self.ui, self.left, self.right, theme=({}, dark))
                self.addCleanup(window.close)
                group, s, t = window._navigation, window._scale, window.theme
                rgb = lambda color: tuple(value // 257 for value in group.winfo_rgb(color))
                with self.subTest(dpi=dpi, dark=dark):
                    window._previous.configure(state='normal')
                    window._next.configure(state='normal')
                    base = group._photo.copy()
                    count = window._block_label
                    self.assertEqual(group._segments, [window._previous, window._next, count])
                    self.assertIsNone(count._command)
                    self.assertEqual(count.canvas.options['takefocus'], 0)
                    self.assertEqual(count._paint_fg, t['label2'])
                    count._enter(None)
                    count._click(SimpleNamespace(x=10, y=10))
                    count._focus(True)
                    count._activate()
                    self.assertFalse(count._focused)
                    self.assertNotIn('focused', count.canvas.options)
                    self.assertFalse(count._hover)
                    self.assertEqual(group._photo.tobytes(), base.tobytes())
                    for segment in group._segments:
                        x, y = segment._segment_start, group._height // 2
                        self.assertLessEqual(x + segment._width, group._width)
                        self.assertEqual(segment._photo.tobytes(),
                                         group._photo.crop((x, 0, x + segment._width, group._height)).tobytes())
                        if x:
                            self.assert_aa_pixel(group._photo, (x, y), t['toolbar_border'], tolerance=12)
                    for button in (window._previous, window._next):
                        x = button._segment_start
                        before_count = count._photo.copy()
                        other = window._next if button is window._previous else window._previous
                        before_other = other._photo.copy()
                        button._enter(None)
                        self.assertEqual(button._paint_bg, t['toolbar_hover_bg'])
                        self.assertEqual(button._paint_border, t['toolbar_hover_border'])
                        self.assertEqual(group._photo.getpixel((x + button._width // 2, group._height // 2)),
                                         rgb(t['toolbar_hover_bg']))
                        self.assert_aa_pixel(group._photo,
                                             (x + button._width // 2, int(2.5 * s)),
                                             t['toolbar_hover_border'], tolerance=30)
                        self.assertEqual(count._photo.tobytes(), before_count.tobytes())
                        self.assertEqual(other._photo.tobytes(), before_other.tobytes())
                        hovered = button._photo.copy()
                        button._leave(None)
                        button._focus(True)
                        self.assertEqual(button._paint_bg, t['toolbar_bg'])
                        self.assertNotEqual(button._photo.tobytes(), hovered.tobytes())
                        self.assert_aa_pixel(button._photo, (button._width // 2, 0), t['toolbar_focus'])
                        button.configure(state='disabled')
                        button._enter(None)
                        self.assertFalse(button._hover)
                        self.assertEqual(button._paint_border, t['toolbar_disabled_border'])
                        self.assert_aa_pixel(button._photo, (button._width // 2, 0),
                                             t['window_bg'], tolerance=2)
                        self.assertEqual(count._photo.tobytes(), before_count.tobytes())
                        button._focus(False)
                        button.configure(state='normal')
                    self.assertEqual(group._photo.tobytes(), base.tobytes())

    def use_session_server(self):
        self.window.close()
        server = UiServer(SimpleNamespace(), {}, self.ui.actions)
        server._root, server._tk = self.ui._root, self.tk
        override = patch.dict(UI_GLOBALS, {'DiffWindow': diff_window.DiffWindow})
        override.start()
        self.addCleanup(override.stop)
        self.addCleanup(server.clear_text_diff)
        self.ui = server
        return server

    def test_session_close_reopen_json_rules_swap_and_scroll_not_saved(self):
        server = self.use_session_server()
        rows = (self.row('{"a":1}', 11), self.row('{ "a" : 2 }', 12))
        server.show_text_diff(*rows)
        self.window = server._diff_window
        self.finish()
        for var in self.window._options.values():
            var.set(True)
        self.window._swap()
        self.finish()
        self.window._scroll_y('moveto', '0.7')
        old = self.window
        old.win.protocols['WM_DELETE_WINDOW']()
        snapshot = server._diff_session
        self.assertEqual(snapshot['rows'], rows[::-1])
        self.assertEqual(snapshot['roles'], ('B', 'A'))
        self.assertTrue(all(snapshot['options'].values()))
        rows[0]['content'] = '外部修改'
        server.view_text_diff()
        self.window = server._diff_window
        self.assertIsNot(self.window, old)
        self.finish()
        self.assertEqual(self.window.export_session(), snapshot)
        self.assertEqual(self.window._result, diff_tools.compare_text(
            *(row['content'] for row in snapshot['rows']), **snapshot['options']))
        self.assertEqual(self.window._texts[0].view[0], 0)
        with patch.object(self.window, 'update') as update, \
                patch.object(self.window, '_request') as request:
            server.view_text_diff()
        update.assert_not_called()
        request.assert_not_called()

    def test_session_busy_escape_retains_latest_full_request_and_ignores_worker(self):
        server = self.use_session_server()
        server.show_text_diff(self.left, self.right)
        self.window = server._diff_window
        worker = self.window._worker
        rows = (self.row('{"尾部":"' + '左' * 140000 + '"}', 21),
                self.row('{"尾部":"完整右侧"}', 22))
        self.window.update(*rows)
        for var in self.window._options.values():
            var.set(True)
        self.window._swap()
        expected = self.window.export_session()
        self.window._escape(None)
        self.assertIsNone(server._diff_window)
        self.assertEqual(server._diff_session, expected)
        worker.finish()
        self.window._poll()
        self.assertEqual(server._diff_session, expected)
        server.view_text_diff()
        self.window = server._diff_window
        self.finish()
        self.assertEqual(self.window.export_session(), expected)
        self.assertEqual(len(self.window._right_row['content']), 140009)

    def test_session_failure_restores_rows_options_roles_before_close(self):
        server = self.use_session_server()
        server.show_text_diff(self.left, self.right)
        self.window = server._diff_window
        self.finish()
        self.window._swap()
        self.finish()
        expected = self.window.export_session()
        previous = self.window._result
        for var in self.window._options.values():
            var.set(True)
        self.window.update(self.row('bad', 31), self.row('also bad', 32))
        self.assertNotEqual(server._diff_session, expected)
        self.finish()
        self.assertIs(self.window._result, previous)
        self.assertEqual(server._diff_session, expected)
        self.window.close()
        server.view_text_diff()
        self.window = server._diff_window
        self.finish()
        self.assertEqual(self.window.export_session(), expected)
        self.assertEqual(self.window._result, previous)

    def test_session_destroy_after_tk_variables_gone_keeps_last_snapshot(self):
        server = self.use_session_server()
        server.show_text_diff(self.left, self.right)
        self.window = server._diff_window
        self.window._swap()
        expected = server._diff_session
        with patch.object(self.window, 'export_session',
                          side_effect=diff_window.TclError('变量已销毁')):
            self.window.win.destroy()
        self.assertEqual(server._diff_session, expected)
        self.assertIsNone(server._diff_window)
        self.assertTrue(self.window._closed)

    def test_session_snapshot_exports_only_on_ui_thread_not_worker(self):
        server = self.use_session_server()
        server.show_text_diff(self.left, self.right)
        self.window = server._diff_window
        owner = get_ident()
        exported = []
        export = self.window.export_session

        def checked_export():
            self.assertEqual(get_ident(), owner)
            exported.append(owner)
            return export()

        with patch.object(self.window, 'export_session', side_effect=checked_export):
            self.window._options['ignore_line_endings'].set(True)
            self.window._request()
            worker = self.window._worker
            from threading import Thread
            thread = Thread(target=worker.finish)
            thread.start()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(exported), 1)
            self.poll()
            self.finish()
            self.window.close()
        self.assertEqual(len(exported), 3)
        self.assertTrue(server._diff_session['options']['ignore_line_endings'])

    def test_session_new_pair_pending_failure_never_restores_old_pair(self):
        server = self.use_session_server()
        server.show_text_diff(self.left, self.right)
        self.window = server._diff_window
        self.finish()
        self.window._swap()
        old = self.window
        old_worker = old._worker
        server.show_text_diff(self.row(None, 41), self.row('新 B', 42))
        self.window = server._diff_window
        expected = server._diff_session
        self.assertTrue(old._closed)
        self.assertIsNone(self.window._result)
        old_worker.finish()
        old._poll()
        old.close()
        self.finish()
        self.assertEqual(server._diff_session, expected)
        self.assertIsNone(self.window._result)
        self.assertFalse(self.window._retained)
        self.assertEqual(self.window._roles, ('A', 'B'))

    def test_ast_worker_is_data_only_and_no_direct_clipboard_or_paste(self):
        source = Path(diff_window.__file__).read_text(encoding='utf-8')
        tree = ast.parse(source)
        worker = next(node for node in ast.walk(tree)
                      if isinstance(node, ast.FunctionDef) and node.name == '_compare')
        attributes = {node.attr for node in ast.walk(worker) if isinstance(node, ast.Attribute)}
        self.assertEqual(attributes, {'compare_text', 'put'})
        all_attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        self.assertFalse({'clipboard_clear', 'clipboard_append', 'paste_to_foreground',
                          'event_generate', 'overrideredirect', 'grab_set', 'Menu'} & all_attributes)
        copy_calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                      and isinstance(node.func, ast.Attribute) and node.func.attr == 'copy_to_clipboard']
        self.assertEqual(len(copy_calls), 1)
        self.assertEqual(list(ast.parse(source).body)[-1].name, 'DiffWindow')


if __name__ == '__main__':
    unittest.main()
