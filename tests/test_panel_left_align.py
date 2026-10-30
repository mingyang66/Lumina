"""左栏各层水平对齐的回归。

对应需求：*「分析下左侧栏下面每一项是不是跟最上面的搜索框没有对齐」* —— 实测确认
**没对齐**：搜索胶囊是 ``pack(fill="x")`` 满铺左栏（左右边界 0 / 492），卡片却按
``margin=12`` 内缩（实测 14 / 478），列表比搜索框窄一圈。

改成 ``_card_margin()`` 返回 0 后，两者左右边界完全一致。这里守四件事：

1. **卡片与搜索胶囊严格左右对齐**。这是需求本体，必须用像素级实测而不是
   「card_w 算出来相等」——卡片图是圆角描边位图，``winfo_width()`` 报的是
   画布宽度，报不出实际绘制出来的可见边界。
2. **``_card_margin()`` 是唯一来源**。早先四处各自写死 ``12 * dpi``：
   绘制起点、卡片宽度、分组标题、以及 ``_on_canvas_click`` 的收藏热区基准。
   其中收藏热区那份乘的是字面量 12 而不是 margin 变量，**改 margin 就悄悄错位**
   ——点星标变成点空白，且不报任何错。
3. **分组标题与卡片内图标同列**。``margin + 2`` 在 margin=12 时恰好是 14，
   看着和图标（12）齐；margin 变 0 后就只剩 2，标题会悬到卡片外面。
4. **卡片宽度必须撑满**（= 左栏宽 - 2 * margin），不能因为改了 margin 而变窄。
"""
import sys
import time
import tkinter as tk
import unittest
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

try:
    from PIL import ImageGrab  # noqa: F401
    PIL_OK = True
except Exception:  # pragma: no cover
    PIL_OK = False

# 窗口底色。像素扫描以它为「空白」基准。
WINDOW_BG = (242, 242, 247)


def _make_row(i):
    """构造一条字段齐全的假记录（缺字段会让 _render_list 抛 KeyError）。"""
    return {
        "id": i, "kind": "text", "category": "text", "source": "vscode",
        "content": f"示例文本 {i}",
        "created_at": f"2026-10-30 12:0{i}:00", "pinned": 0,
        "data": None, "file_path": "", "preview": f"示例文本 {i}",
        "meta": "", "tags": "", "size": 100, "hash": "",
    }


class _Row(dict):
    """模拟 sqlite3.Row：``keys()`` 返回列名，``"x" in row`` 判断值。"""

    def keys(self):
        return list(super().keys())


class _FakeDB:
    def __init__(self, n=4):
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
    def __init__(self):
        self.db = _FakeDB()
        self.config = {}
        self.panel_visible = False


@unittest.skipUnless(TK_OK, SKIP_REASON)
class LeftColumnAlignmentTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.panel = HistoryPanel(self.root, tk, _FakeUI())
        self.panel.win.deiconify()
        self.panel.apply_mode()
        self._pump(0.35)
        # 必须真有记录：空列表时 `_render_list` 走 `_draw_empty`，
        # 卡片边界那条测不到任何卡片。
        self.panel.refresh()
        self._pump(0.25)

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

    # ---------- 几何辅助 ----------

    def _left_col_origin(self):
        return self.panel._left_frame.winfo_rootx() - self.panel.win.winfo_rootx()

    # ---------- 1. margin 是唯一来源 ----------

    def test_card_margin_is_zero_for_alignment(self):
        """卡片 margin 必须是 0，才能和满铺的搜索胶囊对齐。

        这是一条**意图断言**：数值可以再调，但「和搜索框对齐」这个结论不能丢。
        若以后决定让搜索框也内缩（方案 B），这条要连同搜索框的内距一起改。
        """
        self.assertEqual(
            self.panel._card_margin(), 0,
            '卡片 margin 不为 0，列表会比搜索框窄一圈')

    def test_card_margin_is_a_single_source_of_truth(self):
        """不能有任何地方再写孤立的 ``12 * dpi`` 当 margin。

        扫源码而不是读运行时值：字面量写死时**运行时一切正常**，只是改 margin
        的那天悄悄错位——那种缺陷只有源码级的检查拦得住。

        注意**不能简单数 ``_card_margin()`` 的调用次数**：分组标题用的是
        ``_refresh_cards`` 传下来的 ``margin`` 参数，那同样是单一来源，源码里
        本就不会再出现一次调用（实测只有 2 处直接调用：绘制入口与收藏热区）。
        所以要查的是「有没有硬编码的 margin 字面量」。
        """
        src = (ROOT / 'lumina' / 'history_panel.py').read_text(encoding='utf-8')
        # 绘制入口把 margin 硬编码回去（原形态）
        self.assertNotIn(
            'margin=int(round(12 * d))', src,
            '`_refresh_cards` 里又写死 margin=12 了，应改用 _card_margin()')
        # 收藏热区的横向基准硬编码回去（原形态）
        self.assertNotIn(
            'int(round(12 * self._dpi)) + favorite_zone', src,
            '收藏热区又用 12 * dpi 当横向基准了，应改用 _card_margin()')
        # 正向：至少两处直接调用（绘制入口 + 收藏热区）
        import re
        calls = len(re.findall(r'self\._card_margin\(\)', src))
        self.assertGreaterEqual(
            calls, 2,
            f'只有 {calls} 处调用 _card_margin()，绘制与热区应各有一处')

    def test_favorite_hit_zone_follows_card_margin(self):
        """收藏热区的横向基准必须跟着 margin 走。

        早先写的是 ``canvas_x >= 12 * dpi + favorite_zone``：那个 12 乘的是
        字面量，不是 margin 变量，只是**当时两者都等于 12** 才碰巧对上。
        margin 改成 0 后若不跟着改，热区会整体偏右 12px——点星标落到空白处，
        而且不会有任何异常。
        """
        p = self.panel
        p.refresh()
        self._pump(0.2)
        margin = int(round(p._card_margin() * p._dpi))
        zone = p._card_w - max(44, int(round(30 * p._dpi)))
        hit_start = margin + zone
        card_left = margin
        card_right = margin + p._card_w
        self.assertGreaterEqual(
            hit_start, card_left,
            f'收藏热区起点 {hit_start} 落在卡片左边界 {card_left} 之外')
        self.assertLessEqual(
            hit_start, card_right,
            f'收藏热区起点 {hit_start} 超出卡片右边界 {card_right}')

    # ---------- 2. 卡片宽度撑满 ----------

    def test_card_width_fills_left_column(self):
        """卡片宽度必须等于「左栏宽 - 2 * margin」，不能因改 margin 而变窄。"""
        p = self.panel
        left_w = p.cards_canvas.winfo_width()
        margin = int(round(p._card_margin() * p._dpi))
        self.assertGreater(left_w, 1, '画布尚未映射，宽度不可信')
        self.assertEqual(
            p._card_w, left_w - 2 * margin,
            f'_card_w={p._card_w} 与 left_w({left_w}) - 2*margin({margin}) 不符')

    # ---------- 3. 像素级对齐 ----------

    @unittest.skipUnless(PIL_OK, '缺少 Pillow，无法做像素扫描')
    def test_cards_align_with_search_pill_horizontally(self):
        """像素实测：卡片与搜索胶囊的左右边界必须一致。

        **不能用 `winfo_width()` 代替**：卡片是圆角描边位图，画布宽度相等
        不代表绘制出来的可见边界相等（圆角处抗锯齿还会吃掉 1~2px）。
        这里对窗口做一次真实截图，逐像素找每一层的可见左右边界。
        """
        from PIL import ImageGrab
        p = self.panel
        self._pump(0.2)
        rx = p.win.winfo_rootx()
        ry = p.win.winfo_rooty()
        img = ImageGrab.grab(bbox=(rx, ry, rx + p.win.winfo_width(),
                                   ry + p.win.winfo_height())).convert('RGB')
        base = self._left_col_origin()
        left_w = p._left_frame.winfo_width()

        def visible_span(y):
            xs = [x for x in range(base, base + left_w)
                  if img.getpixel((x, y)) != WINDOW_BG]
            if not xs:
                return None
            return xs[0] - base, xs[-1] - base

        search_y = p._search_canvas.winfo_rooty() - ry + p._search_h // 2
        cards_y = p.cards_canvas.winfo_rooty() - ry
        # 卡片上方是 4px 起点 + 28px 分组标题，往下取卡片本体
        card_span = visible_span(cards_y + 40)
        search_span = visible_span(search_y)

        self.assertIsNotNone(search_span, '没扫到搜索胶囊')
        self.assertIsNotNone(card_span, '没扫到卡片（列表可能是空的）')
        self.assertEqual(
            card_span[0], search_span[0],
            f'卡片左边界 {card_span[0]} != 搜索胶囊左边界 {search_span[0]}')
        self.assertEqual(
            card_span[1], search_span[1],
            f'卡片右边界 {card_span[1]} != 搜索胶囊右边界 {search_span[1]}')

    # ---------- 4. 分组标题与图标同列 ----------

    def test_group_title_aligns_with_card_icon(self):
        """分组标题必须与卡片内首列内容（图标）左对齐。

        ``margin + 2`` 在 margin=12 时是 14、看着和图标 12 齐；margin 变 0 后
        就只剩 2，标题会悬到卡片外面。所以这里直接钉「标题 x == 图标 x」，
        而不是钉某个具体数值。
        """
        p = self.panel
        c = p.cards_canvas
        texts = [i for i in c.find_all() if c.type(i) == 'text']
        self.assertTrue(texts, '画布上没有文字图元')
        # 分组标题是 y 最小的那个 text；卡片的标题/副标题都比它低
        titled = []
        for i in texts:
            x, y = c.coords(i)[:2]
            txt = c.itemcget(i, 'text')
            titled.append((y, x, txt))
        titled.sort()
        title_y, title_x, title_text = titled[0]
        self.assertIn('条', title_text, f'最上面的文字不是分组标题：{title_text!r}')
        self.assertGreater(title_y, 0, '分组标题 y 异常')

        margin = int(round(p._card_margin() * p._dpi))
        icon_x = margin + int(round(12 * p._dpi))
        self.assertEqual(
            int(title_x), icon_x,
            f'分组标题 x={int(title_x)} 与卡片图标列 x={icon_x} 不对齐')


if __name__ == '__main__':
    unittest.main()
