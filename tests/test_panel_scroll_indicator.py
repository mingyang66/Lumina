"""列表滚动指示条的回归。

对应缺陷：*「滚动条为啥滚动的时候直接到最上面隐藏了，滚动条的滚动逻辑有问题」*

**根因**：``_on_list_scroll`` 把 ``yview()`` 给的**视口比例**直接当作 Canvas
图元的 y 坐标用。``yview()`` 描述的是「视口在整段内容里的位置」，而图元坐标
是**画布坐标**，会随滚动一起移动。于是图元被钉死在滚动区域的最顶端那一段：

    偏移 6   → 图元 y=[2, 110]，还在屏内（所以初看以为正常）
    偏移 144 → 图元 y=[33, 142]，而视口从 144 开始 → 整条跑到上方消失

用户看到的「一滚就往上跑去隐藏」就是这个。修法是加上当前滚动偏移
``canvas.canvasy(0)``，把图元抬到此刻视口所在的位置。

这里守四件事：

1. **指示条必须始终落在视口内**。这是缺陷本体，且必须**在多个滚动位置**上都
   验——只在 ``yview`` 接近 0 的初始态测，缺陷根本暴露不出来（初始偏移只有
   6px，图元还在屏内）。
2. **相对视口的位置必须符合比例**，且随滚动单调下移。既要「不动」（不飞出视口），
   又要「动」（跟着内容走），这两个方向都要钉住，否则修成一个永远不动的死条
   也能让第 1 条通过。
3. **高度按可见比例**，内容溢出越多滑块越短。
4. **内容不溢出时不画**（整段可见时 ``yview`` 差值约 1，指示条应被删除）。
"""
import sys
import time
import tkinter as tk
import unittest
from datetime import datetime
from pathlib import Path

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
    """字段齐全的假记录。``created_at`` 用合法时间串，避免解析失败影响分组。"""
    return {
        "id": i, "kind": "text", "category": "text", "source": "vscode",
        "content": f"示例文本 {i}", "created_at": "2026-10-30 12:00:00",
        "pinned": 0, "data": None, "file_path": "",
        "preview": f"示例文本 {i}", "meta": "", "tags": "",
        "size": 100, "hash": "",
    }


class _Row(dict):
    """模拟 sqlite3.Row：``keys()`` 返回列名，``"x" in row`` 判断值。"""

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
class ScrollIndicatorTests(unittest.TestCase):
    # 60 行 ≈ 60*74 = 4440px，远超 699px 的视口，保证一定有溢出
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

    def _scroll_units(self, n):
        self.canvas.yview_scroll(n, 'units')
        self._pump(0.05)

    # ---------- 几何辅助 ----------

    def _thumb_rel(self):
        """返回指示条在**视口内**的相对纵向区间 ``(top, bottom)``。

        拿 ``canvasy(0)`` 换算而不是直接用图元坐标：图元坐标是画布坐标，
        正是这个混淆造成了原缺陷。
        """
        ind = self.panel._scroll_ind
        self.assertIsNotNone(ind, '滚动指示条未被创建')
        _x0, y0, _x1, y1 = self.canvas.coords(ind)
        off = self.canvas.canvasy(0)
        return y0 - off, y1 - off

    # ---------- 1. 必须始终在视口内 ----------

    def test_indicator_stays_inside_viewport_while_scrolling(self):
        """滚动过程中，指示条任何时刻都不得跑出视口。

        **必须扫多个位置**：缺陷在初始态（偏移仅 6px）看不出来，图元还在屏内，
        要滚过一两屏才会整条飞到可视区上方。只测初始态等于没测。
        """
        h = self.canvas.winfo_height()
        self.assertGreater(h, 20, '画布尚未映射，高度不可信')
        observed = []
        for step in range(8):
            if step:
                self._scroll_units(2)
            top, bottom = self._thumb_rel()
            observed.append((step, round(top), round(bottom)))
            self.assertGreaterEqual(
                bottom, 0,
                f'第 {step} 次滚动后指示条整体在视口上方（bottom={bottom:.0f}）'
                f'——正是「一滚就跑到最上面隐藏」的缺陷。轨迹：{observed}')
            self.assertLessEqual(
                top, h,
                f'第 {step} 次滚动后指示条整体在视口下方（top={top:.0f} > h={h}）。'
                f'轨迹：{observed}')

    def test_indicator_stays_inside_viewport_at_bottom(self):
        """滚到最底部时也要在视口内（边界最容易算出界的位置）。"""
        self.canvas.yview_moveto(1.0)
        self._pump(0.1)
        h = self.canvas.winfo_height()
        top, bottom = self._thumb_rel()
        self.assertGreaterEqual(top, 0, f'滚到底时指示条上边界 {top:.0f} 出界')
        self.assertLessEqual(bottom, h, f'滚到底时指示条下边界 {bottom:.0f} 超出画布 {h}')

    # ---------- 2. 跟着内容走（不能修成死条） ----------

    def test_indicator_tracks_scroll_position(self):
        """指示条要随滚动单调下移，且与 ``yview()[0]`` 成比例。

        这条防的是「为了不飞出视口，干脆钉死不动」这种修法——那样第 1 条会通过，
        但滚动条失去意义。期望值按 ``first * h`` 算，同时验证相对位置在增长。
        """
        h = self.canvas.winfo_height()
        tops = []
        for _ in range(5):
            first, _last = self.canvas.yview()
            top, _bottom = self._thumb_rel()
            tops.append(top)
            self.assertAlmostEqual(
                top, first * h, delta=3,
                msg=f'指示条顶端 {top:.1f} 与 first*h={first * h:.1f} 不符')
            self._scroll_units(2)
        self.assertEqual(
            tops, sorted(tops),
            f'滚动时指示条顶端没有单调下移：{tops}')
        self.assertGreater(
            tops[-1] - tops[0], 20,
            f'滚动多格后指示条几乎没移动（{tops[0]:.0f}→{tops[-1]:.0f}），'
            '像是个钉死不动的假滚动条')

    def test_indicator_height_follows_visible_ratio(self):
        """滑块高度应正比于可见比例 ``last - first``。"""
        h = self.canvas.winfo_height()
        first, last = self.canvas.yview()
        top, bottom = self._thumb_rel()
        want = (last - first) * h
        self.assertAlmostEqual(
            bottom - top, want, delta=4,
            msg=f'滑块高 {bottom - top:.1f} 与可见比例对应的高度 {want:.1f} 不符')

    # ---------- 3. 内容不溢出时不画 ----------

    def test_indicator_hidden_when_content_fits(self):
        """整段内容都能看见时不应画指示条（否则会出现一条满高的灰条）。"""
        panel = self.panel
        small = [_Row(_make_row(i)) for i in range(1, 4)]
        panel._render_list(
            self.canvas, small, datetime.now(),
            row_h=int(round(66 * panel._dpi)),
            tile_size=int(round(46 * panel._dpi)),
            margin=int(round(panel._card_margin() * panel._dpi)),
            title_size=11, meta_size=9)
        self._pump(0.2)
        # 内容变少后 yscrollcommand 会重新触发；此时应删除旧图元
        self.canvas.yview_moveto(0.0)
        self._pump(0.1)
        ind = panel._scroll_ind
        if ind is not None:
            self.assertIsNone(
                self.canvas.find_withtag('scrollind') or None,
                '内容不溢出时滚动指示条应被删除')

    # ---------- 4. 源码级：偏移不能又被漏掉 ----------

    def test_indicator_drawn_above_cards(self):
        """指示条必须压在所有卡片与收藏按钮之上，否则会被卡片盖住看不见。

        【变异结论，如实记录】把 ``tag_raise`` 删掉**测不出**——因为
        ``_render_list`` 里 ``self._scroll_ind = None`` 之后，下一次滚动是
        **新建**图元，创建顺序天然就在最后，z 序已经正确。所以这条守的是
        **不变量**（谁在最上面），不是 `tag_raise` 这行代码本身。

        保留 ``tag_raise`` 的理由是防御：将来若有人把指示条的创建挪到渲染
        之前，或改成复用旧图元，创建顺序就不再能保证，那时它会救一次。
        """
        self._scroll_units(2)
        ind = self.panel._scroll_ind
        self.assertIsNotNone(ind, '滚动指示条未被创建')
        order = self.canvas.find_all()
        self.assertIn(ind, order)
        pos = order.index(ind)
        for tag in ('cardbg', 'card'):
            for item in self.canvas.find_withtag(tag):
                if item == ind:
                    continue
                self.assertLess(
                    order.index(item), pos,
                    f'指示条被图元 {item}（tag={tag}）盖住了')

    def test_scroll_indicator_offsets_by_canvas_origin(self):
        """``_on_list_scroll`` 必须显式取 ``canvasy(0)`` 做偏移。

        这是缺陷的**直接**守卫。上面的几何测试依赖真实滚动，一旦哪天被
        skip（无图形环境）就没人拦了；这条纯源码检查不依赖运行时。
        字面量式写法（``y0 = first * h``）在**初始态看起来完全正常**，
        只有滚过一两屏才暴露，正是典型的「运行时状态相关」缺陷。
        """
        src = SOURCE.read_text(encoding='utf-8')
        start = src.index('def _on_list_scroll')
        end = src.index('\n    def ', start + 10)
        body = src[start:end]
        self.assertIn(
            'canvasy(0)', body,
            '`_on_list_scroll` 又没把图元坐标偏移到当前视口了——'
            '直接拿 yview 比例当画布 y 用，滚动后指示条会飞出视口')
        self.assertNotIn(
            'y0 = max(2, first_x * h)', body,
            '`_on_list_scroll` 又回到「yview 比例直接当画布 y」的老写法了')


if __name__ == '__main__':
    unittest.main()
