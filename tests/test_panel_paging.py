"""左栏分页：一次 60 条，滚到底自动续拉下一页。

两层验证：

1. **数据库层**（不需要 Tk）：``list_recent`` / ``search`` 的 ``offset`` 必须
   真正分页——页与页之间不重不漏，越界 offset 返回空。
2. **面板层**（需要 Tk）：``refresh()`` 只画第一页；滚到底触发
   ``_load_next_page()`` 后再追加一页；到底后不再请求。用假 DB 而不是真库，
   这样分页逻辑与 SQL 细节解耦，本机没有临时目录权限也能跑。
"""

import sys
import time
import tkinter as tk
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import lumina.history_panel as hp  # noqa: E402

HistoryPanel = hp.HistoryPanel

try:
    _PROBE = tk.Tk()
    _PROBE.withdraw()
    _PROBE.destroy()
    TK_OK = True
except Exception:  # pragma: no cover - 仅在无图形环境时触发
    TK_OK = False

SKIP_REASON = '本机 Tk 不可用'


# ---------- 数据库层 ----------

class DatabaseOffsetTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from lumina.database import Database
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.temp.name) / 'paging.db'))
        self.db.init_schema()
        for i in range(130):
            self.db.add_text(f'分页样本 {i:03d}', source='test')

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_list_recent_pages_do_not_overlap(self):
        first = [r['id'] for r in self.db.list_recent(60, offset=0)]
        second = [r['id'] for r in self.db.list_recent(60, offset=60)]
        third = [r['id'] for r in self.db.list_recent(60, offset=120)]
        self.assertEqual((len(first), len(second), len(third)), (60, 60, 10))
        self.assertFalse(set(first) & set(second))
        self.assertFalse(set(second) & set(third))
        self.assertEqual(len(set(first) | set(second) | set(third)), 130)

    def test_offset_past_the_end_is_empty(self):
        self.assertEqual(self.db.list_recent(60, offset=130), [])
        self.assertEqual(self.db.list_recent(60, offset=9999), [])

    def test_search_honours_offset(self):
        page1 = [r['id'] for r in self.db.search('分页样本', 60, offset=0)]
        page2 = [r['id'] for r in self.db.search('分页样本', 60, offset=60)]
        self.assertEqual((len(page1), len(page2)), (60, 60))
        self.assertFalse(set(page1) & set(page2))

    def test_count_is_independent_of_page_size(self):
        self.assertEqual(self.db.count('分页样本'), 130)


# ---------- 面板层 ----------

def _row(i):
    ts = (datetime.now() - timedelta(minutes=i)).strftime('%Y-%m-%d %H:%M:%S')
    return {
        'id': i, 'kind': 'text', 'category': 'text', 'source': 'vscode',
        'content': f'分页样本 {i:03d}', 'created_at': ts, 'pinned': 0,
        'data': None, 'file_path': '', 'preview': f'分页样本 {i:03d}',
        'meta': '', 'tags': '', 'size': 10, 'hash': '',
    }


class _Row(dict):
    """模拟 sqlite3.Row：``keys()`` 返回列名。"""

    def keys(self):
        return list(super().keys())


class _PagingDB:
    """按 offset 真分页的假库，行为对齐真实 Database。"""

    def __init__(self, n=130):
        self.rows = [_Row(_row(i)) for i in range(1, n + 1)]
        self.queries = []

    def _slice(self, n, offset):
        self.queries.append((n, offset))
        return self.rows[offset:offset + n]

    def list_recent(self, n=20, category=None, pinned=False, offset=0):
        return self._slice(n, offset)

    def search(self, query, n=20, category=None, pinned=False, offset=0):
        return self._slice(n, offset)

    def count(self, *a, **k):
        return len(self.rows)

    def counts_by_category(self, *a, **k):
        return {}

    def get(self, rid, *a, **k):
        for row in self.rows:
            if str(row['id']) == str(rid):
                return row
        return self.rows[0] if self.rows else None

    def __getattr__(self, name):
        return lambda *a, **k: None


class _FakeUI:
    def __init__(self, n=130):
        self.db = _PagingDB(n)
        self.config = {}
        self.panel_visible = False

    def show_panel(self):
        pass


@unittest.skipUnless(TK_OK, SKIP_REASON)
class PanelPagingTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.ui = _FakeUI(130)
        self.panel = HistoryPanel(self.root, tk, self.ui)
        self.panel.win.deiconify()
        self.panel.apply_mode()
        self._pump(0.2)
        self.panel.refresh()
        self._pump(0.15)

    def tearDown(self):
        for target in (self.panel.win, self.root):
            try:
                target.destroy()
            except tk.TclError:
                pass

    def _pump(self, seconds=0.0):
        self.panel.win.update()
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.panel.win.update()
            time.sleep(0.01)

    def test_first_page_is_sixty(self):
        self.assertEqual(len(self.panel._rows_displayed), 60)
        self.assertTrue(self.panel._has_more)
        self.assertEqual(self.panel.status_var.get(), '显示 60 / 共 130 条')

    def test_next_page_appends_without_replacing(self):
        before = [r['id'] for r in self.panel._rows_displayed]
        self.assertTrue(self.panel._load_next_page())
        after = [r['id'] for r in self.panel._rows_displayed]
        self.assertEqual(len(after), 120)
        self.assertEqual(after[:60], before)
        self.assertTrue(self.panel._has_more)

    def test_stops_when_everything_is_loaded(self):
        self.panel._load_next_page()
        self.assertTrue(self.panel._load_next_page())
        self.assertEqual(len(self.panel._rows_displayed), 130)
        self.assertFalse(self.panel._has_more)
        self.assertEqual(self.panel.status_var.get(), '共 130 条')
        # 到底之后再调不应再查库
        calls = len(self.ui.db.queries)
        self.assertFalse(self.panel._load_next_page())
        self.assertEqual(len(self.ui.db.queries), calls)

    def test_scroll_to_bottom_triggers_one_page(self):
        self.panel._on_list_scroll('0.9', '1.0')
        self._pump(0.1)
        self.assertEqual(len(self.panel._rows_displayed), 120)

    def test_repeated_scroll_events_do_not_stack_queries(self):
        calls = len(self.ui.db.queries)
        for _ in range(5):
            self.panel._on_list_scroll('0.9', '1.0')
        self._pump(0.1)
        # 第一次触发一页；其余事件在 pending 期间被丢弃
        self.assertEqual(len(self.ui.db.queries), calls + 1)
        self.assertEqual(len(self.panel._rows_displayed), 120)

    def test_refresh_resets_pagination(self):
        self.panel._load_next_page()
        self.assertEqual(len(self.panel._rows_displayed), 120)
        self.panel.refresh()
        self._pump(0.05)
        self.assertEqual(len(self.panel._rows_displayed), 60)
        self.assertTrue(self.panel._has_more)


if __name__ == '__main__':
    unittest.main()