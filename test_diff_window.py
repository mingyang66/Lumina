import ast
import importlib.util
import sys
from pathlib import Path
from threading import Event, get_ident
from types import SimpleNamespace
import unittest
from unittest.mock import mock_open, patch

from lumina import diff_tools


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
        self.options = dict(kwargs)
        self.bindings = {}
        self.tags = []
        self.content = ''
        self.exists = True
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
        self.options['grid'] = kwargs

    def pack(self, **kwargs):
        self.options['pack'] = kwargs

    def grid_rowconfigure(self, *args, **kwargs):
        pass

    def grid_columnconfigure(self, *args, **kwargs):
        pass

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
        self.copies = []
        self.tk = SimpleNamespace(**{name: FakeWidget for name in
                                    ('Toplevel', 'Frame', 'Text', 'Label', 'Button',
                                     'Scrollbar', 'Checkbutton')}, BooleanVar=FakeVar)
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
        self.finish()
        self.assertEqual(self.copies, [])
        self.assertEqual(window._copy_left.options['state'], 'normal')

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
        self.assertIn('规则相等', window._status.options['text'])

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
        # JSON 空文本失败，先关闭格式化再比较空文本。
        window._options['json_format'].set(False)
        window._request()
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
        self.assertEqual(window._block_label.options['text'], '差异块 1/2')
        window.update(self.row('same'), self.row('same'))
        self.finish()
        self.assertEqual(window._next.options['state'], 'disabled')
        self.assertEqual(window._block_label.options['text'], '差异块 0/0')

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

    def test_ast_worker_is_data_only_and_no_direct_clipboard_or_paste(self):
        source = Path(diff_window.__file__).read_text(encoding='utf-8')
        tree = ast.parse(source)
        worker = next(node for node in ast.walk(tree)
                      if isinstance(node, ast.FunctionDef) and node.name == '_compare')
        attributes = {node.attr for node in ast.walk(worker) if isinstance(node, ast.Attribute)}
        self.assertEqual(attributes, {'compare_text', 'put'})
        all_attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        self.assertFalse({'clipboard_clear', 'clipboard_append', 'paste_to_foreground',
                          'event_generate', 'overrideredirect', 'grab_set'} & all_attributes)
        copy_calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                      and isinstance(node.func, ast.Attribute) and node.func.attr == 'copy_to_clipboard']
        self.assertEqual(len(copy_calls), 1)
        self.assertEqual(list(ast.parse(source).body)[-1].name, 'DiffWindow')


if __name__ == '__main__':
    unittest.main()
