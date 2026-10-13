import ast
import gc
from pathlib import Path
import queue
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from contextlib import suppress


ROOT = Path(__file__).resolve().parent
UI_SOURCE = ROOT / 'lumina' / 'ui.py'
DIFF_SOURCE = ROOT / 'lumina' / 'diff_window.py'
UI_TREE = ast.parse(UI_SOURCE.read_text(encoding='utf-8'))
DIFF_TREE = ast.parse(DIFF_SOURCE.read_text(encoding='utf-8'))


def load_class(tree, name, namespace):
    cls = next(node for node in tree.body
               if isinstance(node, ast.ClassDef) and node.name == name)
    exec(compile(ast.Module(body=[cls], type_ignores=[]), str(UI_SOURCE), 'exec'), namespace)
    return namespace[name]


class FakeWindow:
    def __init__(self):
        self.owner = threading.get_ident()
        self.calls = []

    def record(self, name):
        if threading.get_ident() != self.owner:
            raise AssertionError('窗口操作必须在 UI 线程执行')
        self.calls.append(name)

    def deiconify(self):
        self.record('deiconify')

    def lift(self):
        self.record('lift')

    def focus_force(self):
        self.record('focus_force')


class FakeDiffWindow:
    instances = []

    def __init__(self, ui, left_row, right_row, session=None):
        self.ui = ui
        self.win = FakeWindow()
        self.alive = True
        self.rows = tuple(map(dict, session['rows'] if session else (left_row, right_row)))
        self.roles = tuple(session['roles']) if session else ('A', 'B')
        self.options = dict(session['options']) if session else dict.fromkeys(
            ('ignore_trailing_space', 'ignore_line_endings', 'json_format'), False)
        self.updates = []
        self.close_count = 0
        self.instances.append(self)

    def is_alive(self):
        self.win.record('is_alive')
        return self.alive

    def update(self, left_row, right_row):
        self.win.record('update')
        self.updates.append((left_row, right_row))

    def export_session(self):
        self.win.record('export_session')
        return {'rows': tuple(map(dict, self.rows)), 'roles': self.roles,
                'options': dict(self.options)}

    def close(self):
        self.ui._save_diff_session(self)
        self.win.record('close')
        self.close_count += 1
        self.alive = False
        if self.ui._diff_window is self:
            self.ui._diff_window = None


# 只加载 UiServer AST，隔离 Win32、真实 Tk 和其他 UI 组件。
NAMESPACE = {'queue': queue, 'threading': threading, 'gc': gc,
             'DiffWindow': FakeDiffWindow}
UiServer = load_class(UI_TREE, 'UiServer', NAMESPACE)


class DiffUiTests(unittest.TestCase):
    def setUp(self):
        FakeDiffWindow.instances = []
        self.server = UiServer(MagicMock(), {}, SimpleNamespace())
        self.left = {'id': 1, 'content': '左侧'}
        self.right = {'id': 2, 'content': '右侧'}

    def test_initial_reference_is_none(self):
        self.assertIsNone(self.server._diff_window)

    def test_create_with_original_rows_and_restore_focus(self):
        self.server.show_text_diff(self.left, self.right)
        window = self.server._diff_window
        self.assertEqual(FakeDiffWindow.instances, [window])
        self.assertIs(window.ui, self.server)
        self.assertEqual(window.rows, (self.left, self.right))
        self.assertIsNot(window.rows[0], self.left)
        self.assertIsNot(window.rows[1], self.right)
        self.assertEqual(window.updates, [])
        self.assertEqual(window.win.calls, ['deiconify', 'lift', 'focus_force'])
        self.assertEqual(self.server._pins, [])
        self.assertIsNone(self.server._image_preview)

    def test_view_alive_only_activates_without_update_or_resetting_swap(self):
        self.server.show_text_diff(self.left, self.right)
        window = self.server._diff_window
        window.rows = window.rows[::-1]
        window.roles = ('B', 'A')
        for _ in range(2):
            self.server.view_text_diff()
            self.assertIs(self.server._diff_window, window)
        self.assertEqual(len(FakeDiffWindow.instances), 1)
        self.assertEqual(window.updates, [])
        self.assertEqual(window.roles, ('B', 'A'))
        self.assertEqual(window.rows, (self.right, self.left))
        self.assertEqual(window.win.calls, ['deiconify', 'lift', 'focus_force'] +
                         ['is_alive', 'deiconify', 'lift', 'focus_force'] * 2)

    def test_dead_window_rebuilt_without_updating_old_instance(self):
        self.server.show_text_diff(self.left, self.right)
        old = self.server._diff_window
        old.alive = False
        self.server.show_text_diff(self.right, self.left)
        new = self.server._diff_window
        self.assertIsNot(new, old)
        self.assertEqual(FakeDiffWindow.instances, [old, new])
        self.assertEqual(old.updates, [])
        self.assertEqual(old.close_count, 1)
        self.assertEqual(new.rows, (self.right, self.left))
        self.assertEqual(new.win.calls, ['deiconify', 'lift', 'focus_force'])

    def test_constructor_failure_does_not_keep_dead_reference(self):
        self.server.show_text_diff(self.left, self.right)
        self.server._diff_window.alive = False
        with patch.dict(NAMESPACE, {'DiffWindow': MagicMock(side_effect=RuntimeError('创建失败'))}):
            with self.assertRaisesRegex(RuntimeError, '创建失败'):
                self.server.view_text_diff()
        self.assertIsNone(self.server._diff_window)

    def test_close_reopen_restores_full_snapshots_options_and_swap(self):
        self.left['content'] = '原文尾部' * 40000
        self.server.show_text_diff(self.left, self.right)
        old = self.server._diff_window
        old.rows = old.rows[::-1]
        old.roles = ('B', 'A')
        old.options = dict.fromkeys(old.options, True)
        old.close()
        expected = self.server._diff_session
        self.left['content'] = '数据库或调用方已改变'
        self.server.view_text_diff()
        new = self.server._diff_window
        self.assertIsNot(new, old)
        self.assertEqual(new.export_session(), expected)
        self.assertEqual(len(new.rows[1]['content']), 160000)
        self.assertEqual(new.updates, [])

    def test_new_pair_replaces_settings_and_stale_close_cannot_overwrite(self):
        self.server.show_text_diff(self.left, self.right)
        old = self.server._diff_window
        old.options['json_format'] = True
        old.rows = old.rows[::-1]
        old.roles = ('B', 'A')
        self.server.show_text_diff(self.right, {'id': 3, 'content': '新记录'})
        new = self.server._diff_window
        expected = new.export_session()
        self.assertFalse(any(expected['options'].values()))
        self.assertEqual(expected['roles'], ('A', 'B'))
        self.assertEqual(self.server._diff_session, expected)
        old.close()
        self.server._save_diff_session(old)
        self.assertIs(self.server._diff_window, new)
        self.assertEqual(self.server._diff_session, expected)

    def test_cancel_closes_before_clearing_and_view_no_session_is_noop(self):
        self.server.show_text_diff(self.left, self.right)
        old = self.server._diff_window
        self.server.clear_text_diff()
        self.assertEqual(old.close_count, 1)
        self.assertIsNone(self.server._diff_session)
        self.server.view_text_diff()
        self.assertIsNone(self.server._diff_window)
        self.assertEqual(len(FakeDiffWindow.instances), 1)

    def test_new_pair_constructor_failure_keeps_previous_session(self):
        self.server.show_text_diff(self.left, self.right)
        old = self.server._diff_window
        expected = self.server._diff_session
        with patch.dict(NAMESPACE, {'DiffWindow': MagicMock(side_effect=RuntimeError('失败'))}):
            with self.assertRaises(RuntimeError):
                self.server.show_text_diff(self.right, self.left)
        self.assertIs(self.server._diff_window, old)
        self.assertIs(self.server._diff_session, expected)
        self.assertTrue(old.alive)

    def test_existing_close_clears_only_matching_identity(self):
        cls = next(node for node in DIFF_TREE.body
                   if isinstance(node, ast.ClassDef) and node.name == 'DiffWindow')
        close = next(node for node in cls.body
                     if isinstance(node, ast.FunctionDef) and node.name == 'close')
        namespace = {'suppress': suppress, 'TclError': type('TclError', (Exception,), {})}
        exec(compile(ast.Module(body=[close], type_ignores=[]), str(DIFF_SOURCE), 'exec'), namespace)
        for replaced in (False, True):
            with self.subTest(replaced=replaced):
                window = SimpleNamespace(ui=self.server, win=MagicMock(), _closed=False,
                                         _generation=1, _pending=object(), _after_id='after-1')
                replacement = object() if replaced else window
                self.server._diff_window = replacement
                namespace['close'](window)
                namespace['close'](window)
                self.assertIs(self.server._diff_window, replacement if replaced else None)
                self.assertTrue(window._closed)
                self.assertIsNone(window._pending)
                self.assertIsNone(window._after_id)
                window.win.after_cancel.assert_called_once_with('after-1')
                window.win.destroy.assert_called_once()

    def run_lifecycle(self, has_window=True, abnormal=False):
        server = self.server
        server._build_panel = MagicMock()
        server._build_floating_ball = MagicMock()
        root = MagicMock()
        events = []
        window = None

        def mainloop():
            nonlocal window
            if has_window:
                server.show_text_diff(self.left, self.right)
                window = server._diff_window
                original_close = window.close

                def close():
                    self.assertIs(server._root, root)
                    self.assertTrue(server.started)
                    events.append('diff.close')
                    original_close()

                window.close = close
            if abnormal:
                raise RuntimeError('主循环异常')
            server._put(('quit',))
            server._poll()

        root.mainloop.side_effect = mainloop
        root.quit.side_effect = lambda: events.append('root.quit')
        root.destroy.side_effect = lambda: events.append('root.destroy')
        server.db.close.side_effect = lambda: events.append('db.close')
        with patch.dict('sys.modules', {'tkinter': SimpleNamespace(Tk=lambda: root)}):
            with patch.object(gc, 'collect'):
                if abnormal:
                    with self.assertRaisesRegex(RuntimeError, '主循环异常'):
                        server._run()
                else:
                    server._run()
        self.assertIsNone(server._diff_window)
        self.assertIsNone(server._diff_session)
        self.assertIsNone(server._root)
        self.assertFalse(server.started)
        server.db.close.assert_called_once()
        root.destroy.assert_called_once()
        if has_window:
            self.assertEqual(window.close_count, 1)
            self.assertLess(events.index('diff.close'), events.index('root.destroy'))
        return events

    def test_quit_closes_diff_before_root_destroy(self):
        self.assertEqual(self.run_lifecycle(),
                         ['root.quit', 'diff.close', 'db.close', 'root.destroy'])

    def test_unexpected_mainloop_exit_also_closes_diff(self):
        self.assertEqual(self.run_lifecycle(abnormal=True),
                         ['diff.close', 'db.close', 'root.destroy'])

    def test_exit_without_diff_keeps_existing_cleanup(self):
        self.assertEqual(self.run_lifecycle(has_window=False),
                         ['root.quit', 'db.close', 'root.destroy'])

    def test_ast_ui_method_does_not_spawn_thread_or_make_topmost_window(self):
        cls = next(node for node in UI_TREE.body
                   if isinstance(node, ast.ClassDef) and node.name == 'UiServer')
        method = next(node for node in cls.body
                      if isinstance(node, ast.FunctionDef) and node.name == 'show_text_diff')
        attributes = {node.attr for node in ast.walk(method) if isinstance(node, ast.Attribute)}
        self.assertFalse({'Thread', 'Tk', 'Toplevel', 'overrideredirect', 'attributes',
                          'grab_set', 'call_ui', 'print_exc'} & attributes)
        self.assertTrue({'clear_text_diff', 'deiconify', 'lift', 'focus_force'} <= attributes)
        self.assertNotIn('update', attributes)
        imports = [node for node in UI_TREE.body if isinstance(node, ast.ImportFrom)
                   and node.module == 'diff_window']
        self.assertEqual(len(imports), 1)
        self.assertEqual([alias.name for alias in imports[0].names], ['DiffWindow'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
