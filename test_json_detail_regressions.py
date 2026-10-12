import ast
from collections import deque
from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import traceback
import types
import unittest
from unittest.mock import Mock, patch

from lumina import json_tools


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / 'lumina' / 'history_panel.py'


def load_detail_pane():
    tree = ast.parse(SOURCE.read_text(encoding='utf-8'), filename=str(SOURCE))
    detail = next(node for node in tree.body
                  if isinstance(node, ast.ClassDef) and node.name == 'DetailPane')
    namespace = {
        '__name__': 'lumina._json_detail_regressions',
        '__package__': 'lumina',
        're': re, 'json': json, 'os': os, 'traceback': traceback,
        # 不导入 history_panel 或真实 threading/剪贴板模块。
        'threading': types.SimpleNamespace(Thread=None),
        'FlatMenu': Mock(),
    }
    exec(compile(ast.Module(body=[detail], type_ignores=[]), str(SOURCE), 'exec'),
         namespace)
    return namespace['DetailPane'], namespace


DetailPane, DETAIL_GLOBALS = load_detail_pane()


class FakeWidget:
    def __init__(self, parent=None, **options):
        self.parent = parent
        self.options = options
        self.children = []
        self.bindings = {}
        self.text = ''
        self.exists = True
        self.queue = deque()
        self.next_job = 0
        if isinstance(parent, FakeWidget):
            parent.children.append(self)

    def pack(self, **options):
        pass

    def pack_propagate(self, value):
        pass

    def bind(self, event, callback):
        self.bindings[event] = callback

    def configure(self, **options):
        self.options.update(options)

    def destroy(self):
        self.exists = False
        if self.parent is not None:
            self.parent.children.remove(self)

    def winfo_children(self):
        return list(self.children)

    def winfo_exists(self):
        return self.exists

    def winfo_rootx(self):
        return 10

    def winfo_rooty(self):
        return 20

    def insert(self, index, text):
        self.text += text

    def index(self, index):
        return f'1.{len(self.text)}'

    def get(self, *args):
        raise AssertionError('完整输出不得读取截断后的预览控件')

    def tag_add(self, *args):
        pass

    def tag_configure(self, *args, **kwargs):
        pass

    def itemconfigure(self, *args, **kwargs):
        pass

    def create_image(self, *args, **kwargs):
        return 1

    def create_text(self, *args, **kwargs):
        self.options['text'] = kwargs['text']
        return 2

    def after(self, delay, callback):
        self.next_job += 1
        self.queue.append((self.next_job, delay, callback))
        return self.next_job

    def after_idle(self, callback):
        return self.after(0, callback)

    def after_cancel(self, job):
        self.queue = deque(entry for entry in self.queue if entry[0] != job)

    def run_next(self):
        _, _, callback = self.queue.popleft()
        callback()

    def drain(self):
        for _ in range(100):
            if not self.queue:
                return
            self.run_next()
        raise AssertionError('after 队列未收敛')


FakeTk = types.SimpleNamespace(
    Frame=FakeWidget, Label=FakeWidget, Text=FakeWidget, Canvas=FakeWidget)


class FakeThreads:
    def __init__(self):
        self.deferred = False
        self.created = []
        self.pending = deque()
        self.factory = Mock(side_effect=self.make_thread)

    def make_thread(self, target, args=(), **options):
        thread = Mock()

        def start():
            if self.deferred:
                self.pending.append((target, args))
            else:
                target(*args)

        thread.start.side_effect = start
        self.created.append(thread)
        return thread

    def finish_next(self):
        target, args = self.pending.popleft()
        target(*args)


@contextmanager
def fake_photos():
    image = Mock()
    image_module = types.SimpleNamespace(new=Mock(return_value=image), LANCZOS=1)
    draw_module = types.SimpleNamespace(Draw=Mock(return_value=Mock()))
    tk_module = types.SimpleNamespace(PhotoImage=Mock(return_value=object()))
    pil = types.ModuleType('PIL')
    pil.Image, pil.ImageDraw, pil.ImageTk = image_module, draw_module, tk_module
    with patch.dict(sys.modules, {
            'PIL': pil, 'PIL.Image': image_module,
            'PIL.ImageDraw': draw_module, 'PIL.ImageTk': tk_module}):
        yield


class JsonDetailRegressionTests(unittest.TestCase):
    def setUp(self):
        self.threads = FakeThreads()
        thread_patch = patch.object(DETAIL_GLOBALS['threading'], 'Thread',
                                    self.threads.factory)
        thread_patch.start()
        self.addCleanup(thread_patch.stop)
        self.menu_factory = Mock()

        # 导入替身必须是真实类，供 _json_menu 的 isinstance/toggle 分支使用。
        class FakeJsonPopup:
            def __init__(menu, *args, **kwargs):
                self.menu_factory(*args, **kwargs)
                menu.popup = Mock()
                menu.close = Mock(side_effect=self.detail._menu_closed)

        popup_module = types.ModuleType('lumina.json_popup')
        popup_module.JsonPopup = FakeJsonPopup
        menu_patch = patch.dict(sys.modules, {'lumina.json_popup': popup_module})
        menu_patch.start()
        self.addCleanup(menu_patch.stop)
        theme = dict.fromkeys(('card_bg', 'field', 'hairline', 'label', 'label2',
                               'label3', 'accent', 'accent_soft'), '#334155')
        self.panel = types.SimpleNamespace(
            ui=types.SimpleNamespace(actions=types.SimpleNamespace(
                copy_to_clipboard=Mock())),
            tk=FakeTk, win=Mock(), status_var=Mock(), db=Mock(),
            _rows={}, _prev_hwnd=123, _dpi=1,
            _theme=Mock(return_value=(theme, False)),
            _sys=Mock(return_value='#334155'), _measure=Mock(return_value=40),
            _hex_to_rgb=Mock(return_value=(51, 65, 85)),
            _source_app=Mock(return_value=('测试来源', 'blue', 'plain')),
            _is_dark_mode=Mock(return_value=False),
            _do_paste_back=Mock(), _hdr_press=Mock(), _hdr_move=Mock(),
            _category_tile=Mock(return_value=object()))
        with fake_photos():
            self.detail = DetailPane(self.panel, FakeWidget(), 380)
        self.detail._json_anchor = FakeWidget()
        self.detail._update_meta = Mock()
        self.detail._update_actions = Mock()
        self.detail._highlight_code = Mock()
        self.detail._linkify = Mock()
        self.set_row(' { "a" : [1, true, null] } ')

    def make_row(self, raw, row_id=1, category='text', pinned=0):
        return dict(id=row_id, content=raw, kind='text', category=category,
                    pinned=pinned, data=None, data_size=0, source='test',
                    created_at='2000-01-01', tags='', file_status='none')

    def set_row(self, raw, row_id=1, category='text'):
        row = self.make_row(raw, row_id, category)
        self.panel._rows[str(row_id)] = row
        self.panel.db.get_text.side_effect = lambda rid: self.panel._rows[str(rid)]
        # 测试夹具明确重建预览；同 id 更新的缓存问题由独立测试覆盖。
        self.detail._cur_key = None
        self.detail.show(row_id, immediate=True)
        return row

    def transform(self, mode):
        self.detail._transform_json(mode)
        self.detail.frame.drain()

    def status(self):
        return self.panel.status_var.set.call_args.args[0]

    def assert_original(self, row, snapshot):
        self.assertIs(self.detail.row, row)
        self.assertEqual(row, snapshot)
        self.panel.db.get.assert_not_called()
        self.panel.db.execute.assert_not_called()
        self.panel.db.update.assert_not_called()

    def test_initial_state_and_effective_row_without_result(self):
        self.assertIsNone(self.detail._json_text)
        self.assertIsNone(self.detail._json_mode)
        self.assertFalse(self.detail._json_busy)
        self.assertIs(self.detail._effective_row(), self.detail.row)
        self.detail.row = None
        self.assertIsNone(self.detail._effective_row())

    def test_format_and_compress_preserve_exact_tokens_and_original_row(self):
        raw = r' { "中文" : "  a  b\t\n 中文 ", "esc" : "\u4e2d\/\\\"", "n" : [-0,1E+009,0.123456789012345678901234567890,-2.00e-003], "empty": {} } '
        compact = r'{"中文":"  a  b\t\n 中文 ","esc":"\u4e2d\/\\\"","n":[-0,1E+009,0.123456789012345678901234567890,-2.00e-003],"empty":{}}'
        for category in ('text', 'code'):
            with self.subTest(category=category):
                row = self.set_row(raw, category=category)
                snapshot = deepcopy(row)
                self.transform('format')
                formatted = self.detail._json_text
                self.assertIn('\n  "中文": ', formatted)
                self.assertEqual(json_tools.transform_json(formatted, 'compress').text,
                                 compact)
                self.assertEqual(self.detail._body.text, formatted)
                self.assertEqual(self.detail._meta_title.options['text'], 'JSON · 已格式化')
                self.assertIn('原文未修改', self.status())
                self.assert_original(row, snapshot)
                self.transform('compress')
                self.assertEqual(self.detail._json_text, compact)
                self.assertEqual(self.detail._body.text, compact)
                self.assertEqual(self.detail._json_mode, 'compress')
                effective = self.detail._effective_row()
                self.assertIsNot(effective, row)
                self.assertEqual(effective, dict(snapshot, content=compact))
                self.assert_original(row, snapshot)

    def test_copy_paste_export_use_full_result_not_truncated_preview(self):
        raw = json.dumps({'文本': '中' * (DetailPane.TEXT_MAX + 100), '尾部': '完整结尾'},
                         ensure_ascii=False)
        for mode in ('format', 'compress'):
            with self.subTest(mode=mode):
                row = self.set_row(raw)
                snapshot = deepcopy(row)
                self.transform(mode)
                expected = json_tools.transform_json(raw, mode).text
                self.assertGreater(len(expected), DetailPane.TEXT_MAX)
                self.assertIn('已截断', self.detail._body.text)
                self.assertNotIn('完整结尾', self.detail._body.text)
                self.detail._copy()
                copied = self.panel.ui.actions.copy_to_clipboard.call_args.args[0]
                self.assertEqual(copied, dict(snapshot, content=expected))
                self.detail._paste_this()
                pasted, hwnd = self.panel._do_paste_back.call_args.args
                self.assertEqual(pasted, copied)
                self.assertEqual(hwnd, 123)
                self.assertEqual([call.args[0] for call in self.panel.win.after.call_args_list[-2:]],
                                 [500, 520])
                filedialog = types.SimpleNamespace(asksaveasfilename=Mock())
                tkinter = types.ModuleType('tkinter')
                tkinter.filedialog = filedialog
                with tempfile.TemporaryDirectory(prefix='lumina-json-test-') as directory:
                    path = Path(directory) / 'result.txt'
                    filedialog.asksaveasfilename.return_value = str(path)
                    with patch.dict(sys.modules, {'tkinter': tkinter,
                                                 'tkinter.filedialog': filedialog}):
                        self.detail._save_as()
                    self.assertEqual(path.read_bytes(), expected.encode('utf-8'))
                    filedialog.asksaveasfilename.assert_called_once()
                    self.assertIs(filedialog.asksaveasfilename.call_args.kwargs['parent'],
                                  self.panel.win)
                self.assert_original(row, snapshot)

    def test_restore_clears_result_and_outputs_original(self):
        row = self.detail.row
        snapshot = deepcopy(row)
        self.transform('format')
        request = self.detail._json_request
        self.detail._restore_json()
        self.assertGreater(self.detail._json_request, request)
        self.assertIsNone(self.detail._json_text)
        self.assertIsNone(self.detail._json_mode)
        self.assertFalse(self.detail._json_busy)
        self.assertEqual(self.detail._body.text, snapshot['content'])
        self.assertEqual(self.status(), '已恢复原文')
        self.detail._copy()
        self.panel.ui.actions.copy_to_clipboard.assert_called_once_with(row)
        self.detail._paste_this()
        self.panel._do_paste_back.assert_called_once_with(row, 123)
        self.assert_original(row, snapshot)

    def test_restore_discards_pending_result_before_and_after_worker_finishes(self):
        for completed in (False, True):
            with self.subTest(completed=completed):
                self.threads.deferred = True
                self.detail._transform_json('format')
                self.assertTrue(self.detail._json_busy)
                if completed:
                    self.threads.finish_next()
                self.detail._restore_json()
                body = self.detail._body
                if not completed:
                    self.threads.finish_next()
                self.detail.frame.drain()
                self.assertIs(self.detail._body, body)
                self.assertIsNone(self.detail._json_text)
                self.assertFalse(self.detail._json_busy)
                self.assertEqual(self.status(), '已恢复原文')

    def test_switch_discards_old_result_without_clearing_new_busy_state(self):
        self.threads.deferred = True
        old = deepcopy(self.detail.row)
        self.detail._transform_json('format')
        self.set_row('{"新":2}', row_id=2)
        self.detail._transform_json('compress')
        self.threads.finish_next()
        self.detail.frame.run_next()
        self.assertTrue(self.detail._json_busy)
        self.assertIsNone(self.detail._json_text)
        self.assertEqual(self.detail._body.text, '{"新":2}')
        self.threads.finish_next()
        self.detail.frame.drain()
        self.assertEqual(self.detail._json_text, '{"新":2}')
        self.assertEqual(self.panel._rows['1'], old)

    def test_placeholder_clears_state_and_discards_pending_result(self):
        self.transform('compress')
        self.threads.deferred = True
        self.detail._transform_json('format')
        with fake_photos():
            self.detail.show(None)
        self.threads.finish_next()
        self.detail.frame.drain()
        self.assertIsNone(self.detail.row)
        self.assertIsNone(self.detail._json_text)
        self.assertIsNone(self.detail._json_mode)
        self.assertFalse(self.detail._json_busy)
        self.assertIsNone(self.detail._effective_row())

    def test_same_row_rerender_preserves_result_but_content_change_invalidates_it(self):
        self.transform('format')
        result = self.detail._json_text
        self.detail._cur_key = None
        self.detail._rerender_current()
        self.assertEqual(self.detail._json_text, result)
        self.assertEqual(self.detail._body.text, result)
        # 收藏变化触发重渲染，但正文没变，不应清掉结果。
        self.panel._rows['1'] = dict(self.detail.row, pinned=1)
        self.detail.show(1, immediate=True)
        self.assertEqual(self.detail._json_text, result)
        self.panel._rows['1'] = dict(self.detail.row, content='{"更新":3}')
        self.detail._cur_key = None
        self.detail.show(1, immediate=True)
        self.assertIsNone(self.detail._json_text)
        self.assertEqual(self.detail._body.text, '{"更新":3}')

    def test_same_id_content_update_discards_pending_result_without_size_invalidation(self):
        self.threads.deferred = True
        self.detail._transform_json('format')
        updated = dict(self.detail.row, content='{"更新":3}')
        self.panel._rows['1'] = updated
        self.detail.show(1, immediate=True)
        self.threads.finish_next()
        self.detail.frame.drain()
        self.assertEqual(self.detail.row['content'], updated['content'])
        self.assertIsNone(self.detail._json_text)
        self.assertFalse(self.detail._json_busy)
        self.assertEqual(self.detail._body.text, updated['content'])

    def test_invalid_json_keeps_previous_result_and_reports_line_column(self):
        self.transform('format')
        result, body = self.detail._json_text, self.detail._body
        # 保持原行有效，用转换器错误模拟异步解析失败。
        error = json.JSONDecodeError('模拟非法 JSON', '{\n  "a": @\n}', 9)
        with patch.object(json_tools, 'transform_json', side_effect=error):
            self.transform('compress')
        self.assertEqual(self.detail._json_text, result)
        self.assertEqual(self.detail._json_mode, 'format')
        self.assertIs(self.detail._body, body)
        self.assertFalse(self.detail._json_busy)
        self.assertIn('第 2 行，第 8 列', self.status())
        self.assertIn('模拟非法 JSON', self.status())
        self.detail._copy()
        self.assertEqual(self.panel.ui.actions.copy_to_clipboard.call_args.args[0]['content'],
                         result)

    def test_actual_invalid_inputs_leave_existing_result_unchanged(self):
        for raw in ('', '[1,]', '{"a":NaN}', '{\n"a": @\n}'):
            with self.subTest(raw=raw):
                row = self.set_row(raw)
                snapshot = deepcopy(row)
                self.detail._json_text = '上一次成功结果'
                self.detail._json_mode = 'format'
                self.transform('compress')
                self.assertEqual(self.detail._json_text, '上一次成功结果')
                self.assertEqual(self.detail._json_mode, 'format')
                self.assertFalse(self.detail._json_busy)
                self.assertIn('JSON 格式错误：第', self.status())
                self.assert_original(row, snapshot)

    def test_duplicate_keys_are_preserved_and_reported(self):
        raw = r'{"a":1,"a":2,"\u0061":3,"nested":{"x":1,"x":2}}'
        row = self.set_row(raw)
        snapshot = deepcopy(row)
        for mode in ('format', 'compress'):
            with self.subTest(mode=mode):
                self.transform(mode)
                self.assertEqual(json_tools.transform_json(self.detail._json_text,
                                                           'compress').text, raw)
                self.assertIn('发现 2 个重复键，已保留', self.status())
                self.assert_original(row, snapshot)

    def test_json_menu_has_three_correctly_bound_actions(self):
        old_menu = Mock()
        self.detail._flat_menu = old_menu
        self.detail._json_menu()
        old_menu.close.assert_called_once()
        self.menu_factory.assert_called_once()
        args, kwargs = self.menu_factory.call_args
        self.assertIs(args[0], FakeTk)
        self.assertIs(args[1], self.panel.win)
        items = args[2]
        self.assertTrue(all(isinstance(item, dict) for item in items))
        self.assertEqual([item['label'] for item in items],
                         ['格式化', '压缩', '恢复原文'])
        self.assertEqual([item['hint'] for item in items],
                         ['2 空格', '紧凑输出', ''])
        self.assertEqual([item['kind'] for item in items],
                         ['format', 'compress', 'restore'])
        self.assertEqual(args[3], self.panel._theme()[0])
        self.assertEqual(kwargs, {'dpi': self.panel._dpi})
        menu = self.detail._flat_menu
        self.assertTrue(self.detail._in_menu)
        menu.popup.assert_called_once_with(self.detail._json_anchor,
                                           on_close=self.detail._menu_closed)
        with patch.object(self.detail, '_transform_json') as transform:
            items[0]['command']()
            items[1]['command']()
            self.assertEqual([call.args for call in transform.call_args_list],
                             [('format',), ('compress',)])
        self.transform('format')
        items[2]['command']()
        self.assertIsNone(self.detail._json_text)
        menu.popup.call_args.kwargs['on_close']()
        self.assertFalse(self.detail._in_menu)
        self.assertIsNone(self.detail._flat_menu)

    def test_json_button_second_click_toggles_existing_popup_closed(self):
        with fake_photos():
            DetailPane._update_actions(self.detail)
        anchor = self.detail._json_anchor
        click = anchor.bindings['<ButtonRelease-1>']
        click(None)
        menu = self.detail._flat_menu
        self.assertTrue(self.detail._in_menu)
        menu.popup.assert_called_once_with(anchor, on_close=self.detail._menu_closed)
        click(None)
        menu.close.assert_called_once_with()
        self.menu_factory.assert_called_once()
        self.assertFalse(self.detail._in_menu)
        self.assertIsNone(self.detail._flat_menu)

    def test_json_menu_restore_initially_disabled(self):
        self.detail._json_menu()
        items = self.menu_factory.call_args.args[2]
        self.assertEqual([item['enabled'] for item in items], [True, True, False])
        self.assertEqual([item['selected'] for item in items], [False, False, False])
        self.threads.factory.assert_not_called()

    def test_json_menu_selects_current_mode_and_enables_restore(self):
        for mode in ('format', 'compress'):
            with self.subTest(mode=mode):
                self.detail._close_menu()
                self.transform(mode)
                self.detail._json_menu()
                items = self.menu_factory.call_args.args[2]
                self.assertEqual([item['enabled'] for item in items], [True] * 3)
                self.assertEqual([item['selected'] for item in items],
                                 [mode == 'format', mode == 'compress', False])
                items[2]['command']()
                self.assertIsNone(self.detail._json_text)
                self.assertIsNone(self.detail._json_mode)
                self.assertEqual(self.detail._body.text, self.detail.row['content'])
                self.assertEqual(self.status(), '已恢复原文')

    def test_json_menu_busy_disables_transforms_and_restore_cancels_pending_work(self):
        for previous_mode in (None, 'format', 'compress'):
            with self.subTest(previous_mode=previous_mode):
                self.detail._close_menu()
                self.threads.deferred = False
                self.detail._restore_json()
                if previous_mode is not None:
                    self.transform(previous_mode)
                self.threads.deferred = True
                self.detail._transform_json('compress' if previous_mode == 'format'
                                            else 'format')
                request = self.detail._json_request
                self.assertTrue(self.detail._json_busy)
                self.detail._json_menu()
                items = self.menu_factory.call_args.args[2]
                self.assertEqual([item['enabled'] for item in items],
                                 [False, False, True])
                self.assertEqual([item['selected'] for item in items],
                                 [previous_mode == 'format',
                                  previous_mode == 'compress', False])
                items[2]['command']()
                self.assertGreater(self.detail._json_request, request)
                self.assertFalse(self.detail._json_busy)
                self.assertIsNone(self.detail._json_text)
                self.assertIsNone(self.detail._json_mode)
                body = self.detail._body
                self.threads.finish_next()
                self.detail.frame.drain()
                self.assertIs(self.detail._body, body)
                self.assertEqual(body.text, self.detail.row['content'])
                self.assertIsNone(self.detail._json_text)
                self.assertFalse(self.detail._json_busy)
                self.assertEqual(self.status(), '已恢复原文')

    def test_action_bar_exposes_json_for_nonempty_text_and_code_only(self):
        for category, raw, visible in (('text', '{}', True), ('code', '{}', True),
                                       ('text', ' \n ', False), ('link', '{}', False),
                                       ('file', '{}', False), ('image', '{}', False)):
            with self.subTest(category=category, raw=raw):
                self.detail.cat = category
                self.detail.row = self.make_row(raw, category=category)
                with patch.object(self.detail, '_json_menu') as menu:
                    with fake_photos():
                        DetailPane._update_actions(self.detail)
                    buttons = {widget.options['text']: widget
                               for widget in self.detail._actions.winfo_children()}
                    self.assertEqual('JSON ▾' in buttons, visible)
                    self.assertNotIn('JSON', buttons)
                    if visible:
                        self.assertIs(self.detail._json_anchor, buttons['JSON ▾'])
                        buttons['JSON ▾'].bindings['<ButtonRelease-1>'](None)
                        menu.assert_called_once_with()

    def test_busy_repeated_requests_start_only_one_worker_and_keep_request_token(self):
        self.threads.deferred = True
        self.detail._transform_json('format')
        request = self.detail._json_request
        self.detail.frame.run_next()
        self.assertEqual(len(self.detail.frame.queue), 1)
        for mode in ('compress', 'format', 'compress'):
            self.detail._transform_json(mode)
            self.assertEqual(self.detail._json_request, request)
            self.assertTrue(self.detail._json_busy)
            self.assertIn('正在处理中', self.status())
        self.threads.factory.assert_called_once()
        self.threads.created[0].start.assert_called_once()
        self.assertEqual(len(self.detail.frame.queue), 1)
        self.threads.finish_next()
        self.detail.frame.drain()
        self.assertFalse(self.detail._json_busy)
        self.assertEqual(self.detail._json_mode, 'format')
        self.assertEqual(self.detail._json_text,
                         json_tools.transform_json(self.detail.row['content']).text)

    def test_nontext_and_missing_rows_do_not_start_workers(self):
        for category in ('file', 'image', 'link'):
            self.detail.cat = category
            self.detail._transform_json('format')
        self.detail.row = None
        self.detail.cat = 'text'
        self.detail._transform_json('format')
        self.threads.factory.assert_not_called()
        self.assertFalse(self.detail._json_busy)

    def test_size_and_value_errors_keep_result_and_release_busy(self):
        self.transform('format')
        previous = self.detail._json_text
        body = self.detail._body
        for exception in (ValueError('JSON 输入过大'), TypeError('JSON 类型错误'),
                          RecursionError('JSON 嵌套过深')):
            with self.subTest(exception=type(exception).__name__):
                with patch.object(json_tools, 'transform_json', side_effect=exception):
                    self.transform('compress')
                self.assertEqual(self.detail._json_text, previous)
                self.assertIs(self.detail._body, body)
                self.assertFalse(self.detail._json_busy)
                self.assertEqual(self.status(), str(exception))
        with patch.object(json_tools, 'MAX_INPUT_BYTES', 1):
            count = self.threads.factory.call_count
            self.detail._transform_json('compress')
            self.assertEqual(self.threads.factory.call_count, count)
        self.assertEqual(self.detail._json_text, previous)
        self.assertFalse(self.detail._json_busy)
        self.assertIn('输入过大', self.status())


if __name__ == '__main__':
    unittest.main(verbosity=2)
