"""搜索框按 Enter 的行为回归。

原来 ``_on_enter`` 直接 ``paste_back()``，而回贴会 ``hide()`` 面板——用户想确认
搜索结果，按下回车剪贴板就消失了。现在第一次 Enter 只提交搜索并选中第一条，
焦点交给列表；再按 Enter 才回贴。
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


def _row(i, content):
    ts = (datetime.now() - timedelta(minutes=i)).strftime('%Y-%m-%d %H:%M:%S')
    return {
        'id': i, 'kind': 'text', 'category': 'text', 'source': 'vscode',
        'content': content, 'created_at': ts, 'pinned': 0, 'data': None,
        'file_path': '', 'preview': content, 'meta': '', 'tags': '',
        'size': len(content), 'hash': '',
    }


class _RowDict(dict):
    def keys(self):
        return list(super().keys())


class _SearchDB:
    def __init__(self):
        self.rows = [
            _RowDict(_row(1, '图片说明 A')),
            _RowDict(_row(2, '一段普通文本')),
            _RowDict(_row(3, '图片说明 B')),
            _RowDict(_row(4, '另一段文本')),
        ]

    def _match(self, query):
        q = (query or '').strip()
        if not q:
            return list(self.rows)
        return [r for r in self.rows if q in (r.get('content') or '')]

    def list_recent(self, n=20, category=None, pinned=False, offset=0):
        return self.rows[offset:offset + n]

    def search(self, query, n=20, category=None, pinned=False, offset=0):
        return self._match(query)[offset:offset + n]

    def count(self, query='', category=None, pinned=False):
        return len(self._match(query))

    def counts_by_category(self, *a, **k):
        return {}

    def get(self, rid, *a, **k):
        for row in self.rows:
            if str(row['id']) == str(rid):
                return row
        return None

    def get_text(self, rid, *a, **k):
        return self.get(rid)

    def __getattr__(self, name):
        return lambda *a, **k: None


class _FakeUI:
    def __init__(self):
        self.db = _SearchDB()
        self.config = {}
        self.panel_visible = False

    def show_panel(self):
        pass


@unittest.skipUnless(TK_OK, SKIP_REASON)
class SearchEnterTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.panel = HistoryPanel(self.root, tk, _FakeUI())
        self.panel.win.deiconify()
        self.panel.apply_mode()
        self._pump(0.25)
        self.calls = []
        self.panel.paste_back = lambda: self.calls.append('paste')
        self.panel.hide = lambda: self.calls.append('hide')

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

    def test_enter_in_search_keeps_panel_open(self):
        self.panel.search_var.set('图片')
        result = self.panel._on_enter()
        self._pump(0.1)
        self.assertEqual(result, 'break')
        self.assertNotIn('hide', self.calls)
        self.assertNotIn('paste', self.calls)
        self.assertTrue(self.panel.win.winfo_exists())

    def test_enter_selects_first_match(self):
        self.panel.search_var.set('图片')
        self.panel._on_enter()
        self._pump(0.1)
        ids = [str(r['id']) for r in self.panel._rows_displayed]
        self.assertEqual(ids, ['1', '3'])
        self.assertEqual(self.panel._sel_id, '1')

    def test_enter_moves_focus_to_list(self):
        self.panel.search_var.set('图片')
        self.panel._on_enter()
        self._pump(0.1)
        focused = self.panel.win.focus_get()
        self.assertIn(focused, (None, self.panel.cards_canvas))

    def test_enter_without_match_reports_status(self):
        self.panel.search_var.set('不存在的关键词')
        self.panel._on_enter()
        self._pump(0.1)
        self.assertIn('没有匹配记录', self.panel.status_var.get())
        self.assertNotIn('hide', self.calls)

    def test_ctrl_enter_still_pastes_from_search(self):
        """Ctrl+Enter 保留旧的“搜索框里直接回贴”路径。"""
        self.panel.search_var.set('图片')
        self.panel._on_enter_paste()
        self.assertEqual(self.calls, ['paste'])

    def test_list_still_pastes_with_enter(self):
        """焦点在列表时 Enter 仍是回贴（窗口级绑定不能被这次改动弄丢）。"""
        self.assertTrue(self.panel.win.bind('<Return>'))


if __name__ == '__main__':
    unittest.main()
