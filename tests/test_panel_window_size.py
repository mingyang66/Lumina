"""主面板默认窗口尺寸与 ``config.json`` 覆盖的回归。

对应需求：*「剪贴板默认宽高是不是可以适当调大一点」*——默认值从
1160x720 提到 1360x840，并允许 ``config.json`` 用 ``panel_width`` /
``panel_height`` 覆盖。

这里守的四类点都**不会报错**，只会让窗口悄悄用错尺寸，肉眼很难发现：

1. **高度必须按「工作区」算，不能按整屏**。整屏高含任务栏；按它兜底时
   调大默认高度会把窗口底边塞到任务栏底下（1366x768 上工作区约 720，
   整屏 768）。这条是最容易在重构中被改回去的，单独钉住。
2. **配置能覆盖，但非法值一律回落默认**。配置文件是用户手改的，
   一个拼错的 ``panel_width`` 不该让面板打不开，也不该把窗口撑爆。
3. **配置太小会压塌双栏布局**。左栏 ``LEFT_MIN=400`` + 详情
   ``DETAIL_MIN=380`` + 分隔与内边距，低于硬下限时左栏撑不住标题、
   详情区横向滚动条常驻。
4. **``apply_mode`` 的居中与夹紧同样按工作区**，且夹紧结果不得越过
   任务栏上沿。

第 1、4 条用 ``mock`` 改 ``_work_area`` 来模拟小屏，**不依赖本机分辨率**，
所以在 1920x1080 的机器上也能验证 1366x768 的兜底路径。
"""
import sys
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock

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

# 足够大的工作区：保证「配置覆盖」类用例不会被兜底夹紧干扰。
# 取 4K 量级，任何默认值 / 上限值都装得下。
ROOMY = (0, 0, 3840, 2160)


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
    def __init__(self, config=None):
        self.db = _FakeDB()
        # 传的是「用户手改的 config.json」内容，可能是 {} 也可能带脏值
        self.config = {} if config is None else config
        self.panel_visible = False


@unittest.skipUnless(TK_OK, SKIP_REASON)
class WindowSizeTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.panel = self._make_panel()
        self.panel.win.deiconify()
        self._pump(0.2)

    def tearDown(self):
        for target in (self.panel.win, self.root):
            try:
                target.destroy()
            except tk.TclError:
                pass

    # ---------- 辅助 ----------

    def _make_panel(self, config=None):
        return HistoryPanel(self.root, tk, _FakeUI(config))

    def _rebuild(self, config):
        """换一份 config 重建面板（配置只在构造期读一次）。"""
        try:
            self.panel.win.destroy()
        except tk.TclError:
            pass
        self.panel = self._make_panel(config)
        self.panel.win.deiconify()
        self._pump(0.2)
        return self.panel

    def _pump(self, seconds=0.0):
        self.panel.win.update()
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.panel.win.update()
            time.sleep(0.01)

    def _resolve(self, config, dpi=1.0, area=ROOMY):
        """在指定 config / 工作区下算初始尺寸（不重建真实窗口）。

        ``area`` 按**工作区**格式给 ``(x, y, 宽, 高)``（与 ``_work_area()``
        一致），这里转成 ``_window_size_limits()`` 的 ``(宽, 高, x, y)``。
        **两套顺序不同，混用会得到「宽=0」**——实测踩过，别直接透传。
        """
        panel = self._rebuild(config)
        ax, ay, aw, ah = area
        with mock.patch.object(panel, '_window_size_limits',
                               return_value=(aw, ah, ax, ay)):
            return panel._resolve_window_size(dpi)

    # ---------- 1. 默认尺寸 ----------

    def test_default_size_is_1360x840(self):
        """默认 1360x840（逻辑像素），由 ``_resolve_window_size`` 给出。

        只断言结果不断言常量：中间经过 DPI 乘法与工作区夹紧，
        直接比常量会让「夹紧逻辑被绕过」这种缺陷漏过去。
        """
        w, h = self._resolve({})
        self.assertEqual((w, h), (1360, 840))

    def test_default_size_is_larger_than_the_old_one(self):
        """默认值必须比旧版 1160x720 大，防止无意中改回去。

        这是一条**意图断言**：数值本身可以再调，但「调大」这个结论不能丢。
        """
        self.assertGreater(HistoryPanel.COMPACT_W, 1160)
        self.assertGreater(HistoryPanel.COMPACT_H, 720)

    def test_dpi_scales_both_dimensions(self):
        """DPI 缩放对宽高同时生效，且是四舍五入到整数。"""
        w, h = self._resolve({}, dpi=1.5)
        self.assertEqual((w, h), (2040, 1260))

    # ---------- 2. 配置覆盖 ----------

    def test_config_overrides_width(self):
        w, h = self._resolve({'panel_width': 1200})
        self.assertEqual(w, 1200)
        self.assertEqual(h, 840, '只覆盖宽时高应保持默认')

    def test_config_overrides_both(self):
        w, h = self._resolve({'panel_width': 1500, 'panel_height': 900})
        self.assertEqual((w, h), (1500, 900))

    def test_config_accepts_numeric_string(self):
        """手改 JSON 时容易写成字符串 ``"1400"``，应照样吃下。"""
        w, _h = self._resolve({'panel_width': '1400'})
        self.assertEqual(w, 1400)

    def test_config_accepts_float(self):
        """``1400.0`` 这种浮点写法按整数处理，不留下小数尺寸。"""
        w, _h = self._resolve({'panel_width': 1400.9})
        self.assertEqual(w, 1400)

    def test_invalid_config_falls_back_to_default(self):
        """非数字静默回落默认，**不抛错、不改尺寸**。

        配置文件是用户手改的；为一行拼错的值让剪贴板打不开是本末倒置。
        """
        for bad in ('abc', None, '宽一点', [1400], {'w': 1400}):
            with self.subTest(bad=bad):
                w, h = self._resolve({'panel_width': bad, 'panel_height': bad})
                self.assertEqual((w, h), (1360, 840),
                                 f'非法值 {bad!r} 未回落默认尺寸')

    def test_empty_config_falls_back_to_default(self):
        """空串是「没填」的同义词，不是 0。"""
        w, h = self._resolve({'panel_width': '', 'panel_height': '  '.strip()})
        self.assertEqual((w, h), (1360, 840))

    # ---------- 3. 上下限 ----------

    def test_tiny_config_is_clamped_to_hard_minimum(self):
        """``panel_width=100`` 必须被抬到硬下限，不能真按 100 开窗。

        低于下限时双栏布局被压塌：左栏撑不住标题、详情区横向滚动条常驻。
        """
        w, h = self._resolve({'panel_width': 100, 'panel_height': 100})
        self.assertEqual(w, HistoryPanel.MIN_PANEL_W)
        self.assertEqual(h, HistoryPanel.MIN_PANEL_H)

    def test_zero_config_is_clamped_to_hard_minimum(self):
        """``0`` 是最容易手滑写出来的值，同样要走下限。"""
        w, h = self._resolve({'panel_width': 0, 'panel_height': 0})
        self.assertEqual(w, HistoryPanel.MIN_PANEL_W)
        self.assertEqual(h, HistoryPanel.MIN_PANEL_H)

    def test_hard_minimum_covers_two_column_layout(self):
        """硬下限必须真的装得下双栏，而不只是一个「看起来差不多」的数字。

        ``LEFT_MIN`` / ``DETAIL_MIN`` 是运行时布局的硬约束；
        ``_left_col_width()`` 里还有 ``- 56`` 的内边距减数、分隔占 21。
        任何一处调大而 ``MIN_PANEL_W`` 没跟着走，下限就形同虚设。
        """
        need_w = (HistoryPanel.LEFT_MIN + HistoryPanel.DETAIL_MIN + 21 + 56)
        self.assertGreaterEqual(
            HistoryPanel.MIN_PANEL_W, need_w,
            f'MIN_PANEL_W={HistoryPanel.MIN_PANEL_W} 装不下双栏所需的 {need_w}')
        self.assertGreaterEqual(HistoryPanel.MIN_PANEL_H, 400,
                                'MIN_PANEL_H 过矮，列表与详情都放不下')

    def test_huge_config_is_capped(self):
        """``99999`` 不能真开一个 10 万像素宽的窗口。"""
        w, h = self._resolve({'panel_width': 99999, 'panel_height': 99999})
        self.assertLessEqual(w, HistoryPanel.COMPACT_W * 2)
        self.assertLessEqual(h, HistoryPanel.COMPACT_H * 2)

    # ---------- 4. 工作区 vs 整屏 ----------

    def test_size_limits_use_work_area_not_full_screen(self):
        """``_window_size_limits`` 必须返回工作区高度，而不是整屏高度。

        **这条是「窗口钻到任务栏底下」的直接防线**：早先 ``apply_mode``
        用 ``min(self._ch, winfo_screenheight())``，整屏 768 / 工作区 720
        的机器上默认高度一调大就压到任务栏下面。
        这里让工作区比整屏矮 48px（模拟任务栏），断言返回的是矮的那个。
        """
        screen_h = self.panel.win.winfo_screenheight()
        area = (0, 0, screen_h + 200, screen_h - 48)
        with mock.patch.object(HistoryPanel, '_work_area',
                               staticmethod(lambda: area)):
            aw, ah, ax, ay = self.panel._window_size_limits()
        self.assertEqual(ah, screen_h - 48,
                         '高度取了整屏而不是工作区')
        self.assertNotEqual(ah, screen_h)
        self.assertEqual((aw, ax, ay), (screen_h + 200, 0, 0))

    def test_broken_work_area_falls_back_to_full_screen(self):
        """工作区报 0 / 负数时（远程桌面、多屏未就绪）退整屏，不能得到 0。"""
        for bad in ((0, 0, 0, 0), (0, 0, -100, -100)):
            with self.subTest(bad=bad):
                with mock.patch.object(HistoryPanel, '_work_area',
                                       staticmethod(lambda b=bad: b)):
                    aw, ah, _ax, _ay = self.panel._window_size_limits()
                self.assertEqual(aw, self.panel.win.winfo_screenwidth())
                self.assertEqual(ah, self.panel.win.winfo_screenheight())

    def test_size_is_clamped_to_small_work_area(self):
        """1366x768 的小屏（工作区约 720 高）上，窗口高不得超过 720。"""
        w, h = self._resolve({}, area=(0, 0, 1366, 720))
        self.assertLessEqual(w, 1366)
        self.assertLessEqual(h, 720, '窗口高超出工作区，会压到任务栏')
        self.assertEqual(h, 720, '应占满可用高度而不是原样保留 840')

    def test_tiny_work_area_clamps_both_dimensions(self):
        w, h = self._resolve({}, area=(0, 0, 1024, 600))
        self.assertEqual((w, h), (1024, 600))

    # ---------- 5. apply_mode 的居中与夹紧 ----------

    def test_apply_mode_keeps_window_inside_work_area(self):
        """``apply_mode`` 之后窗口必须整块落在工作区内（不越过任务栏）。

        这条走**真实** ``apply_mode``，只把工作区换成 1366x720：
        读 ``winfo_x/y/width/height`` 反推边界，比解析 geometry 字符串稳。
        """
        area = (0, 0, 1366, 720)
        with mock.patch.object(HistoryPanel, '_work_area',
                               staticmethod(lambda: area)):
            self.panel.apply_mode()
            self._pump(0.2)
            self.panel.win.update_idletasks()
            x = self.panel.win.winfo_x()
            y = self.panel.win.winfo_y()
            w = self.panel.win.winfo_width()
            h = self.panel.win.winfo_height()
        self.assertLessEqual(x + w, area[0] + area[2],
                             f'窗口右边界 {x + w} 超出工作区右边界 '
                             f'{area[0] + area[2]}')
        self.assertLessEqual(y + h, area[1] + area[3],
                             f'窗口底边 {y + h} 越过工作区底边 '
                             f'{area[1] + area[3]}（任务栏会盖住窗口）')
        self.assertGreaterEqual(x, area[0])
        self.assertGreaterEqual(y, area[1])

    def test_apply_mode_centers_inside_work_area(self):
        """居中基准是工作区，不是整屏——否则多任务栏一侧会偏出几像素。"""
        area = (100, 50, 1200, 800)
        with mock.patch.object(HistoryPanel, '_work_area',
                               staticmethod(lambda: area)):
            self.panel.apply_mode()
            self._pump(0.2)
            self.panel.win.update_idletasks()
            x = self.panel.win.winfo_x()
            y = self.panel.win.winfo_y()
            w = self.panel.win.winfo_width()
            h = self.panel.win.winfo_height()
        # 允许 1px 的整数取整误差
        self.assertLessEqual(abs(x - (area[0] + (area[2] - w) // 2)), 1,
                             f'x={x} 未按工作区居中')
        self.assertLessEqual(abs(y - (area[1] + (area[3] - h) // 2)), 1,
                             f'y={y} 未按工作区居中')


if __name__ == '__main__':
    unittest.main()
