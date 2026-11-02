"""滚动条鼠标拖拽的回归。

对应需求：*「这个滚动条能不能改成可以鼠标拖拽的，能不能稍微靠右侧边框点」*

两件事分开守：

1. **靠右**：指示条的右端距画布右缘只剩 1 逻辑像素，视觉上几乎贴着边框。
2. **可拖拽**：按下能抓住、拖动跟手、点轨道空白处会直接跳、松开能恢复正常。

**设计上的三个坑**（都是实测踩出来的）：

- **细条 4px 抓不中**。视觉宽度和可抓取热区必须分开：``SCROLLBAR_W``(4) 是画多宽，
  ``SCROLLBAR_HIT_W``(12) 是能点多宽。热区比通道宽是**故意的**——多出来的部分
  盖在卡片右侧边缘上，不影响绘制，但让 4px 的细条真的能抓住。
- **`<Button-1>` 只能绑一次**。Tk 同事件重复 `bind` 是**覆盖**不是叠加，
  所以必须先问滚动条、再交回列表选择，合在一个分派器里做。
- **必须 `grab_set`**。`<B1-Motion>` 只在指针位于控件内时投递，拖快了滑出画布
  就会突然断掉。松开时**必须配对 `grab_release`**，否则面板其他区域全无反应。
"""
import sys
import time
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import lumina.history_panel as hp  # noqa: E402

HistoryPanel = hp.HistoryPanel
SOURCE = ROOT / 'lumina' / 'history_panel.py'

try:
    _PROBE = tk.Tk()
    _PROBE.withdraw()
    _PROBE.destroy()
    TK_OK = True
except Exception:  # pragma: no cover - 仅在无图形环境时触发
    TK_OK = False

SKIP_REASON = '本机 Tk 不可用'


def _make_row(i):
    return {
        "id": i, "kind": "text", "category": "text", "source": "vscode",
        "content": f"示例文本 {i}", "created_at": "2026-10-30 12:00:00",
        "pinned": 0, "data": None, "file_path": "",
        "preview": f"示例文本 {i}", "meta": "", "tags": "",
        "size": 100, "hash": "",
    }


class _Row(dict):
    def keys(self):
        return list(super().keys())


class _FakeDB:
    def __init__(self, n):
        self.rows = [_Row(_make_row(i)) for i in range(1, n + 1)]

    def list_recent(self, *a, **k):
        return self.rows

    def count(self, *a, **k):
        return len(self.rows)

    def counts_by_category(self, *a, **k):
        return {}

    def get(self, rid, *a, **k):
        return self.rows[0] if self.rows else None

    def __getattr__(self, name):
        return lambda *a, **k: None


class _FakeUI:
    def __init__(self, n):
        self.db = _FakeDB(n)
        self.config = {}
        self.panel_visible = False


@unittest.skipUnless(TK_OK, SKIP_REASON)
class ScrollbarDragTests(unittest.TestCase):
    ROWS = 60

    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.panel = HistoryPanel(self.root, tk, _FakeUI(self.ROWS))
        self.panel.win.deiconify()
        self.panel.apply_mode()
        self._pump(0.35)
        self.panel.refresh()
        self._pump(0.3)
        self.canvas = self.panel.cards_canvas
        self.W = self.canvas.winfo_width()
        self.H = self.canvas.winfo_height()

    def tearDown(self):
        try:
            self.canvas.grab_release()
        except Exception:
            pass
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

    def _event(self, x, y):
        return SimpleNamespace(widget=self.canvas, x=x, y=y,
                               x_root=x, y_root=y)

    def _press(self, x, y):
        return self.panel._on_scrollbar_or_click(self._event(x, y))

    def _drag(self, x, y):
        return self.panel._on_scrollbar_motion(self._event(x, y))

    def _release(self, x, y):
        return self.panel._on_scrollbar_release(self._event(x, y))

    def _thumb_top_in_view(self):
        ind = self.panel._scroll_ind
        self.assertIsNotNone(ind, '滚动指示条未被创建')
        return self.canvas.coords(ind)[1] - self.canvas.canvasy(0)

    # ---------- 1. 靠右 ----------

    def test_scrollbar_hugs_right_edge(self):
        """指示条右端距画布右缘必须 <= 2 逻辑像素（用户要求「稍微靠右」）。"""
        _x0, x1 = self.panel._scrollbar_rect()
        pad = self.W - x1
        limit = max(2, int(round(2 * self.panel._dpi)))
        self.assertLessEqual(
            pad, limit,
            f'指示条右端距画布右缘还有 {pad}px，没贴到边框')

    def test_scrollbar_does_not_overflow_canvas(self):
        """指示条不能越过画布右缘（会直接被裁掉，看起来像缺一截）。"""
        _x0, x1 = self.panel._scrollbar_rect()
        self.assertLessEqual(x1, self.W, f'指示条右端 {x1} 超出画布宽 {self.W}')

    # ---------- 2. 可抓取 ----------

    def test_hit_zone_is_wider_than_visible_bar(self):
        """可抓取热区必须明显宽于画出来的细条，否则 4px 根本点不中。"""
        x0, x1 = self.panel._scrollbar_rect()
        bar_w = x1 - x0
        hit_x = self.panel._scrollbar_hit_x()
        hit_w = self.W - hit_x
        self.assertGreater(
            hit_w, bar_w,
            f'热区宽 {hit_w} 不大于条宽 {bar_w}，4px 的细条几乎抓不住')
        # 热区必须从最右缘往左算（含条本体）
        self.assertLessEqual(hit_x, x0, '热区没覆盖到指示条本体')

    def test_press_on_scrollbar_starts_drag(self):
        """在原抓取热区内按下必须进入拖拽态，并拦住行选择。"""
        result = self._press(self.W - 5, int(self._thumb_top_in_view() + 6))
        self.assertEqual(result, 'break', '按在滚动条上没拦住列表点击')
        self.assertIsNotNone(self.panel._scroll_drag, '按在滚动条上没进入拖拽态')

    def test_press_on_list_area_does_not_start_drag(self):
        """在列表中间按下不能误触发拖拽（否则列表点选会失效）。"""
        self._press(self.W // 2, 120)
        self.assertIsNone(
            self.panel._scroll_drag,
            '列表中间按下误进入了滚动条拖拽态')

    # ---------- 3. 跟手 ----------

    def test_drag_moves_view_monotonically(self):
        """往下拖时滚动位置必须单调增大，且大致跟手。"""
        top = self._thumb_top_in_view()
        self._press(self.W - 5, int(top + 8))
        path = [self.canvas.yview()[0]]
        for step in (80, 160, 240):
            self._drag(self.W - 5, int(top + 8 + step))
            self._pump(0.05)
            path.append(self.canvas.yview()[0])
        self._release(self.W - 5, int(top + 8 + 240))
        self.assertEqual(path, sorted(path), f'拖动时滚动位置不单调：{path}')
        self.assertGreater(
            path[-1] - path[0], 0.05,
            f'拖了 240px 视图几乎没动（{path[0]:.3f}→{path[-1]:.3f}）')

    def test_drag_preserves_grab_offset(self):
        """抓住滑块中段拖到某高度时，滑块顶端应落在「指针 y - 抓取偏移」。

        这条防的是「一按滑块就跳一下」：若按下时不记住抓取偏移、而是把指针
        当滑块中心，第一次拖动就会突然位移，手感很差。
        """
        self.canvas.yview_moveto(0.3)
        self._pump(0.1)
        top = self._thumb_top_in_view()
        grab = 12
        press_y = int(top + grab)
        self._press(self.W - 5, press_y)
        self.assertAlmostEqual(
            self.panel._scroll_drag_grab, grab, delta=2,
            msg='按下时没记住指针相对滑块顶端的偏移')
        self._drag(self.W - 5, press_y + 150)
        self._pump(0.1)
        got_top = self._thumb_top_in_view()
        self.assertAlmostEqual(
            got_top, press_y + 150 - grab, delta=6,
            msg=f'拖动后滑块顶端 {got_top:.0f} 与期望 {press_y + 150 - grab} 不符，'
                '抓取偏移没有保持')
        self._release(self.W - 5, press_y + 150)

    def test_click_on_track_jumps(self):
        """点在轨道空白处应直接跳到该位置（不是必须拖才算）。"""
        self.canvas.yview_moveto(0.0)
        self._pump(0.1)
        self._press(self.W - 5, self.H - 30)
        self._pump(0.1)
        self.assertGreater(
            self.canvas.yview()[0], 0.5,
            '点轨道底部后视图没跳过去')
        self._release(self.W - 5, self.H - 30)

    # ---------- 4. 状态收尾 ----------

    def test_release_clears_drag_and_grabs(self):
        """松开必须清状态并释放 grab，否则整个面板的鼠标事件被吞掉。"""
        top = self._thumb_top_in_view()
        self._press(self.W - 5, int(top + 8))
        self._release(self.W - 5, int(top + 8))
        self.assertIsNone(self.panel._scroll_drag, '松开后拖拽态没清掉')
        self.assertIsNone(self.panel._scroll_drag_grab, '松开后抓取偏移没清掉')
        self.assertIsNone(
            self.canvas.grab_current(),
            '松开后 grab 没释放，面板其他区域会点不动')

    # ---------- 5. 源码级 ----------

    def test_button1_has_a_single_binding(self):
        """`<Button-1>` 只能绑一次（Tk 重复 bind 是覆盖，会静默丢掉行选择）。"""
        import re
        src = SOURCE.read_text(encoding='utf-8')
        binds = re.findall(r'cards_canvas\.bind\(\s*[\'"]<Button-1>[\'"]',
                           src)
        self.assertEqual(
            len(binds), 1,
            f'`<Button-1>` 绑了 {len(binds)} 次；Tk 里后面的会覆盖前面的，'
            '应只绑分派器 `_on_scrollbar_or_click`')

    def test_grab_is_released_on_release(self):
        """松开处理器里必须有 grab_release（配对 grab_set）。"""
        src = SOURCE.read_text(encoding='utf-8')
        start = src.index('def _on_scrollbar_release')
        end = src.index('\n    def ', start + 10)
        body = src[start:end]
        self.assertIn('grab_release', body,
                      '`_on_scrollbar_release` 里没有 grab_release，'
                      'grab 会一直占着，面板其他区域点不动')


if __name__ == '__main__':
    unittest.main()
