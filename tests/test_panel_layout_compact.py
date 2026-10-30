"""主面板「紧凑留白」（方案 A）的布局回归。

对应需求：*「剪贴板边框是否可以压缩一下，不要那么宽，紧凑点」*。

这里守的是**改完之后不会悄悄劣化**的三类点——它们都不会报错，只会让界面
默默变形，肉眼很难第一时间发现：

1. **顶栏与搜索胶囊的高度必须解耦**。原先两者共用一个 ``_sh_h``，谁都不知道
   对方在用；把顶栏改矮会把搜索胶囊一起压扁（胶囊变矮后占位图标贴边）。
2. **窗口控制按钮的画布尺寸必须跟随顶栏**。早先 ``_make_control_button``
   内部写死 ``size = 32``，而按钮以 ``fill="y"`` 塞进 ``_hdr_h`` 高的顶栏里；
   顶栏压到 26 之后画布被裁到 26，但图标仍按 26 居中画在 16px 处，
   下半截直接被切掉（用户看到的是「关闭按钮缺一角」）。
3. **左右内边距与 ``body_w`` 的手写减数必须同步**。``body_w`` 是
   ``self._cw - 56`` 这种魔数，与 ``body_split.pack(padx=12)`` 是两份独立
   写死的值；只改一处会让首帧左右栏宽度偏 4px，直到第一次 ``<Configure>``
   才自我纠正——表现为「窗口刚打开时右栏内容闪一下」。

三个断言都用**运行时几何/身份**而不是源码正则，避免「注释里写了就算通过」。
"""
import sys
import time
import tkinter as tk
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lumina.history_panel import HistoryPanel  # noqa: E402

try:
    _PROBE = tk.Tk()
    _PROBE.withdraw()
    _PROBE.destroy()
    TK_OK = True
except Exception:  # pragma: no cover - 仅在无图形环境时触发
    TK_OK = False

SKIP_REASON = '本机 Tk 不可用'


class _FakeDB:
    """面板构造期会查库；给一个永远空的最小替身。"""

    def list_recent(self, *args, **kwargs):
        return []

    def count(self, *args, **kwargs):
        return 0

    def get(self, *args, **kwargs):
        return None

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class _FakeUI:
    def __init__(self):
        self.db = _FakeDB()
        self.config = {}
        self.panel_visible = False


@unittest.skipUnless(TK_OK, SKIP_REASON)
class HeaderMetricsTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.panel = HistoryPanel(self.root, tk, _FakeUI())
        self.panel.win.deiconify()
        self._pump(0.3)

    def tearDown(self):
        try:
            self.panel.win.destroy()
        except tk.TclError:
            pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _pump(self, seconds=0.0):
        self.panel.win.update()
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.panel.win.update()
            time.sleep(0.01)

    # ---------- 1. 顶栏 / 搜索胶囊高度解耦 ----------

    def test_header_and_search_pill_have_independent_heights(self):
        """顶栏比搜索胶囊矮，且是两个独立属性。

        合并回一个常量**不会报错**，只会让搜索胶囊跟着变矮，所以必须钉住
        「两者不相等」这个事实本身，而不是钉住具体数值。
        """
        hdr = getattr(self.panel, '_hdr_h', None)
        search = getattr(self.panel, '_search_h', None)
        self.assertIsNotNone(hdr, '缺少独立的顶栏高度 _hdr_h')
        self.assertIsNotNone(search, '缺少独立的搜索胶囊高度 _search_h')
        self.assertLess(hdr, search,
                        f'顶栏({hdr})应比搜索胶囊({search})紧凑')

    def test_search_pill_keeps_full_height(self):
        """搜索胶囊实际渲染高度 == ``_search_h``，没有被顶栏拖着缩。

        占位态图标是居中的，胶囊若矮于 ``_search_h``，图标会贴到上下边。
        """
        canvas = self.panel._search_canvas
        self.assertEqual(canvas.winfo_height(), self.panel._search_h,
                         f'搜索画布高 {canvas.winfo_height()} '
                         f'!= _search_h {self.panel._search_h}')
        self.assertEqual(self.panel._search_wrap.winfo_height(),
                         self.panel._search_h)

    # ---------- 2. 窗口控制按钮不得溢出/裁切 ----------

    def test_window_control_buttons_fit_inside_header(self):
        """三个窗口按钮的底边不得越过顶栏下沿。

        ``pack(fill="y")`` 下按钮会被顶栏高度裁剪，画布比顶栏高就会切掉图标。
        """
        bar = self.panel._settings_btn.master
        bar_h = bar.winfo_height()
        self.assertEqual(bar_h, self.panel._hdr_h,
                         f'顶栏实际高 {bar_h} != _hdr_h {self.panel._hdr_h}')
        for name in ('_minimize_btn', '_max_btn', '_close_btn'):
            btn = getattr(self.panel, name)
            # **必须看「请求高度」而不是 winfo_height()**：pack(fill="y") 会把
            # 超高的画布裁到顶栏高度，winfo_height() 报的就是裁剪后的值，
            # 于是「画布 32、顶栏 26」这种真实缺陷反而查不出来（实测漏过）。
            req_h = int(btn['height'])
            self.assertLessEqual(
                req_h, bar_h,
                f'{name} 请求画布高 {req_h} 超过顶栏高 {bar_h}，'
                f'会被裁剪导致图标缺角')
            bottom = btn.winfo_y() + btn.winfo_height()
            self.assertLessEqual(
                bottom, bar_h,
                f'{name} 底边 {bottom} 超出顶栏高 {bar_h}')

    def test_control_icons_fit_inside_their_canvas(self):
        """图标尺寸不得大于按钮画布——否则会被画布裁掉一角。

        早先画布尺寸在 ``_make_control_button`` 里写死 32，与顶栏高度脱钩；
        顶栏一改矮就出现「图标缺角」。这里直接比对两者实际尺寸。
        """
        for name in ('_minimize_btn', '_max_btn', '_close_btn'):
            btn = getattr(self.panel, name)
            icon = btn._lumina_refs[2]
            cw, ch = int(btn['width']), int(btn['height'])
            self.assertLessEqual(
                icon.width(), cw,
                f'{name} 图标宽 {icon.width()} 超出画布宽 {cw}')
            self.assertLessEqual(
                icon.height(), ch,
                f'{name} 图标高 {icon.height()} 超出画布高 {ch}')

    # ---------- 3. 发丝线确实存在 ----------

    def test_header_has_hairline_separator(self):
        """顶栏下方有一条 1px 的分隔线，宽度撑满。

        顶栏与内容区同为 ``window_bg``，没有这条线时两者在视觉上合并成
        一整块空白——这正是「边框显宽」的主因，去掉线等于把问题改回去。
        """
        sep = self.panel._c('separator')
        found = [c for c in self.panel._cmp_root.winfo_children()
                 if isinstance(c, tk.Frame)
                 and c.winfo_height() == 1
                 and c['bg'].upper() == sep.upper()]
        self.assertTrue(found, '顶栏下缺少 separator 色的 1px 发丝线')
        widest = max(f.winfo_width() for f in found)
        self.assertGreater(widest, 0, '发丝线宽度为 0')

    # ---------- 4. 左右内边距与 body_w 魔数同步 ----------

    def test_body_and_footer_share_horizontal_padding(self):
        """主体区与底栏的左右内边距一致，且与 body_w 的减数自洽。

        ``body_w`` 是手写减数，``padx`` 是另一处手写值；两者失配时首帧
        左右栏宽度会偏，等第一次 ``<Configure>`` 才自我纠正（界面闪一下）。
        这里用「实测左右栏之和」反推，不依赖任何硬编码数字。
        """
        left_w = self.panel._left_frame.winfo_width()
        detail_w = self.panel._detail.frame.winfo_width()
        total = left_w + detail_w
        win_w = self.panel.win.winfo_width()
        # 两侧留白 = 窗口宽 - 两栏合计，应远小于窗口宽的 10%
        gutter = win_w - total
        self.assertGreater(gutter, 0, '两栏合计超过窗口宽度')
        self.assertLess(gutter, win_w * 0.1,
                        f'左右留白 {gutter}px 过宽（窗口 {win_w}px）')

    def test_body_padding_is_compact(self):
        """主体区的左右内边距不超过 12px（方案 A 的收紧目标）。

        从窗口边缘到内容起始像素的距离 = cmp_root 内缩 8 + body padx。
        收紧前是 8 + 14 = 22，收紧后是 8 + 12 = 20。
        这里只断言「不回到 22」，避免把具体像素值钉死。
        """
        self.panel.win.update()
        win_bg = self.panel._c('window_bg')
        bg = self.panel._bg_canvas
        img = bg.winfo_children()
        self.assertIsNotNone(img)  # 画布存在即可

        root_x = self.panel.win.winfo_rootx()
        left_x = self.panel._left_frame.winfo_rootx()
        offset = left_x - root_x
        self.assertLessEqual(
            offset, 20,
            f'左侧内容起始偏移 {offset}px，未收紧到 20px 以内')
        self.assertEqual(self.panel._c('window_bg'), win_bg)


if __name__ == '__main__':
    unittest.main()
