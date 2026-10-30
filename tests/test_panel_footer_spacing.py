"""底部状态行与详情栏上下两条固定条的高度回归。

对应需求：*「剪贴板下面边框高度是否可以缩减一点，右侧栏中上下比如安放按钮的
位置高度是不是可以缩减一点」*。

这两处「显宽」的成因**和顶栏那 48px 完全一样**：同色块连成一片。所以这里守的
不是「数值更小」，而是**三条不会报错的隐形撑高**——它们只会让界面默默变胖：

1. **Tk `Label` 默认 `bd=2` + `pady=1`**。8 号 YaHei 的 16px 文字行被撑到 22px，
   一个像素都看不出来（底纹与窗口同色），只是白白吃掉 6px。早先底部状态行就是
   22px 而不是 16px。这条**最容易在「重写这段布局」时被退回**，因为新写的
   `tk.Label(...)` 又不会带 `bd=0`。
2. **固定条高度与内容需求脱钩**。`_meta_wrap` 写死 58 而两行内容只需 47；
   `_actions` 写死 42 而按钮固定 30。两处都靠 `pack_propagate(False)` 强撑，
   改内容时高度不会自动跟随，于是越留越空。
3. **底部那段空白必须有个上限**。footer 的 bg 与窗口同为 `window_bg`，
   「footer 高 + 下方间距 + 窗口内缩」在视觉上是**一整块**，不是几段。

前两条用运行时几何断言，第三条用像素扫描——`winfo_height()` 只能证明「控件多高」，
证不出「用户看到多大一块空白」。
"""
import sys
import time
import tkinter as tk
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lumina.history_panel import DetailPane, HistoryPanel  # noqa: E402

try:
    _PROBE = tk.Tk()
    _PROBE.withdraw()
    _PROBE.destroy()
    TK_OK = True
except Exception:  # pragma: no cover - 仅在无图形环境时触发
    TK_OK = False

SKIP_REASON = '本机 Tk 不可用'


class _FakeDB:
    def get(self, *a, **k):
        return None

    def __getattr__(self, name):
        return lambda *a, **k: ([] if name.startswith(('list', 'recent')) else
                                0 if name == 'count' else None)


class _FakeUI:
    def __init__(self):
        self.db = _FakeDB()
        self.config = {}
        self.panel_visible = False


@unittest.skipUnless(TK_OK, SKIP_REASON)
class FooterAndActionBarTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.panel = HistoryPanel(self.root, tk, _FakeUI())
        self.panel.win.deiconify()
        self.panel.apply_mode()
        self._pump(0.3)
        # 造一条文本记录，让 _update_actions 真正把按钮建出来
        # （不建的话 _actions 是空的，按钮高度那条测不到任何东西）
        d = self.panel._detail
        d.row = {"id": 1, "kind": "text", "category": "text", "source": "s",
                 "content": "x" * 40, "created_at": 1667000000, "pinned": 0,
                 "data": None, "file_path": "", "preview": "x"}
        d.cat = "text"
        d._urls = []
        d._update_actions()
        self._pump(0.2)

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

    # ---------- 1. Label 的隐形撑高 ----------

    def test_footer_labels_have_no_hidden_border_padding(self):
        """底部状态行的 Label 必须显式 ``bd=0`` / ``pady=0``。

        这是**最隐蔽的一条**：Tk Label 默认 `bd=2`（上下各 2px 立体边框）+
        `pady=1`，把 8 号 YaHei 的 16px 文字行撑到 22px。底纹与窗口同色，
        肉眼看不出边框，只觉得「下面这块空白有点厚」。
        """
        footer = self.panel._cmp_status.master
        labels = [w for w in footer.winfo_children() if isinstance(w, tk.Label)]
        self.assertTrue(labels, '底部状态行里没有 Label')
        for lb in labels:
            self.assertEqual(
                int(lb.cget('bd')), 0,
                f'footer Label bd={lb.cget("bd")}，默认 2 会上下各撑 2px')
            self.assertEqual(
                int(lb.cget('pady')), 0,
                f'footer Label pady={lb.cget("pady")}，默认 1 会再撑 2px')

    def test_footer_row_height_matches_text_line(self):
        """footer 实际高度应贴着文字行（16px），不能被撑到 22px。

        不钉死具体数值：字体或 DPI 变化时行高本来就会变。断言的是
        **「没有多出 6px 的边框/内边距」**——22 - 16 = 6 正是 bd(2+2) + pady(1+1)。
        """
        footer = self.panel._cmp_status.master
        h = footer.winfo_height()
        # 8 号 YaHei 单行在 96 DPI 下是 16px；留 4px 余量给字体渲染差异
        self.assertLessEqual(
            h, 20,
            f'footer 高 {h}px 偏厚（文字行约 16px，多出的部分是边框/内边距）')

    def test_meta_labels_have_no_hidden_border_padding(self):
        """右栏元信息区两个 Label 同样不能带默认 bd。"""
        d = self.panel._detail
        for name, lb in (('_meta_title', d._meta_title), ('_meta', d._meta)):
            self.assertEqual(int(lb.cget('bd')), 0,
                             f'{name} bd={lb.cget("bd")}，会上下各撑 2px')
            self.assertEqual(int(lb.cget('pady')), 0,
                             f'{name} pady={lb.cget("pady")}，会再撑 2px')

    # ---------- 2. 固定条高度与内容需求自洽 ----------

    def test_meta_wrap_height_fits_content(self):
        """顶部元信息条的高度必须装得下两行，且不过度宽松。

        上下界同时卡：低于内容会截断「预览 / 元信息」，高于内容 + 余量
        就是白白留白（原值 58 而内容只需 47，多了 11px）。
        """
        d = self.panel._detail
        h = d._meta_wrap.winfo_height()
        self.assertEqual(h, d.META_H,
                         f'_meta_wrap 实际高 {h} != META_H {d.META_H}')
        need = (d._meta_title.winfo_reqheight() + d._meta.winfo_reqheight())
        self.assertGreaterEqual(
            h, need, f'META_H={h} 装不下两行内容所需 {need}px，会被截断')
        self.assertLessEqual(
            h, need + 16,
            f'META_H={h} 比内容需求 {need}px 宽松超过 16px，属于冗余留白')

    def test_action_bar_height_fits_buttons(self):
        """底部动作栏的高度必须装得下按钮，且按钮不贴边。

        按钮是固定高度的 Canvas、在 ``pack(side="left")`` 下交叉轴居中。
        栏高比按钮矮会裁掉按钮；高出太多则是上下各空一大截（原 42 vs 30，
        上下各空 6px）。
        """
        d = self.panel._detail
        bar = d._actions
        self.assertEqual(bar.winfo_height(), d.ACTION_H,
                         f'_actions 实际高 {bar.winfo_height()} != ACTION_H')
        buttons = list(bar.winfo_children())
        self.assertTrue(buttons, '动作栏里没有按钮，这条测不到东西')
        for btn in buttons:
            self.assertLessEqual(
                btn.winfo_height(), bar.winfo_height(),
                f'按钮高 {btn.winfo_height()} 超出动作栏高 {bar.winfo_height()}')
            self.assertGreaterEqual(
                btn.winfo_y(), 0,
                f'按钮顶边 {btn.winfo_y()} 越出动作栏')
            self.assertLessEqual(
                btn.winfo_y() + btn.winfo_height(), bar.winfo_height(),
                '按钮底边越出动作栏，会被裁剪')

    def test_action_buttons_are_vertically_centered(self):
        """按钮在动作栏里上下留白应大致相等（居中），不是一头贴边。"""
        d = self.panel._detail
        bar = d._actions
        btn = bar.winfo_children()[0]
        top_gap = btn.winfo_y()
        bottom_gap = bar.winfo_height() - (btn.winfo_y() + btn.winfo_height())
        self.assertLessEqual(
            abs(top_gap - bottom_gap), 2,
            f'按钮未居中：上 {top_gap}px / 下 {bottom_gap}px')

    # ---------- 3. 底部整块空白的高度上限 ----------

    def test_bottom_blank_block_is_compact(self):
        """窗口底边到 footer 底边之间那段同色空白不得过厚。

        footer 的 bg 与窗口同为 ``window_bg``，这段间距在视觉上和 footer
        连成一块。原来 footer 22(被 bd 撑厚) + 12(内缩 8 + 间距 4) = 34px。
        """
        p = self.panel
        win_h = p.win.winfo_height()
        root_y = p.win.winfo_rooty()
        footer = p._cmp_status.master
        footer_bottom = footer.winfo_rooty() + footer.winfo_height() - root_y
        gap = win_h - footer_bottom
        # gap 的构成是确定的：_cmp_root 内缩 8 + footer 的 pady 下间距 2 = 10。
        # **阈值必须卡在 10**，不能像早先那样留 12 的余量——留 12 时
        # 「pady 退回 4」这个变异体会正好落在边界内侧，测试就抓不到（实测漏过）。
        self.assertLessEqual(
            gap, 10,
            f'窗口底边到 footer 底边还有 {gap}px 同色空白（现在应为 10px）')

    def test_packing_paddings_are_tight(self):
        """三处 pack 间距必须保持收紧后的值。

        **几何断言抓不到这几处**：footer 的 pady 从 2 退回 4、``_actions``
        的下间距从 4 退回 6、``meta_text`` 的 pady 从 4 退回 6，都只是让
        某个框多 2px，落在「宽松一点也说得过去」的范围内，用阈值比较会
        被容忍（实测三个变异体全部漏过）。所以这里直接读 ``pack_info()``
        的实际间距——它是精确的，不做任何宽容。
        """
        p = self.panel
        d = p._detail

        def bottom_pady(widget):
            pady = widget.pack_info().get('pady', 0)
            if isinstance(pady, (tuple, list)):
                return int(pady[-1])
            return int(pady)

        footer = p._cmp_status.master
        self.assertLessEqual(
            bottom_pady(footer), 2,
            f'底部 footer 下间距 {bottom_pady(footer)}px，应收紧到 2px')

        self.assertLessEqual(
            bottom_pady(d._actions), 4,
            f'动作栏下间距 {bottom_pady(d._actions)}px，应收紧到 4px')

        meta_pady = d._meta_title.master.pack_info().get('pady', 0)
        if isinstance(meta_pady, (tuple, list)):
            meta_pady = max(int(v) for v in meta_pady)
        self.assertLessEqual(
            int(meta_pady), 4,
            f'元信息文字块 pady {meta_pady}px，应收紧到 4px')

    def test_whole_bottom_block_does_not_regress(self):
        """整块底部空白（窗口底 - footer 顶）不超过 30px。

        这是**用户观感的那一条**：footer 22 + 12 = 34px 时看着「下面很厚」。
        现在 16 + 10 = 26px。留 4px 余量给字体渲染，但不允许回到 34。
        """
        p = self.panel
        win_h = p.win.winfo_height()
        root_y = p.win.winfo_rooty()
        footer = p._cmp_status.master
        block = win_h - (footer.winfo_rooty() - root_y)
        self.assertLessEqual(
            block, 30, f'底部整块空白 {block}px，接近改前的 34px')
        self.assertGreater(block, 0)

    # ---------- 4. 常量本身 ----------

    def test_action_and_meta_heights_are_class_constants(self):
        """两个高度必须是类常量，不能回去当字面量。

        字面量散落在 ``Frame(height=58)`` 与 ``Frame(height=42)`` 里时，
        改一处漏一处不会有任何报错（布局只是「看起来不对」）。
        """
        self.assertTrue(hasattr(DetailPane, 'META_H'))
        self.assertTrue(hasattr(DetailPane, 'ACTION_H'))
        self.assertLess(DetailPane.ACTION_H, 42,
                        'ACTION_H 未收紧（原 42）')
        self.assertLess(DetailPane.META_H, 58,
                        'META_H 未收紧（原 58）')
        # 按钮固定 30 高，栏高必须大于它才不裁剪
        self.assertGreater(DetailPane.ACTION_H, 30,
                           'ACTION_H 不大于按钮高度，按钮会被裁剪')


if __name__ == '__main__':
    unittest.main()
