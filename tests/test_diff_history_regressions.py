import ast
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

# 测试位于 tests/ 子目录，上跳一层才是项目根（lumina/ 与 main.py 所在处）。
ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / 'lumina' / 'history_panel.py'
TREE = ast.parse(SOURCE.read_text(encoding='utf-8'), filename=str(SOURCE))
METHOD_NAMES = {
    '_context_menu', '_diff_record', '_set_diff_base', '_clear_diff_base',
    '_validate_diff_base', '_compare_with_base', 'refresh', '_apply_filter',
    '_cat_of', '_row_meta', '_diff_summary', '_build_diff_banner',
    '_layout_diff_banner', '_sync_diff_banner', '_fit_text', '_sync_theme',
    '_diff_role', '_diff_color', '_draw_diff_marker', '_render_list',
    '_close_diff_session', '_view_diff',
}


def load_history_methods():
    panel = next(node for node in TREE.body
                 if isinstance(node, ast.ClassDef) and node.name == 'HistoryPanel')
    methods = [node for node in panel.body
               if isinstance(node, ast.FunctionDef) and node.name in METHOD_NAMES]
    if {node.name for node in methods} != METHOD_NAMES:
        raise AssertionError('HistoryPanel 缺少待测方法')
    # 直接执行源码方法，不导入真实 Tk、Win32 或剪贴板组件。
    cls = ast.ClassDef(name='HistoryPanel', bases=[], keywords=[], body=methods,
                       decorator_list=[])
    module = ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[]))
    namespace = {'FlatMenu': Mock()}
    exec(compile(module, str(SOURCE), 'exec'), namespace)
    return namespace['HistoryPanel'], namespace


HistoryPanel, HISTORY_GLOBALS = load_history_methods()


class FakeVar:
    def __init__(self, value=''):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class FakeWidget:
    def __init__(self, parent=None, **options):
        self.parent = parent
        self.options = options
        self.visible = False
        self.pack_options = None
        self.grid_options = None
        self.bindings = {}
        self.width = 400
        self.reqwidth = 60

    def configure(self, **options):
        self.options.update(options)

    def grid_columnconfigure(self, column, **options):
        self.column_config = (column, options)

    def grid(self, **options):
        self.grid_options = options
        self.visible = True

    def grid_remove(self):
        self.visible = False

    def pack(self, **options):
        self.visible = True
        self.pack_options = options

    def pack_forget(self):
        self.visible = False

    def bind(self, event, callback):
        self.bindings[event] = callback

    def winfo_width(self):
        return self.width

    def winfo_reqwidth(self):
        return self.reqwidth


class FakeCanvas(FakeWidget):
    def __init__(self):
        super().__init__()
        self.create_rectangle = Mock()
        self.create_text = Mock()
        self.create_image = Mock()
        self.create_window = Mock()
        self.delete = Mock()

    def winfo_children(self):
        return []


class DiffHistoryRegressionTests(unittest.TestCase):
    def setUp(self):
        self.records = {
            '1': self.make_row(1, 'text', '数据库左侧原文'),
            '2': self.make_row(2, 'code', '数据库右侧原文'),
            '3': self.make_row(3, 'link', '链接记录原文'),
            '4': self.make_row(4, 'image', None),
            '5': self.make_row(5, 'file', '文件路径'),
        }
        self.panel = HistoryPanel()
        self.panel.db = Mock()
        self.panel.db.get_text.side_effect = lambda cid: self.records.get(str(cid))
        # 替身需接受下推筛选参数：refresh() 现在把 category/pinned 传给查询，
        # 由 SQL 层完成筛选（避免匹配记录被 LIMIT 窗口挤出）。此处仍返回全集，
        # 交由 _apply_filter 兜底筛选，以保持既有断言语义不变。
        self.panel.db.list_recent.side_effect = (
            lambda limit, category=None, pinned=False: list(self.records.values()))
        self.panel.db.search.side_effect = (
            lambda query, limit, category=None, pinned=False: [self.records['2']])
        self.panel.db.count.return_value = len(self.records)
        self.panel.ui = SimpleNamespace(show_text_diff=Mock())
        self.panel._diff_base_id = None
        self.panel._diff_base_summary = ''
        self.panel._diff_peer_id = None
        self.panel._diff_peer_summary = ''
        self.panel._dpi = 1
        self.panel._sel_id = '5'
        self.panel._rows = dict(self.records)
        self.panel._rows_displayed = list(self.records.values())
        self.panel._refresh_cards = Mock(side_effect=self.refresh_cards)
        self.panel.status_var = FakeVar()
        self.panel.search_var = FakeVar()
        self.panel._filter = 'all'
        self.panel._detail = None
        self.panel.tk = SimpleNamespace(Frame=FakeWidget, Label=FakeWidget,
                                        Button=FakeWidget)
        self.panel.win = Mock()
        self.panel._theme = Mock(return_value=({
            'label': '#334155', 'label2': '#64748b', 'accent_soft': '#e0f2fe',
            'accent': '#0284c7', 'fill_hover': '#bae6fd',
            'window_bg': '#ffffff'}, False))
        self.panel._font = Mock(side_effect=lambda size, weight='normal':
                                ('测试字体', size, weight))
        self.panel._measure = Mock(side_effect=lambda text, font: len(text) * 10)
        self.banner_parent = FakeWidget()
        self.chips = FakeWidget(self.banner_parent)
        self.chips.pack(fill='x')
        self.panel._build_diff_banner(self.banner_parent, self.chips)
        for name in ('copy_only', 'toggle_pin', '_export_selected', 'delete_selected',
                     '_open_file_from_row', '_reveal_file_from_row'):
            setattr(self.panel, name, Mock())
        self.panel._source_app = Mock(return_value=('测试来源', 'blue', 'plain'))
        self.panel._fmt_row_time = Mock(return_value='刚刚')
        self.panel._fmt_size = Mock(return_value='12 B')
        self.menu_factory = Mock()
        menu_patch = patch.dict(HISTORY_GLOBALS, {'FlatMenu': self.menu_factory})
        menu_patch.start()
        self.addCleanup(menu_patch.stop)

    @staticmethod
    def make_row(cid, category, content, pinned=0, tags=''):
        return {'id': cid, 'category': category, 'content': content, 'pinned': pinned,
                    'kind': 'image' if category == 'image' else 'text',
                    'source': 'test.exe', 'created_at': '2000-01-01', 'tags': tags,
                    'data_size': 0, 'preview': '截断列表预览'}

    def refresh_cards(self, rows):
        self.panel._rows_displayed = rows

    def menu(self, cid):
        self.panel._context_menu(cid, 10, 20)
        args = self.menu_factory.call_args.args
        self.assertIs(args[0], self.panel.tk)
        self.assertIs(args[1], self.panel.win)
        self.panel._flat_menu.popup.assert_called_with(10, 20)
        return dict(item for item in args[2] if item is not None)

    def assert_not_opened(self):
        self.panel.ui.show_text_diff.assert_not_called()

    def test_menu_callbacks_capture_explicit_clip_id_not_selection(self):
        for base, label, method in (
                (None, '选为比对 A', '_set_diff_base'),
                ('1', '与 A 比对', '_compare_with_base')):
            with self.subTest(base=base):
                self.panel._diff_base_id = base
                first = self.menu(2)
                second = self.menu('3')
                with patch.object(self.panel, method) as callback:
                    # 菜单创建后选择改变乃至消失，闭包仍绑定各自的参数。
                    self.panel._sel_id = '4'
                    first[label]()
                    del self.panel._sel_id
                    second[label]()
                self.assertEqual(callback.call_args_list, [call(2), call('3')])

    def test_menu_category_limits_and_self_comparison_hidden(self):
        for base in (None, '1'):
            self.panel._diff_base_id = base
            for cid, row in self.records.items():
                with self.subTest(base=base, category=row['category']):
                    commands = self.menu(int(cid))
                    allowed = row['category'] in ('text', 'code', 'link')
                    self.assertEqual('选为比对 A' in commands,
                                     allowed and base is None)
                    self.assertEqual('与 A 比对' in commands,
                                     allowed and base is not None and cid != base)
                    self.assertNotIn('设为比对基准', commands)
                    self.assertNotIn('与基准比对', commands)
                    self.assertNotIn('取消比对基准', commands)

    def test_banner_default_hidden_and_sync_without_widgets_is_safe(self):
        banner = self.panel._diff_banner
        self.assertIs(banner.parent, self.banner_parent)
        self.assertFalse(banner.visible)
        self.assertIsNone(banner.pack_options)
        self.assertEqual(self.panel._diff_banner_text.options['text'], '')
        self.assertEqual(banner.column_config, (0, {'weight': 1}))
        self.assertEqual(self.panel._diff_cancel.grid_options['column'], 2)
        self.assertEqual(self.panel._diff_cancel.grid_options['rowspan'], 2)
        self.assertEqual(banner.bindings['<Configure>'], self.panel._layout_diff_banner)
        self.panel._sync_diff_banner()
        self.assertFalse(banner.visible)
        del self.panel._diff_banner
        self.panel._sync_diff_banner()
        self.panel._diff_base_id = '1'
        self.panel._sync_diff_banner()
        self.assert_not_opened()

    def test_select_a_shows_summary_before_chips_without_opening_diff(self):
        self.menu(2)['选为比对 A']()
        banner = self.panel._diff_banner
        self.assertTrue(banner.visible)
        self.assertEqual(banner.pack_options,
                         {'fill': 'x', 'pady': (0, 6), 'before': self.chips})
        self.assertTrue(self.chips.visible)
        self.assertEqual(self.panel._diff_base_summary, '代码 · 数据库右侧原文')
        self.assertEqual(self.panel._diff_banner_text.options['text'],
                         '比对 A：代码 · 数据库右侧原文')
        self.assertNotIn('选为比对 A', self.menu(1))
        self.assertIn('与 A 比对', self.menu(1))
        self.assertNotIn('与 A 比对', self.menu(2))
        self.assert_not_opened()

    def test_summary_category_whitespace_empty_and_length_boundaries(self):
        cases = [
            ('text', '  第一行\r\n\t第二行  ', '文本 · 第一行 第二行'),
            ('code', 'x = 1', '代码 · x = 1'),
            ('link', '链接原文', '链接 · 链接原文'),
            ('text', '', '文本 · （空文本）'),
            ('code', ' \t\r\n\u3000', '代码 · （空文本）'),
            ('text', '字' * 42, '文本 · ' + '字' * 42),
            ('text', '字' * 43, '文本 · ' + '字' * 42 + '…'),
            ('text', '字' * 240, '文本 · ' + '字' * 42 + '…'),
            ('text', '短' + ' ' * 239, '文本 · 短'),
            ('text', '短' + ' ' * 240, '文本 · 短…'),
            ('text', ' ' * 240 + '不可读取的尾部', '文本 · （空文本）…'),
        ]
        for category, content, expected in cases:
            with self.subTest(category=category, length=len(content)):
                row = self.make_row(1, category, content)
                snapshot = deepcopy(row)
                self.assertEqual(self.panel._diff_summary(row), expected)
                self.assertEqual(row, snapshot)
        for content in (None, b'bytes', 123):
            with self.subTest(content=content):
                with self.assertRaises(TypeError):
                    self.panel._diff_summary(self.make_row(1, 'text', content))
        with self.assertRaises(KeyError):
            self.panel._diff_summary(self.make_row(1, 'image', '正文'))

    def test_banner_small_window_ellipsizes_and_resizes_without_losing_summary(self):
        self.panel._set_diff_base(2)
        full = self.panel._diff_banner_text.options['text']
        summary = self.panel._diff_base_summary
        banner = self.panel._diff_banner
        banner.width = 189
        banner.bindings['<Configure>'](SimpleNamespace(width=189))
        fitted = self.panel._diff_banner_text.options['text']
        self.assertEqual(fitted, full[:9] + '…')
        self.assertLessEqual(self.panel._measure(fitted, self.panel._font(9, 'bold')), 100)
        self.assertEqual(self.panel._diff_base_summary, summary)
        self.assertEqual(self.panel._diff_cancel.options['text'], '取消比对 ×')
        banner.width = 1000
        self.panel._layout_diff_banner()
        self.assertEqual(self.panel._diff_banner_text.options['text'], full)

    def test_banner_layout_ignores_nonpositive_width_or_unset_base(self):
        self.panel._set_diff_base(1)
        text = self.panel._diff_banner_text.options['text']
        for width in (1, 89):
            with self.subTest(width=width):
                self.panel._diff_banner.width = width
                self.panel._measure.reset_mock()
                self.panel._layout_diff_banner()
                self.panel._measure.assert_not_called()
                self.assertEqual(self.panel._diff_banner_text.options['text'], text)
        self.panel._clear_diff_base()
        self.panel._measure.reset_mock()
        self.panel._layout_diff_banner()
        self.panel._measure.assert_not_called()
        self.assertFalse(self.panel._diff_banner.visible)

    def test_validation_updates_banner_from_database_not_cached_rows(self):
        self.panel._set_diff_base(1)
        self.records['1'] = self.make_row(1, 'code', '  更新\n正文  ')
        self.assertFalse(self.panel._validate_diff_base())
        self.assertEqual(self.panel._diff_base_summary, '代码 · 更新 正文')
        self.assertEqual(self.panel._diff_banner_text.options['text'],
                         '比对 A：代码 · 更新 正文')
        self.assertTrue(self.panel._diff_banner.visible)
        self.assertIs(self.panel._diff_banner.pack_options['before'], self.chips)

    def test_invalid_base_hides_banner_and_clears_summary(self):
        for category in (None, 'image', 'file'):
            with self.subTest(category=category):
                self.records['1'] = self.make_row(1, 'text', '有效 A')
                self.panel._set_diff_base(1)
                self.assertTrue(self.panel._diff_banner.visible)
                if category is None:
                    self.records.pop('1')
                else:
                    self.records['1'] = self.make_row(1, category, '不支持')
                self.assertTrue(self.panel._validate_diff_base())
                self.assertIsNone(self.panel._diff_base_id)
                self.assertEqual(self.panel._diff_base_summary, '')
                self.assertFalse(self.panel._diff_banner.visible)
                self.assertTrue(self.chips.visible)
        self.assert_not_opened()

    def test_theme_rebuild_restores_a_and_revalidates_banner(self):
        self.panel._root = object()
        self.panel._dark = False
        self.panel._is_dark_mode = Mock(return_value=True)
        self.panel._maxed = True
        self.panel._close_popup_menu = Mock()
        caches = ('_thumb_cache', '_tile_cache', '_cardbg_cache',
                  '_marker_cache', '_preview_cache')
        for name in caches:
            setattr(self.panel, name, {'旧主题': object()})

        def rebuild(root, tk, ui):
            panel = self.panel
            panel._diff_base_id = None
            panel._diff_base_summary = ''
            panel._diff_peer_id = None
            panel._diff_peer_summary = ''
            panel._sel_id = None
            panel._maxed = False
            panel._build_diff_banner(self.banner_parent, self.chips)

        for category in ('code', None, 'image'):
            with self.subTest(category=category):
                self.records['1'] = self.make_row(1, 'text', '旧摘要')
                self.panel._set_diff_base(1)
                old_banner = self.panel._diff_banner
                if category is None:
                    self.records.pop('1')
                else:
                    self.records['1'] = self.make_row(1, category, '新摘要')
                with patch.object(HistoryPanel, '__init__', side_effect=rebuild) as init:
                    self.assertTrue(self.panel._sync_theme())
                init.assert_called_once_with(self.panel._root, self.panel.tk, self.panel.ui)
                self.assertIsNot(self.panel._diff_banner, old_banner)
                self.assertEqual(self.panel._sel_id, '5')
                self.assertTrue(self.panel._maxed)
                for name in caches:
                    self.assertEqual(getattr(self.panel, name), {})
                valid = category == 'code'
                self.assertEqual(self.panel._diff_base_id, '1' if valid else None)
                self.assertEqual(self.panel._diff_banner.visible, valid)
                self.assertEqual(self.panel._diff_base_summary,
                                 '代码 · 新摘要' if valid else '')
                if valid:
                    self.assertEqual(self.panel._diff_banner_text.options['text'],
                                     '比对 A：代码 · 新摘要')
                    self.assertIs(self.panel._diff_banner.pack_options['before'], self.chips)
        self.panel.win.destroy.assert_has_calls([call(), call(), call()])
        self.assert_not_opened()

    def test_unchanged_theme_does_not_rebuild_or_change_banner(self):
        self.select_pair()
        banner = self.panel._diff_banner
        self.panel._dark = False
        self.panel._is_dark_mode = Mock(return_value=False)
        with patch.object(HistoryPanel, '__init__') as init:
            self.assertFalse(self.panel._sync_theme())
        init.assert_not_called()
        self.panel.win.destroy.assert_not_called()
        self.assertIs(self.panel._diff_banner, banner)
        self.assertTrue(banner.visible)
        self.assert_pair()
        self.assert_not_opened()

    def test_compact_layout_builds_banner_before_category_chips(self):
        panel = next(node for node in TREE.body
                     if isinstance(node, ast.ClassDef) and node.name == 'HistoryPanel')
        compact = next(node for node in panel.body
                       if isinstance(node, ast.FunctionDef) and node.name == '_build_compact')
        calls = [node for node in ast.walk(compact)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                 and node.func.attr == '_build_diff_banner']
        self.assertEqual(len(calls), 1)
        self.assertEqual([ast.unparse(arg) for arg in calls[0].args],
                         ['left', 'chip_wrap'])

    def test_diff_record_accepts_all_supported_categories_and_copies_row(self):
        for cid in ('1', '2', '3'):
            with self.subTest(category=self.records[cid]['category']):
                row = self.panel._diff_record(int(cid))
                self.assertEqual(row, self.records[cid])
                self.assertIsNot(row, self.records[cid])
        self.assertEqual(self.panel.db.get_text.call_args_list,
                         [call(1), call(2), call(3)])

    def test_diff_record_rejects_missing_unsupported_and_nonstring_content(self):
        invalid = [None] + [self.make_row(9, cat, '正文')
                           for cat in ('image', 'file', '', 'other')]
        invalid += [self.make_row(9, cat, value)
                    for cat in ('text', 'code', 'link')
                    for value in (None, '正文'.encode(), 123)]
        for row in invalid:
            with self.subTest(row=row):
                with patch.object(self.panel.db, 'get_text', return_value=row):
                    with self.assertRaises(ValueError):
                        self.panel._diff_record(9)
        self.assert_not_opened()

    def test_set_base_uses_explicit_id_and_refreshes_marker_without_opening(self):
        del self.panel._sel_id
        displayed = self.panel._rows_displayed
        self.panel._set_diff_base(2)
        self.assertEqual(self.panel._diff_base_id, '2')
        self.panel.db.get_text.assert_called_once_with(2)
        self.panel._refresh_cards.assert_called_once_with(displayed)
        self.assertEqual(self.panel.status_var.get(),
                         '已选定 A；右键另一条记录选择“与 A 比对”')
        self.assert_not_opened()

    def test_failed_set_keeps_previous_base_and_does_not_refresh_or_open(self):
        self.panel._set_diff_base(1)
        self.panel._refresh_cards.reset_mock()
        summary = self.panel._diff_base_summary
        text = self.panel._diff_banner_text.options['text']
        for cid in (4, 5, 999):
            with self.subTest(cid=cid):
                self.panel._diff_base_id = '1'
                self.panel._set_diff_base(cid)
                self.assertEqual(self.panel._diff_base_id, '1')
                self.assertIn('无法设置基准', self.panel.status_var.get())
        with patch.object(self.panel.db, 'get_text', side_effect=RuntimeError('读取失败')):
            self.panel._set_diff_base(2)
        self.assertEqual(self.panel._diff_base_id, '1')
        self.assertIn('读取失败', self.panel.status_var.get())
        self.assertEqual(self.panel._diff_base_summary, summary)
        self.assertEqual(self.panel._diff_banner_text.options['text'], text)
        self.assertTrue(self.panel._diff_banner.visible)
        self.panel._refresh_cards.assert_not_called()
        self.assert_not_opened()

    def test_compare_uses_full_database_originals_not_preview_or_json_result(self):
        raw = ' { "a" : [1, 2], "尾部" : "完整结尾" } '
        self.records['1']['content'] = raw + '\r\n' + '左' * 130000
        self.records['2']['content'] = '普通非 JSON 正文\r\n' + '右' * 130000 + '\n尾部'
        snapshot = deepcopy(self.records)
        self.panel._rows = {cid: dict(row, content='列表截断内容')
                            for cid, row in self.records.items()}
        self.panel._rows_displayed = list(self.panel._rows.values())
        self.panel._detail = SimpleNamespace(
            row=dict(self.records['2'], content='详情截断内容'),
            _json_text='JSON 临时转换结果',
            _effective_row=Mock(side_effect=AssertionError('不得读取 JSON 临时结果')))
        self.panel._diff_base_id = '1'
        del self.panel._sel_id
        self.menu(2)['与 A 比对']()
        self.panel.ui.show_text_diff.assert_called_once_with(snapshot['1'], snapshot['2'])
        left, right = self.panel.ui.show_text_diff.call_args.args
        self.assertIsNot(left, self.records['1'])
        self.assertIsNot(right, self.records['2'])
        self.assertEqual(self.records, snapshot)
        self.assertEqual(self.panel.db.get_text.call_args_list,
                         [call('1'), call('1'), call(2)])
        self.panel.db.get.assert_not_called()
        self.panel._detail._effective_row.assert_not_called()
        self.assertEqual(self.panel.status_var.get(), '正在比对：基准 1 ↔ 记录 2')
        self.assertEqual(self.panel._diff_peer_id, '2')
        self.assertEqual(self.panel._diff_peer_summary,
                         self.panel._diff_summary(snapshot['2']))
        self.panel._refresh_cards.assert_called_once_with(self.panel._rows_displayed)

    def test_refresh_preserves_base_across_search_category_and_pinned_filters(self):
        self.panel._diff_base_id = '1'
        for query, category in (('只匹配右侧', 'all'), ('', 'code'), ('', 'pinned')):
            with self.subTest(query=query, category=category):
                self.panel.search_var.set(query)
                self.panel._filter = category
                self.panel.refresh()
                self.assertNotIn('1', self.panel._rows)
                self.assertEqual(self.panel._diff_base_id, '1')
                self.assertIn('比对基准 ID 1', self.panel.status_var.get())
                self.assertEqual(self.panel._refresh_cards.call_args.args[0],
                                 list(self.panel._rows.values()))
        self.panel.db.search.assert_called_once_with(
            '只匹配右侧', 60, category=None, pinned=False)
        self.panel.db.count.assert_has_calls([
            call('只匹配右侧'), call('', category='code'), call('', pinned=True)])
        self.panel._filter = 'all'
        self.panel.refresh()
        self.assertIn('1', self.panel._rows)
        self.panel._compare_with_base(2)
        self.panel.ui.show_text_diff.assert_called_once_with(
            self.records['1'], self.records['2'])

    def test_validate_base_no_base_or_supported_category_does_not_clear(self):
        del self.panel._diff_base_id
        self.assertFalse(self.panel._validate_diff_base())
        self.panel.db.get_text.assert_not_called()
        for cid in ('1', '2', '3'):
            self.panel._diff_base_id = cid
            self.assertFalse(self.panel._validate_diff_base())
            self.assertEqual(self.panel._diff_base_id, cid)
        self.panel._refresh_cards.assert_not_called()

    def test_refresh_clears_deleted_or_reclassified_base_and_reports_it(self):
        for category in (None, 'image', 'file'):
            with self.subTest(category=category):
                self.panel._diff_base_id = '1'
                if category is None:
                    self.records.pop('1', None)
                else:
                    self.records['1'] = self.make_row(1, category, '原文')
                self.panel.refresh()
                self.assertIsNone(self.panel._diff_base_id)
                self.assertIn('比对基准已删除，已取消', self.panel.status_var.get())
                self.assertNotIn('比对基准 ID', self.panel.status_var.get())
                self.assertNotIn('取消比对基准', self.menu(2))
        self.assert_not_opened()

    def test_compare_clears_invalid_base_refreshes_and_does_not_open(self):
        for category in (None, 'image', 'file'):
            with self.subTest(category=category):
                self.panel._diff_base_id = '1'
                if category is None:
                    self.records.pop('1', None)
                else:
                    self.records['1'] = self.make_row(1, category, '原文')
                self.panel._refresh_cards.reset_mock()
                self.panel._compare_with_base(2)
                self.assertIsNone(self.panel._diff_base_id)
                self.panel._refresh_cards.assert_called_once_with(self.panel._rows_displayed)
                self.assertIn('比对基准已删除或不可用', self.panel.status_var.get())
                self.assert_not_opened()

    def test_self_comparison_rejected_for_int_and_string_without_database_read(self):
        self.panel._diff_base_id = '1'
        for cid in (1, '1'):
            self.panel._compare_with_base(cid)
            self.assertIn('不能与自身比对', self.panel.status_var.get())
        self.panel.db.get_text.assert_not_called()
        self.panel._refresh_cards.assert_not_called()
        self.assert_not_opened()

    def test_no_base_requests_setting_one_without_reading_or_opening(self):
        self.panel._compare_with_base(2)
        self.assertIn('请先', self.panel.status_var.get())
        self.panel.db.get_text.assert_not_called()
        self.assert_not_opened()

    def test_cancel_only_from_banner_clears_base_and_marker(self):
        self.panel._set_diff_base(1)
        self.panel._compare_with_base(2)
        self.panel.ui.show_text_diff.reset_mock()
        self.panel.db.get_text.reset_mock()
        self.panel._refresh_cards.reset_mock()
        self.assertIn('比对基准', self.panel._row_meta(self.records['1'], 'today', None))
        displayed = self.panel._rows_displayed
        for cid in self.records:
            self.assertNotIn('取消比对基准', self.menu(cid))
        self.panel._diff_cancel.options['command']()
        self.assertIsNone(self.panel._diff_base_id)
        self.assertEqual(self.panel._diff_base_summary, '')
        self.assertFalse(self.panel._diff_banner.visible)
        self.panel._refresh_cards.assert_called_once_with(displayed)
        self.assertEqual(self.panel.status_var.get(), '已取消比对基准')
        self.assertIsNone(self.panel._diff_peer_id)
        self.assertEqual(self.panel._diff_peer_summary, '')
        for cid in (1, 2):
            self.assertIsNone(self.panel._diff_role(cid))
        self.assertNotIn('比对基准', self.panel._row_meta(self.records['1'], 'today', None))
        commands = self.menu(2)
        self.assertNotIn('与 A 比对', commands)
        self.assertIn('选为比对 A', commands)
        self.panel.db.get_text.assert_not_called()
        self.assert_not_opened()

    def test_row_meta_marks_only_matching_id_and_preserves_existing_fields(self):
        self.panel._diff_base_id = '1'
        self.records['1']['tags'] = '回归,其他'
        self.assertEqual(self.panel._row_meta(self.records['1'], 'today', None),
                         '#回归 · 比对基准 · 刚刚 · 测试来源 · 12 B')
        self.assertEqual(self.panel._row_meta(self.records['2'], 'today', None),
                         '刚刚 · 测试来源 · 12 B')
        self.assertEqual(self.panel._row_meta(self.records['4'], 'today', None),
                         '刚刚 · 测试来源')

    def test_invalid_target_never_opens_and_keeps_valid_base(self):
        invalid = [None, self.records['4'], self.records['5'],
                   dict(self.records['2'], content=None),
                   dict(self.records['2'], content='非文本'.encode())]
        for row in invalid:
            with self.subTest(row=row):
                self.panel._diff_base_id = '1'
                with patch.object(self.panel.db, 'get_text', side_effect=
                                  lambda cid: self.records['1'] if str(cid) == '1' else row):
                    self.panel._compare_with_base(2)
                self.assertEqual(self.panel._diff_base_id, '1')
                self.assertIn('无法打开比对', self.panel.status_var.get())
                self.assert_not_opened()
        self.panel._refresh_cards.assert_not_called()

    def test_database_exceptions_at_each_read_never_open(self):
        for failure_index in range(3):
            with self.subTest(failure_index=failure_index):
                results = [self.records['1'], self.records['1'], self.records['2']]
                results[failure_index] = RuntimeError('数据库读取失败')
                self.panel._diff_base_id = '1'
                with patch.object(self.panel.db, 'get_text', side_effect=results):
                    self.panel._compare_with_base(2)
                self.assertEqual(self.panel._diff_base_id, '1')
                self.assertEqual(self.panel.status_var.get(), '无法打开比对：数据库读取失败')
                self.assert_not_opened()
        self.panel._refresh_cards.assert_not_called()

    def test_base_disappears_between_validation_and_full_read_never_opens(self):
        self.panel._diff_base_id = '1'
        with patch.object(self.panel.db, 'get_text', side_effect=[self.records['1'], None]):
            self.panel._compare_with_base(2)
        self.assertIn('记录已删除或不存在', self.panel.status_var.get())
        self.assert_not_opened()

    def select_pair(self):
        self.panel._set_diff_base(1)
        self.panel._compare_with_base(2)
        self.panel.ui.show_text_diff.reset_mock()
        self.panel._refresh_cards.reset_mock()
        self.panel.db.get_text.reset_mock()

    def assert_pair(self, base='1', peer='2'):
        self.assertEqual(self.panel._diff_base_id, base)
        self.assertEqual(self.panel._diff_peer_id, peer)
        self.assertEqual(self.panel._diff_base_summary,
                         self.panel._diff_summary(self.records[base]) if base else '')
        self.assertEqual(self.panel._diff_peer_summary,
                         self.panel._diff_summary(self.records[peer]) if peer else '')
        self.assertEqual(self.panel._diff_banner.visible, base is not None)

    def test_successful_compare_sets_b_only_after_open_and_replaces_previous_b(self):
        self.select_pair()
        displayed = self.panel._rows_displayed
        selection = self.panel._sel_id

        def opened(left, right):
            self.assert_pair()
            self.assertEqual(left, self.records['1'])
            self.assertEqual(right, self.records['3'])
            self.panel._refresh_cards.assert_not_called()

        self.panel.ui.show_text_diff.side_effect = opened
        self.panel._compare_with_base(3)
        self.panel.ui.show_text_diff.assert_called_once()
        self.assert_pair(peer='3')
        self.assertIsNone(self.panel._diff_role(2))
        self.assertEqual(self.panel._diff_role('3'), 'B')
        self.assertEqual(self.panel._sel_id, selection)
        self.panel._refresh_cards.assert_called_once_with(displayed)
        self.assertEqual(self.panel._diff_banner_peer.options['text'],
                         '比对 B：链接 · 链接记录原文')

    def test_selecting_a_clears_old_b_even_when_reselecting_same_a(self):
        for cid in (1, 2, 3):
            with self.subTest(cid=cid):
                self.select_pair()
                self.panel._set_diff_base(cid)
                self.assert_pair(base=str(cid), peer=None)
                self.assertEqual(self.panel._diff_role(cid), 'A')
                self.assertNotIn('比对 B：', self.panel._diff_banner_peer.options['text'])
                self.assertEqual(self.panel._diff_banner_peer.options['fg'],
                                 self.panel._theme()[0]['label2'])
                self.panel._refresh_cards.assert_called_once_with(self.panel._rows_displayed)
                self.assert_not_opened()

    def test_failed_set_keeps_b_and_both_summaries_without_redraw(self):
        self.select_pair()
        for cid in (4, 5, 999):
            with self.subTest(cid=cid):
                self.panel._set_diff_base(cid)
                self.assert_pair()
        with patch.object(self.panel.db, 'get_text', side_effect=RuntimeError('读取失败')):
            self.panel._set_diff_base(3)
        self.assert_pair()
        self.panel._refresh_cards.assert_not_called()
        self.assert_not_opened()

    def test_failed_compare_keeps_previous_b_on_bad_target_or_open_exception(self):
        self.select_pair()
        for cid in (4, 5, 999, 1):
            with self.subTest(cid=cid):
                self.panel._compare_with_base(cid)
                self.assert_pair()
                self.panel._refresh_cards.assert_not_called()
                self.assert_not_opened()
        self.panel.ui.show_text_diff.side_effect = RuntimeError('窗口打开失败')
        self.panel._compare_with_base(3)
        self.panel.ui.show_text_diff.assert_called_once_with(
            self.records['1'], self.records['3'])
        self.assert_pair()
        self.assertEqual(self.panel.status_var.get(), '无法打开比对：窗口打开失败')
        self.panel._refresh_cards.assert_not_called()
        self.assertEqual(self.panel._diff_banner_peer.options['text'],
                         '比对 B：代码 · 数据库右侧原文')

    def test_database_failures_with_existing_b_preserve_pair_at_each_read(self):
        self.select_pair()
        for index in range(4):
            with self.subTest(index=index):
                results = [self.records['1'], self.records['2'],
                           self.records['1'], self.records['3']]
                results[index] = RuntimeError('读取失败')
                with patch.object(self.panel.db, 'get_text', side_effect=results):
                    self.panel._compare_with_base(3)
                self.assert_pair()
                self.assertEqual(self.panel.status_var.get(), '无法打开比对：读取失败')
                self.panel._refresh_cards.assert_not_called()
                self.assert_not_opened()

    def test_validation_and_refresh_clear_only_invalid_b_but_all_for_invalid_a(self):
        for invalid_id in ('1', '2'):
            for category in (None, 'image', 'file'):
                for action in ('_validate_diff_base', 'refresh'):
                    with self.subTest(invalid_id=invalid_id, category=category, action=action):
                        self.records['1'] = self.make_row(1, 'text', '有效 A')
                        self.records['2'] = self.make_row(2, 'code', '有效 B')
                        self.select_pair()
                        if category is None:
                            self.records.pop(invalid_id)
                        else:
                            self.records[invalid_id] = self.make_row(
                                int(invalid_id), category, '不支持')
                        removed = getattr(self.panel, action)()
                        self.assert_pair(base='1' if invalid_id == '2' else None, peer=None)
                        self.assertIsNone(self.panel._diff_role(2))
                        if action == '_validate_diff_base':
                            self.assertEqual(removed, invalid_id == '1')
                            self.panel._refresh_cards.assert_not_called()
                        else:
                            self.panel._refresh_cards.assert_called_once_with(
                                self.panel._rows_displayed)
                        if invalid_id == '2':
                            self.assertEqual(self.panel._diff_role(1), 'A')
                            self.assertEqual(self.panel._diff_banner_peer.options['text'],
                                             '右键另一条记录，选择“与 A 比对”')
                        self.assert_not_opened()

    def test_search_and_filters_preserve_both_ids_and_database_summaries(self):
        self.select_pair()
        self.records['1'] = self.make_row(1, 'text', '新 A 摘要')
        self.records['2'] = self.make_row(2, 'code', '新 B 摘要')
        for query, category in (('隐藏二者', 'all'), ('', 'link'), ('', 'pinned')):
            with self.subTest(query=query, category=category):
                with patch.object(self.panel.db, 'search', return_value=[self.records['3']]):
                    self.panel.search_var.set(query)
                    self.panel._filter = category
                    self.panel.refresh()
                self.assertNotIn('1', self.panel._rows)
                self.assertNotIn('2', self.panel._rows)
                self.assert_pair()
                self.assertEqual(self.panel._diff_role(1), 'A')
                self.assertEqual(self.panel._diff_role(2), 'B')
        self.panel.search_var.set('')
        self.panel._filter = 'all'
        self.panel.refresh()
        self.assertIn('1', self.panel._rows)
        self.assertIn('2', self.panel._rows)
        self.assert_pair()
        self.assert_not_opened()

    def test_validation_updates_both_summaries_from_database_without_redraw(self):
        self.select_pair()
        self.records['1'] = self.make_row(1, 'link', '  新 A\n正文 ')
        self.records['2'] = self.make_row(2, 'text', ' 新 B\t正文 ')
        self.assertFalse(self.panel._validate_diff_base())
        self.assert_pair()
        self.assertEqual(self.panel.db.get_text.call_args_list, [call('1'), call('2')])
        self.assertEqual(self.panel._diff_banner_text.options['text'], '比对 A：链接 · 新 A 正文')
        self.assertEqual(self.panel._diff_banner_peer.options['text'], '比对 B：文本 · 新 B 正文')
        self.panel._refresh_cards.assert_not_called()
        self.assert_not_opened()

    def test_theme_rebuild_preserves_pair_and_revalidates_deleted_peer(self):
        self.panel._root = object()
        self.panel._dark = False
        self.panel._is_dark_mode = Mock(return_value=True)
        self.panel._maxed = True
        self.panel._close_popup_menu = Mock()
        for name in ('_thumb_cache', '_tile_cache', '_cardbg_cache',
                     '_marker_cache', '_preview_cache'):
            setattr(self.panel, name, {})
        theme = self.panel._theme()[0]

        def rebuild(root, tk, ui):
            self.panel._diff_base_id = self.panel._diff_peer_id = None
            self.panel._diff_base_summary = self.panel._diff_peer_summary = ''
            self.panel._sel_id = None
            self.panel._maxed = False
            self.panel._theme.return_value = (theme, True)
            self.panel._build_diff_banner(self.banner_parent, self.chips)

        for deleted in (None, '2', '1'):
            with self.subTest(deleted=deleted):
                self.records['1'] = self.make_row(1, 'text', 'A')
                self.records['2'] = self.make_row(2, 'code', 'B')
                self.select_pair()
                old_banner = self.panel._diff_banner
                self.records['1']['content'] = '重建 A'
                self.records['2']['content'] = '重建 B'
                if deleted:
                    self.records.pop(deleted)
                with patch.object(HistoryPanel, '__init__', side_effect=rebuild):
                    self.assertTrue(self.panel._sync_theme())
                self.assertIsNot(self.panel._diff_banner, old_banner)
                self.assert_pair(base=None if deleted == '1' else '1',
                                 peer='2' if deleted is None else None)
                self.assertEqual(self.panel._sel_id, '5')
                self.assertTrue(self.panel._maxed)
                if deleted is None:
                    self.assertEqual(self.panel._diff_banner_text.options['fg'], '#69AAFF')
                    self.assertEqual(self.panel._diff_banner_peer.options['fg'], '#C39AFF')
                self.assert_not_opened()

    def test_marker_roles_normalize_ids_ignore_selection_and_prefer_a(self):
        self.select_pair()
        for selection in ('1', '2', '3', None):
            self.panel._sel_id = selection
            for cid, expected in ((1, 'A'), ('1', 'A'), (2, 'B'), ('2', 'B'), (3, None)):
                self.assertEqual(self.panel._diff_role(cid), expected)
        del self.panel._sel_id
        self.assertEqual(self.panel._diff_role(2), 'B')
        self.panel._diff_peer_id = '1'
        self.assertEqual(self.panel._diff_role(1), 'A')
        del self.panel._diff_base_id
        del self.panel._diff_peer_id
        self.assertIsNone(self.panel._diff_role(1))

    def test_marker_canvas_coordinates_width_tags_and_theme_colors(self):
        self.select_pair()
        theme = self.panel._theme()[0]
        for dark, colors in ((False, ('#007AFF', '#8050CC')),
                             (True, ('#69AAFF', '#C39AFF'))):
            self.panel._theme.return_value = (theme, dark)
            for dpi in (0.5, 1, 1.25, 2):
                self.panel._dpi = dpi
                for cid, role, color in ((1, 'A', colors[0]), ('2', 'B', colors[1])):
                    with self.subTest(dark=dark, dpi=dpi, role=role):
                        canvas = FakeCanvas()
                        width = self.panel._draw_diff_marker(canvas, cid, 12, 32, 67, 82)
                        size = max(14, round(18 * dpi))
                        cy = 32 + 67 // 2 - round(10 * dpi)
                        tags = ('card', str(cid), 'diff_marker')
                        self.assertEqual(width, size + round(6 * dpi))
                        self.assertEqual(canvas.create_rectangle.call_args_list, [
                            call(12 + round(3 * dpi), 32 + round(10 * dpi),
                                 12 + round(6 * dpi), 99 - round(10 * dpi),
                                 fill=color, outline='', tags=tags),
                            call(82, cy - size // 2, 82 + size, cy + size // 2,
                                 fill=color, outline='', tags=tags)])
                        canvas.create_text.assert_called_once_with(
                            82 + size // 2, cy, text=role, fill='#FFFFFF',
                            font=self.panel._font(9, 'bold'), tags=tags)
                canvas = FakeCanvas()
                self.assertEqual(self.panel._draw_diff_marker(canvas, 3, 12, 32, 67, 82), 0)
                canvas.create_rectangle.assert_not_called()
                canvas.create_text.assert_not_called()

    def test_render_list_reserves_badge_width_only_for_title_independent_of_selection(self):
        self.select_pair()
        self.panel._group_of = Mock(return_value='今天')
        self.panel._parse_ts = Mock(return_value=None)
        self.panel._row_card_bg = Mock(return_value=object())
        self.panel._row_tile = Mock(return_value=None)
        self.panel._row_main = Mock(return_value='标题')
        self.panel._favorite_marker = Mock(return_value=object())
        self.panel._set_favorite_button_bg = Mock()
        self.panel._fit_text = Mock(side_effect=lambda text, font, width: text)
        for selected in ('1', '2', '3'):
            with self.subTest(selected=selected):
                self.panel._sel_id = selected
                self.panel._row_bg_items = {}
                self.panel._row_rects = []
                self.panel._card_photos = []
                self.panel._row_card_bg.reset_mock()
                self.panel._fit_text.reset_mock()
                canvas = FakeCanvas()
                rows = [self.records[cid] for cid in ('1', '2', '3')]
                self.panel._render_list(canvas, rows, None, 66, 46, 12, 11, 9)
                titles = [c for c in canvas.create_text.call_args_list
                          if c.kwargs.get('text') == '标题']
                metas = [c for c in canvas.create_text.call_args_list
                         if c.kwargs.get('font') == self.panel._font(9)]
                self.assertEqual([c.args[0] for c in titles], [106, 106, 82])
                self.assertEqual([c.args[0] for c in metas], [82, 82, 82])
                self.assertEqual([c.args[2] for c in self.panel._fit_text.call_args_list],
                                 [236, 260, 236, 260, 260, 260])
                self.assertEqual([c.args[2] for c in self.panel._row_card_bg.call_args_list],
                                 ['selected' if cid == selected else 'normal'
                                  for cid in ('1', '2', '3')])
                badges = [c.kwargs['text'] for c in canvas.create_text.call_args_list
                          if 'diff_marker' in c.kwargs.get('tags', ())]
                self.assertEqual(badges, ['A', 'B'])
                self.assertEqual(self.panel._sel_id, selected)
                self.assert_pair()

    def test_dual_banner_layout_ellipsizes_each_line_and_restores_full_summaries(self):
        self.select_pair()
        self.records['1']['content'] = '左' * 43
        self.records['2']['content'] = '右' * 43
        self.panel._diff_banner.width = 1000
        self.panel._validate_diff_base()
        summaries = (self.panel._diff_base_summary, self.panel._diff_peer_summary)
        full = ('比对 A：' + summaries[0], '比对 B：' + summaries[1])
        labels = (self.panel._diff_banner_text, self.panel._diff_banner_peer)
        self.assertEqual([label.options['text'] for label in labels], list(full))
        for width in (254, 164):
            with self.subTest(width=width):
                self.panel._diff_banner.width = width
                self.panel._layout_diff_banner(SimpleNamespace(width=9999))
                available = width - 60 - 29 - 65
                for label, text in zip(labels, full):
                    fitted = label.options['text']
                    self.assertEqual(fitted, text[:available // 10 - 1] + '…')
                    self.assertLessEqual(self.panel._measure(fitted, label.options['font']), available)
                self.assertEqual((self.panel._diff_base_summary, self.panel._diff_peer_summary), summaries)
                self.assertEqual(self.panel._diff_cancel.options['text'], '取消比对 ×')
        for width in (1, 89):
            self.panel._diff_banner.width = width
            previous = [label.options['text'] for label in labels]
            self.panel._measure.reset_mock()
            self.panel._layout_diff_banner()
            self.panel._measure.assert_not_called()
            self.assertEqual([label.options['text'] for label in labels], previous)
        self.panel._diff_banner.width = 1000
        self.panel._layout_diff_banner()
        self.assertEqual([label.options['text'] for label in labels], list(full))
        self.assertEqual(self.panel._diff_banner_peer.grid_options,
                         {'row': 1, 'column': 0, 'sticky': 'ew', 'padx': (10, 4), 'pady': (0, 6)})

    def test_view_button_only_with_pair_validates_without_reloading_snapshot(self):
        self.panel.ui.view_text_diff = Mock()
        self.assertFalse(self.panel._diff_view.visible)
        self.panel._set_diff_base(1)
        self.assertFalse(self.panel._diff_view.visible)
        self.panel._compare_with_base(2)
        self.assertTrue(self.panel._diff_view.visible)
        self.panel.ui.show_text_diff.reset_mock()
        self.records['1']['content'] = '数据库更新不应替换内存快照'
        self.panel.db.get_text.reset_mock()
        self.panel._diff_view.options['command']()
        self.panel.ui.view_text_diff.assert_called_once_with()
        self.panel.ui.show_text_diff.assert_not_called()
        self.assertEqual(self.panel.db.get_text.call_args_list, [call('1'), call('2')])
        self.panel._clear_diff_base()
        self.assertFalse(self.panel._diff_view.visible)

    def test_cancel_new_a_and_deleted_records_close_and_clear_session(self):
        self.panel.ui.clear_text_diff = Mock()
        for action in ('cancel', 'new_a', 'delete_a', 'delete_b', 'bad_content'):
            with self.subTest(action=action):
                self.records['1'] = self.make_row(1, 'text', 'A')
                self.records['2'] = self.make_row(2, 'code', 'B')
                self.select_pair()
                self.panel.ui.clear_text_diff.reset_mock()
                if action == 'cancel':
                    self.panel._clear_diff_base()
                elif action == 'new_a':
                    self.panel._set_diff_base(3)
                else:
                    if action == 'bad_content':
                        self.records['2']['content'] = None
                    else:
                        self.records.pop('1' if action == 'delete_a' else '2')
                    self.panel._validate_diff_base()
                self.panel.ui.clear_text_diff.assert_called_once_with()
                self.assertIsNone(self.panel._diff_peer_id)
                self.assertFalse(self.panel._diff_view.visible)
                self.assertEqual(self.panel._diff_base_id,
                                 None if action in ('cancel', 'delete_a') else
                                 '3' if action == 'new_a' else '1')

    def test_view_deleted_peer_closes_session_and_does_not_open(self):
        self.select_pair()
        self.panel.ui.clear_text_diff = Mock()
        self.panel.ui.view_text_diff = Mock()
        del self.records['2']
        self.panel._view_diff()
        self.panel.ui.clear_text_diff.assert_called_once_with()
        self.panel.ui.view_text_diff.assert_not_called()
        self.assertEqual(self.panel._diff_base_id, '1')
        self.assertIsNone(self.panel._diff_peer_id)
        self.assertFalse(self.panel._diff_view.visible)

    def test_initial_validation_without_a_does_not_clear_new_session(self):
        snapshot = {'rows': ('完整 A', '完整 B')}
        self.panel.ui._diff_session = snapshot
        self.panel.ui.clear_text_diff = Mock()
        self.assertFalse(self.panel._validate_diff_base())
        self.panel.ui.clear_text_diff.assert_not_called()
        self.assertIs(self.panel.ui._diff_session, snapshot)

    def test_theme_rebuild_initial_refresh_preserves_session_and_view(self):
        self.select_pair()
        snapshot = {'rows': ('完整 A', '完整 B'), 'roles': ('B', 'A'),
                    'mode': 'json_fields',
                    'options': {'ignore_trailing_space': True, 'ignore_line_endings': False}}
        self.panel.ui._diff_session = snapshot
        self.panel.ui.clear_text_diff = Mock()
        self.panel._root = object()
        self.panel._dark = False
        self.panel._is_dark_mode = Mock(return_value=True)
        self.panel._maxed = False
        self.panel._close_popup_menu = Mock()
        for name in ('_thumb_cache', '_tile_cache', '_cardbg_cache',
                     '_marker_cache', '_preview_cache'):
            setattr(self.panel, name, {})

        def rebuild(root, tk, ui):
            self.panel._diff_base_id = self.panel._diff_peer_id = None
            self.panel._build_diff_banner(self.banner_parent, self.chips)
            self.panel._validate_diff_base()

        with patch.object(HistoryPanel, '__init__', side_effect=rebuild):
            self.assertTrue(self.panel._sync_theme())
        self.panel.ui.clear_text_diff.assert_not_called()
        self.assertIs(self.panel.ui._diff_session, snapshot)
        self.assertTrue(self.panel._diff_view.visible)
        self.assert_pair()

    def test_banner_and_marker_share_theme_colors_for_both_roles(self):
        self.select_pair()
        theme = self.panel._theme()[0]
        for dark, colors in ((False, ('#007AFF', '#8050CC')),
                             (True, ('#69AAFF', '#C39AFF'))):
            with self.subTest(dark=dark):
                self.panel._theme.return_value = (theme, dark)
                self.panel._sync_diff_banner()
                for cid, label, role, color in (
                        (1, self.panel._diff_banner_text, 'A', colors[0]),
                        (2, self.panel._diff_banner_peer, 'B', colors[1])):
                    canvas = FakeCanvas()
                    self.panel._draw_diff_marker(canvas, cid, 12, 32, 66, 82)
                    self.assertEqual(self.panel._diff_color(role), color)
                    self.assertEqual(label.options['fg'], color)
                    self.assertEqual(canvas.create_rectangle.call_args.kwargs['fill'], color)
        self.assert_not_opened()


if __name__ == '__main__':
    unittest.main(verbosity=2)
