"""sqlite3.Row 的 `in` 判断的是值而非列名，不能用来探测列是否存在。

历史 bug：`"source" in r` 恒为 False，导致列表行图标的来源全部回落成空串、
整列显示「未知来源」；`copy_to_clipboard` 里同样的写法让 data/file_path/content
全部读成默认值。这两处都必须用 `keys()`。
"""
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lumina.database import LIST_COLUMNS  # noqa: E402
from lumina.history_panel import HistoryPanel  # noqa: E402

SCHEMA = """CREATE TABLE clipboard (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL, content TEXT, data BLOB,
    file_path TEXT DEFAULT '', file_status TEXT NOT NULL DEFAULT 'none',
    hash TEXT NOT NULL DEFAULT '', source TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '', pinned INTEGER NOT NULL DEFAULT 0,
    tags TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT ''
)"""


def make_row(source='chrome.exe', content='hello', category='text'):
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA)
    conn.execute(
        "INSERT INTO clipboard (kind, content, category, source) VALUES (?,?,?,?)",
        ('text', content, category, source))
    conn.commit()
    row = conn.execute(f'SELECT {LIST_COLUMNS} FROM clipboard').fetchone()
    return conn, row


class RowMembershipSemanticsTests(unittest.TestCase):
    """先把 Row 的语义钉死，避免以后又有人用 `in`。"""

    def setUp(self):
        self.conn, self.row = make_row()

    def tearDown(self):
        self.conn.close()

    def test_in_operator_checks_values_not_column_names(self):
        # 这条断言解释 bug 的成因：列名 "source" 不可能等于任何一列的值。
        self.assertNotIn("source", self.row)
        # 而实际的值能被命中，证明 in 走的是值。
        self.assertIn("chrome.exe", self.row)

    def test_keys_reports_column_names(self):
        self.assertIn("source", self.row.keys())
        self.assertIn("category", self.row.keys())


class RowTileSourceTests(unittest.TestCase):
    """列表行图标必须拿到真实 source，而不是恒空串。"""

    def setUp(self):
        self.conn, self.row = make_row(source='idea64.exe')

    def tearDown(self):
        self.conn.close()

    def _make_minimal_detail_pane(self):
        """只挂 _row_tile 依赖的少量属性，绕过整套 Tk 构建。"""
        pane = HistoryPanel.__new__(HistoryPanel)
        pane._thumb_cache = {}
        pane._tile_cache = {}

        def source_app(source):
            s = (source or '').strip()
            hit = HistoryPanel.SOURCE_APPS.get(s.lower())
            if hit:
                return hit
            name = s[:-4] if s.lower().endswith('.exe') else s
            return (name or '未知来源', 'gray', 'app')

        pane._source_app = source_app
        pane._is_dark_mode = lambda: True
        pane._sys = lambda name: HistoryPanel.SYS_COLORS.get(
            name, HistoryPanel.SYS_COLORS['gray'])[1]
        pane._load_font = lambda size: None
        pane._source_code_tile = (
            lambda source, size: HistoryPanel._source_code_tile(pane, source, size))
        return pane

    def test_row_tile_reads_source_from_real_sqlite_row(self):
        pane = self._make_minimal_detail_pane()
        # 直接调被修复的那段逻辑：source 必须来自 row，不能是空串。
        source = self.row["source"] if "source" in self.row.keys() else ""  # noqa: SIM118
        self.assertEqual(source, 'idea64.exe')
        # 且该 source 能被解析成正式的应用名，而不是「未知来源」。
        self.assertEqual(pane._source_app(source)[0], 'IDEA')


class CopyUsesCorrectColumnsTests(unittest.TestCase):
    """copy_to_clipboard 的 data/file_path/content 必须真的读到。"""

    def setUp(self):
        self.conn, _ = make_row(source='chrome.exe', content='{"a":1}')
        self.row = self.conn.execute('SELECT * FROM clipboard').fetchone()

    def tearDown(self):
        self.conn.close()

    def test_columns_are_detected_via_keys(self):
        # 复现修复后的取值方式，确认 content 拿到原文而非 None。
        content = self.row["content"] if "content" in self.row.keys() else None  # noqa: SIM118
        self.assertEqual(content, '{"a":1}')
        data = self.row["data"] if "data" in self.row.keys() else None  # noqa: SIM118
        self.assertIsNone(data)
        file_path = (
            self.row["file_path"] if "file_path" in self.row.keys() else "")  # noqa: SIM118
        self.assertEqual(file_path, '')

    def test_old_membership_check_would_lose_content(self):
        # 反证：老写法会把 content 读成 None，从而丢掉调用方内容。
        # 这里刻意保留裸 in 以演示 bug，故豁免 SIM401（它建议改 .get()）。
        legacy = self.row["content"] if "content" in self.row else None  # noqa: SIM401
        self.assertIsNone(legacy)

    def test_dict_input_still_supported(self):
        # keys() 对 dict 也成立，动作层可能收到构造出来的临时行。
        row = {'content': 'x', 'data': None, 'file_path': ''}
        self.assertEqual(
            row["content"] if "content" in row.keys() else None, 'x')  # noqa: SIM118


class SourceCodeGuardTests(unittest.TestCase):
    """源码守卫：生产代码里不能再出现 `"列名" in <row变量>` 的写法。

    上面那些用例只验证「正确写法能得到正确结果」，如果有人把 `keys()` 改回
    裸 `in`，它们依然全绿——真正能拦住回退的是这里对源码的检查。
    """

    ROW_VARS = ('row', 'r', 'full', 'full_row', 'hit', 'item', 'record')

    def _sources(self):
        return {
            'history_panel': (ROOT / 'lumina' / 'history_panel.py').read_text(
                encoding='utf-8'),
            'application': (ROOT / 'lumina' / 'application.py').read_text(
                encoding='utf-8'),
        }

    def test_no_bare_column_membership_on_row_like_vars(self):
        import re
        pattern = re.compile(
            r'"[a-z_]+"\s+in\s+(' + '|'.join(self.ROW_VARS) + r')\b(?!\.keys\()')
        offenders = []
        for name, text in self._sources().items():
            for lineno, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith('#'):
                    continue
                for match in pattern.finditer(line):
                    # .keys() 形式被负向前瞻排除，这里只剩裸 in。
                    offenders.append(
                        f'{name}.py:{lineno}: {match.group(0)}')
        self.assertEqual(
            offenders, [],
            'sqlite3.Row 的 in 判断的是值而非列名，请改用 `<row>.keys()`：\n'
            + '\n'.join(offenders))

    def test_keys_based_reads_present(self):
        # 修复必须真实落在两处生产代码上，防止只改了一边。
        sources = self._sources()
        self.assertIn(
            '"source" in r.keys()', sources['history_panel'])
        self.assertIn(
            '"data" in row.keys()', sources['application'])


if __name__ == '__main__':
    unittest.main()
