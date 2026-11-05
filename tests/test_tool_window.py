"""工具窗口的 UI 集成测试：外壳、主题、实时解析、复制、关闭。

需要真实 Tk；TTY 环境无 Tk 时整体跳过，避免 CI 报错。
"""
import base64
import ctypes
import itertools
import os
import random
import sys
import time
import tkinter as tk
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lumina import image_b64, taskbar  # noqa: E402
from lumina import tool_window as TW  # noqa: E402
from lumina.history_panel import HistoryPanel  # noqa: E402
from lumina.tool_window import (  # noqa: E402
    JSON_MODE_LABELS,
    MIN_H,
    MIN_W,
    TOOLS,
    WS_EX_APPWINDOW,
    WS_EX_TOOLWINDOW,
    WS_MAXIMIZEBOX,
    WS_MINIMIZEBOX,
    WS_SYSMENU,
    Base64ToImageWindow,
    DateTimeWindow,
    ImageToBase64Window,
    JsonParamWindow,
    JwtWindow,
    open_tool,
)

try:
    _PROBE = tk.Tk()
    _PROBE.withdraw()
    _PROBE.destroy()
    TK_OK = True
except Exception:  # pragma: no cover - 仅在无图形环境时触发
    TK_OK = False

SKIP_REASON = '本机 Tk 不可用'

WM_SETICON = 0x0080
WM_GETICON = 0x007F


def _icon_slots(hwnd):
    """读窗口两个图标槽位的 HICON，用于断言「最小化前后没变」。

    **必须用独立的 ``WinDLL`` 句柄**：``history_panel`` 已给模块级
    ``user32.GetWindowRect`` 设过自己的 RECT argtypes，共用句柄会让别处的
    类型校验串味。
    """
    user32 = ctypes.WinDLL('user32')
    user32.SendMessageW.restype = ctypes.c_ssize_t
    user32.SendMessageW.argtypes = [
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
    return tuple(
        user32.SendMessageW(ctypes.c_void_p(hwnd), WM_GETICON, index, 0)
        for index in (0, 1))


def _write_exstyle(hwnd, value):
    """直接改写扩展样式，用来模拟「样式被外部重设」的场景。"""
    user32 = ctypes.WinDLL('user32')
    setter = user32.SetWindowLongPtrW
    setter.restype = ctypes.c_void_p
    setter.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    setter(ctypes.c_void_p(hwnd), -20, ctypes.c_void_p(value))


class FakePanel:
    """只提供工具窗口需要的成员；真实历史面板构建成本太高。"""

    FONT_FAMILY = HistoryPanel.FONT_FAMILY

    def __init__(self, root, tk_mod):
        self.tk = tk_mod
        self.ui = type('U', (), {'config': {}})()
        self.win = tk_mod.Toplevel(root)
        self.win.withdraw()

    def _theme(self):
        # 固定深色，便于断言前景色必须是浅色（防止深色主题下白字 bug 回归）
        return {k: v[1] for k, v in HistoryPanel.THEME.items()}, True

    def _sys(self, name):
        return HistoryPanel.SYS_COLORS.get(
            name, HistoryPanel.SYS_COLORS['gray'])[1]

    def _font_path(self):
        return HistoryPanel._font_path(self)


@unittest.skipUnless(TK_OK, SKIP_REASON)
class ToolWindowTests(unittest.TestCase):
    """工具窗口。

    窗口由 ``tests/__init__.py`` 的全局挂钩设成全透明，跑测试不在屏幕上闪。
    例外是 :meth:`test_edge_pixels_actually_hit_the_zone_not_the_shell`——
    它用 ``winfo_containing`` 做屏幕命中测试，必须真可见。
    """

    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.panel = FakePanel(self.root, tk)
        self.created = []

    def tearDown(self):
        for window in self.created:
            window.close()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _open(self, cls):
        window = cls(self.panel)
        # 构造后登记进实例表：``open_tool`` 靠它做单实例复用，直接构造会绕过
        # 注册，导致同一测试里「直接建」和「open_tool 建」的窗口不一致。
        key = next((k for k, _l, c in TOOLS if c is cls), None)
        if key is not None:
            registry = getattr(self.panel, '_tool_windows', None)
            if registry is None:
                registry = {}
                self.panel._tool_windows = registry
            registry[key] = window
        self.created.append(window)
        # 无边框窗口的几何与控件坐标要跑一轮事件循环才被 Tk 算出来，
        # 否则 winfo_x()/wm_geometry() 全是 0，断言会得到误导性结果。
        window.win.update()
        return window

    def _type(self, window, text):
        window.input.delete('1.0', 'end')
        window.input.insert('1.0', text)
        window._refresh()

    def _all_texts(self, widget):
        """递归收集界面上以「文字」形式呈现的内容。

        只看 Label 与 Canvas 文字项——按钮文案（如「复制结果」）是操作名而非
        标题，不在本测试的检查范围内。
        """
        found = []
        for child in widget.winfo_children():
            if isinstance(child, tk.Label):
                text = child.cget('text')
                if text:
                    found.append(str(text))
            elif isinstance(child, tk.Canvas):
                found += [child.itemcget(item, 'text')
                          for item in child.find_all()
                          if child.type(item) == 'text']
            found += self._all_texts(child)
        return found

    def _descendants(self, widget):
        """递归取出所有后代控件（含自身之外的每一层）。"""
        found = []
        for child in widget.winfo_children():
            found.append(child)
            found += self._descendants(child)
        return found

    # ---------- 无边框外壳 + 顶部工具名 ----------
    def test_window_uses_custom_chrome_without_native_title_bar(self):
        """无边框窗口：原生标题栏必须去掉，否则用户仍会看到一条系统标题。"""
        window = self._open(JsonParamWindow)
        # 返回值在 Windows 上是 '1'/'true'，关闭时是 None——统一按字符串判断，
        # 避免直接 int() 得到误导性的 TypeError。
        self.assertIn(str(window.win.overrideredirect()).lower(), ('1', 'true'))

    def test_tool_name_shown_in_top_rail(self):
        """顶部必须显示对应工具名，否则用户不知道自己在哪个工具里。

        用户反馈过「弹框上面没有对应标题」——之前把标题连同原生标题栏一起删掉了。
        现在标题以次级文字色画在左上角信号条之后，**不是**系统标题栏。
        """
        for cls in (JsonParamWindow, JwtWindow):
            window = self._open(cls)
            label = window.title_label
            self.assertEqual(str(label.cget('text')), cls.title,
                             f'{cls.__name__} 顶部没有显示工具名')
            self.assertEqual(label.winfo_manager(), 'pack',
                             '标题没有参与布局，用户看不到')

    def test_tool_name_is_visible_and_uses_secondary_color(self):
        """标题要真的显示出来，且用次级色——不能抢正文的视线。"""
        window = self._open(JsonParamWindow)
        label = window.title_label
        self.assertGreater(label.winfo_width(), 1, '标题被压成 0 宽，实际看不见')
        self.assertGreater(label.winfo_height(), 1)
        self.assertEqual(str(label.cget('fg')).upper(),
                         window.T['label2'].upper(),
                         '标题应使用次级文字色，而不是与正文同色')

    def test_title_also_set_as_window_caption(self):
        """窗口标题（任务栏 / Alt+Tab 显示的名字）必须与工具名一致。"""
        for cls in (JsonParamWindow, JwtWindow):
            window = self._open(cls)
            self.assertEqual(window.win.title(), cls.title,
                             f'{cls.__name__} 的窗口标题与工具名不一致')

    def test_section_headings_still_absent(self):
        """「输入」「结果」这类分区小标题仍然不出现——只加工具名，不加分区标题。"""
        for cls in (JsonParamWindow, JwtWindow):
            window = self._open(cls)
            texts = self._all_texts(window.win)
            for banned in ('输入', '结果'):
                self.assertNotIn(banned, texts)

    def test_placeholder_replaces_removed_hint_text(self):
        """原来的标题说明被移除后，提示语改由状态栏在空输入时承担。"""
        window = self._open(JwtWindow)
        window._refresh()
        self.assertIn('不验签', window.status.cget('text'))

    def test_shell_canvas_and_close_button_present(self):
        window = self._open(JsonParamWindow)
        self.assertTrue(window.shell.winfo_exists())
        self.assertTrue(window._close_btn.winfo_exists())
        # 外壳背景图必须真正画出来（Pillow 缺失时会是 None）
        self.assertIsNotNone(window._shell_image)

    def test_three_window_controls_present_in_windows_order(self):
        """无边框不等于砍掉窗口控制：最小化/最大化/关闭必须都在，且顺序正确。"""
        window = self._open(JsonParamWindow)
        for name in ('_min_btn', '_max_btn', '_close_btn'):
            button = getattr(window, name)
            self.assertTrue(button.winfo_exists(), f'{name} 缺失')
        # x 递增即视觉顺序：最小化 → 最大化 → 关闭
        self.assertLess(window._min_btn.winfo_x(), window._max_btn.winfo_x())
        self.assertLess(window._max_btn.winfo_x(), window._close_btn.winfo_x())

    def test_control_buttons_are_glyphs_not_text(self):
        """控制按钮只画线条/方框图形，不能退化成文字按钮。"""
        window = self._open(JsonParamWindow)
        for name in ('_min_btn', '_max_btn', '_close_btn'):
            button = getattr(window, name)
            items = button.find_all()
            self.assertTrue(items, f'{name} 没有任何图形')
            kinds = [button.type(i) for i in items]
            # 叉线是两笔 line，方框是一个 rectangle——只要不是文字即可
            self.assertNotIn('text', kinds, f'{name} 不应是文字按钮')
            self.assertTrue(set(kinds) <= {'line', 'rectangle'},
                            f'{name} 图形类型异常：{kinds}')

    def test_minimize_keeps_borderless_without_rebuilding_hwnd(self):
        """最小化必须**不翻转 overrideredirect**：那会重建 HWND，图标和框位标志全丢。

        早先的实现是 ``overrideredirect(False)`` → ``iconify()`` → 唤回再翻回来。
        实测每次翻转都重建 HWND（``0x3a2e1480`` → ``0x559a07c8`` → ``0x1f40160c``），
        新窗口带着 Tk 重设的 ``WS_EX_TOOLWINDOW`` 回来，explorer 于是撤掉任务栏
        按钮——用户看到的就是「显示后图标消失、点击切换失效」。

        现在走 ``ShowWindow(SW_MINIMIZE)``，无边框标志全程不动、HWND 不变。
        """
        window = self._open(JsonParamWindow)
        hwnd_before = window._top_level_hwnd()
        flag_before = str(window.win.overrideredirect()).lower()

        window._min_btn.event_generate('<Button-1>')
        window.win.update()

        self.assertEqual(window.win.state(), 'iconic')
        # 无边框必须还在——翻转它就是重建 HWND
        self.assertIn(str(window.win.overrideredirect()).lower(),
                      ('1', 'true'),
                      '最小化翻转了 overrideredirect → HWND 会重建，任务栏按钮会丢')
        self.assertEqual(window._top_level_hwnd(), hwnd_before,
                         '最小化后 HWND 变了 → 图标与框位标志不会跟着搬过去')
        self.assertEqual(str(window.win.overrideredirect()).lower(), flag_before)
        self._restore(window)

    def _restore(self, window):
        """把窗口从最小化拉回来并恢复外壳，避免影响后续用例。"""
        try:
            window.win.deiconify()
            window._restore_chrome()
            window.win.update()
        except tk.TclError:
            pass

    def test_double_click_rail_toggles_maximize(self):
        """双击顶栏必须切换最大化/还原（Windows 原生习惯）。

        顶栏由三块拼成：``rail`` 容器、左侧信号条 ``mark``、标题 ``title_label``。
        ``<Double-Button-1>`` 只会送到「收到第一次 press」的那个控件，所以三块
        都得绑——只绑容器的话，点在标题文字上不会触发。

        这里分两段验证，避免依赖 Tk 的连击计时（阈值 500ms，测试里靠 sleep
        对齐既慢又不稳）：
        ① 三块控件都必须真的绑上处理函数，且调用它就切换状态；
        ② 还原方向单独测，确认第二次调用能回到未最大化。
        """
        window = self._open(JsonParamWindow)
        rail = window.title_label.master
        marks = [w for w in rail.winfo_children() if w.winfo_class() == 'Canvas']
        widgets = [rail, window.title_label, *marks[:1]]
        self.assertEqual(len(widgets), 3, '顶栏控件结构变了，请同步更新本用例')

        for widget in widgets:
            self.assertTrue(widget.bind('<Double-Button-1>'),
                            f'{widget.winfo_class()} 没有绑定双击')

        # 双击处理本身：一次最大化、再一次还原
        window._maxed = False
        window._normal_geom = '300x200+80+80'
        self.assertEqual(window._rail_double(), 'break')
        self.assertTrue(window._maxed, '第一次双击没有最大化')
        window._rail_double()
        self.assertFalse(window._maxed, '第二次双击没有还原')

    # ---------- 拖拽缩放 ----------
    def test_eight_resize_zones_exist_around_the_window(self):
        """四边 + 四角共 8 个热区必须都在，且光标形状按边/角区分。"""
        window = self._open(JsonParamWindow)
        zones = window._resize_edges
        self.assertEqual(sorted(zones), ['e', 'n', 'ne', 'nw', 's', 'se', 'sw', 'w'])

        for name in ('n', 's'):
            self.assertEqual(zones[name].cget('cursor'), 'sb_v_double_arrow',
                             f'{name} 边光标不是上下箭头')
        for name in ('w', 'e'):
            self.assertEqual(zones[name].cget('cursor'), 'sb_h_double_arrow',
                             f'{name} 边光标不是左右箭头')
        for name in ('nw', 'se'):
            self.assertEqual(zones[name].cget('cursor'), 'size_nw_se')
        for name in ('ne', 'sw'):
            self.assertEqual(zones[name].cget('cursor'), 'size_ne_sw')

    def test_resize_zones_cover_window_edges(self):
        """热区必须真的贴在窗口边缘——位置偏了用户就抓不到。

        这是回归守卫：早先只有一个 pack 在底部状态栏里的 14px 抓手，
        实测位置偏到「右边界内 8px、下边界内 18px」，用户完全抓不着。
        """
        window = self._open(JsonParamWindow)
        window.win.geometry('920x640+420+180')
        window.win.update()

        w = window.win.winfo_width()
        h = window.win.winfo_height()
        zones = window._resize_edges

        self.assertEqual(zones['n'].winfo_y(), 0, '上边热区没有贴住顶沿')
        self.assertEqual(zones['w'].winfo_x(), 0, '左边热区没有贴住左沿')
        self.assertEqual(zones['s'].winfo_y() + zones['s'].winfo_height(), h,
                         '下边热区没有贴住底沿')
        self.assertEqual(zones['e'].winfo_x() + zones['e'].winfo_width(), w,
                         '右边热区没有贴住右沿')
        self.assertEqual((zones['nw'].winfo_x(), zones['nw'].winfo_y()), (0, 0))
        self.assertEqual((zones['se'].winfo_x() + zones['se'].winfo_width(),
                          zones['se'].winfo_y() + zones['se'].winfo_height()),
                         (w, h))

    def test_edge_pixels_actually_hit_the_zone_not_the_shell(self):
        """边缘像素必须真的落到热区上，不能被外壳 Canvas 抢走。

        **这是不可省的一条**：``shell`` 是 ``pack(fill=both, expand=True)``
        铺满整窗的 Canvas，热区虽然 place 在它上面，实测未 ``lift()`` 时
        ``winfo_containing`` 在角上会判成 ``!canvas``（事件落到外壳，热区收
        不到）——而 ``winfo_x()/winfo_width()`` 那些几何断言全都照常通过，
        拦不住。必须按「命中归属」判。

        取样点里四角特意取**最靠角的 (1,1) / (w-2,1) / (1,h-2) / (w-2,h-2)**：
        窗口现在是尖角（``_apply_round_region`` 用 ``CreateRectRgn``），这几个
        点都有真实像素。若有人把区域裁剪改回 ``CreateRoundRectRgn``，半径 12
        的扇形会把 (1,1) 一带直接裁掉，``winfo_containing`` 返回 None，
        这条断言立刻 FAIL —— 它就是「尖角 vs 圆角」的回归守卫。
        """
        window = self._open(JsonParamWindow)
        window.win.geometry('920x640+420+180')
        # **必须可见**：`winfo_containing` 是对屏幕坐标做命中测试，而全透明的
        # layered window 不参与命中——实测 alpha=0 时 (1,1) 返回 None，
        # 这条会假红。只有本用例需要可见，跑完立刻压回透明。
        from tests.silent_ui import make_visible, silence
        make_visible(window.win)
        window.win.update()
        window.win.update_idletasks()

        w = window.win.winfo_width()
        h = window.win.winfo_height()
        if w < 200 or h < 200:      # 无图形环境下几何没结算，跳过
            self.skipTest('窗口几何未就绪')

        root_x = window.win.winfo_rootx()
        root_y = window.win.winfo_rooty()
        zone_of = {str(z): name for name, z in window._resize_edges.items()}

        def hit_name(x, y):
            widget = window.win.winfo_containing(root_x + x, root_y + y)
            return zone_of.get(str(widget)) if widget is not None else None

        probes = {
            'n': (w // 2, 2),
            's': (w // 2, h - 3),
            'w': (2, h // 2),
            'e': (w - 3, h // 2),
            # 四角是尖角，整块 12x12 都是真实像素，取**最角上的 (1,1)** 也能命中。
            # 这一条正是「尖角 vs 圆角」的回归守卫：圆角版本下 (1,1) 落在被
            # SetWindowRgn 裁掉的扇形外，winfo_containing 返回 None，会直接 FAIL。
            'nw': (1, 1),
            'ne': (w - 2, 1),
            'sw': (1, h - 2),
            'se': (w - 2, h - 2),
        }
        for name, (x, y) in probes.items():
            with self.subTest(zone=name):
                self.assertEqual(hit_name(x, y), name,
                                 f'({x},{y}) 没有落到 {name} 热区')

        # 正文区不能被热区覆盖——否则用户点边框附近的文字会变成改尺寸
        self.assertIsNone(hit_name(w // 2, h // 2), '窗口中心被热区盖住了')
        self.assertIsNone(hit_name(w // 2, 20), '顶栏被热区盖住了')
        silence(window.win)      # 用完立刻压回透明，后面的用例照旧不闪

    # ---------- 默认尺寸：四个工具必须统一 ----------
    def test_all_tools_share_one_default_size(self):
        """四个工具窗口的默认尺寸必须**完全一致**。

        用户原话：「剪贴板中工具的弹框现在有点小，**统一**调整大一点」。
        尺寸定义在 ``_ToolWindow`` 这一层、子类都不覆盖，所以想让它们不一致
        反而需要额外写代码——这个用例守的就是「别做那件事」。

        **断言的是「四者相等」而不是「等于 1120x780」**：具体数值是观感偏好，
        用户随时可能再让调大一点，钉死数值只会让下次调整多改一处。真正有
        价值的不变量是统一性，以及「比旧的 920x640 大」这个方向。
        """
        sizes = {}
        for _key, label, cls in TOOLS:
            window = self._open(cls)
            window.win.update()
            sizes[label] = (window.win.winfo_width(),
                            window.win.winfo_height())
        distinct = set(sizes.values())
        self.assertEqual(len(distinct), 1,
                         f'四个工具默认尺寸不一致：{sizes}')

        width, height = distinct.pop()
        self.assertGreater(width, 920, f'弹框没有被调大：{width}')
        self.assertGreater(height, 640, f'弹框没有被调高：{height}')

    def test_center_clamps_an_oversized_default_to_fit_the_screen(self):
        """默认尺寸比屏幕还大时，``_center`` 必须把它收窄到屏幕内。

        这条守的是**小屏兜底**：默认尺寸调大（1120x780）之后，在 1366x768
        这类小屏上就超过工作区了，必须靠 ``_center`` 里的 ``min(...)`` 收住，
        否则窗口会顶到屏幕边缘、右下角的缩放抓手甚至跑到屏幕外。

        **不能用「读当前窗口尺寸再和屏幕比」来写**——那是恒真断言：
        ``winfo_width()`` 读的是**收窄后**的值，无论 ``_center`` 里的
        ``min(...)`` 在不在，它永远都装得进屏幕。实测把这个用例写成那种形式
        后，把默认尺寸改成 3000x2000（远超屏幕）仍然全绿——完全测不到东西。
        所以这里**主动改大实例的声明值再重摆一次**，逼 ``_center`` 真正执行
        收窄逻辑，断言才有判别力。
        """
        window = self._open(JsonParamWindow)
        sw = window.win.winfo_screenwidth()
        sh = window.win.winfo_screenheight()

        window.width = sw + 500
        window.height = sh + 500
        window._center()
        window.win.update()

        self.assertLessEqual(window.win.winfo_width(), max(MIN_W, sw - 80),
                             '超宽默认值没有被收窄，窗口会探出屏幕')
        self.assertLessEqual(window.win.winfo_height(), max(MIN_H, sh - 120),
                             '超高默认值没有被收窄，窗口会探出屏幕')

    def _drag_edge(self, window, name, dx, dy):
        """模拟从某条边/角拖拽，返回窗口几何的实际变化量。"""
        class _Ev:
            def __init__(self, x, y):
                self.x_root, self.y_root = x, y

        before = (window.win.winfo_width(), window.win.winfo_height(),
                  window.win.winfo_x(), window.win.winfo_y())
        window._edge_press(_Ev(1000, 600), name)
        window._edge_move(_Ev(1000 + dx, 600 + dy))
        window.win.update()
        after = (window.win.winfo_width(), window.win.winfo_height(),
                 window.win.winfo_x(), window.win.winfo_y())
        return tuple(a - b for a, b in zip(after, before))

    def test_each_edge_resizes_along_its_axis(self):
        """每条边只改它自己那个方向；左/上边还要同步移动原点。

        只改尺寸不改原点是「窗口在飞」而不是「边在动」，所以左边与上边
        必须让原点反向补偿同样的量（下面期望值里的 -dx）。
        """
        cases = (
            ('e', (100, 0), (100, 0, 0, 0)),
            ('w', (100, 0), (-100, 0, 100, 0)),
            ('s', (0, 80), (0, 80, 0, 0)),
            ('n', (0, 80), (0, -80, 0, 80)),
            ('se', (60, 40), (60, 40, 0, 0)),
            ('nw', (-60, -40), (60, 40, -60, -40)),
            ('ne', (60, -40), (60, 40, 0, -40)),
            ('sw', (-60, 40), (60, 40, -60, 0)),
        )
        for name, (dx, dy), expect in cases:
            with self.subTest(edge=name):
                window = self._open(JsonParamWindow)
                window.win.geometry('920x640+420+180')
                window.win.update()
                got = self._drag_edge(window, name, dx, dy)
                self.assertEqual(got, expect, f'{name} 边缩放方向/原点不对')
                window.close()
                self.created.remove(window)

    def test_resize_clamps_to_minimum_without_drifting(self):
        """缩到下限后继续拖，窗口不能再变小，也不能飘走。

        夹紧时若原点仍按原始 dx 位移（而不是按「实际改掉的宽度」），
        窗口右边界会跟着左移——用户看到的是窗口一边缩一边飘。
        """
        window = self._open(JsonParamWindow)
        window.win.geometry('920x640+420+180')
        window.win.update()

        right_before = window.win.winfo_x() + window.win.winfo_width()
        self._drag_edge(window, 'w', 5000, 0)
        self.assertEqual(window.win.winfo_width(), MIN_W, '没有夹在最小宽度')
        self.assertEqual(window.win.winfo_x() + window.win.winfo_width(),
                         right_before, '夹紧后右边界飘走了')

        self._drag_edge(window, 'n', 0, 5000)
        self.assertEqual(window.win.winfo_height(), MIN_H, '没有夹在最小高度')

    def test_resize_zones_refuse_while_maximized(self):
        """最大化时不许改尺寸，且不能拿上一次遗留的起点继续算。

        回归守卫：早先 ``_edge_press`` 在最大化时直接 return 却不清
        ``_resize_from``，上一次拖拽残留的起点会被随后的 ``<B1-Motion>``
        当成有效状态，实测最大化窗口一拖就缩成 MIN_W。

        这里**直接置 ``_maxed`` 而不走 ``_toggle_maximize``**——后者自己也会
        清状态，会把 ``_edge_press`` 里的清理完全掩盖掉（注释掉它测试仍全绿，
        已实测）。两道防线要能分别守住。
        """
        window = self._open(JsonParamWindow)
        window.win.geometry('920x640+420+180')
        window.win.update()

        class _Ev:
            def __init__(self, x, y):
                self.x_root, self.y_root = x, y

        # ① 按下时必须清掉上次残留的状态
        window._resize_from = {'edge': 'e', 'x': 0, 'y': 0,
                               'geom': (920, 640, 420, 180)}
        window._maxed = True
        window._edge_press(_Ev(100, 100), 'e')
        self.assertIsNone(window._resize_from,
                          '最大化时按下边缘没有清掉缩放状态')

        # ② 即使状态还在，_edge_move 也不能动尺寸（第二道防线）
        window._resize_from = {'edge': 'e', 'x': 1000, 'y': 600,
                               'geom': (920, 640, 420, 180)}
        maxed_w = window.win.winfo_width()
        window._edge_move(_Ev(2000, 600))
        window.win.update()
        self.assertEqual(window.win.winfo_width(), maxed_w,
                         '最大化窗口被拖动了尺寸')
        window._maxed = False

    def test_grip_is_decorative_not_the_only_entry(self):
        """右下角抓手仍在（视觉提示），但八向热区才是真正的缩放入口。"""
        window = self._open(JsonParamWindow)
        canvases = [w for w in self._descendants(window.win)
                    if isinstance(w, tk.Canvas) and w.cget('cursor') == 'size_nw_se']
        self.assertTrue(canvases, '右下角缩放抓手不见了')
        # 抓手自身也接了事件，保证手指正好落在那 14px 上时仍可拖
        grip = [c for c in canvases if c.master is not window._resize_edges['se']]
        self.assertTrue(grip, '抓手没有挂在状态栏上')
        self.assertTrue(grip[0].bind('<B1-Motion>'), '抓手没有绑定拖拽')

    def test_restoring_from_minimize_keeps_geometry_and_chrome(self):
        """还原后尺寸不能变、无边框必须回来——取消边框期间 WM 会把窗口撑大。"""
        window = self._open(JsonParamWindow)
        before = window.win.wm_geometry()
        window._min_btn.event_generate('<Button-1>')
        window.win.update()
        window.win.deiconify()
        window._restore_chrome()          # 正常由 after(120) 触发
        window.win.update()
        self.assertEqual(window.win.wm_geometry(), before)
        self.assertIn(str(window.win.overrideredirect()).lower(), ('1', 'true'))

    def test_minimize_keeps_style_and_icon_stable(self):
        """最小化前后样式与图标必须**完全一致**。

        这是「显示后图标消失、点击切换失效」的根因回归测试：只要最小化过程
        重建了 HWND 或让 Tk 重设了扩展样式，``WS_EX_TOOLWINDOW`` 就会回来，
        任务栏按钮被撤掉、``WM_SETICON`` 的结果也一并丢失。

        正确行为：最小化 → 唤回 多轮往返后，APPWINDOW / 框位标志 / 两个图标
        槽位都保持原值。
        """
        if os.name != 'nt':
            self.skipTest('仅 Windows 有意义')
        window = self._open(JsonParamWindow)
        before = window.win.wm_geometry()
        hwnd = window._top_level_hwnd()
        ex_before = window._read_exstyle(hwnd)
        style_before = window._read_style(hwnd)
        icons_before = _icon_slots(hwnd)

        window._min_btn.event_generate('<Button-1>')
        window.win.update()
        self.assertEqual(window.win.state(), 'iconic')

        # 停一会儿：任何延迟定时器都该跑完了
        deadline = time.time() + 0.5
        while time.time() < deadline:
            window.win.update()
            time.sleep(0.01)

        self.assertEqual(window.win.state(), 'iconic')
        self.assertEqual(window._read_exstyle(hwnd), ex_before,
                         '最小化期间扩展样式变了 → 任务栏按钮会被撤掉')
        self.assertEqual(_icon_slots(hwnd), icons_before,
                         '最小化期间图标槽位变了 → 任务栏图标消失')

        # 唤回后一切照旧
        window.win.deiconify()
        window._restore_chrome()
        window.win.update()
        self.assertEqual(window.win.state(), 'normal')
        self.assertEqual(window.win.winfo_viewable(), 1,
                         '窗口从最小化唤回后不可见，用户会彻底找不到它')
        self.assertEqual(window.win.wm_geometry(), before)
        self.assertEqual(window._read_exstyle(hwnd), ex_before)
        self.assertEqual(window._read_style(hwnd), style_before)
        self.assertEqual(_icon_slots(hwnd), icons_before)

    def test_window_rect_goes_offscreen_when_minimized(self):
        """最小化后窗口矩形必须移到屏幕外（-32000），而不是停在任务栏上沿。

        「在任务栏上面」症状的量化断言：正常最小化的窗口 Win32 矩形是
        ``(-32000, -32000)``；一旦矩形还落在屏幕可见区域（y 接近工作区
        底部），就是那块悬浮碎片。
        """
        if os.name != 'nt':
            self.skipTest('仅 Windows 有意义')
        window = self._open(JsonParamWindow)
        window._min_btn.event_generate('<Button-1>')
        window.win.update()

        rect = window._window_rect()
        self.assertIsNotNone(rect)
        left, top = rect[0], rect[1]
        self.assertLess(left, -10000,
                        f'最小化后窗口没有移到屏幕外，left={left} → 会显示成任务栏上的碎片')
        self.assertLess(top, -10000,
                        f'最小化后窗口没有移到屏幕外，top={top} → 会显示成任务栏上的碎片')

    def test_taskbar_flag_restored_after_summon(self):
        """唤回后任务栏标志必须是 APPWINDOW，否则下次最小化就找不回来。"""
        if os.name != 'nt':
            self.skipTest('仅 Windows 有意义')
        window = self._open(JsonParamWindow)
        window._min_btn.event_generate('<Button-1>')
        window.win.update()
        window.win.deiconify()
        window.win.update()
        ex_style = window._read_exstyle(window._top_level_hwnd())
        self.assertTrue(ex_style & WS_EX_APPWINDOW,
                        '唤回后没重新声明任务栏归属')
        self.assertFalse(ex_style & WS_EX_TOOLWINDOW)

    def test_maximize_toggles_and_restores_geometry(self):
        """最大化往返：尺寸变化后必须精确回到原位。"""
        window = self._open(JsonParamWindow)
        before = window.win.wm_geometry()
        window._toggle_maximize()
        window.win.update()
        self.assertTrue(window._maxed)
        self.assertNotEqual(window.win.wm_geometry(), before)
        window._toggle_maximize()
        window.win.update()
        self.assertFalse(window._maxed)
        self.assertEqual(window.win.wm_geometry(), before)

    def test_maximize_icon_switches_to_restore_glyph(self):
        """最大化后按钮图形要变成「还原」双框，否则用户找不到还原入口。"""
        window = self._open(JsonParamWindow)
        single = len(window._max_btn.find_all())
        window._toggle_maximize()
        window.win.update()
        self.assertGreater(len(window._max_btn.find_all()), single)

    # ---------- 任务栏归属 ----------
    def test_window_claims_taskbar_instead_of_toolwindow(self):
        """无边框窗口必须出现在任务栏，否则最小化后用户找不回来。

        Tk 给 overrideredirect 窗口打的是 WS_EX_TOOLWINDOW（不进任务栏），
        必须换成 WS_EX_APPWINDOW。
        """
        if os.name != 'nt':
            self.skipTest('仅 Windows 有意义')
        window = self._open(JsonParamWindow)
        window._claim_taskbar()
        ex_style = window._read_exstyle(window._top_level_hwnd())
        self.assertTrue(ex_style & WS_EX_APPWINDOW,
                        '缺少 WS_EX_APPWINDOW，窗口不会进任务栏')
        self.assertFalse(ex_style & WS_EX_TOOLWINDOW,
                         '残留 WS_EX_TOOLWINDOW，任务栏仍会隐藏它')

    def test_window_style_allows_taskbar_toggle(self):
        """必须补 WS_MINIMIZEBOX / WS_MAXIMIZEBOX / WS_SYSMENU。

        无边框窗口是纯 WS_POPUP，三个框位标志全为 0。缺了它们，点任务栏按钮
        只会激活窗口，不会「点一下最小化、再点一下还原」。
        """
        if os.name != 'nt':
            self.skipTest('仅 Windows 有意义')
        window = self._open(JsonParamWindow)
        window._claim_taskbar()
        style = window._read_style(window._top_level_hwnd())
        self.assertTrue(style & WS_MINIMIZEBOX, '缺少 WS_MINIMIZEBOX')
        self.assertTrue(style & WS_MAXIMIZEBOX, '缺少 WS_MAXIMIZEBOX')
        self.assertTrue(style & WS_SYSMENU, '缺少 WS_SYSMENU')
        # 补框位标志不能把原生标题栏带出来（WS_CAPTION = WS_BORDER|WS_DLGFRAME）
        self.assertEqual(style & 0xC00000, 0,
                         '补样式后冒出了原生标题栏')

    def test_window_has_taskbar_icon(self):
        """窗口必须挂上任务栏图标。

        iconphoto 对无边框窗口无效（实测 WM_GETICON 恒为 0），只有
        iconbitmap(<ico>) 生效。用 WM_GETICON 直接确认拿到了非零 HICON。
        """
        if os.name != 'nt':
            self.skipTest('仅 Windows 有意义')
        window = self._open(JsonParamWindow)
        user32 = ctypes.WinDLL('user32')
        user32.SendMessageW.restype = ctypes.c_ssize_t
        user32.SendMessageW.argtypes = [
            ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
        hicon = user32.SendMessageW(
            ctypes.c_void_p(window._top_level_hwnd()), 0x007F, 0, 0)
        self.assertNotEqual(hicon, 0, '任务栏没有图标（WM_GETICON 为 0）')

    def test_taskbar_flag_survives_minimize_roundtrip(self):
        """最小化会先取消无边框，样式被重设；还原后必须重新声明。"""
        if os.name != 'nt':
            self.skipTest('仅 Windows 有意义')
        window = self._open(JsonParamWindow)
        window._min_btn.event_generate('<Button-1>')
        window.win.update()
        window.win.deiconify()
        window._restore_chrome()
        window.win.update()
        ex_style = window._read_exstyle(window._top_level_hwnd())
        self.assertTrue(ex_style & WS_EX_APPWINDOW)
        self.assertFalse(ex_style & WS_EX_TOOLWINDOW)

    def test_taskbar_claimed_again_on_every_map(self):
        """每次窗口显示都要重新声明任务栏归属。

        构建期设置会被 Tk 在映射时覆盖（实测构建后读回仍是 TOOLWINDOW），
        所以 `<Map>` 必须挂上 _claim_taskbar。这条用「人为把样式还原成
        TOOLWINDOW 再触发一次 Map」来验证——只测构建期调用的话，删掉 Map
        重声明测试仍会通过（已实测过这个漏洞）。
        """
        if os.name != 'nt':
            self.skipTest('仅 Windows 有意义')
        window = self._open(JsonParamWindow)
        hwnd = window._top_level_hwnd()
        # 故意打回 Tk 的默认样式，模拟「刚被 overrideredirect 重设」
        style = window._read_exstyle(hwnd)
        _write_exstyle(hwnd, (style & ~WS_EX_APPWINDOW) | WS_EX_TOOLWINDOW)
        self.assertFalse(window._read_exstyle(hwnd) & WS_EX_APPWINDOW)
        # 触发一次 Map（真实场景是窗口从最小化恢复/首次显示）
        window.win.event_generate('<Map>')
        window.win.update()
        self.assertTrue(window._read_exstyle(hwnd) & WS_EX_APPWINDOW,
                        '<Map> 没有重新声明任务栏归属')

    def test_window_keeps_position_after_creation(self):
        """窗口必须真正居中摆放，不能落在 (0,0)。

        `_center` 里若先 `update_idletasks()` 再用请求尺寸算几何，窗口映射后
        会被系统重置到 (0,0)——实测踩过。
        """
        window = self._open(JsonParamWindow)
        geometry = window.win.wm_geometry()
        x, y = geometry.split('+')[1:]
        self.assertNotEqual((int(x), int(y)), (0, 0),
                            f'窗口位置被重置为原点：{geometry}')

    def test_no_native_scrollbar_used(self):
        """原生 Scrollbar 在深色窗口里会露出白色凹槽，必须换成自绘细条。"""
        window = self._open(JsonParamWindow)

        def collect(widget):
            found = []
            for child in widget.winfo_children():
                found.append(child)
                found += collect(child)
            return found

        natives = [w for w in collect(window.win)
                   if isinstance(w, tk.Scrollbar)]
        self.assertEqual(natives, [])
        # 两侧都折行、横向永不溢出，所以两侧都只留纵向一条自绘细条。
        self.assertEqual(len(window.input._mini_scrolls), 1)
        self.assertEqual(len(window.output._mini_scrolls), 1)

    def test_both_panes_wrap_long_lines(self):
        """左右两侧都必须超宽自动换行，且**行为一致**。

        用户原话：「工具弹框右侧和左侧如果超过当前长度自动换行，右侧和左侧保持
        一致」。早先右栏是 ``wrap="none"`` + 横向滚动条（理由是保护缩进列对齐），
        用户明确要求放弃那个取舍。
        """
        window = self._open(JsonParamWindow)
        self.assertEqual(str(window.input.cget('wrap')), 'char',
                         '左侧输入框没有开启折行')
        self.assertEqual(str(window.output.cget('wrap')), 'char',
                         '右侧结果框没有开启折行，与左侧不一致')

    def test_input_wraps_at_width_so_no_horizontal_scroll(self):
        """左侧输入必须占满宽度就换行，长行不能横向溢出。"""
        window = self._open(JsonParamWindow)

        long_json = '{"' + 'a' * 400 + '": 1}'
        self._type(window, long_json)
        window.win.update()

        # 折行是**显示层**行为，不会新增换行符，所以 index('end') 仍是第 1 行；
        # 只有 displaylines 才能反映实际占了几个屏幕行。
        display_lines = window.input.count('1.0', 'end', 'displaylines')
        self.assertTrue(display_lines, '取不到 displaylines，Tk 版本可能过旧')
        self.assertGreater(int(display_lines[0]), 1, '超长行没有被折成多行')
        # 横向不溢出 -> 不存在横向滚动
        first, last = window.input.xview()
        self.assertAlmostEqual(first, 0.0, places=3)
        self.assertAlmostEqual(last, 1.0, places=3,
                               msg='折行后仍有横向溢出')
        # 折行只是显示效果，取回的文本必须还是原样一整行
        self.assertEqual(window.input.get('1.0', 'end-1c'), long_json,
                         '折行把换行符写进了实际内容')

    def test_output_wraps_long_lines_without_horizontal_overflow(self):
        """右侧同样不横向溢出——不能靠拖横向滚动条才能看到行尾。

        断言用 ``xview`` 到达 1.0 而不是只看 ``wrap`` 配置：只断言配置的话，
        将来有人把 ``wrap`` 改回 ``none`` 但保留 ``xscrollcommand``，配置断言
        会废话式通过/失败，而真正要守的是「行尾看得见」。
        """
        window = self._open(JsonParamWindow)
        long_value = '{"stack":"' + 'x' * 400 + '"}'
        self._type(window, long_value)
        window.win.update()

        first, last = window.output.xview()
        self.assertAlmostEqual(first, 0.0, places=3)
        self.assertAlmostEqual(last, 1.0, places=3,
                               msg='右侧结果仍横向溢出，行尾看不见')

        display_lines = window.output.count('1.0', 'end', 'displaylines')
        self.assertTrue(display_lines)
        self.assertGreater(int(display_lines[0]), 1, '右侧超长行没有折行')

    def test_both_panes_have_identical_width(self):
        """两栏正文宽度必须**完全相等**，否则同样文本在两栏折在不同位置。

        原先右栏用 ``padx=(GAP, 0)`` 让缝隙，导致右栏窄 10px（实测 440 vs 430）。
        实测 60 个字符的字符串：左栏 1 屏行、右栏 2 屏行——正是用户说的「不一致」。
        改成两栏各让一半缝隙后 435/435。
        """
        window = self._open(JsonParamWindow)
        window.win.update()
        self.assertEqual(window.input.winfo_width(), window.output.winfo_width(),
                         f'两栏宽度不等：左 {window.input.winfo_width()} '
                         f'/ 右 {window.output.winfo_width()}')

    def test_both_panes_wrap_at_the_same_column(self):
        """同一段文本在两栏必须折在**同一处**——逐长度扫描，不允许任何一处分歧。

        只比对一次 ``displaylines`` 不够：宽度差 10px 时，多数长度看起来一致，
        只有恰好跨过那 10px 的少数长度会分歧（实测 n=60 时左 1 行、右 2 行）。
        所以这里连续扫描一串长度，任何一处不同都算失败。
        """
        window = self._open(JsonParamWindow)
        window.win.update()
        for n in range(40, 121, 4):
            with self.subTest(length=n):
                window.input.delete('1.0', 'end')
                window.input.insert('1.0', 'x' * n)
                window._restore_original()
                window.win.update()   # displaylines 要跑一轮事件循环才算得准
                in_lines = int(window.input.count(
                    '1.0', 'end', 'displaylines')[0])
                out_lines = int(window.output.count(
                    '1.0', 'end', 'displaylines')[0])
                self.assertEqual(
                    in_lines, out_lines,
                    f'{n} 个字符时两侧折行不一致：左 {in_lines} 屏行 / 右 {out_lines} 屏行')

    def test_both_panes_share_width_and_height(self):
        """两侧内容区尺寸必须一致，且不能因为「第 2 行没有横向条」白留 10px。

        原先无条件 ``rowconfigure(2, minsize=_MiniScroll.LANE)``，折行后第 2 行
        没有任何控件，仍被撑出 10px（实测容器 551 高、Text 只有 540）。
        """
        window = self._open(JsonParamWindow)
        window.win.update()
        self.assertEqual(window.input.winfo_height(), window.output.winfo_height(),
                         '两侧正文高度不一致')
        for pane in (window.input, window.output):
            container = pane.master
            self.assertEqual(container.winfo_height(), pane.winfo_height() + 1,
                             '容器底部多留了空白（横向条那一行没清掉）')

    def test_self_drawn_scrollbar_thumb_follows_overflow(self):
        """无溢出时不画滑块，纵向溢出才出现；两者都不能抛异常。"""
        window = self._open(JsonParamWindow)
        ys = window.input._mini_scrolls[0]
        self._type(window, '{"a":1}')
        window.win.update()      # Text 的可视区在空闲时才重算，需跑一轮事件循环
        self.assertFalse(ys.canvas.find_withtag('thumb'))
        self._type(window, '\n'.join(f'{{"k{i}": {i}}}' for i in range(80)))
        window.win.update()
        self.assertTrue(ys.canvas.find_withtag('thumb'))

    def test_status_lamp_matches_status_kind(self):
        """状态灯颜色必须跟着成功/失败切换，否则等于装饰。"""
        window = self._open(JsonParamWindow)
        self._type(window, '{"a":1}')
        ok_color = window._lamp.itemcget(window._lamp_id, 'fill')
        self.assertEqual(ok_color.upper(), '#30D158')
        self._type(window, '{oops')
        bad_color = window._lamp.itemcget(window._lamp_id, 'fill')
        self.assertEqual(bad_color.upper(), '#FF453A')
        self.assertNotEqual(ok_color, bad_color)

    # ---------- 通用骨架 ----------
    def test_tool_registry_keys_and_order(self):
        """注册表顺序即下拉顺序。图片两个工具排在三个文本工具之后。

        这是**有意的分组**：JSON / JWT / 日期转换是「文本转换」，图片两个是
        「二进制转换」，中间那条界线让用户不必在一次扫视里比较两类东西。加新
        工具时想插队就要改这条，逼着人想一下分组是否还成立。
        """
        keys = [key for key, _label, _cls in TOOLS]
        self.assertEqual(keys,
                         ['json', 'jwt', 'datetime', 'img2b64', 'b642img'])

    def test_text_tools_come_before_binary_tools(self):
        """分组界线必须仍然成立：文本类在前、图片类在后。

        上面那条钉的是**具体顺序**，这条钉的是**分组依据**。两者不重复：
        把新工具插到末尾（`json/jwt/img2b64/b642img/datetime`）会让上面那条
        失败，但失败信息只说「顺序不对」，看不出真正的问题是「一个文本工具
        混进了图片组」。这条给出的是语义层面的诊断。
        """
        kinds = [(key, cls.uses_image_pane) for key, _l, cls in TOOLS]
        first_binary = next(i for i, (_k, is_img) in enumerate(kinds) if is_img)
        for key, is_img in kinds[first_binary:]:
            self.assertTrue(is_img,
                            f'{key} 是文本工具却排在了图片工具之后')
        for key, is_img in kinds[:first_binary]:
            self.assertFalse(is_img,
                             f'{key} 是图片工具却排在了文本工具之前')

    def test_registry_label_matches_window_title(self):
        """注册表里的下拉文案必须与窗口标题一致。

        同一个工具的名字在**三处**出现：`TOOLS` 里的下拉显示名、`cls.title`
        （既作窗口标题进任务栏，又作顶栏左上角可见标题）、以及文档。前两处是
        独立写死的字符串，改名时极容易只改一处——下拉写着「JSON 格式化」、
        窗口顶部却还是「JSON 参数」。这条把它们钉在一起。
        """
        for key, label, cls in TOOLS:
            with self.subTest(tool=key):
                self.assertEqual(label, cls.title,
                                 f'{key} 的下拉文案与窗口标题不一致')

    def test_every_tool_has_a_registered_icon(self):
        """注册表里每个工具都必须配了图标字形。

        漏登记时下拉不会报错——``TOOL_ICONS.get(key, ("?", "gray"))`` 会静默
        回退成灰色「?」，用户看到的就是一个意义不明的方块。变异验证时确实
        漏过：删掉 ``"img2b64"`` 那一行，全套 106 项测试依然全绿。

        同时钉住「字形非空、颜色是已知系统色」——空字形画出来是个空心框，
        同样没有信息量。
        """
        from lumina.history_panel import HistoryPanel
        icons = HistoryPanel.TOOL_ICONS
        for key, _label, _cls in TOOLS:
            with self.subTest(tool=key):
                self.assertIn(key, icons, f'{key} 没有配图标，下拉里会显示灰色 ?')
                mark, color = icons[key]
                self.assertTrue(mark, f'{key} 的字形是空的')
                self.assertLessEqual(len(mark), 3,
                                     f'{key} 的字形太长，方块里塞不下')
                self.assertIn(color, HistoryPanel.SYS_COLORS,
                              f'{key} 的配色不是已知系统色')

    def test_tool_icon_table_has_no_stale_entries(self):
        """反向：图标表里不能留着已经不存在的工具 key。

        只查「每个工具都有图标」会让删掉的工具在表里慢慢堆积——某天有人把
        ``json`` 改名，旧条目留在那里谁也不会发现。
        """
        from lumina.history_panel import HistoryPanel
        keys = {key for key, _label, _cls in TOOLS}
        extra = set(HistoryPanel.TOOL_ICONS) - keys
        self.assertFalse(extra, f'图标表里有不存在的工具：{sorted(extra)}')

    def test_tool_names_use_current_wording(self):
        """工具名措辞：**JSON 格式化** / **JWT 解密** / 图片双向转换。

        前两条是用户原话：「工具下拉框选项JSON参数-改为 JSON格式化
        JWT解析-JWT解密」。这是用户偏好而非技术判断（「解密」严格说只解码
        不验签，但那是文案选择），别再以"更准确"为理由改回「参数」「解析」。

        图片两条的措辞理由：
        - **「转」而不是「编码/解码」**——对用户来说这是同一次转换的两个方向，
          统一动词才看得出它们是一对。
        - **「Base64」居中在前者、在前者在后者**（图片转 Base64 / Base64 转
          图片），方向由词序自然读出，不需要「→」这类符号（下拉框里会很挤）。
        - **「Base64」大小写固定**，不写 base64 也不写 BASE64：官方写法就是这个。
        """
        labels = {key: label for key, label, _cls in TOOLS}
        self.assertEqual(labels['json'], 'JSON 格式化')
        self.assertEqual(labels['jwt'], 'JWT 解密')
        self.assertEqual(labels['img2b64'], '图片转 Base64')
        self.assertEqual(labels['b642img'], 'Base64 转图片')

    def test_open_tool_rejects_unknown_key(self):
        with self.assertRaises(ValueError):
            open_tool(self.panel, 'nope')

    def test_open_tool_returns_expected_window(self):
        json_window = open_tool(self.panel, 'json')
        jwt_window = open_tool(self.panel, 'jwt')
        img_window = open_tool(self.panel, 'img2b64')
        b64_window = open_tool(self.panel, 'b642img')
        self.created += [json_window, jwt_window, img_window, b64_window]
        self.assertIsInstance(json_window, JsonParamWindow)
        self.assertIsInstance(jwt_window, JwtWindow)
        self.assertIsInstance(img_window, ImageToBase64Window)
        self.assertIsInstance(b64_window, Base64ToImageWindow)

    # ---------- ESC = 最小化到任务栏 ----------
    def test_escape_minimizes_instead_of_closing(self):
        """工具弹框的 ESC 必须是**最小化**，不是关闭。

        用户原话：「现在按 ESC 键剪贴板和弹框都是直接关闭了，改成最小化到
        任务栏」。关闭仍走顶栏那个叉（``close()``）。

        **判据不能用 ``winfo_viewable()``**：最小化后它也会变成 0，与隐藏
        分不开，必须用 ``IsIconic``。
        """
        window = self._open(JsonParamWindow)
        self.assertFalse(window.is_minimized())

        # **必须真的按键**，不能直接调 ``window.escape()``——那样只测到方法体，
        # 测不到绑定有没有改对。反向验证时把绑定改回 ``close()``，只调方法的
        # 写法依然全绿，等于没守住。
        window.win.focus_force()
        window.win.update()
        window.win.event_generate('<Escape>')
        window.win.update()
        time.sleep(0.35)
        window.win.update()

        self.assertFalse(window._closed, 'ESC 把窗口销毁了，应该是最小化')
        self.assertTrue(window.win.winfo_exists(), 'ESC 后窗口被销毁')
        self.assertTrue(window.is_minimized(),
                        'ESC 后没有最小化到任务栏')

    def test_escape_binding_targets_escape_not_close(self):
        """绑定本身必须指向 ``escape``，不能退回 ``close``。

        与上一条互补：上一条测行为（真按键），这条直接查绑定表，即使某天
        事件投递在无焦点环境下失效，也不会让「ESC 又变回关闭」悄悄回归。
        """
        window = self._open(JsonParamWindow)
        bound = window.win.bind('<Escape>')
        self.assertTrue(bound, 'ESC 没有绑定任何处理链')
        # 绑定串里出现的是被调方法的 Tcl 命令名，形如
        # ``... if {"[<bound method ...>]" == "break"} break``。
        # 直接触发一次，验证它确实走到最小化而非销毁。
        before_exists = bool(window.win.winfo_exists())
        window.win.event_generate('<Escape>')
        window.win.update()
        time.sleep(0.3)
        window.win.update()
        self.assertTrue(before_exists)
        self.assertFalse(window._closed, 'ESC 绑定走到了 close，应该是最小化')

    def test_escape_keeps_taskbar_flags_and_icon(self):
        """最小化期间任务栏归属不能丢，否则用户点不回来。"""
        window = self._open(JsonParamWindow)
        window.win.update()
        hwnd_before = taskbar.top_level_hwnd(window.win)
        ex_before = taskbar.read_style(hwnd_before, taskbar.GWL_EXSTYLE)
        icons_before = _icon_slots(hwnd_before)

        window.escape()
        window.win.update()
        time.sleep(0.35)
        window.win.update()

        self.assertEqual(taskbar.top_level_hwnd(window.win), hwnd_before,
                         'ESC 最小化后 HWND 变了 → 任务栏按钮与图标会丢')
        self.assertEqual(
            taskbar.read_style(hwnd_before, taskbar.GWL_EXSTYLE), ex_before,
            'ESC 最小化后扩展样式变了')
        self.assertEqual(_icon_slots(hwnd_before), icons_before,
                         'ESC 最小化后任务栏图标丢了')

    def test_summon_restores_minimized_tool_window(self):
        """``summon`` 能把最小化窗口唤回；``SW_SHOW`` 对最小化是无效的。"""
        window = self._open(JsonParamWindow)
        window.escape()
        window.win.update()
        time.sleep(0.35)
        window.win.update()
        self.assertTrue(window.is_minimized())

        self.assertTrue(window.summon())
        window.win.update()
        time.sleep(0.4)
        window.win.update()

        self.assertFalse(window.is_minimized(), 'summon 没把窗口唤回来')
        self.assertTrue(window.win.winfo_viewable(), '唤回后仍不可见')

    # ---------- 单实例：ESC 最小化之后最容易踩的坑 ----------
    def test_open_tool_reuses_minimized_window_instead_of_duplicating(self):
        """同一工具只能有一个窗口实例。

        ESC 改成最小化之后这条变关键了：最小化的窗口仍挂在任务栏上，若再从
        工具菜单点一次就新建第二个，用户会看到两个同名窗口、各存着一份输入。
        正确行为是**唤回原实例**。
        """
        first = self._open(JsonParamWindow)
        first.input.insert('1.0', '{"a":1}')
        first.escape()
        first.win.update()
        time.sleep(0.3)
        first.win.update()
        self.assertTrue(first.is_minimized())

        second = open_tool(self.panel, 'json')
        self.created.append(second)
        second.win.update()
        time.sleep(0.35)
        second.win.update()

        self.assertIs(second, first, '重复打开了第二个 JSON 窗口')
        self.assertFalse(second.is_minimized(), '复用时应把原窗口唤回')
        self.assertIn('{"a":1}', second.input.get('1.0', 'end'),
                      '唤回后原有输入内容丢了')

    def test_open_tool_creates_fresh_window_after_close(self):
        """点叉关掉之后必须能重新开一个——不能复用已销毁的实例。"""
        first = self._open(JsonParamWindow)
        key = 'json'
        self.assertIn(key, getattr(self.panel, '_tool_windows', {}))
        first.close()
        first.win.update()
        self.assertNotIn(key, getattr(self.panel, '_tool_windows', {}),
                         'close() 后没从实例表里摘掉，会留下悬空引用')

        second = open_tool(self.panel, 'json')
        self.created.append(second)
        self.assertIsNot(second, first, '关闭后重开拿到了已销毁的旧实例')
        self.assertTrue(second.win.winfo_exists())

    def test_text_widgets_use_light_foreground_on_dark_background(self):
        """深色主题下前景必须为浅色，否则文字不可见。"""
        window = self._open(JsonParamWindow)
        for text in (window.input, window.output):
            self.assertEqual(text.cget('bg').upper(), '#2C2C2E')
            self.assertEqual(text.cget('fg').upper(), '#F5F5F7')

    def test_output_is_read_only(self):
        window = self._open(JsonParamWindow)
        self.assertEqual(window.output.cget('state'), 'disabled')

    def test_empty_input_shows_hint_and_clears_output(self):
        window = self._open(JsonParamWindow)
        window._refresh()
        self.assertIn('粘贴', window.status.cget('text'))
        self.assertEqual(window.output.get('1.0', 'end-1c'), '')

    def test_footer_buttons_are_clear_and_copy_result(self):
        """底部只保留「复制结果」与「清空」两个按钮。

        「粘贴」按钮已移除：输入框本身就是可编辑的，Ctrl+V 直接粘贴即可，
        多一个按钮纯属冗余（用户明确要求去掉）。

        右侧按钮文案是「复制结果」——曾短暂改成「复制文字」，后又按用户要求
        改回「复制结果」，不要再为了「更准确」擅自改动。
        """
        window = self._open(JsonParamWindow)
        labels = [str(w.cget('text'))
                  for w in self._descendants(window.win)
                  if isinstance(w, tk.Button)]
        self.assertIn('复制结果', labels, '缺少「复制结果」按钮')
        self.assertIn('清空', labels)
        self.assertNotIn('粘贴', labels, '「粘贴」按钮应该已经移除')
        self.assertNotIn('复制文字', labels, '按钮文案应为「复制结果」')

    def test_long_status_never_squeezes_footer_buttons(self):
        """状态文案再长也不能把底栏按钮挤扁——**逐个长度扫**，不能只测一个。

        **这是用户报过的真实缺陷**：旧文案「左栏还没有图片。按 Ctrl+V 粘贴
        图片，或点「选择图片文件…」；若要把 Base64 还原成图片，请用「Base64
        转图片」。」宽 661px，把「清空」压成 16px（它需要 53px）——界面上只剩
        一个点不出所以然的小方块。根因是 Label 的请求宽度等于整句文字宽度，
        pack 按请求宽度分配，状态文字先把按钮的空间吃掉了。

        护栏是 Label 的 ``width=1``：请求宽度恒定 13px，``fill="x" +
        expand=True`` 仍铺满剩余空间，长文案从句尾裁掉。

        **必须扫一串长度**：实测（原实现）挤压只在「文案宽度略超可用空间」时
        发生——55~120 字必然触发；文字长到几千像素时 Tk 反而先把 Label 自己
        裁掉、按钮平安无事。单测一个长度很容易恰好落在安全区，用例就形同虚设。
        """
        window = self._open(ImageToBase64Window)
        window.win.deiconify()
        window.win.update()

        squeezed = []
        for n in range(10, 121, 5):
            window._set_status('状' * n, 'error')
            window.win.update()
            for widget in window.status.master.winfo_children():
                if widget.winfo_class() == 'Frame' \
                        and widget.winfo_width() < widget.winfo_reqwidth():
                    squeezed.append(n)
                    break
        self.assertEqual(squeezed, [],
                         f'这些字数下按钮被状态文字挤扁了：{squeezed}')

    def test_status_label_has_minimal_requested_width(self):
        """护栏本身：Label 必须 ``width=1``，否则请求宽度跟着文案走。

        上一条测「结果」，这条测「机制」。只测结果的话，将来有人把
        ``width=1`` 换成 ``wraplength`` 之类同样能防挤压的写法，机制变了但
        测试仍绿——那没问题；真正要拦的是**悄悄删掉这层保护**，那时上一条会红，
        这条给出更直接的定位。
        """
        window = self._open(JsonParamWindow)
        self.assertEqual(int(window.status.cget('width')), 1)

    def test_paste_shortcut_still_triggers_refresh(self):
        """移除「粘贴」按钮后，Ctrl+V 仍要能触发实时解析。

        按钮没了，但 <<Paste>> 的刷新绑定必须留着——否则用户粘贴后结果栏
        不更新，会以为工具坏了。这里直接执行绑定的回调来验证契约：
        ``event_generate('<<Paste>>')`` 要求窗口有焦点，而测试里的窗口是
        withdraw 状态，投递不到，只能绕过事件层。
        """
        window = self._open(JsonParamWindow)
        script = window.input.bind('<<Paste>>')
        self.assertTrue(script, '<<Paste>> 没有绑定刷新，粘贴后结果不会更新')

        # 走一遍绑定里声明的那条路径：填入内容 -> 触发 after 刷新
        window.input.insert('1.0', '{"a":1}')
        window.win.after(1, window._refresh)
        deadline = time.time() + 0.8
        while time.time() < deadline:
            window.win.update()
            time.sleep(0.01)
            if window.output.get('1.0', 'end-1c'):
                break
        self.assertNotEqual(window.output.get('1.0', 'end-1c'), '',
                            '粘贴路径没有刷新结果栏')

    def test_clear_resets_input_and_output(self):
        window = self._open(JsonParamWindow)
        self._type(window, '{"a":1}')
        self.assertNotEqual(window.output.get('1.0', 'end-1c'), '')
        window._clear()
        self.assertEqual(window.input.get('1.0', 'end-1c'), '')
        self.assertEqual(window.output.get('1.0', 'end-1c'), '')

    def test_copy_output_puts_result_on_clipboard(self):
        window = self._open(JsonParamWindow)
        self._type(window, '{"a":1}')
        window._copy_output()
        self.assertEqual(self.root.clipboard_get().strip(),
                         window.output.get('1.0', 'end-1c').strip())
        self.assertIn('已复制', window.status.cget('text'))

    def test_close_is_idempotent(self):
        window = self._open(JsonParamWindow)
        window.close()
        window.close()  # 二次关闭不应抛异常
        self.assertTrue(window._closed)

    def test_debounce_schedules_refresh(self):
        window = self._open(JsonParamWindow)
        window.input.insert('1.0', '{')
        window._on_input()
        self.assertIsNotNone(window._after_id)
        window._refresh()  # 立即执行并清空定时器
        self.assertIsNone(window._after_id)

    # ---------- JSON 参数 ----------
    def test_json_formats_output_without_field_summary(self):
        """结果栏只放格式化后的 JSON 本身，不附字段结构摘要。

        原先末尾会追加一段 ``▍ 字段结构`` 摊平清单，用户要求去掉
        （「JSON格式化右侧的字段结构就不要了去掉」）。**不要再以「信息更全」
        加回来**——右侧本来就是完整且已缩进对齐的 JSON。
        """
        window = self._open(JsonParamWindow)
        self._type(window, '{"user":{"name":"张三","age":30}}')
        out = window.output.get('1.0', 'end-1c')
        self.assertIn('"name": "张三"', out)
        self.assertNotIn('字段结构', out, '字段结构摘要应已移除')
        self.assertNotIn('user.name', out, '字段摊平清单应已移除')
        # 输出应恰好等于纯格式化结果，不带任何附加段落
        self.assertEqual(out.strip(),
                         '{\n  "user": {\n    "name": "张三",\n    "age": 30\n  }\n}',
                         '结果里混入了格式化 JSON 之外的内容')
        self.assertEqual(window.status.cget('fg').upper(), '#30D158')

    def test_json_invalid_input_reports_error_without_crashing(self):
        window = self._open(JsonParamWindow)
        self._type(window, "{'a':1}")
        self.assertEqual(window.output.get('1.0', 'end-1c'), '')
        self.assertEqual(window.status.cget('fg').upper(), '#FF453A')

    def test_json_duplicate_keys_flagged(self):
        window = self._open(JsonParamWindow)
        self._type(window, '{"a":1,"a":2}')
        self.assertIn('重复键', window.status.cget('text'))
        self.assertEqual(window.status.cget('fg').upper(), '#FF453A')

    def test_json_python_literal_not_accepted(self):
        # True/None 是 Python 字面量，不是 JSON
        window = self._open(JsonParamWindow)
        self._type(window, '{"a": True}')
        self.assertEqual(window.status.cget('fg').upper(), '#FF453A')

    # ---------- JSON 下拉：从剪贴板详情栏平移过来 ----------
    def test_json_mode_labels_match_detail_pane(self):
        """两边的模式文案**必须是同一个对象**，不是「两份碰巧一样」。

        这条测试的前身是按**交集**比对两个各自维护的字典。整合到
        ``json_modes`` 之后，交集比对已经没有判别力了（同一份对象自己比自己
        恒等），所以改成断言 identity：谁再在本地抄一份，立刻失败。

        同时确认详情栏也吃到了 ``escape_java``——此前它是本窗口独有的第五种，
        现在两边同步（用户要求「工具弹框中JSON转义功能在剪贴板中的JSON工具中
        同步」）。
        """
        from lumina import json_modes
        from lumina.history_panel import DetailPane
        self.assertIs(JSON_MODE_LABELS, json_modes.MODE_LABELS)
        self.assertIs(DetailPane.JSON_MODE_LABELS, json_modes.MODE_LABELS)
        self.assertIn('escape_java', DetailPane.JSON_MODE_LABELS,
                      '详情栏没同步到 Java 转义')

    def test_json_window_has_menu_button_left_of_footer_actions(self):
        """底部要有「JSON ▾」入口，且落在「清空 / 复制结果」左侧。"""
        window = self._open(JsonParamWindow)
        anchor = window._json_anchor
        self.assertEqual(str(anchor.cget('text')), 'JSON ▾')
        self.assertTrue(anchor.winfo_exists())
        self.assertEqual(anchor.winfo_manager(), 'pack')
        clear = next(w for w in self._descendants(window.win)
                     if isinstance(w, tk.Button) and str(w.cget('text')) == '清空')
        self.assertLess(anchor.winfo_rootx(), clear.winfo_rootx(),
                        '「JSON ▾」应落在清空/复制结果左侧')

    def test_jwt_window_has_no_json_menu(self):
        """JWT 没有可转换的模式，不该多一个空下拉。"""
        window = self._open(JwtWindow)
        self.assertFalse(hasattr(window, '_json_anchor'),
                         'JWT 窗口不该出现 JSON 下拉入口')
        labels = [str(w.cget('text'))
                  for w in self._descendants(window.win)
                  if isinstance(w, tk.Button)]
        self.assertNotIn('JSON ▾', labels)

    def _open_popup(self, window):
        window._json_menu()
        window.win.update()
        return window._popup

    def test_json_menu_items_come_from_the_shared_builder(self):
        """菜单项与详情栏**同一份代码**生成，不写死文案。

        这条测试的前身是 ``test_json_menu_has_same_four_items_as_detail_pane``，
        逐字断言 ``['格式化', '压缩', '展开转义', 'Java 转义', '恢复原文']``。
        整合到 ``json_modes`` 之后，再写死字面量就是又造了一份平行定义——
        改成按 ``MODE_ITEMS`` 推导，模式增删时这里不用跟着改。

        「恢复原文」永远在最后，且靠 ``kind=='restore'``（而非下标）让
        ``JsonPopup`` 画分隔线。
        """
        from lumina import json_modes
        window = self._open(JsonParamWindow)
        try:
            popup = self._open_popup(window)
            self.assertIsNotNone(popup, '没有弹出下拉')
            order = json_modes.MODE_ORDER
            self.assertEqual([item['label'] for item in popup.items],
                             [json_modes.MODE_ITEMS[m][0] for m in order]
                             + [json_modes.RESTORE_LABEL])
            self.assertEqual([item['hint'] for item in popup.items],
                             [json_modes.MODE_ITEMS[m][1] for m in order] + [''])
            self.assertEqual([item['kind'] for item in popup.items],
                             [*order, 'restore'])
        finally:
            window._close_popup()

    def test_json_menu_keeps_all_transforms_enabled(self):
        """工具弹框同步计算，**任何时刻**都不该禁用转换项。

        回归：``_json_menu`` 一度误把 ``busy`` 传成 True（照抄详情栏的签名），
        导致下拉里四项全是灰的——点不动，但没有任何报错。原有测试只断言了
        「恢复原文可用」和勾选态，全灰也照样通过，所以单独守这条。
        """
        from lumina import json_modes
        window = self._open(JsonParamWindow)
        try:
            for mode in (None, *json_modes.MODE_ORDER):
                window._close_popup()
                if mode is not None:
                    window._set_mode(mode)
                popup = self._open_popup(window)
                with self.subTest(mode=mode):
                    self.assertEqual(
                        [item['enabled'] for item in popup.items],
                        [True] * len(json_modes.MODE_ORDER) + [True],
                        '转换项被禁用了：工具弹框是同步执行，不该有 busy 态')
        finally:
            window._close_popup()

    def test_escape_java_item_has_an_icon(self):
        """``JsonPopup._icon`` 只认已知 kind，未知 kind 静默不画。

        漏补图标不会有任何报错，只会让下拉里那一项前面空一块——所以直接断言
        该 kind 真的画了图元，而不是「没抛异常」。
        """
        window = self._open(JsonParamWindow)
        try:
            popup = self._open_popup(window)
            canvas = popup.canvas
            before = len([i for i in canvas.find_all()
                          if canvas.type(i) == 'line'])
            canvas.delete('all')
            popup._icon('escape_java', 22, 15, '#FFFFFF')
            drawn = [i for i in canvas.find_all() if canvas.type(i) == 'line']
            self.assertGreater(len(drawn), 0, 'escape_java 图标没有画任何线条')
            # 对照组：未知 kind 必须画 0 条——证明上面不是「画布上本来就有线」。
            canvas.delete('all')
            popup._icon('no-such-kind', 22, 15, '#FFFFFF')
            self.assertEqual(len([i for i in canvas.find_all()
                                  if canvas.type(i) == 'line']), 0)
            self.assertGreaterEqual(before, 0)  # 保留变量，便于排查时打印
        finally:
            window._close_popup()

    def test_json_menu_marks_current_mode_selected(self):
        """默认勾「格式化」；切到别的模式后勾选跟着走。"""
        from lumina import json_modes
        order = json_modes.MODE_ORDER
        window = self._open(JsonParamWindow)
        try:
            for mode in order:
                window._close_popup()
                window._set_mode(mode)
                popup = self._open_popup(window)
                with self.subTest(mode=mode):
                    self.assertEqual([item['selected'] for item in popup.items],
                                     [m == mode for m in order] + [False])
                    # 已转换态下「恢复原文」必须可用。
                    self.assertTrue(popup.items[-1]['enabled'])
        finally:
            window._close_popup()

    def test_json_menu_second_click_closes_instead_of_stacking(self):
        """再点一次同一按钮要收起，不能叠出第二个弹窗。"""
        window = self._open(JsonParamWindow)
        popup = self._open_popup(window)
        self.assertIsNotNone(popup)
        window._json_menu()
        self.assertIsNone(window._popup, '第二次点击没有收起下拉')

    def test_switch_to_compress_outputs_single_line(self):
        window = self._open(JsonParamWindow)
        self._type(window, '{\n  "a": 1,\n  "b": [1, 2]\n}')
        window._set_mode('compress')
        out = window.output.get('1.0', 'end-1c')
        self.assertEqual(out.strip(), '{"a":1,"b":[1,2]}')
        self.assertNotIn('\n', out.strip())
        self.assertIn('已压缩', window.status.cget('text'))

    def test_switch_to_expand_turns_escaped_newlines_into_real_ones(self):
        window = self._open(JsonParamWindow)
        self._type(window, '{"stack":"a\\n  at b\\nc"}')
        window._set_mode('expand')
        out = window.output.get('1.0', 'end-1c')
        self.assertIn('a\n  at b\nc', out, '展开转义没有把 \\n 变成真实换行')
        self.assertIn('已展开转义', window.status.cget('text'))

    def test_restore_original_echoes_input_verbatim(self):
        """恢复原文：原样回显输入，不做任何结构改动。"""
        window = self._open(JsonParamWindow)
        raw = '{\n  "a": 1\n}'
        self._type(window, raw)
        window._set_mode('compress')
        self.assertNotEqual(window.output.get('1.0', 'end-1c').strip(), raw)

        window._restore_original()
        self.assertEqual(window.output.get('1.0', 'end-1c').strip(), raw.strip())
        self.assertIn('原文', window.status.cget('text'))
        self.assertIsNone(window._mode)

    def test_restore_item_disabled_when_already_original(self):
        window = self._open(JsonParamWindow)
        window._restore_original()
        try:
            popup = self._open_popup(window)
            self.assertFalse(popup.items[4]['enabled'],
                             '已在原文态时「恢复原文」应禁用')
        finally:
            window._close_popup()

    def test_switch_to_escape_java_outputs_java_string_body(self):
        """Java 转义：紧凑 JSON 再叠一层 Java 字面量转义，不含外层引号。"""
        window = self._open(JsonParamWindow)
        self._type(window, '{\n  "a": 1,\n  "b": ["x", "y"]\n}')
        window._set_mode('escape_java')
        out = window.output.get('1.0', 'end-1c').strip()
        self.assertEqual(out, r'{\"a\":1,\"b\":[\"x\",\"y\"]}')
        self.assertNotIn('\n', out, 'Java 转义结果必须是单行')
        self.assertFalse(out.startswith('"'), '不该带外层引号')
        self.assertIn('已转义', window.status.cget('text'))
        # 提示必须点明「两侧补双引号」——产物是字面量内容，直接贴进
        # setXxx(...) 会编译不过，这句是功能的一部分而不只是装饰。
        self.assertIn('补双引号', window.status.cget('text'))

    def test_switch_to_escape_java_escapes_quote_and_backslash(self):
        window = self._open(JsonParamWindow)
        self._type(window, r'{"q":"he said \"hi\"","p":"C:\\tmp"}')
        window._set_mode('escape_java')
        out = window.output.get('1.0', 'end-1c')
        self.assertIn(r'he said \\\"hi\\\"', out)
        self.assertIn(r'C:\\\\tmp', out)

    def test_no_field_summary_in_any_mode(self):
        """所有模式（含 Java 转义）的结果里都不能出现字段结构摘要。"""
        window = self._open(JsonParamWindow)
        self._type(window, '{"user":{"name":"张三","tags":["a","b"]}}')
        for mode, label in (('format', '格式化'), ('compress', '压缩'),
                            ('expand', '展开转义'),
                            ('escape_java', 'Java 转义')):
            with self.subTest(mode=mode):
                window._set_mode(mode)
                out = window.output.get('1.0', 'end-1c')
                self.assertNotIn('字段结构', out, f'{label}视图仍带字段结构')
                self.assertNotIn('user.name', out, f'{label}视图仍带字段摊平清单')
        window._restore_original()
        out = window.output.get('1.0', 'end-1c')
        self.assertNotIn('字段结构', out, '原文视图仍带字段结构')
        self.assertNotIn('user.name', out, '原文视图仍带字段摊平清单')

    def test_close_and_minimize_dismiss_the_popup(self):
        """弹窗是独立 Toplevel：窗口收起/销毁时必须一起收掉，否则会飘在桌面上。

        断言的是**弹窗 Toplevel 真的不存在了**，不是「引用被置 None」——后者
        两条路径都可能满足（``JsonPopup`` 自己绑了父窗口的 ``<Destroy>``），
        置 None 但窗口还留着才是真正会飘在桌面上的那种 bug。

        反向验证结论：只有**最小化**那条能拦住回退。关闭那条即使去掉
        ``close()`` 里的显式收尾也依然通过——销毁父窗口会触发弹窗自己的
        ``<Destroy>`` 回调。所以这条 subTest 对关闭路径是「防御性覆盖」，
        对最小化路径才是有判别力的守卫（最小化不销毁窗口，弹窗会真的留在屏幕上）。
        """
        for action in ('minimize', 'close'):
            with self.subTest(action=action):
                window = self._open(JsonParamWindow)
                popup = self._open_popup(window)
                self.assertIsNotNone(popup)
                popup_win = popup.win
                self.assertTrue(popup_win.winfo_exists())
                if action == 'minimize':
                    window.escape()
                    window.win.update()
                    time.sleep(0.3)
                    window.win.update()
                else:
                    window.close()
                    window.win.update()
                self.assertFalse(bool(popup_win.winfo_exists()),
                                 f'{action} 后下拉窗口仍在，会飘在桌面上')
                if action == 'minimize':
                    self.assertIsNone(window._popup, '最小化后引用还留着')
                    window.summon()
                    window.win.update()

    def test_mode_survives_typing_more_input(self):
        """切了压缩之后继续输入，仍按压缩展示——模式是窗口状态，不是一次性动作。"""
        window = self._open(JsonParamWindow)
        window._set_mode('compress')
        self._type(window, '{\n "a": 1\n}')
        self.assertEqual(window.output.get('1.0', 'end-1c').strip(), '{"a":1}')

    # ---------- JWT ----------
    def _token(self, payload, header=None):
        import json as jsonlib
        def enc(obj):
            raw = jsonlib.dumps(obj, ensure_ascii=False).encode('utf-8')
            return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')
        return f'{enc(header or {"alg": "HS256", "typ": "JWT"})}.{enc(payload)}.sig'

    def test_jwt_shows_header_payload_signature(self):
        window = self._open(JwtWindow)
        self._type(window, self._token({'sub': '42'}))
        out = window.output.get('1.0', 'end-1c')
        for part in ('Header', 'Payload', 'Signature', '"sub": "42"'):
            self.assertIn(part, out)

    def test_jwt_signature_labelled_as_unverified(self):
        """绝不能暗示签名已验证——本地没有密钥。"""
        window = self._open(JwtWindow)
        self._type(window, self._token({'sub': '42'}))
        self.assertIn('未校验', window.output.get('1.0', 'end-1c'))

    def test_jwt_accepts_bearer_prefix(self):
        window = self._open(JwtWindow)
        self._type(window, 'Bearer ' + self._token({'sub': '42'}))
        self.assertIn('Payload', window.output.get('1.0', 'end-1c'))

    def test_jwt_expired_token_flagged(self):
        window = self._open(JwtWindow)
        self._type(window, self._token({'sub': '1', 'exp': 1000000000}))
        self.assertIn('已过期', window.status.cget('text'))
        self.assertEqual(window.status.cget('fg').upper(), '#FF453A')

    def test_jwt_future_token_not_flagged_expired(self):
        import time
        window = self._open(JwtWindow)
        self._type(window, self._token({'sub': '1', 'exp': int(time.time()) + 86400 * 3}))
        self.assertNotIn('已过期', window.status.cget('text'))

    def test_jwt_invalid_reports_error(self):
        window = self._open(JwtWindow)
        self._type(window, 'not-a-token')
        self.assertEqual(window.output.get('1.0', 'end-1c'), '')
        self.assertEqual(window.status.cget('fg').upper(), '#FF453A')

    def test_jwt_shows_alg_in_status(self):
        window = self._open(JwtWindow)
        self._type(window, self._token({'sub': '1'}, header={'alg': 'RS256'}))
        self.assertIn('alg=RS256', window.status.cget('text'))

    def test_jwt_no_signature_states_alg_none(self):
        import json as jsonlib
        def enc(obj):
            raw = jsonlib.dumps(obj).encode()
            return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')
        window = self._open(JwtWindow)
        self._type(window, f'{enc({"alg": "none"})}.{enc({"sub": "1"})}')
        self.assertIn('alg=none', window.output.get('1.0', 'end-1c'))

    # ---------- 日期转换（三栏并排版式） ----------
    def _fill_field(self, window, key, text):
        """往某一栏的输入框里填值并立即刷新（跳过 180ms 防抖）。"""
        entry = window.rows[key]['entry']
        window._hide_placeholder(entry)
        entry.delete(0, 'end')
        if text:
            entry.insert(0, text)
        window._on_field_input(key)
        window._refresh()

    def _field_result(self, window, key):
        """某一栏结果区的**主值**。"""
        return window.rows[key]['result'].cget('text')

    def _field_sub(self, window, key):
        """某一栏结果区的**副行**（灰字那一行，可能为空）。"""
        return window.rows[key]['sub'].cget('text')

    def _field_fg(self, window, key):
        return window.rows[key]['result'].cget('fg').upper()

    def _clock_card(self, window):
        """时钟条那张卡。三列的父链是 Label→cell(Frame)→card。"""
        return window._clock_cells['beijing'].master.master

    def _deck_cards(self, window):
        """三栏卡片，按左右顺序。"""
        return [window.rows[c[0]]['card'] for c in window.COLUMNS]

    def _meta_row_of(self, label):
        """一个说明文字 Label 所在的那一行容器（``_make_meta_row`` 的返回值）。"""
        return label.master

    def _row_dots(self, label):
        """说明行里的绿点画布，按 pack 顺序。``[点, 文字]`` 表示点在文字左边。"""
        return [c for c in self._meta_row_of(label).winfo_children()
                if c.winfo_class() == 'Canvas']

    def _widgets_with_text(self, root, text, kind=None):
        """递归找文字里含 ``text`` 的控件（可限定类名）。"""
        out = []
        for widget in self._descendants(root):
            if kind is not None and widget.winfo_class() != kind:
                continue
            try:
                value = str(widget.cget('text'))
            except tk.TclError:
                continue
            if text in value:
                out.append(widget)
        return out

    # ---- 注册与整体结构 ----
    def test_datetime_window_is_reachable_from_the_registry(self):
        """新工具必须真能从注册表打开——只加类不加注册表等于没做。

        走 ``open_tool`` 而不是直接 ``cls(panel)``：注册表的 key→类的映射
        正是在这里被真正使用的。
        """
        window = open_tool(self.panel, 'datetime')
        self.created.append(window)
        window.win.update()
        self.assertIsInstance(window, DateTimeWindow)
        self.assertEqual(window.title, '日期转换')

    def test_datetime_has_exactly_three_columns(self):
        """三栏，顺序与用户要求一致：时间戳→日期、日期→时间戳、北京→美东。

        顺序是用户明确指定的（「时间戳-》日期时间、日期时间-》时间戳、
        北京时间-》美东时间」），所以连顺序一起钉住，不能只断言条数。
        """
        window = self._open(DateTimeWindow)
        self.assertEqual([c[0] for c in window.COLUMNS],
                         ['stamp', 'date', 'zone'])
        self.assertEqual(list(window.rows), ['stamp', 'date', 'zone'])
        titles = [c[1] for c in window.COLUMNS]
        self.assertEqual(titles, ['时间戳 → 日期时间', '日期时间 → 时间戳',
                                  '北京时间 → 美东时间'])

    def test_datetime_every_column_is_wired_to_a_converter(self):
        """每栏都必须真的接上换算函数——漏一处就是一个永远空白的结果块。

        遍历 ``COLUMNS`` 断言而不是逐个点名：将来加第四栏时这条会一起覆盖到。
        """
        window = self._open(DateTimeWindow)
        for spec in window.COLUMNS:
            self.assertTrue(callable(spec[4]),
                            f'{spec[0]} 那栏没有接换算函数')
        self.assertEqual(
            [spec[4] for spec in window.COLUMNS],
            [TW.datetime_tools.timestamp_to_zone, TW.datetime_tools.to_timestamp,
             TW.datetime_tools.convert_zone])

    def test_datetime_columns_sit_side_by_side_on_one_row(self):
        """三栏必须**并排**在同一行上，而不是上下叠。

        用屏幕坐标实测：三栏的 ``winfo_rooty()`` 相等才算真的同一行；只读
        grid 的 ``row=`` 是意图，不是用户看到的结果。左右顺序也要对。
        """
        window = self._open(DateTimeWindow)
        cards = self._deck_cards(window)
        tops = [c.winfo_rooty() for c in cards]
        self.assertEqual(len(set(tops)), 1,
                         f'三栏不在同一行，各自的顶边是 {tops}')
        lefts = [c.winfo_rootx() for c in cards]
        self.assertEqual(lefts, sorted(lefts), f'三栏左右顺序不对：{lefts}')

    def test_datetime_columns_do_not_overlap(self):
        """相邻两栏必须有间隙，不能贴到一起或重叠。"""
        window = self._open(DateTimeWindow)
        for left, right in itertools.pairwise(self._deck_cards(window)):
            gap = right.winfo_rootx() - (left.winfo_rootx() + left.winfo_width())
            self.assertGreaterEqual(gap, 4,
                                    f'两栏贴死了，间隙只有 {gap}px')

    def test_datetime_columns_share_the_same_width(self):
        """三栏等宽——三栏装着结构相同的元素，宽度不齐会让按钮右缘参差不齐。"""
        window = self._open(DateTimeWindow)
        widths = [c.winfo_width() for c in self._deck_cards(window)]
        self.assertLessEqual(max(widths) - min(widths), 2,
                             f'三栏宽度不齐：{widths}')

    def test_datetime_clock_sits_above_the_three_columns(self):
        """时钟条在**三栏上方**（用户确认的三个设计决策之一：保留、放三栏上方）。"""
        window = self._open(DateTimeWindow)
        clock = self._clock_card(window)
        for card in self._deck_cards(window):
            self.assertLess(clock.winfo_rooty() + clock.winfo_height(),
                            card.winfo_rooty() + 1,
                            '时钟条没在三栏上方')
        # 时钟条要横跨整个正文宽度，而不是只占左边一栏。
        self.assertGreater(clock.winfo_width(),
                           self._deck_cards(window)[0].winfo_width() * 2,
                           '时钟条没有横跨三栏')

    def test_datetime_has_no_footer_at_all(self):
        """**底栏整个去掉**（用户明确要求）。

        钉两条：类属性关掉，以及真的一个 Button 都没建出来——只改开关而
        忘了 ``_build`` 里的条件，界面上会冒出一条半残的底栏。
        """
        window = self._open(DateTimeWindow)
        self.assertFalse(window.has_footer, 'has_footer 没关掉')
        self.assertFalse(hasattr(window, '_body_footer'))
        # 底栏的招牌控件：状态灯与状态文字。它们不该存在。
        self.assertFalse(hasattr(window, '_lamp'), '底栏的状态灯还在')
        self.assertFalse(hasattr(window, 'status'), '底栏的状态文字还在')

    def test_datetime_body_uses_the_space_freed_by_the_footer(self):
        """去掉底栏后正文区要**吃掉**那 35px，而不是在底部留一条空白。

        只把 ``_build_footer`` 跳掉、不同步改高度偏移量，正文区的下缘会停在
        「本该有底栏」的位置——截图上看就是窗口底部一条与底色不同的空白带。

        **反证用另一条**：造一个 ``has_footer = True`` 的同版式子类，同一个
        窗口高度下它的正文区必须**矮掉整整 ``FOOT_HEIGHT + 1``**。这比
        「差值小于某个常数」可靠得多——那个常数既包含 ``CONTENT_INSET``，
        又随窗口边框变化，实测差 1px 就会误报，而 1px 完全不是缺陷。
        """
        window = self._open(DateTimeWindow)
        shell_bottom = window.win.winfo_rooty() + window.win.winfo_height()
        body_bottom = window._body.winfo_rooty() + window._body.winfo_height()
        self.assertLessEqual(shell_bottom - body_bottom,
                             TW.FOOT_HEIGHT + 1 + 8,
                             '正文区下缘离窗口底太远，底栏的位置留了空白')

        class WithFooter(DateTimeWindow):
            has_footer = True

        other = self._open(WithFooter)
        other.win.geometry(f'{window.win.winfo_width()}'
                           f'x{window.win.winfo_height()}')
        other.win.update()
        other.win.update_idletasks()
        self.assertEqual(
            window._body.winfo_height() - other._body.winfo_height(),
            TW.FOOT_HEIGHT + 1,
            '关掉底栏后正文区没有正好长高 FOOT_HEIGHT+1')

    # ---- 单栏内部元素顺序 ----
    def test_datetime_each_column_stacks_elements_in_the_shot_order(self):
        """每栏的元素顺序：输入框 → 下拉 → 下拉 → 主按钮 → 结果块。

        这是截图里那套版式的核心结构，必须逐栏实测。用 ``winfo_rooty()``
        而不是 grid ``row=``：后者是意图，前者才是用户看到的位置。
        """
        window = self._open(DateTimeWindow)
        for key in ('stamp', 'date', 'zone'):
            row = window.rows[key]
            names = list(row['drops'])
            chain = [
                ('输入框', row['entry'].master),
                (f'下拉{names[0]}', row['drops'][names[0]].box),
                (f'下拉{names[1]}', row['drops'][names[1]].box),
                ('主按钮', row['button']),
                ('结果块', row['result'].master.master),
            ]
            tops = [w.winfo_rooty() for _, w in chain]
            self.assertEqual(tops, sorted(tops),
                             f'{key} 那栏的元素顺序不对：'
                             f'{[(n, y) for (n, _), y in zip(chain, tops)]}')
            self.assertEqual(len(set(tops)), len(tops),
                             f'{key} 那栏有两个元素顶边相同，说明叠在了一起')

    def test_datetime_each_column_has_title_subtitle_and_input(self):
        """每栏顶部是标题 + 副标题 + 输入框，三者按这个顺序排。"""
        window = self._open(DateTimeWindow)
        for key, title, subtitle, _hint, _f, _g in window.COLUMNS:
            card = window.rows[key]['card']
            heads = self._widgets_with_text(card, title, 'Label')
            self.assertEqual(len(heads), 1, f'{key} 那栏找不到标题 {title!r}')
            subs = self._widgets_with_text(card, subtitle, 'Label')
            self.assertEqual(len(subs), 1, f'{key} 那栏找不到副标题')
            self.assertLess(heads[0].winfo_rooty(), subs[0].winfo_rooty(),
                            f'{key} 那栏标题没在副标题上面')
            self.assertLess(subs[0].winfo_rooty(),
                            window.rows[key]['entry'].master.winfo_rooty(),
                            f'{key} 那栏副标题没在输入框上面')

    def test_datetime_buttons_are_inside_their_own_column(self):
        """每个主按钮必须在**自己那一栏**里，left/right 都落在栏内。

        三栏并排时把按钮挂错父容器，视觉上可能仍「看着像在这一栏下面」，
        实则是全局的一个按钮——点哪一栏都是同一栏的结果。
        """
        window = self._open(DateTimeWindow)
        for key in ('stamp', 'date', 'zone'):
            card = window.rows[key]['card']
            button = window.rows[key]['button']
            self.assertIs(button.master, card,
                          f'{key} 那栏的主按钮不是这张卡的孩子')
            self.assertGreaterEqual(button.winfo_rootx(), card.winfo_rootx())
            self.assertLessEqual(button.winfo_rootx() + button.winfo_width(),
                                 card.winfo_rootx() + card.winfo_width())

    def test_datetime_action_button_spans_the_column(self):
        """主按钮是**通栏**的——左右各留一点内边距，但不该缩成一小块。

        截图里那颗绿按钮几乎占满整栏宽度；它同时也是这一栏最显眼的可点元素，
        缩窄会让人以为它只是个图标。
        """
        window = self._open(DateTimeWindow)
        for key in ('stamp', 'date', 'zone'):
            card = window.rows[key]['card']
            button = window.rows[key]['button']
            self.assertGreater(button.winfo_width(),
                               card.winfo_width() * 0.7,
                               f'{key} 那栏的主按钮不够宽，不像通栏按钮')

    def test_datetime_action_buttons_share_the_same_width(self):
        """三栏的按钮等宽——三栏等宽 + 同样的内边距，按钮自然该齐。

        这条抓的是「某一栏忘了设 padx 或 grid 参数写歪」这类错位。
        """
        window = self._open(DateTimeWindow)
        widths = [window.rows[k]['button'].winfo_width()
                  for k in ('stamp', 'date', 'zone')]
        self.assertLessEqual(max(widths) - min(widths), 2,
                             f'三栏按钮宽度不齐：{widths}')

    # ---- 主按钮与复制 ----
    def test_datetime_action_button_is_green(self):
        """主按钮是绿色实心（截图里那颗通栏按钮）。"""
        window = self._open(DateTimeWindow)
        expected = window.P['button'].upper()
        for key in ('stamp', 'date', 'zone'):
            button = window.rows[key]['button']
            self.assertEqual(button.cget('bg').upper(), expected,
                             f'{key} 那栏的按钮底色不是绿色')
            label = button.label_widget
            self.assertEqual(label.cget('bg').upper(), expected)
            self.assertEqual(label.cget('fg').upper(),
                             window.P['button_fg'].upper())

    def test_datetime_action_button_label_is_translate(self):
        """按钮文案是「转换」，且三栏一致。"""
        window = self._open(DateTimeWindow)
        for key in ('stamp', 'date', 'zone'):
            text = window.rows[key]['button'].label_widget.cget('text')
            self.assertEqual(text, window.ACTION_LABEL)

    def test_datetime_action_button_recomputes_immediately(self):
        """按主按钮要**立刻**出结果，不等 180ms 防抖。

        防抖只服务于「打字」那条路径；按钮是用户主动按下的，等 180ms 才出
        结果会让人怀疑「是不是没按到」。
        """
        window = self._open(DateTimeWindow)
        entry = window.rows['stamp']['entry']
        window._hide_placeholder(entry)
        entry.insert(0, '1793777553681')     # 故意不触发 _on_field_input
        window.rows['stamp']['button'].label_widget.event_generate(
            '<ButtonPress-1>', x=5, y=5)
        self.assertEqual(self._field_result(window, 'stamp'),
                         '2026-11-04 15:32:33',
                         '按了按钮但没有立刻出结果')

    def test_datetime_each_column_has_its_own_copy(self):
        """每栏结果区里都有一个「复制」，且复制的是**那一栏**的结果。

        三份结果同时在屏幕上，复制哪一份必须有确定答案——点哪栏的复制就是
        哪栏的，不能全都复制同一份。
        """
        window = self._open(DateTimeWindow)
        self._fill_field(window, 'stamp', '1793777553681')
        self._fill_field(window, 'zone', '2026-11-04 15:32:33')
        window.rows['zone']['copy'].event_generate('<Button-1>', x=2, y=2)
        copied = window.win.clipboard_get()
        self.assertIn('2026-11-04 02:32:33', copied)
        self.assertIn('EST', copied, '美东那栏的副行没被一起复制')

    def test_datetime_copy_without_result_reports_instead_of_crashing(self):
        """没结果时点复制要给出提示，而不是静默无反应或报错。"""
        window = self._open(DateTimeWindow)
        window._copy_column('stamp')
        head = window.rows['stamp']['head_label'].cget('text')
        self.assertIn('没有结果', head)

    def test_datetime_toast_is_per_column(self):
        """提示只出现在**被点那一栏**，不串到别的栏。"""
        window = self._open(DateTimeWindow)
        window._copy_column('date')
        self.assertIn('没有结果',
                      window.rows['date']['head_label'].cget('text'))
        self.assertEqual(window.rows['stamp']['head_label'].cget('text'),
                         '转换结果', '提示串到别的栏去了')

    def test_datetime_close_cancels_the_pending_toast_timer(self):
        """关窗口必须**取消**那条 1.6 秒的提示定时器。

        不取消的话，定时器到点时窗口已销毁、回调命令也随 Tk 一起删掉了，
        Tk 会往 stderr 打 ``invalid command name "...restore"``——全量测试里
        实测刷出来过（用户看到的是「关不干净」）。

        **判据是句柄被清空 + 定时器真的不再排队**，不是「有没有异常」：
        这个错误在 Tk 的 after 队列里静默发生，Python 侧一个异常都不抛。
        """
        window = self._open(DateTimeWindow)
        window._copy_column('stamp')          # 必然挂上一个待恢复的提示
        self.assertIsNotNone(window._toast_id, '提示定时器没挂上')
        window.close()
        self.assertIsNone(window._toast_id,
                          'close() 没取消提示定时器，Tk 会报 invalid command name')

    def test_datetime_new_toast_replaces_the_pending_timer(self):
        """同一条栏连着两次提示，只留最后一个定时器，不堆积。

        堆积本身不会报错，但窗口连点十几次「复制」就攒下十几个待触发回调，
        它们会在 1.6 秒后几乎同时执行、把标题来回改——表现为「标题闪一下」。

        **判据必须问 Tcl「那个定时器还在不在」**，不能比较两次 ``after()``
        的返回值：``after()`` 每次都返回一个**新的** id（``after#0``、
        ``after#1``…），拿「新旧 id 不相等」当判据是**恒真**的——不取消旧的
        也照样通过（第一版就这么写的，变异验证直接逃逸）。正确写法是拿旧 id
        去问 ``after info``，被取消过的会抛 ``TclError``。
        """
        window = self._open(DateTimeWindow)
        window._copy_column('stamp')
        first = window._toast_id
        window._copy_column('stamp')
        self.assertIsNotNone(window._toast_id)

        with self.assertRaises(tk.TclError,
                               msg='旧定时器没被取消，两个都还在排队'):
            window.win.tk.call('after', 'info', first)
        # 新的那个必须还在（别把两个都取消了，提示就永远不恢复）
        self.assertTrue(window.win.tk.call('after', 'info', window._toast_id))

    # ---- 下拉 ----
    def test_datetime_every_column_has_the_dropdowns_from_the_spec(self):
        """``COLUMNS`` 里声明了几个下拉，界面上就得真建出几个。

        「照图片全做」是用户明确的答复；漏建一个下拉不会报错，只会让某一栏
        少一个选项——没有任何断言拦得住，所以这里遍历声明去比对。
        """
        window = self._open(DateTimeWindow)
        for key, _t, _s, _h, _f, groups in window.COLUMNS:
            drops = window.rows[key]['drops']
            self.assertEqual(list(drops), [g[0] for g in groups],
                             f'{key} 那栏的下拉与声明不符')
            for gkey, _glabel, choices, default in groups:
                drop = drops[gkey]
                self.assertEqual(list(drop.order), [c[0] for c in choices],
                                 f'{key}.{gkey} 的选项不对')
                self.assertEqual(drop.value, default,
                                 f'{key}.{gkey} 的默认值不对')
                self.assertEqual(drop.label,
                                 dict(choices)[default],
                                 f'{key}.{gkey} 的展示名不对')

    def test_datetime_dropdown_labels_match_the_shot(self):
        """下拉的标签文字照截图：自动识别 / 目标时区 / 时间基准 / 输出单位。

        这些字写在输入框下面，用户靠它们判断「这个框是干什么的」。
        """
        window = self._open(DateTimeWindow)
        expected = {
            'stamp': ['自动识别', '目标时区'],
            'date': ['时间基准', '输出单位'],
            'zone': ['时间基准', '结果时区'],
        }
        for key, labels in expected.items():
            card = window.rows[key]['card']
            for label in labels:
                found = self._widgets_with_text(card, label, 'Label')
                self.assertTrue(found, f'{key} 那栏找不到标签 {label!r}')

    def test_datetime_dropdown_shows_option_label_not_key(self):
        """下拉显示的必须是**选项的展示名**，不是内部 key。

        显示 ``beijing`` 而不是「北京时间 (UTC+08:00)」，用户根本不知道那是
        哪个时区。
        """
        window = self._open(DateTimeWindow)
        for key in ('stamp', 'date', 'zone'):
            for drop in window.rows[key]['drops'].values():
                self.assertIn(drop.value, dict(drop.choices))
                self.assertNotEqual(drop.label, drop.value,
                                    f'{key} 的下拉显示了内部 key')
                self.assertEqual(drop.label_widget.cget('text'), drop.label)

    def test_datetime_dropdown_click_opens_a_popup_and_selects(self):
        """点下拉要弹出选项，选中后要**立刻**按新选项重算。

        只断言 ``set()`` 会漏掉「点了没反应」——那正是自绘下拉最容易出的问题
        （事件没绑到真正被点的那个小控件上）。
        """
        window = self._open(DateTimeWindow)
        self._fill_field(window, 'stamp', '1793777553681')
        self.assertEqual(self._field_result(window, 'stamp'),
                         '2026-11-04 15:32:33')     # 目标时区默认北京
        drop = window.rows['stamp']['drops']['zone']
        drop.label_widget.event_generate('<Button-1>', x=2, y=2)
        window.win.update()
        self.assertIsNotNone(window._popup, '点了下拉但没有弹出选项')
        window._close_popup()
        drop.set('eastern')
        self.assertEqual(self._field_result(window, 'stamp'),
                         '2026-11-04 02:32:33',
                         '切换目标时区后没有重算')

    def test_datetime_no_footer_dropdown_remains(self):
        """底栏的下拉入口（如果有）要随底栏一起消失。"""
        window = self._open(DateTimeWindow)
        self.assertFalse(hasattr(window, '_json_anchor'))

    # ---- 结果区 ----
    def test_datetime_results_are_dark_green_on_a_soft_green_block(self):
        """结果区是「浅绿底 + 深绿字」（截图配色）。

        用**实测像素**而不是只读 ``cget``：``cget`` 只说明控件被配置成什么，
        而浅绿底是由内外两个 Frame 叠出来的，很容易配对了外层漏了内层。
        """
        window = self._open(DateTimeWindow)
        self._fill_field(window, 'stamp', '1793777553681')
        for key in ('stamp', 'date', 'zone'):
            row = window.rows[key]
            value, inner = row['result'], row['result'].master
            self.assertEqual(value.cget('bg').upper(),
                             window.P['soft'].upper(),
                             f'{key} 结果文字不在浅绿底上')
            self.assertEqual(inner.cget('bg').upper(),
                             window.P['soft'].upper(),
                             f'{key} 结果块内层不是浅绿')
            self.assertEqual(
                inner.master.cget('bg').upper(),
                window.P['soft_border'].upper(),
                f'{key} 结果块外圈描边色不对')
            self.assertEqual(value.cget('fg').upper(),
                             window.P['value_fg'].upper(),
                             f'{key} 结果文字不是深绿')

    def test_datetime_result_head_sits_above_the_value(self):
        """结果块的「转换结果」标题在结果值**上面**，复制在标题同一行右侧。"""
        window = self._open(DateTimeWindow)
        self._fill_field(window, 'stamp', '1793777553681')
        row = window.rows['stamp']
        head, value = row['head_label'], row['result']
        self.assertLess(head.winfo_rooty(), value.winfo_rooty(),
                        '「转换结果」没在结果值上面')
        copy = row['copy']
        self.assertLessEqual(
            abs(copy.winfo_rooty() - head.winfo_rooty()), 6,
            '复制按钮没和「转换结果」在同一行')
        self.assertGreater(copy.winfo_rootx(), head.winfo_rootx(),
                           '复制按钮跑到标题左边了')

    def test_datetime_error_result_turns_red(self):
        """报错用红字——与深绿的正常结果区分开。"""
        window = self._open(DateTimeWindow)
        self._fill_field(window, 'stamp', '不是时间戳')
        self.assertIn('不是时间戳', self._field_result(window, 'stamp'))
        self.assertEqual(self._field_fg(window, 'stamp'), '#FF453A')

    def test_datetime_result_box_grows_with_the_window(self):
        """结果块要吃掉栏内多余高度，而不是留一段空白在栏底。

        三栏并排后纵向空间只有原来的一半，结果块不长高的话整栏下半部分就是
        一片与卡片同色的空白——看起来像内容没建完。
        """
        window = self._open(DateTimeWindow)
        window.win.geometry('1120x620')
        window.win.update()
        window.win.update_idletasks()
        small = window.rows['stamp']['result'].master.master.winfo_height()
        window.win.geometry('1120x800')
        window.win.update()
        window.win.update_idletasks()
        big = window.rows['stamp']['result'].master.master.winfo_height()
        self.assertGreater(big, small + 40,
                           f'窗口变高结果块没跟着长：{small} → {big}')

    # ---- 换算正确性 ----
    def test_datetime_stamp_column_converts_to_beijing_by_default(self):
        """第 1 栏：时间戳 → 日期时间，目标时区默认北京时间。"""
        window = self._open(DateTimeWindow)
        self._fill_field(window, 'stamp', '1793777553681')
        self.assertEqual(self._field_result(window, 'stamp'),
                         '2026-11-04 15:32:33')

    def test_datetime_stamp_column_follows_the_target_zone(self):
        """第 1 栏切目标时区要真的换一个结果（含夏令时分支）。"""
        window = self._open(DateTimeWindow)
        self._fill_field(window, 'stamp', '1793777553681')
        window.rows['stamp']['drops']['zone'].set('eastern')
        self.assertEqual(self._field_result(window, 'stamp'),
                         '2026-11-04 02:32:33')          # 11 月 EST
        window.rows['stamp']['drops']['zone'].set('utc')
        self.assertEqual(self._field_result(window, 'stamp'),
                         '2026-11-04 07:32:33')

    def test_datetime_stamp_column_follows_the_input_unit(self):
        """第 1 栏的输入单位下拉要能覆盖位数判定。

        **拿 10 位数字验**：它默认按「秒」解释（位数规则），显式选「毫秒」后
        会变成 1000 倍——两个结果必然不同，而且都落在可表示范围内。

        早先这里用 13 位数字选「秒」，期望它变成 2286 年，实际是**溢出**：
        13 位当秒就是 55000 年，超出 ``datetime`` 的上限，函数按设计报
        「数值太大」。那是正确的行为，是断言挑错了样本。
        """
        window = self._open(DateTimeWindow)
        self._fill_field(window, 'stamp', '1793777553')     # 10 位 = 秒
        auto = self._field_result(window, 'stamp')
        self.assertEqual(auto, '2026-11-04 15:32:33')
        window.rows['stamp']['drops']['unit'].set('millis')
        forced = self._field_result(window, 'stamp')
        self.assertNotEqual(auto, forced,
                            '显式选了「毫秒」但结果没变，说明单位没生效')
        # 按毫秒解释 = 1970-01-21 前后（1793777553 ms ≈ 20.8 天）
        self.assertIn('1970-01', forced)

    def test_datetime_date_column_converts_to_millis_by_default(self):
        """第 2 栏：日期时间 → 时间戳，输出单位默认毫秒。"""
        window = self._open(DateTimeWindow)
        self._fill_field(window, 'date', '2026-11-04 15:32:33')
        self.assertEqual(self._field_result(window, 'date'), '1793777553000')

    def test_datetime_date_column_follows_the_output_unit(self):
        """第 2 栏的「秒 / 毫秒」下拉。秒的时间戳是毫秒的 1/1000。"""
        window = self._open(DateTimeWindow)
        self._fill_field(window, 'date', '2026-11-04 15:32:33')
        millis = int(self._field_result(window, 'date'))
        window.rows['date']['drops']['unit'].set('seconds')
        seconds = int(self._field_result(window, 'date'))
        self.assertEqual(millis, seconds * 1000,
                         '秒与毫秒的换算关系不对')

    def test_datetime_date_column_follows_the_time_base(self):
        """同一串「15:32:33」按不同基准解释，得到的时间戳必须不同。

        **方向要算对**：北京 = UTC+8。同一串「15:32:33」当成 UTC（= 15:32
        UTC）比当成北京（= 07:32 UTC）**晚** 8 小时，所以差值应当是 ``+8``。
        这条第一版把符号写反了，实测才纠正过来——方向性断言不能凭直觉写。
        """
        window = self._open(DateTimeWindow)
        self._fill_field(window, 'date', '2026-11-04 15:32:33')
        beijing = int(self._field_result(window, 'date'))
        window.rows['date']['drops']['zone'].set('utc')
        utc = int(self._field_result(window, 'date'))
        delta_hours = (utc - beijing) / 1000 / 3600
        self.assertAlmostEqual(delta_hours, 8, places=3,
                               msg='同一串数字按 UTC 解释应比按北京解释晚 8 小时')

    def test_datetime_zone_column_converts_beijing_to_eastern(self):
        """第 3 栏：北京时间 → 美东时间（用户点名的功能）。"""
        window = self._open(DateTimeWindow)
        self._fill_field(window, 'zone', '2026-11-04 15:32:33')
        self.assertEqual(self._field_result(window, 'zone'),
                         '2026-11-04 02:32:33')
        self.assertIn('EST', self._field_sub(window, 'zone'))
        self.assertIn('UTC-5', self._field_sub(window, 'zone'))

    def test_datetime_zone_column_uses_dst_in_summer(self):
        """夏天要走 EDT（UTC-4）。只测冬天会漏掉整个夏令时分支。"""
        window = self._open(DateTimeWindow)
        self._fill_field(window, 'zone', '2026-07-01 20:00:00')
        self.assertEqual(self._field_result(window, 'zone'),
                         '2026-07-01 08:00:00')          # 20:00 - 12h
        self.assertIn('EDT', self._field_sub(window, 'zone'))

    def test_datetime_zone_column_can_go_the_other_way(self):
        """第 3 栏的两个下拉可以反过来：美东 → 北京。"""
        window = self._open(DateTimeWindow)
        row = window.rows['zone']
        row['drops']['source'].set('eastern')
        row['drops']['target'].set('beijing')
        self._fill_field(window, 'zone', '2026-11-04 02:32:33')
        self.assertEqual(self._field_result(window, 'zone'),
                         '2026-11-04 15:32:33')

    def test_datetime_zone_column_round_trips(self):
        """北京 → 美东 → 北京 必须回到原值。方向性 bug 单跑一边抓不到。"""
        window = self._open(DateTimeWindow)
        for text in ('2026-11-04 15:32:33', '2026-07-01 20:00:00'):
            # **每一轮都要把两个下拉复位**：上一轮结束时它们是「美东 → 北京」，
            # 直接进下一轮会把北京时间的输入当成美东来解析，往返自然对不上。
            row = window.rows['zone']
            row['drops']['source'].set('beijing')
            row['drops']['target'].set('eastern')
            self._fill_field(window, 'zone', text)
            eastern = self._field_result(window, 'zone')
            row['drops']['source'].set('eastern')
            row['drops']['target'].set('beijing')
            self._fill_field(window, 'zone', eastern)
            self.assertEqual(self._field_result(window, 'zone'), text,
                             f'{text} 往返后对不上')

    def test_datetime_columns_are_independent(self):
        """三栏各自算各自的，互不干扰。"""
        window = self._open(DateTimeWindow)
        self._fill_field(window, 'stamp', '1793777553681')
        self._fill_field(window, 'date', '2026-11-04 15:32:33')
        self._fill_field(window, 'zone', '2026-11-04 15:32:33')
        self.assertEqual(self._field_result(window, 'stamp'),
                         '2026-11-04 15:32:33')
        self.assertEqual(self._field_result(window, 'date'), '1793777553000')
        self.assertEqual(self._field_result(window, 'zone'),
                         '2026-11-04 02:32:33')

    def test_datetime_garbage_only_affects_its_own_column(self):
        """一栏填错不该把另外两栏的结果清掉。"""
        window = self._open(DateTimeWindow)
        self._fill_field(window, 'stamp', '1793777553681')
        self._fill_field(window, 'zone', '2026-11-04 15:32:33')
        good_stamp = self._field_result(window, 'stamp')
        good_zone = self._field_result(window, 'zone')
        self._fill_field(window, 'date', '不是时间')
        self.assertEqual(self._field_result(window, 'stamp'), good_stamp)
        self.assertEqual(self._field_result(window, 'zone'), good_zone)
        self.assertIn('认不出', self._field_result(window, 'date'))

    def test_datetime_cross_column_hint_points_at_the_right_column(self):
        """填错栏时要说清该用哪一栏，而不是干巴巴一句「认不出」。"""
        window = self._open(DateTimeWindow)
        self._fill_field(window, 'date', '1793777553681')
        self.assertIn('时间戳', self._field_result(window, 'date'))
        self._fill_field(window, 'zone', '1793777553681')
        self.assertIn('时间戳', self._field_result(window, 'zone'))

    def test_datetime_clear_empties_all_entries_but_keeps_the_clock(self):
        """清空只清用户输入；顶部时钟**不该被清掉**。

        它们是实时值而非用户输入。一并清空会让窗口看起来坏了（顶部突然空白）。
        """
        window = self._open(DateTimeWindow)
        self._fill_field(window, 'stamp', '1793777553681')
        self._fill_field(window, 'date', '2026-11-04 15:32:33')
        self._fill_field(window, 'zone', '2026-11-04 15:32:33')
        clock_before = window._clock_cells['beijing'].cget('text')
        eastern_before = window._clock_cells['eastern'].cget('text')

        window._clear()
        window.win.update()
        for key in ('stamp', 'date', 'zone'):
            self.assertEqual(
                window._entry_value(window.rows[key]['entry']), '',
                f'{key} 输入框没清干净')
            self.assertEqual(self._field_result(window, key), '')
            self.assertEqual(self._field_sub(window, key), '')
        self.assertEqual(window._clock_cells['beijing'].cget('text'),
                         clock_before)
        self.assertEqual(window._clock_cells['eastern'].cget('text'),
                         eastern_before)

    def test_datetime_entries_are_single_line(self):
        """三栏的输入框都是单行 ``Entry``——输入就是一行时间串。"""
        window = self._open(DateTimeWindow)
        for key in ('stamp', 'date', 'zone'):
            entry = window.rows[key]['entry']
            self.assertIsInstance(entry, tk.Entry)
            self.assertEqual(str(entry.cget('state')), 'normal')

    def test_datetime_placeholder_disappears_when_typing(self):
        """占位示例文字不是输入内容：填了真值后 ``_entry_value`` 必须是真值。"""
        window = self._open(DateTimeWindow)
        entry = window.rows['stamp']['entry']
        self.assertTrue(entry.get(), '空态应显示示例文字')
        self.assertEqual(window._entry_value(entry), '',
                         '占位示例不能被当成输入')
        self._fill_field(window, 'stamp', '1793777553681')
        self.assertEqual(window._entry_value(entry), '1793777553681')

    def test_datetime_placeholder_stays_hidden_after_losing_focus(self):
        """用户照着示例打了一遍，失去焦点后不能被当成「还空着」。

        **这是占位符实现里最容易错的一处**：若用「文字是否等于示例」来判断，
        用户真输入了同样的内容时就会被清掉、并被当成空输入。
        """
        window = self._open(DateTimeWindow)
        entry = window.rows['stamp']['entry']
        hint = entry._placeholder_text
        self._fill_field(window, 'stamp', hint)
        window._sync_placeholder(entry)
        self.assertEqual(window._entry_value(entry), hint,
                         '照着示例输入的内容被误当成占位符')

    # ---- 初始焦点：三栏都不聚焦 ----
    def test_datetime_opens_without_focusing_any_entry(self):
        """开窗后**没有任何一条栏的输入框拿到焦点**（用户明确要求）。

        **必须三栏逐个查**，不能只看第一栏：只断言 ``stamp`` 没焦点的话，
        把 ``_initial_focus_target`` 改成返回 ``date`` 或 ``zone`` 的输入框
        照样全绿——而那样用户看到的是「光标鬼使神差落在第二栏」。
        """
        window = self._open(DateTimeWindow)
        window.win.update()
        for key in ('stamp', 'date', 'zone'):
            entry = window.rows[key]['entry']
            self.assertIsNot(
                entry.focus_get(), entry,
                f'{key} 那栏的输入框开窗就抢了焦点')

    def test_datetime_initial_focus_target_is_none(self):
        """``_initial_focus_target`` 返回 ``None``——这是「不指定」的契约。

        用 ``focus_get()`` 单查在当前环境里**判别力有限**（测试窗口是
        ``-alpha 0``，焦点不一定真落到控件上），所以再钉一条源码级的：
        返回 ``None`` 才是基类文档里写明的「不指定」语义，返回别的东西
        （哪怕是个 ``Entry``）都不该出现。
        """
        window = self._open(DateTimeWindow)
        self.assertIsNone(window._initial_focus_target())

    def test_datetime_window_itself_still_takes_focus(self):
        """窗口**自己**仍要拿到焦点——否则 ESC 最小化会失灵。

        「不聚焦输入框」不等于「窗口不要焦点」：``<Escape>`` 挂在窗口上，
        没有窗口焦点就收不到按键。

        **这条只能钉源码，不能钉运行时**：实测把 ``focus_force()`` 整行删掉
        后 ``win.focus_get()`` 照样返回窗口自身（Tk 的兜底），而本会话已锁屏、
        ``GetForegroundWindow()`` 直接返回 0，OS 层的真实前台窗口观测不到——
        写成运行时断言就是**恒真**，变异逃逸（踩过）。所以这里扫源码：
        ``focus_force`` 调用必须还在，且必须排在 ``_initial_focus_target``
        之前（顺序反了会让窗口焦点覆盖掉「不聚焦控件」的效果）。
        """
        source = Path(TW.__file__).read_text(encoding='utf-8')
        body = source.split('def _initial_focus_target', 1)[0]
        self.assertIn('focus_force', body,
                      'focus_force() 被删了，ESC 最小化会失灵')
        force_at = source.index('self.win.focus_force()')
        target_at = source.index('target = self._initial_focus_target()')
        self.assertLess(force_at, target_at,
                        '顺序反了：先给控件焦点、再 focus_force 窗口')

    def test_datetime_all_three_hints_visible_on_open(self):
        """开窗时**三栏的灰色示例都要在**——聚焦会让那一栏的提示消失。

        这条是「不聚焦」的用户可见收益：焦点一落在某栏，``_hide_placeholder``
        就把那栏的示例清掉（挂在 ``<FocusIn>`` 上），用户开窗看到的是「一栏
        空的、两栏有字」，容易以为那一栏不用填。
        """
        window = self._open(DateTimeWindow)
        window.win.update()
        for key in ('stamp', 'date', 'zone'):
            entry = window.rows[key]['entry']
            self.assertTrue(
                getattr(entry, '_placeholder_on', False),
                f'{key} 那栏开窗时没有显示示例文字')
            self.assertEqual(entry.get(), entry._placeholder_text)
            self.assertEqual(window._entry_value(entry), '',
                             '示例被当成了真实输入')

    # ---- 时钟条（沿用，未受改版影响） ----
    def test_datetime_clock_shows_beijing_and_timestamp(self):
        """顶部时钟条：当前北京时间与当前时间戳，两者是同一时刻的两种表示。"""
        window = self._open(DateTimeWindow)
        beijing = window._clock_cells['beijing'].cget('text')
        millis = window._clock_cells['millis'].cget('text')
        self.assertRegex(beijing, r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$')
        self.assertTrue(millis.isdigit(), f'时间戳不是数字：{millis!r}')

        from datetime import datetime, timedelta
        as_beijing = (datetime(1970, 1, 1)
                      + timedelta(milliseconds=int(millis))
                      + timedelta(hours=8))
        self.assertEqual(
            as_beijing.strftime('%Y-%m-%d %H:%M:%S'), beijing,
            '时钟条两栏对不上——它们是同一时刻的两种表示，必须一致')

    def test_datetime_clock_third_column_is_eastern_date(self):
        """第 3 列是当前美东日期，带 EST/EDT 标记。"""
        window = self._open(DateTimeWindow)
        eastern = window._clock_cells['eastern'].cget('text')
        self.assertRegex(eastern, r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$')
        tag = window._clock_cells['eastern_sub'].cget('text')
        self.assertTrue('EST' in tag or 'EDT' in tag,
                        f'美东那格没标出标准时/夏令时：{tag!r}')

    def test_datetime_clock_columns_share_one_row_in_order(self):
        """三列必须在**同一行**上，顺序为 北京时间 → 时间戳 → 美东日期。

        用屏幕坐标实测：三格的 ``winfo_rooty()`` 相等才算真的同一行；只读
        grid 的 ``row=`` 是意图，不是用户看到的结果。
        """
        window = self._open(DateTimeWindow)
        labels = [window._clock_cells[k] for k in
                  ('beijing', 'millis', 'eastern')]
        tops = [w.winfo_rooty() for w in labels]
        self.assertEqual(len(set(tops)), 1,
                         f'三列不在同一行，各自的顶边是 {tops}')
        lefts = [w.winfo_rootx() for w in labels]
        self.assertEqual(lefts, sorted(lefts), f'三列左右顺序不对：{lefts}')

    def test_datetime_clock_columns_are_separated_by_vertical_rules(self):
        """相邻两列之间有一条**竖**的分隔线，且真的可见。

        找线的方式是「夹在两列之间的那条窄 Frame」而不是「``width == 1`` 的
        Frame」：后者把线宽写死在断言里，将来把线调成 2px 只为更好看，测试却
        会红，那是**断言错对象**——用户要的是「有竖线」，不是「线正好 1px」。
        宽度只留一个「够窄才算分隔线」的上限。
        """
        window = self._open(DateTimeWindow)
        card = self._clock_card(window)
        beijing = window._clock_cells['beijing']
        millis = window._clock_cells['millis']
        eastern = window._clock_cells['eastern']

        def between(left, right):
            out = []
            for child in card.winfo_children():
                if child.winfo_class() != 'Frame':
                    continue
                if child.winfo_width() > 4:
                    continue        # 格与底行都是宽 Frame，排除掉
                cx = child.winfo_rootx()
                if (left.winfo_rootx() + left.winfo_width() <= cx
                        <= right.winfo_rootx()):
                    out.append(child)
            return out

        for left, right, where in ((beijing, millis, '北京时间|时间戳'),
                                   (millis, eastern, '时间戳|美东日期')):
            rules = between(left, right)
            self.assertEqual(len(rules), 1,
                             f'{where} 之间应有 1 条竖线，实际 {len(rules)} 条')
            rule = rules[0]
            self.assertGreaterEqual(rule.winfo_width(), 1,
                                    f'{where} 的竖线宽度为 0，屏幕上看不见')
            self.assertLess(rule.winfo_width(), 6,
                            f'{where} 的分隔物太宽，不像一条线')
            self.assertGreater(rule.winfo_height(), 20,
                               f'{where} 的竖线高度不足，不像分隔线')
            self.assertGreater(rule.winfo_height(), rule.winfo_width() * 5,
                               f'{where} 的分隔物是横的不是竖的')

    def test_datetime_clock_columns_share_the_same_width(self):
        """三列等宽——三列装的是三样结构相同的值，宽度不齐会显得歪。"""
        window = self._open(DateTimeWindow)
        widths = [window._clock_cells[k].master.winfo_width()
                  for k in ('beijing', 'millis', 'eastern')]
        self.assertLessEqual(max(widths) - min(widths), 2,
                             f'三列宽度不齐：{widths}')

    def test_datetime_clock_values_are_all_green(self):
        """三列的时间值统一用绿色——用户明确要求「时间都是用绿色」。"""
        window = self._open(DateTimeWindow)
        green = window._sys('green').upper()
        for key in ('beijing', 'millis', 'eastern'):
            self.assertEqual(window._clock_cells[key].cget('fg').upper(), green,
                             f'{key} 那一格的时间不是绿色')

    def _dot_in_row(self, label):
        """说明行里那个绿点画布；没有则返回 ``None``。"""
        dots = self._row_dots(label)
        return dots[0] if dots else None

    def _assert_green_dot_left_of(self, window, label, where):
        """断言 ``label`` 那行有绿点、是绿的、且在文字**左边**。"""
        dot = self._dot_in_row(label)
        self.assertIsNotNone(dot, f'{where} 那行没有小绿点')
        ovals = dot.find_all()
        self.assertEqual(len(ovals), 1, f'{where} 的绿点不是单个圆')
        self.assertEqual(dot.itemcget(ovals[0], 'fill').upper(),
                         window._sys('green').upper(),
                         f'{where} 的绿点不是绿色')
        self.assertLess(dot.winfo_rootx(), label.winfo_rootx(),
                        f'{where} 的绿点跑到文字右边了')

    def test_datetime_live_line_has_a_green_dot(self):
        """「实时更新中」前面有绿点——这是刷新还活着的可见凭据。"""
        window = self._open(DateTimeWindow)
        card = self._clock_card(window)
        live = [w for w in self._descendants(card)
                if isinstance(w, tk.Label) and '实时更新中' in str(w.cget('text'))]
        self.assertEqual(len(live), 1, '找不到「实时更新中」那一行')
        self._assert_green_dot_left_of(window, live[0], '实时更新中')

    def test_datetime_zone_line_has_a_green_dot(self):
        """「本地时区」前面加绿点（用户明确点名的一处）。"""
        window = self._open(DateTimeWindow)
        self._assert_green_dot_left_of(
            window, window._clock_zone, '本地时区')

    def test_datetime_zone_line_sits_under_the_eastern_column(self):
        """「本地时区」要在**当前美东日期那一列**下面，并与该列左对齐。

        用户明确要求「当前美东日期下面的本地时区靠当前列左边」。这条必须用
        几何实测、且**逐个宽度验**：靠 pack 到整宽底行再 ``side='right'`` 的
        写法在默认宽度下看着也「在右边」，窗口一变宽就飘走。
        """
        window = self._open(DateTimeWindow)
        eastern_sub = window._clock_cells['eastern_sub']
        zone = window._clock_zone

        for width in (1000, 1120, 1300, 1500):
            window.win.geometry(f'{width}x720')
            window.win.update()
            window.win.update_idletasks()
            # 比的是**行左缘**而不是文字左缘：两行的结构不同（时区那行前面有
            # 绿点、美东那行没有），拿两侧文字 x 比会差出一个绿点的宽度。
            zone_edge = zone.master.winfo_rootx()
            east_edge = eastern_sub.master.winfo_rootx()
            self.assertEqual(
                zone_edge, east_edge,
                f'窗口 {width} 时「本地时区」没和美东那一列左对齐：'
                f'时区行={zone_edge} 美东行={east_edge}')
            self.assertGreater(
                zone.master.winfo_rooty(), eastern_sub.master.winfo_rooty(),
                f'窗口 {width} 时「本地时区」没在美东那行下面')

    def test_datetime_live_line_stays_at_the_left_edge(self):
        """「实时更新中」仍在时钟卡最左边——它不该被时区那行挤动。"""
        window = self._open(DateTimeWindow)
        card = self._clock_card(window)
        live = [w for w in self._descendants(card)
                if isinstance(w, tk.Label) and '实时更新中' in str(w.cget('text'))]
        self.assertEqual(len(live), 1, '找不到「实时更新中」那一行')
        live_edge = live[0].master.winfo_rootx()
        first_cell = window._clock_cells['beijing'].master
        self.assertLessEqual(
            abs(live_edge - first_cell.winfo_rootx()), 1,
            '「实时更新中」没贴在左边，'
            f'行左缘={live_edge} 第一列左缘={first_cell.winfo_rootx()}')

    def test_datetime_live_and_zone_lines_never_overlap(self):
        """底行两段**不许重叠**——「实时更新中」跨前几列、「本地时区」占最后一列。

        跨列跨度（``columnspan``）是算出来的（``len(columns)*2 - 2``），列数一
        变化或跨度写错，两段就会叠在一起：文字互相压字，界面上看是「乱码」。
        **逐个宽度验**：默认宽度下余量大、重叠不一定显形，窄到 1000 才暴露。
        """
        window = self._open(DateTimeWindow)
        card = self._clock_card(window)
        live = [w for w in self._descendants(card)
                if isinstance(w, tk.Label) and '实时更新中' in str(w.cget('text'))]
        self.assertEqual(len(live), 1)
        live, zone = live[0], window._clock_zone

        for width in (1000, 1120, 1300, 1500):
            window.win.geometry(f'{width}x720')
            window.win.update()
            window.win.update_idletasks()
            live_right = live.winfo_rootx() + live.winfo_width()
            zone_left = zone.master.winfo_rootx()
            self.assertLess(
                live_right, zone_left,
                f'窗口 {width} 时两段重叠了：'
                f'实时更新中右缘={live_right} 本地时区左缘={zone_left}')

    def test_datetime_no_clock_cell_sub_line_has_a_dot(self):
        """**三格的说明行都不带绿点**——绿点只在底行两处。

        美东那格曾按用户要求加过点，随后被要求「周三前面的绿点去掉」。这条把
        「三格都不带点」钉住，避免哪天又顺手加回去。
        """
        window = self._open(DateTimeWindow)
        for key in ('beijing_sub', 'millis_sub', 'eastern_sub'):
            self.assertEqual(
                self._row_dots(window._clock_cells[key]), [],
                f'{key} 那格不该有绿点')
            label = window._clock_cells[key]
            self.assertEqual(
                label.winfo_rootx(), label.master.winfo_rootx(),
                f'{key} 那格的文字没顶到行首——绿点去掉了，缩进还在')

    def test_datetime_clock_timer_is_cancelled_on_close(self):
        """定时器必须在关闭时取消。

        漏掉这一步，窗口销毁后下一次 tick 会让 Tk 报
        ``invalid command name``——控制台刷屏，用户看到的是「关不干净」。
        """
        window = self._open(DateTimeWindow)
        self.assertIsNotNone(window._clock_id, '时钟定时器没有启动')
        window.close()
        self.assertIsNone(window._clock_id, '关闭后定时器句柄没清')
        self.panel.win.update()

    def test_datetime_clock_timer_is_separate_from_the_debounce_timer(self):
        """时钟与输入防抖用两个独立句柄，不能共用一个。

        共用会让「敲键盘」取消掉时钟（``_on_input`` 会 cancel ``_after_id``），
        时钟在用户输入的那一刻停掉——正好是最容易被误解成「界面卡住」的时机。
        """
        window = self._open(DateTimeWindow)
        window._on_input()
        self.assertIsNotNone(window._after_id)
        self.assertIsNotNone(window._clock_id)
        self.assertNotEqual(window._after_id, window._clock_id,
                            '时钟与防抖共用了同一个句柄')

    def test_datetime_min_size_keeps_the_three_columns_usable(self):
        """最小尺寸下三栏都要还能用：下拉文字不被裁、结果块还有高度。

        **断言的是结果块高度而不是输入框**：输入框是单行 ``Entry``，高度恒为
        20px，无论怎么压都不会变——第一版就是这么写的，变异验证里把
        ``min_height`` 降到 440 也照样通过，属于**断言错了对象**。
        """
        window = self._open(DateTimeWindow)
        window.win.geometry(f'{window.min_width}x{window.min_height}')
        window.win.update()
        window.win.update_idletasks()
        for key in ('stamp', 'date', 'zone'):
            block = window.rows[key]['result'].master.master
            self.assertGreaterEqual(
                block.winfo_height(), 60,
                f'{key} 结果块被压扁了：{block.winfo_height()}px')
            drops = window.rows[key]['drops']
            drop = drops[next(iter(drops))]
            # 下拉文字没被裁（Label 实测宽度不显著小于请求宽度）
            self.assertGreaterEqual(
                drop.label_widget.winfo_width() + 4,
                drop.label_widget.winfo_reqwidth(),
                f'{key} 那栏的下拉文字被裁了，用户会看到断掉的选项')

    def test_datetime_min_width_keeps_the_dropdown_text_whole(self):
        """最小宽度必须够宽，让三个下拉的完整文案都放得下。

        **宽度也要调高**（与别的工具不同）：三栏并排，每栏至少要放得下
        「北京时间 (UTC+08:00)」（实测约 185px）。这条把那个下限钉住——把
        ``min_width`` 退回 660 时每栏只剩 ~205px，文案会被裁成
        「北京时间 (UTC+08:0」。
        """
        window = self._open(DateTimeWindow)
        self.assertGreater(window.min_width, TW.MIN_W,
                           '三栏版式需要比默认更宽的下限')
        window.win.geometry(f'{window.min_width}x{window.min_height}')
        window.win.update()
        window.win.update_idletasks()
        card = window.rows['stamp']['card']
        # 「北京时间 (UTC+08:00)」在整个窗口最小宽度下必须完整可见。
        longest = '北京时间 (UTC+08:00)'
        label = window.rows['stamp']['drops']['zone'].label_widget
        self.assertEqual(label.cget('text'), longest)
        self.assertGreaterEqual(label.winfo_width() + 4,
                                label.winfo_reqwidth(),
                                '最小宽度下最长的下拉文案被裁掉了')
        self.assertGreater(card.winfo_width(), 240,
                           '每栏太窄，装不下完整的下拉文案')

    def test_datetime_default_size_shows_everything(self):
        """默认尺寸下三栏的**结果块**都在正文区内完整可见（不被裁掉底部）。"""
        window = self._open(DateTimeWindow)
        window.win.update()
        for key in ('stamp', 'date', 'zone'):
            block = window.rows[key]['result'].master.master
            bottom = (block.winfo_rooty() - window._body.winfo_rooty()
                      + block.winfo_height())
            self.assertLessEqual(bottom, window._body.winfo_height() + 2,
                                 f'{key} 的结果块超出了正文区')


def _png_bytes(size=(40, 30), color=(220, 60, 60)):
    """内存里造一张 PNG，不落任何临时文件。"""
    import io as iolib

    from PIL import Image
    buffer = iolib.BytesIO()
    Image.new('RGB', size, color).save(buffer, 'PNG')
    return buffer.getvalue()


def _b64(text):
    import base64 as b64lib
    return b64lib.b64encode(text).decode('ascii')


def _jpeg_bytes(size=(28, 20), quality=85):
    """内存里造一张 JPEG。前缀那条测试需要**非 PNG** 的格式。"""
    import io as iolib

    from PIL import Image
    buffer = iolib.BytesIO()
    Image.new('RGB', size, (40, 120, 220)).save(buffer, 'JPEG', quality=quality)
    return buffer.getvalue()


def _data_uri(raw, mime='image/png'):
    """原始字节 → 带前缀的 Data URI（单行，与「复制结果」的产物同形）。"""
    return f'data:{mime};base64,' + _b64(raw)


@unittest.skipUnless(TK_OK, SKIP_REASON)
class ImageToolWindowTests(unittest.TestCase):
    """图片 ↔ Base64 两个工具窗口。

    纯逻辑（编码/解码/异常）在 ``tests/test_image_b64.py`` 里已经测透，这里
    只盯**外壳接线**：左栏换成了图片面板、按钮对不对、状态灯跟不跟、清空是否
    真的把图和缓存一起丢掉。
    """

    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.panel = FakePanel(self.root, tk)
        self.created = []

    def tearDown(self):
        for window in self.created:
            window.close()
        if hasattr(self, '_tmp_ctx'):
            self._tmp_ctx.cleanup()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _open(self, cls):
        window = cls(self.panel)
        key = next((k for k, _l, c in TOOLS if c is cls), None)
        if key is not None:
            registry = getattr(self.panel, '_tool_windows', None)
            if registry is None:
                registry = {}
                self.panel._tool_windows = registry
            registry[key] = window
        self.created.append(window)
        window.win.update()
        return window

    def _buttons(self, window):
        return [str(w.cget('text')) for w in self._descendants(window.win)
                if isinstance(w, tk.Button)]

    def _type(self, window, text):
        window.input.delete('1.0', 'end')
        window.input.insert('1.0', text)
        window._refresh()

    def _descendants(self, widget):
        found = []
        for child in widget.winfo_children():
            found.append(child)
            found += self._descendants(child)
        return found

    # ---------- 布局 ----------
    def test_left_column_is_a_preview_not_a_text_box(self):
        """左栏必须是图片预览。**不能留一个能键入的空文本框**——图片打不进去。

        判据是「预览已 mapped」且「隐藏 Text 未 mapped」：后者正是被
        ``grid_remove()`` 藏起来的那个粘贴入口，它必须还在（删除会丢掉
        base64 的粘贴路径），但绝不能占屏幕。
        """
        window = self._open(ImageToBase64Window)
        self.assertTrue(window.uses_image_pane)
        self.assertTrue(window.preview.winfo_ismapped(), '预览区没有显示')
        self.assertFalse(window.input.winfo_ismapped(),
                         '隐藏的粘贴 Text 不该占屏幕')

    def test_left_column_matches_right_column_width(self):
        """两栏仍要等宽——换了左栏内容不能把这条既有约束弄丢。"""
        window = self._open(ImageToBase64Window)
        self.assertEqual(window.preview.master.master.winfo_width(),
                         window.output.master.winfo_width())

    def test_hint_asks_for_ctrl_v(self):
        """空态提示必须写明 Ctrl+V：这是本工具的主要输入方式。

        **判据用 ``assertIn(('', 'normal'))``**：Tk 对未显式设过 state 的 canvas
        项返回空字符串，语义上等同于 ``normal``。直接断言 ``== 'normal'`` 会红，
        而这个差别与「提示可见与否」无关，不能拿它当判据。
        """
        window = self._open(ImageToBase64Window)
        self.assertIn('Ctrl+V', window.preview_hint.cget('text'))
        self.assertIn(window.preview.itemcget(window._hint_id, 'state'),
                      ('', 'normal'), '空态提示被隐藏了')

    def test_open_file_button_is_named_for_files(self):
        """文件名按钮要带省略号：它确实会弹对话框，省略号是这个约定的标记。"""
        window = self._open(ImageToBase64Window)
        labels = self._buttons(window)
        self.assertIn('选择图片文件…', labels)
        self.assertIn('从剪贴板取图', labels)

    # ---------- 图片转 Base64 ----------
    def test_image_encodes_to_base64_in_output(self):
        import base64 as b64lib
        raw = _png_bytes()
        window = self._open(ImageToBase64Window)
        self.assertTrue(window._adopt_raw(raw, source='t.png'))
        window.win.update()
        text = window.output.get('1.0', 'end-1c')
        # 结果栏里是折行的 Data URI，去空白后再解。
        self.assertEqual(b64lib.b64decode(image_b64.strip_payload(text)[0]), raw,
                         '结果不是原字节的 base64——中间被重新编码了')
        self.assertIn('PNG', window.status.cget('text'))
        self.assertEqual(window.status.cget('fg').upper(), '#30D158')
        self.assertEqual(window._lamp.itemcget(window._lamp_id, 'fill').upper(),
                         '#30D158')

    def test_preview_shows_photo_and_hides_hint(self):
        window = self._open(ImageToBase64Window)
        window._adopt_raw(_png_bytes(), source='t.png')
        window.win.update()
        self.assertTrue(window.preview.find_withtag('photo'), '预览没有画出图像')
        self.assertEqual(window.preview.itemcget(window._hint_id, 'state'),
                         'hidden', '有图之后空态提示还在')

    def test_result_carries_data_uri_prefix(self):
        """产物必须带 ``data:image/png;base64,`` 前缀（用户明确要求）。

        也能被「Base64 转图片」原样解回来——前缀是可选输入，不是额外负担。
        """
        window = self._open(ImageToBase64Window)
        window._adopt_raw(_png_bytes(), source='t.png')
        window.win.update()
        text = window.output.get('1.0', 'end-1c')
        self.assertTrue(text.startswith('data:image/png;base64,'),
                        f'前提缀缺失：{text[:40]!r}')
        self.assertEqual(window._payload, image_b64.compact(text),
                         '产物缓冲与结果栏内容不一致')
        self.assertEqual(image_b64.decode_base64(window._payload).format, 'PNG')

    def test_clear_drops_image_and_restores_hint(self):
        window = self._open(ImageToBase64Window)
        window._adopt_raw(_png_bytes(), source='t.png')
        window.win.update()
        window._clear()
        window.win.update()
        self.assertIsNone(window._image)
        self.assertEqual(window.output.get('1.0', 'end-1c'), '')
        self.assertEqual(window.preview.itemcget(window._hint_id, 'state'),
                         'normal', '清空后空态提示没回来')

    def test_image_tool_export_buttons_are_present(self):
        """图片工具的导出按钮在场，且文案与「导出的是图片」相符。"""
        img_labels = self._buttons(self._open(ImageToBase64Window))
        b64_labels = self._buttons(self._open(Base64ToImageWindow))
        self.assertIn('导出为 .txt', img_labels)
        self.assertIn('保存图片…', b64_labels)
        # 「导出图片」这种写法会被读成「把图另存出来」，而这里导出的是 .txt。
        self.assertNotIn('导出图片', img_labels)
        self.assertNotIn('', img_labels)
        self.assertNotIn('', b64_labels)

    def test_text_tools_have_exactly_two_footer_buttons(self):
        """文本工具的底栏必须**恰好**是「复制结果」「清空」两个按钮。

        **不能只查文字**。原先这条用「文字里没有『导出』」判断，但变异验证
        揭出真相：去掉 ``if self.export_label:`` 这个判断后，文本工具会多出
        一个**空文字按钮**——它没有文字，按文字比对的断言完全看不见，界面
        上就是一个点不出所以然的小方块。

        所以判据换成「按钮数量 + 非空文字」。空文字按钮在用户眼里是一个
        无意义的方框，与「多了一个导出按钮」是同一个缺陷的两个面孔。
        """
        for cls, name in ((JsonParamWindow, 'JSON'), (JwtWindow, 'JWT')):
            with self.subTest(tool=name):
                window = self._open(cls)
                buttons = [str(w.cget('text'))
                           for w in self._descendants(window.win)
                           if isinstance(w, tk.Button)]
                # 「JSON ▾」是 JSON 工具特有的下拉入口，不算底栏动作按钮。
                footer = [t for t in buttons if t not in ('', 'JSON ▾')]
                self.assertEqual(footer, ['复制结果', '清空'],
                                 f'{name} 底栏按钮不对：{buttons}')
                self.assertNotIn('', buttons, f'{name} 出现了空文字按钮')

    def test_bad_file_bytes_report_error_without_crashing(self):
        window = self._open(ImageToBase64Window)
        self.assertFalse(window._adopt_raw(b'not an image at all'))
        self.assertEqual(window.status.cget('fg').upper(), '#FF453A')
        self.assertEqual(window.output.get('1.0', 'end-1c'), '')

    def test_missing_image_message_is_short(self):
        """「还没有图片」的报错必须一句话说完（用户要求：文案太长挤压按钮）。

        旧文案把操作指引与「请用另一个工具」的分诊信息全塞进底栏状态行，
        661px 宽直接压扁了「清空」按钮。分诊信息已挪到左栏空态提示里
        （``_preview_hint_text``），状态行只留一句。

        判据用**字符数上限**而不是「等于某句」：文案可以再润色，但不能又变长。
        """
        window = self._open(ImageToBase64Window)
        window.input.insert('1.0', 'iVBORw0KGgo=')   # 有内容但不是图
        window._refresh()
        window.win.update()
        message = window.status.cget('text')
        self.assertEqual(window.status.cget('fg').upper(), '#FF453A',
                         '没走进报错分支，用例失去判别力')
        self.assertIn('图片', message)
        self.assertLessEqual(len(message), 20,
                             f'状态行文案太长（{len(message)} 字）：{message!r}')

    def test_diagnostic_guidance_moved_to_left_pane_hint(self):
        """「想反过来解 base64 请用另一个工具」要说，但放在左栏提示里。

        这是从底栏状态行挪过来的内容。左栏正中留白足够，三行提示不会挤到
        任何控件；放回状态行就会重演挤压缺陷。
        """
        window = self._open(ImageToBase64Window)
        hint = window.preview_hint.cget('text')
        self.assertIn('Base64 转图片', hint, '分诊指路不见了')
        self.assertIn('Ctrl+V', hint)
        self.assertIn('选择图片文件…', hint)

    # ---------- Base64 转图片 ----------
    def test_base64_text_left_and_image_right(self):
        """布局契约（用户要求）：左栏贴 Base64 文本，右栏展示图片。

        与「图片转 Base64」正好镜像。左栏必须是**可见可编辑**的 Text；
        右栏是图片面板、**没有**结果 Text——格式/尺寸/体积进状态栏。
        面板也不建隐藏 Text（``self.input`` 就是左栏那个可见框）。
        """
        window = self._open(Base64ToImageWindow)
        self.assertEqual(str(window.input.cget('state')), 'normal',
                         '左栏 Base64 文本框必须可编辑')
        self.assertEqual(window.input.master.grid_info()['column'], 0,
                         'Base64 输入框应占据左栏')
        self.assertEqual(window.image_pane.grid_info()['column'], 1,
                         '图片预览面板应占据右栏')
        self.assertFalse(hasattr(window, 'output'),
                         '右栏是图片面板，不应再建结果文本框')
        # 镜像的另一侧不能被顺手改掉：图片转 Base64 仍是左图右文。
        img_window = self._open(ImageToBase64Window)
        self.assertIs(img_window.input.master, img_window.image_pane,
                      '图片转 Base64 的输入框应是面板里的隐藏 Text')
        self.assertEqual(img_window.image_pane.grid_info()['column'], 0)
        self.assertEqual(img_window.output.master.grid_info()['column'], 1)

    def test_base64_decodes_into_preview(self):
        from PIL import Image
        raw = _png_bytes((24, 18))
        window = self._open(Base64ToImageWindow)
        window._set_paste_text(_b64(raw))
        window.win.update()
        self.assertIsInstance(window._image, Image.Image)
        self.assertEqual(window._image.size, (24, 18))
        self.assertTrue(window.preview.find_withtag('photo'))
        # 右栏没有文字结果了：格式与尺寸只进状态栏。
        status = window.status.cget('text')
        self.assertIn('PNG', status)
        self.assertIn('24 × 18', status)

    def test_prefix_matches_actual_format(self):
        """前缀的 MIME 必须跟着**实际格式**走，不能一律写 ``image/png``。

        写死 png 时浏览器会拿 PNG 解码器去解 JPEG 字节，图直接显示不出来——
        而界面上一切正常（状态栏也写着 JPEG），是典型的「看着对、用起来坏」。
        """
        window = self._open(ImageToBase64Window)
        raw = _png_bytes((30, 20))
        jpeg = _jpeg_bytes()
        for sample, mime in ((raw, 'image/png'), (jpeg, 'image/jpeg')):
            with self.subTest(mime=mime):
                window._adopt_raw(sample, source='x')
                window.win.update()
                self.assertTrue(window._payload.startswith(f'data:{mime};base64,'),
                                f'{mime} 的前缀不对：{window._payload[:40]!r}')

    def test_prefix_survives_bmp_forced_to_png(self):
        """BMP 会被降级成 PNG，前缀也必须跟着变成 ``image/png``。

        这里挡的是「按原始 MIME 拼前缀」的写法：原始格式是 BMP，实际编码出来
        的字节却是 PNG，前缀若还用 ``image/bmp`` 就与实际字节不符。
        """
        import io as iolib

        from PIL import Image
        buf = iolib.BytesIO()
        Image.new('RGB', (12, 9), (30, 90, 200)).save(buf, 'BMP')
        window = self._open(ImageToBase64Window)
        window._adopt_raw(buf.getvalue(), source='x.bmp')
        window.win.update()
        self.assertTrue(window._payload.startswith('data:image/png;base64,'))
        self.assertIn('PNG', window.status.cget('text'))

    def test_roundtrip_through_both_tools(self):
        """从「图片转 Base64」复制出来的东西，直接粘进「Base64 转图片」要能解。

        这是两个工具互为反向的实际契约——前缀是这里面唯一的接口约定，
        改动它的形态（比如改成 ``data:;base64,``）会静默破坏往返。
        """
        encoder = self._open(ImageToBase64Window)
        encoder._adopt_raw(_png_bytes((36, 24)), source='t.png')
        encoder.win.update()
        copied = encoder._copy_content(encoder.output.get('1.0', 'end-1c'))

        decoder = self._open(Base64ToImageWindow)
        decoder._set_paste_text(copied)
        decoder.win.update()
        self.assertIsNotNone(decoder._image)
        self.assertEqual(decoder._image.size, (36, 24))
        self.assertEqual(decoder.status.cget('fg').upper(), '#30D158')

    def test_prefix_is_optional_on_the_decode_side(self):
        """解码侧既收带前缀的串，也收裸串——用户手里两种形态都有。"""
        raw = _png_bytes((20, 16))
        window = self._open(Base64ToImageWindow)
        for payload in (_data_uri(raw), _b64(raw)):
            with self.subTest(starts_with_data_uri=payload.startswith('data:')):
                window._set_paste_text(payload)
                window.win.update()
                self.assertIsNotNone(window._image)
                self.assertEqual(window._image.size, (20, 16))

    def test_hint_tells_users_the_prefix_is_accepted(self):
        """左栏提示要写明前缀可带可不带，否则用户会以为自己粘错了。"""
        window = self._open(Base64ToImageWindow)
        self.assertIn('data:image/png;base64,', window.preview_hint.cget('text'))

    def test_copy_and_save_live_in_the_footer(self):
        """复制/保存图片都在底栏，面板不重复摆按钮（右栏整栏留给预览）。

        旧布局「复制图片」挂在预览下方；改成左文右图后动作统一收进底栏：
        复制图片 / 保存图片… / 清空。右栏没有文字结果，不该再出现「复制结果」。
        """
        labels = self._buttons(self._open(Base64ToImageWindow))
        self.assertIn('复制图片', labels)
        self.assertIn('保存图片…', labels)
        self.assertNotIn('复制结果', labels,
                         '右栏没有文字结果，不该有「复制结果」按钮')
        self.assertNotIn('从剪贴板取图', labels)
        self.assertNotIn('选择图片文件…', labels)

    def test_copy_without_image_reports_error(self):
        """右栏是图片面板时「复制」动作分派到 ``_copy_image``。没图时报错。"""
        window = self._open(Base64ToImageWindow)
        window._copy_output()
        self.assertIn('图片', window.status.cget('text'))
        self.assertEqual(window.status.cget('fg').upper(), '#FF453A')

    def test_invalid_base64_reports_error_and_clears_preview(self):
        """解码失败要报错，且**上一张旧图必须清掉**——图是产物不是输入。"""
        window = self._open(Base64ToImageWindow)
        window._set_paste_text(_b64(_png_bytes((24, 18))))
        window.win.update()
        self.assertIsNotNone(window._image)
        window._set_paste_text('this is not base64 !!!')
        window.win.update()
        self.assertEqual(window.status.cget('fg').upper(), '#FF453A')
        self.assertIsNone(window._image, '解码失败后旧图还挂在预览里')
        self.assertFalse(window.preview.find_withtag('photo'))
        self.assertEqual(window.preview.itemcget(window._hint_id, 'state'),
                         'normal', '出错后空态提示没回来')

    def test_clearing_input_clears_stale_preview(self):
        """删光左栏文本后右栏不能还挂着上一张图。"""
        window = self._open(Base64ToImageWindow)
        window._set_paste_text(_b64(_png_bytes((24, 18))))
        window.win.update()
        window._set_paste_text('')
        window.win.update()
        self.assertIsNone(window._image)
        self.assertEqual(window.status.cget('text'), window.placeholder)

    def test_valid_base64_of_non_image_reports_error(self):
        window = self._open(Base64ToImageWindow)
        window._set_paste_text(_b64(b'just some text'))
        window.win.update()
        self.assertEqual(window.status.cget('fg').upper(), '#FF453A')
        self.assertIn('图片', window.status.cget('text'))

    def test_repeated_refresh_does_not_redecode(self):
        """同一段载荷被反复刷新时不该重解码。

        ``_refresh`` 会因粘贴、防抖、预览重建被多次调到；对一张几 MB 的图
        每次都 decode 会让界面明显卡顿。这里用「解码结果的 identity」当判据
        ——只有命中缓存才可能拿到同一个对象。
        """
        window = self._open(Base64ToImageWindow)
        window._set_paste_text(_b64(_png_bytes()))
        window.win.update()
        first = window._decoded
        window._refresh()
        window._refresh()
        self.assertIs(window._decoded, first, '每次刷新都重新解码了')

    def test_changed_payload_invalidates_cache(self):
        """缓存不能粘住：内容一变必须重解，否则改一个字还是旧图。"""
        window = self._open(Base64ToImageWindow)
        window._set_paste_text(_b64(_png_bytes((24, 18))))
        window.win.update()
        first = window._decoded
        window._set_paste_text(_b64(_png_bytes((30, 22))))
        window.win.update()
        self.assertIsNot(window._decoded, first)
        self.assertEqual(window._image.size, (30, 22))

    def test_clear_drops_decode_cache(self):
        """清空后必须丢掉缓存，否则再粘同一段文本会命中旧结果。"""
        window = self._open(Base64ToImageWindow)
        window._set_paste_text(_b64(_png_bytes()))
        window.win.update()
        window._clear()
        self.assertIsNone(window._decoded)
        self.assertIsNone(window._decoded_for)
        self.assertIsNone(window._image)

    def test_save_dialog_uses_format_derived_suffix(self):
        """保存对话框的后缀必须来自 ``image_b64.save_suffix``，不能写死。

        这条**替换掉了原先「读 ``export`` 源码里有没有 ``write_fmt``」**的写法
        ——用测试去断言「代码长什么样」是用测试补架构缺口。后缀映射抽成纯
        函数之后，行为本身在 ``tests/test_image_b64.py`` 里直测；这里只守
        「保存路径确实用了它」这一条接线。

        拦截方式：把假 ``filedialog`` 模块塞进 ``sys.modules``，捕获它收到的
        ``defaultextension``，同时把 ``save_suffix`` 换成返回标记值的替身。
        写死常量的实现拿不到标记值，立刻变红。

        **不能靠 ``mock.patch('builtins.__import__')``**：``from tkinter import
        filedialog`` 是在已导入的 ``tkinter`` 包上取属性，根本不走 import 钩子；
        钩子没拦住，真的对话框就会弹出来把测试挂住（实测直接 SIGTERM）。
        """
        import sys as sysmod
        import types
        from unittest import mock

        window = self._open(Base64ToImageWindow)
        window._set_paste_text(_b64(_png_bytes()))
        window.win.update()

        captured = {}
        calls = []

        def fake_suffix(fmt):
            calls.append(fmt)
            return '.MARKER'

        fake = types.ModuleType('tkinter.filedialog')
        fake.asksaveasfilename = lambda **kw: captured.update(kw) or ''
        with mock.patch.dict(sysmod.modules, {'tkinter.filedialog': fake}), \
                mock.patch.object(tk, 'filedialog', fake, create=True), \
                mock.patch.object(image_b64, 'save_suffix', fake_suffix):
            window.export()
        # ``patch.object(obj, name, new)`` 的 ``as`` 拿到的是 ``new`` 本身而不是
        # MagicMock（只有不传 new 时才给你 mock），所以计数得自己攒。
        self.assertEqual(len(calls), 1, '保存时没调用 save_suffix')
        self.assertEqual(captured.get('defaultextension'), '.MARKER',
                         '保存对话框没用 save_suffix 算出来的后缀')

    # ---------- 保存/选择对话框必须盖在主面板之上（用户报「点保存后卡死」） ----------
    def test_file_dialog_raises_tool_window_topmost(self):
        """打开系统文件对话框前，工具窗口必须临时置顶。

        用户报「Base64 转图片 右下角的保存按钮点击后就卡死整个页面」。根因：
        系统对话框的最顶层**只跟着它的 owner（工具窗口）走**，而工具窗口的
        ``-topmost`` 只在构造时同步过一次（``_follow_topmost``）。主面板之后
        才变置顶时（鼠标悬停悬浮球唤起面板，``ui._show_panel_from_floating``
        传 ``topmost=not locked`` 即为 True），工具窗仍是 0，于是对话框落在
        主面板**下面**——它确实弹出来了、也确实可交互，只是被完全盖住。用户
        看到「点了保存什么都没出现」，同时工具窗口已被模态对话框禁用，
        合起来就是「整个页面卡死」。

        这条守的是**接线**：对话框打开期间工具窗必须处于置顶。判据取「调用
        opener 那一刻的 ``-topmost``」，用一个假的 filedialog 在对话框弹出
        的瞬间采样——不是断言源码长什么样。
        """
        import sys as sysmod
        import types
        from unittest import mock

        window = self._open(Base64ToImageWindow)
        window._set_paste_text(_b64(_png_bytes()))
        window.win.update()
        # 构造时的同步不足以代表此刻：主面板可能是在工具窗建好之后才置顶的。
        window.win.attributes('-topmost', False)
        window.win.update()
        self.assertFalse(window.win.attributes('-topmost'), '前置条件没建立')

        seen = {}

        def fake_save(**kw):
            # 对话框创建瞬间的层级——这正是决定它盖不盖得住主面板的那个值。
            seen['topmost'] = window.win.attributes('-topmost')
            return ''

        fake = types.ModuleType('tkinter.filedialog')
        fake.asksaveasfilename = fake_save
        with mock.patch.dict(sysmod.modules, {'tkinter.filedialog': fake}), \
                mock.patch.object(tk, 'filedialog', fake, create=True):
            window.export()

        self.assertTrue(seen.get('topmost'),
                        '保存对话框不是在工具窗置顶的状态下打开的——'
                        '主面板置顶时它会被完全盖住，表现就是「点保存后卡死」')

    def test_file_dialog_restores_previous_topmost(self):
        """对话框关闭后工具窗要回到原来的层级，不能把置顶永久留下。

        临时置顶是**对话框的护栏**，不是工具的显示偏好：用完必须还原，否则
        用户关掉对话框后会得到一个意外常驻最前的窗口（那会重新引入「盖住别的
        应用」这类问题），而且会掩盖 ``_follow_topmost`` 的跟随语义。
        """
        import sys as sysmod
        import types
        from unittest import mock

        window = self._open(Base64ToImageWindow)
        window._set_paste_text(_b64(_png_bytes()))
        window.win.update()
        window.win.attributes('-topmost', False)
        window.win.update()

        fake = types.ModuleType('tkinter.filedialog')
        fake.asksaveasfilename = lambda **kw: ''
        with mock.patch.dict(sysmod.modules, {'tkinter.filedialog': fake}), \
                mock.patch.object(tk, 'filedialog', fake, create=True):
            window.export()
        window.win.update()

        self.assertFalse(window.win.attributes('-topmost'),
                         '对话框用的临时置顶没有还原')

    def test_file_dialog_keeps_topmost_when_already_set(self):
        """本来就置顶时，对话框走完仍应置顶——别把用户原有的层级降下来。

        ``_run_file_dialog`` 的 ``finally`` 只该在**自己动过**置顶时才恢复。
        无条件恢复会把「跟随主面板置顶」的工具窗在第一次开对话框后永久变成
        非置顶，反过来被主面板盖住——一个修复制造另一个缺陷。
        """
        import sys as sysmod
        import types
        from unittest import mock

        window = self._open(Base64ToImageWindow)
        window._set_paste_text(_b64(_png_bytes()))
        window.win.update()
        window.win.attributes('-topmost', True)
        window.win.update()

        fake = types.ModuleType('tkinter.filedialog')
        fake.asksaveasfilename = lambda **kw: ''
        with mock.patch.dict(sysmod.modules, {'tkinter.filedialog': fake}), \
                mock.patch.object(tk, 'filedialog', fake, create=True):
            window.export()
        window.win.update()

        self.assertTrue(window.win.attributes('-topmost'),
                        '本就是置顶的工具窗被降级了')

    def test_open_file_dialog_also_raises_topmost(self):
        """「选择图片文件…」走同一条保护——它不是保存对话框的例外。

        图片转 Base64 的这个入口同样会弹系统对话框，同样会被置顶主面板盖住。
        **逐个入口都要覆盖**：只给 ``export`` 加护栏的话，用户换个按钮点一次
        就复现同一个「卡死」，而且更难联想到是同一处根因。
        """
        import sys as sysmod
        import types
        from unittest import mock

        window = self._open(ImageToBase64Window)
        window.win.attributes('-topmost', False)
        window.win.update()

        seen = {}

        def fake_open(**kw):
            seen['topmost'] = window.win.attributes('-topmost')
            return ''

        fake = types.ModuleType('tkinter.filedialog')
        fake.askopenfilename = fake_open
        with mock.patch.dict(sysmod.modules, {'tkinter.filedialog': fake}), \
                mock.patch.object(tk, 'filedialog', fake, create=True):
            window._load_from_file()

        self.assertTrue(seen.get('topmost'),
                        '「选择图片文件…」没走置顶保护，主面板置顶时会盖住对话框')

    def test_export_dialog_is_actually_on_top_of_a_topmost_panel(self):
        """端到端判决：主面板置顶时，对话框必须真的盖在它上面。

        用一个**真的系统对话框**（不 mock）在子线程里打开，然后用
        ``WindowFromPoint`` 取对话框中心点上最顶层的窗口句柄，逐级回到最外层
        再比对是不是对话框自己。

        为什么是这个判据：``IsWindowVisible`` 与逐像素采样都**分不出**「对话框
        被盖住」——被盖住的对话框依然 visible，屏幕上也确实有一片白色（那是
        主面板）。``WindowFromPoint`` 直接回答「用户在这个坐标上看到的是谁」，
        这是该缺陷唯一可靠的观测方式（实测修复前命中主面板的 ``TkChild``、
        修复后命中 ``#32770``）。

        **必须把窗口调成真不透明。** ``tests/__init__.py`` 的全局挂钩让窗口
        ``-alpha 0``，而全透明的 layered window 不参与命中测试——盖在上面的
        主面板会从 ``WindowFromPoint`` 里「消失」，对话框看起来永远在最前，
        这条断言就失去了判别力（实测：加 ``make_visible`` 之前，即便把修复
        拆掉本条也照样绿）。这也是本文件里仅有的第二个需要真可见的用例。
        """
        import threading
        import time

        from tests.silent_ui import make_visible, silence

        user32 = ctypes.WinDLL('user32', use_last_error=True)
        user32.WindowFromPoint.restype = ctypes.c_void_p
        user32.WindowFromPoint.argtypes = [ctypes.wintypes.POINT]
        user32.GetAncestor.restype = ctypes.c_void_p
        user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        GA_ROOT = 2

        class _Rect(ctypes.Structure):
            _fields_ = [('left', ctypes.c_long), ('top', ctypes.c_long),
                        ('right', ctypes.c_long), ('bottom', ctypes.c_long)]

        def class_name(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(ctypes.c_void_p(hwnd), buf, 256)
            return buf.value

        def root_of(hwnd):
            return user32.GetAncestor(ctypes.c_void_p(hwnd), GA_ROOT)

        def find_dialog():
            user32 = ctypes.WinDLL('user32')
            proc = ctypes.WINFUNCTYPE(
                ctypes.wintypes.BOOL, ctypes.wintypes.HWND,
                ctypes.wintypes.LPARAM)
            found = []
            me = os.getpid()

            def cb(hwnd, _l):
                pid = ctypes.wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value == me and class_name(hwnd) == '#32770':
                    found.append(hwnd)
                return True

            user32.EnumWindows(proc(cb), 0)
            return found

        # 主面板置顶，工具窗**不**跟随——正是用户遇到的那个组合。
        self.panel.win.deiconify()
        self.panel.win.geometry('900x600+80+40')
        self.panel.win.attributes('-topmost', True)
        window = self._open(Base64ToImageWindow)
        window.win.deiconify()
        window.win.attributes('-topmost', False)
        window.win.geometry('760x520+420+150')
        window.win.update()
        window._set_paste_text(_b64(_png_bytes()))
        window.win.update()
        # 命中测试需要真实不透明（见 docstring）；结束时在 finally 里压回去。
        make_visible(self.panel.win, window.win)
        window.win.update()

        hit = {}
        stop = threading.Event()

        def watcher():
            deadline = time.time() + 6
            dlg = None
            while time.time() < deadline and not stop.is_set():
                found = find_dialog()
                if found:
                    dlg = found[0]
                    break
                time.sleep(0.05)
            if not dlg:
                hit['error'] = '对话框没出现'
                return
            # **连续采样取稳定值，不能只看一个瞬间。** 对话框创建时 Z 序会短暂
            # 变化（owner 与对话框之间要重排一次），单次采样有机会正好撞上那个
            # 过渡态而误判成「被盖住」——实测在整套测试连跑时偶发失败、单独跑
            # 却稳定通过，就是这个原因。要求连续 3 次都命中同一窗口才算数。
            rect = _Rect()
            user32.GetWindowRect(ctypes.c_void_p(dlg), ctypes.byref(rect))
            pt = ctypes.wintypes.POINT((rect.left + rect.right) // 2,
                                       (rect.top + rect.bottom) // 2)
            dialog_root = root_of(dlg)
            stable = None
            streak = 0
            deadline = time.time() + 5
            while time.time() < deadline:
                current = root_of(user32.WindowFromPoint(pt))
                if current == dialog_root:
                    streak += 1
                    if streak >= 3:
                        stable = current
                        break
                else:
                    streak = 0
                time.sleep(0.1)
            hit['at_center'] = stable
            hit['dialog'] = dialog_root
            for handle in find_dialog():
                user32.PostMessageW(ctypes.c_void_p(handle), 0x0010, 0, 0)

        thread = threading.Thread(target=watcher, daemon=True)
        thread.start()
        window.win.after(300, window.export)
        # 模态对话框阻塞在 tk.call 里，用 after 轮询驱动事件循环直到它关闭。
        deadline = time.time() + 8
        while time.time() < deadline and 'dialog' not in hit:
            window.win.update()
            time.sleep(0.05)
        stop.set()
        thread.join(timeout=4)

        try:
            self.assertNotIn('error', hit, hit.get('error', ''))
            self.assertIsNotNone(
                hit['at_center'],
                '对话框中心点连续 5 秒都没轮到对话框在最顶层——被主面板盖住了，'
                '用户看到的就是「点了保存没反应，界面像卡死」')
            self.assertEqual(
                hit['at_center'], hit['dialog'],
                f'对话框中心点上最顶层的是 0x{(hit["at_center"] or 0):X}，'
                f'而对话框是 0x{(hit["dialog"] or 0):X}——被盖住了')
        finally:
            self.panel.win.attributes('-topmost', False)
            silence(self.panel.win, window.win)

    # ---------- 大图性能（用户报「图片稍微大一点转换就很卡」） ----------
    def test_output_is_wrapped_into_lines(self):
        """结果栏里必须是**折行**的 base64，不能是一整行。

        **这是性能守卫，不是排版偏好。** 用户报「图片稍微大一点转换就很卡」，
        实测根因就在这里：base64 是一整行、不含空格，``tk.Text(wrap="char")``
        会为它逐字符找折行点。1.79 M 字符平铺单行实测让 Tk 重排 **310 秒**
        （另一轮 48.7 s），折成 76 字符的行后只要 **18.4 ms**，而 ``insert()``
        本身始终只花 2.4 ms —— 全部代价都在折行计算上。

        判据用「显示行数 ≈ 字符数 / 76」而**不是计时**：计时在共享 CI 上会
        抖动，而这个数量关系是缺陷的直接指纹，且与机器快慢无关。
        """
        window = self._open(ImageToBase64Window)
        window._adopt_raw(_png_bytes(), source='t.png')
        window.win.update()
        chars = len(window._payload)
        shown = int(window.output.index('end-1c').split('.')[0])
        self.assertEqual(shown, -(-chars // image_b64.WRAP_WIDTH),
                         f'{chars} 字符应折成 {-(-chars // image_b64.WRAP_WIDTH)} 行，'
                         f'实际 {shown} 行——单行会把 Tk 卡死')

    def test_wrap_survives_a_large_payload(self):
        """大图也必须折行。小图的行数关系对了，不代表大图没走特例分支。

        这里的样本要大到「若退回单行则必然卡顿」的量级。**不能用计时断言**，
        仍用行数：几百万字符折行后行数上万，这个量级骗不了人。

        样本用**纯随机字节**而不是 ``Image.new`` 填色：填色图 JPEG 压得极狠，
        1400x1000 也才 70 KB，测不出「大」。随机噪声压不动，同样的尺寸能到
        2 MB 以上。
        """
        import io as iomod

        from PIL import Image
        # 固定种子保证可复现。噪声数据的字节数是尺寸决定的，不受 PIL 版本影响。
        rnd = random.Random(11)
        side = 1400
        img = Image.frombytes(
            'RGB', (side, 1000),
            bytes(rnd.randrange(256) for _ in range(side * 1000 * 3)))
        buf = iomod.BytesIO()
        img.save(buf, 'JPEG', quality=95)
        raw = buf.getvalue()
        self.assertGreater(len(raw), 1_500_000,
                           f'样本只有 {len(raw):,} 字节，测不出折行缺陷')

        window = self._open(ImageToBase64Window)
        self.assertTrue(window._adopt_raw(raw, source='big.jpg'))
        window.win.update()
        chars = len(window._payload)
        self.assertGreater(chars, 2_000_000, 'base64 不够大，用例失去判别力')
        shown = int(window.output.index('end-1c').split('.')[0])
        self.assertEqual(shown, -(-chars // image_b64.WRAP_WIDTH),
                         f'{chars:,} 字符没被折行（显示 {shown} 行）')

    # ---------- 左栏单行超长 base64 必须预先折行（用户报「保存按钮卡很久」）----------
    def test_long_single_line_base64_is_prewrapped_on_paste(self):
        """粘贴超长单行 base64 后，左栏内容必须已被折成多行。

        用户报「base64转图片 右下角的保存图片按钮点击还是卡很久」。真机实测：
        4000×3000 的图 → base64 **56320 字符一整行** → 打开保存对话框要
        **2.927s**；同一段内容预先按 76 字符折行后 **0.358s**。根因是
        ``wrap="char"`` 的 ``tk.Text`` 要为不含空格的超长行逐字符找折行点，
        而这个计算在打开模态对话框时被整篇重跑（栈上恒停在原生 ``tk.call``
        内，没有任何 Python 帧——所以既不是落盘慢也不是对话框初始化慢）。

        **判据是行数而不是计时**：计时在共享机器上会抖，而行数是缺陷的直接
        指纹且与机器快慢无关（与 ``test_output_is_wrapped_into_lines`` 同思路）。
        折行也**不改语义**：base64 解码器吃所有空白，下面顺带断言仍能解出图。
        """
        payload = 'A' * 50000
        window = self._open(Base64ToImageWindow)
        window._set_paste_text(payload)
        window.win.update()

        text = window.input.get('1.0', 'end-1c')
        lines = int(window.input.index('end-1c').split('.')[0])
        self.assertEqual(lines, -(-50000 // image_b64.WRAP_WIDTH),
                         f'50000 字符应折成 {-(-50000 // image_b64.WRAP_WIDTH)} 行，'
                         f'实际 {lines} 行——单行会让保存对话框卡好几秒')
        self.assertNotIn(' ', text, '折行不该引入空格')
        self.assertEqual(len(image_b64.compact(text)), 50000,
                         '折行改变了字符总数（丢了内容）')

    def test_prewrapping_keeps_the_payload_decodable(self):
        """折行是**显示层**改动，粘进来的 base64 必须照样能解码出图。

        这条防的是「为了性能把内容改坏」：折行插入的位置若算错一位，base64
        整体移位，解码直接失败或解出花屏。所以必须端到端跑一次真图。
        """
        raw = _png_bytes((64, 48))
        window = self._open(Base64ToImageWindow)
        window._set_paste_text(_b64(raw))
        window.win.update()
        self.assertIsNotNone(window._image, '折行后解码失败')
        self.assertEqual(window._image.size, (64, 48))

    def test_wrapping_skips_content_that_would_be_corrupted(self):
        """**JSON / JWT / 普通长文本绝不能被折行**——这是安全护栏，不是偏好。

        ``<<Paste>>`` 绑定挂在**通用输入框**上，三个工具都走它。折行＝往正文里
        插真换行，而 JSON 字符串字面量里插进真换行就不再合法
        （``json.loads`` 直接报错）。所以「能不能折」由**内容形态**决定：
        只有整篇都是不含空白的紧凑 token（base64 / Data URI / JWT）才敢动。

        变异说明：去掉 ``_can_soft_wrap`` 中的字符白名单后，长 JSON 会被折行、
        本用例变红。
        """
        import json

        window = self._open(Base64ToImageWindow)
        for label, payload in (
            ('JSON 单行', json.dumps({'k': 'y' * 60000})),
            ('带空格长文本', 'hello world ' * 6000),
            ('多行文本', ('line of text\n' * 5000)),
        ):
            self.assertFalse(window._can_soft_wrap(payload),
                             f'{label} 不该被判为可折行——折了会破坏内容')

        for label, payload in (
            ('裸 base64', 'A' * 50000),
            ('Data URI', 'data:image/png;base64,' + 'A' * 50000),
            ('URL-safe base64', 'A-_B' * 12000),
        ):
            self.assertTrue(window._can_soft_wrap(payload),
                            f'{label} 应该可折行（否则卡顿没修好）')

    def test_json_tool_paste_never_gets_a_newline_inserted(self):
        """端到端确认 JSON 工具粘长 JSON 后**内容未被改动**。

        比 ``_can_soft_wrap`` 的单元判定更靠外一层：走真正的 ``<<Paste>>``
        处理器，然后断言文本里没有换行且仍是合法 JSON。
        """
        import json

        payload = json.dumps({'k': 'y' * 60000})
        window = self._open(JsonParamWindow)
        window.input.delete('1.0', 'end')
        window.input.insert('1.0', payload)
        window._on_paste_into_input()
        deadline = time.time() + 2
        while time.time() < deadline:
            window.win.update()

        text = window.input.get('1.0', 'end-1c')
        self.assertNotIn('\n', text, '粘贴路径往 JSON 里插入了换行，内容已损坏')
        self.assertEqual(json.loads(text), json.loads(payload))

    def test_short_base64_is_left_untouched(self):
        """低于阈值的内容不做改写——小图本来就不慢，没必要整篇删了重插。

        阈值存在是因为「每次粘贴都无条件重写全文」本身有代价（还会打乱撤销栈
        与光标）。实测 800×600 的小图（11264 字符）本来就是 0.292s，不需要折。
        """
        window = self._open(Base64ToImageWindow)
        payload = 'A' * 100
        window._set_paste_text(payload)
        window.win.update()
        self.assertEqual(window.input.get('1.0', 'end-1c'), payload)
        self.assertEqual(window.input.index('end-1c'), '1.100')

    def test_already_wrapped_content_is_not_rewritten(self):
        """已折行的内容再走一次粘贴路径时不该被动（幂等）。

        剪贴板里本来就带换行的 base64（从终端或编辑器复制）很常见，这种情况
        无需处理；不拦的话每次粘贴都要整篇重写一遍，白付代价。
        """
        wrapped = image_b64.wrap_text('A' * 50000)
        window = self._open(Base64ToImageWindow)
        window._set_paste_text(wrapped)
        window.win.update()
        self.assertEqual(window.input.get('1.0', 'end-1c'), wrapped)

    def test_soft_wrap_preserves_cursor_ratio(self):
        """折行后插入光标要按比例留在原处，不能跳回文首。

        用户在长 base64 里点到中间想编辑某一段，一折行就跳到开头会很恼人。
        这里按「字符偏移比例」保留：折行前后光标在全文中的相对位置不变。
        """
        payload = 'A' * 50000
        window = self._open(Base64ToImageWindow)
        window.input.delete('1.0', 'end')
        window.input.insert('1.0', payload)
        window.input.mark_set('insert', '1.0 + 25000 chars')
        window._soft_wrap_input()
        window.win.update()

        offset = len(window.input.get('1.0', window.input.index('insert')))
        self.assertAlmostEqual(offset / 25000, 1.0, delta=0.05,
                               msg=f'光标跑到 {offset}（原为 25000），比例没保住')

    def test_paste_event_path_also_prewraps(self):
        """**剪贴板事件路径**也必须折行，不能只有 ``_set_paste_text`` 那条。

        两条粘贴路径是独立的：``_set_paste_text`` 供面板 Ctrl+V / 「从剪贴板
        取图」走；用户在左栏 Text 里直接按 Ctrl+V 则走 ``<<Paste>>`` 绑定。
        只修一条，另一条照样卡——而用户不会知道自己走的是哪条入口。

        这条用**新进程实测会慢的那个量级**驱动真正的绑定处理器。原先的用例集
        只测了 ``_set_paste_text``，变异验证时「去掉 ``_on_paste_into_input``
        里的折行」直接逃逸，才发现这个缺口。
        """
        window = self._open(Base64ToImageWindow)
        # Tk 的默认粘贴处理器先跑（这里等价为直接 insert），再由绑定接手。
        window.input.insert('1.0', 'A' * 50000)
        window._on_paste_into_input()
        deadline = time.time() + 3
        while time.time() < deadline and int(
                window.input.index('end-1c').split('.')[0]) == 1:
            window.win.update()

        lines = int(window.input.index('end-1c').split('.')[0])
        self.assertEqual(lines, -(-50000 // image_b64.WRAP_WIDTH),
                         f'走 <<Paste>> 路径没折行（{lines} 行），保存对话框会卡')
        self.assertEqual(len(image_b64.compact(
            window.input.get('1.0', 'end-1c'))), 50000)

    def test_copy_gives_single_line_payload(self):
        """「复制结果」给的必须是**单行** base64，不能带折行。

        这是本工具唯一「显示 ≠ 复制」的地方。用户拿到 base64 的去处是 DB 字段、
        JSON 参数、CSS ``url()``——正文夹换行会直接把它们弄坏，而且**报错信息
        通常离得很远**，排查成本极高。
        """
        window = self._open(ImageToBase64Window)
        raw = _png_bytes((60, 40))
        window._adopt_raw(raw, source='t.png')
        window.win.update()

        displayed = window.output.get('1.0', 'end-1c')
        self.assertIn('\n', displayed, '显示文本没折行，这条测试就失去判别力')

        copied = window._copy_content(displayed)
        self.assertNotIn('\n', copied, '复制出去的内容带了换行')
        self.assertEqual(copied, _data_uri(raw),
                         '复制内容不是原始字节的 Data URI')

    def test_copy_falls_back_to_compacting_without_dropping_the_prefix(self):
        """缓冲为空时，兜底分支去换行**且保留** ``data:`` 前缀，与主路径一致。

        两件事都要守：

        1. 写成 ``self._payload or displayed`` ——看着像无害的默认值，实际把
           折行原样复制出去，正是本方法要防的事。
        2. 写成 ``unwrap_text(displayed)`` ——去换行对了，但它顺带把
           ``data:image/png;base64,`` 也剥掉了：有缓冲时复制带前缀、没缓冲时
           不带，同一份数据两种形态。

        判据直接与主路径比对（``_payload``），「两条路给同一个答案」才是契约。
        """
        window = self._open(ImageToBase64Window)
        window._adopt_raw(_png_bytes((60, 40)), source='t.png')
        window.win.update()
        displayed = window.output.get('1.0', 'end-1c')
        self.assertIn('\n', displayed, '显示文本没折行，用例失去判别力')

        expected = window._payload
        self.assertTrue(expected.startswith('data:image/png;base64,'))
        window._payload = ''                      # 模拟缓冲缺失
        copied = window._copy_content(displayed)
        self.assertNotIn('\n', copied, '兜底分支把折行复制出去了')
        self.assertEqual(copied, expected,
                         '兜底分支与主路径结果不一致（前缀被剥掉？）')

    def test_export_writes_wrapped_text(self):
        """导出的是**折行版**（与结果栏所见一致），且带尾换行。

        尾换行不是可有可无的：``base64 -d`` 与 PEM 解析器都按行读，末行没有
        行尾在有些工具上会报警告。
        """
        window = self._open(ImageToBase64Window)
        raw = _png_bytes((60, 40))
        window._adopt_raw(raw, source='t.png')
        window.win.update()

        path = os.path.join(self._tmpdir(), 'out.txt')
        self._run_with_save_path(window, path)
        with open(path, encoding='utf-8') as handle:
            written = handle.read()

        self.assertTrue(written.endswith('\n'), '导出文件缺尾换行')
        self.assertEqual(written.count('\n'), window._displayed.count('\n') + 1)
        # 去换行后必须等于原始字节的 Data URI —— 折行只是形态，不是内容
        self.assertEqual(written.replace('\n', ''), _data_uri(raw))

    def test_clear_drops_payload_buffer(self):
        """清空后缓冲必须一起丢。

        不丢的话「贴图 → 清空 → 点复制结果」会把上一张图的 base64 送进剪贴板
        ——用户以为清空了，实际上手上是一份看不出错的旧数据。
        """
        window = self._open(ImageToBase64Window)
        window._adopt_raw(_png_bytes(), source='t.png')
        window.win.update()
        self.assertTrue(window._payload, '贴图后缓冲是空的，用例失效')
        window._clear()
        self.assertEqual(window._payload, '', '清空后 _payload 还留着旧内容')
        self.assertEqual(window._displayed, '', '清空后 _displayed 还留着旧内容')
        self.assertEqual(window._copy_content(''), '', '清空后还能复制出内容')

    def test_failed_analysis_drops_payload_buffer(self):
        """解析失败时缓冲也要丢，否则导出的是上一次成功的结果。

        **必须让 ``_analyze`` 真的抛错，而不是让输入变空**：输入为空会走
        ``_refresh`` 的「没有内容」早退分支（那条也清缓冲），两条路径长得像
        但覆盖的是不同代码。变异验证正是这样漏过一轮：只把 ``_image`` 设为
        None 而左栏仍空，测试停在早退分支上，异常分支的清理根本没被执行到。

        构造方式：撤掉图（让 ``_analyze`` 报「没有图片」），同时在左栏塞一个
        非空字符——否则早退分支会先拦下来。
        """
        window = self._open(ImageToBase64Window)
        window._adopt_raw(_png_bytes(), source='t.png')
        window.win.update()
        self.assertTrue(window._payload, '贴图后缓冲是空的，用例失效')

        window._image = None
        window.input.insert('1.0', 'x')      # 防止走「输入为空」早退分支
        window._refresh()
        window.win.update()

        self.assertIn('图片', window.status.cget('text'))
        self.assertEqual(window.status.cget('fg').upper(), '#FF453A',
                         '没走进报错分支，用例失去判别力')
        self.assertEqual(window._payload, '', '报错后 _payload 没清')
        self.assertEqual(window._displayed, '', '报错后 _displayed 没清')
        self.assertEqual(window.output.get('1.0', 'end-1c'), '')

    def test_export_reads_buffer_not_the_widget(self):
        """导出读的是产物缓冲，不是结果栏控件里的显示文本。

        缓冲存在的意义就是「数据源与控件分离」。这条通过**人为把结果栏改坏**
        来验证分离确实成立：结果栏被外部改动后，导出内容不受影响。
        （现实里对应「将来给结果栏加行号/统计等装饰」这类改动——那时若 export
        还从控件读，导出文件就会被装饰内容污染，而且不会有任何报错。）
        """
        window = self._open(ImageToBase64Window)
        raw = _png_bytes((60, 40))
        window._adopt_raw(raw, source='t.png')
        window.win.update()

        # 把结果栏改成别的内容（模拟控件被装饰/污染）
        window.output.configure(state='normal')
        window.output.insert('1.0', '被污染的前缀\n')
        window.output.configure(state='disabled')

        path = os.path.join(self._tmpdir(), 'from_buffer.txt')
        self._run_with_save_path(window, path)
        with open(path, encoding='utf-8') as handle:
            written = handle.read()

        self.assertNotIn('污染', written, '导出内容跟着结果栏控件走了')
        self.assertEqual(written.replace('\n', ''), _data_uri(raw))

    # ---------- 预览重缩放（拖窗口卡顿） ----------
    def test_preview_scales_from_a_bounded_master(self):
        """预览重缩放必须走**有界主图**，不能每次都拿原图缩。

        拖动窗口时 ``<Configure>`` 密集触发，每次都会重跑一遍缩放：
        4000×3000 的原图每次 **61 ms**，一次拖动累积到秒级——用户感知就是
        「拖窗口一卡一卡的」。改成从最长边 ≤ ``PREVIEW_MASTER_BOX`` 的主图缩，
        同样条件下每次约 11 ms。

        断言用「主图尺寸有界」这个**可观察的不变量**，不测时间（CI 上会抖）。
        """
        import io as iomod

        from PIL import Image
        rnd = random.Random(3)
        side = 2600
        img = Image.frombytes(
            'RGB', (side, 1800),
            bytes(rnd.randrange(256) for _ in range(side * 1800 * 3)))
        buf = iomod.BytesIO()
        img.save(buf, 'PNG')
        window = self._open(ImageToBase64Window)
        self.assertTrue(window._adopt_raw(buf.getvalue(), source='big.png'))

        master = window._preview_master()
        self.assertIsNotNone(master)
        self.assertLessEqual(max(master.size), TW.PREVIEW_MASTER_BOX,
                             '主图没有被限制尺寸，拖窗口时会拿原图重缩')

    def test_master_is_cached_until_image_changes(self):
        """主图要缓存，不能每次 Configure 重建（重建等于没省）。

        用 identity 判断命中，并用「换图后必须拿到新主图」兜住缓存粘滞——
        那会让预览显示上一张图，比卡顿严重得多。

        **样本必须大于 ``PREVIEW_MASTER_BOX``**：小图的主图就是原图本身
        （不做无谓拷贝），identity 会恒成立，测不出缓存有没有生效。
        """
        import io as iomod

        from PIL import Image
        side = TW.PREVIEW_MASTER_BOX + 400
        for label in ('a', 'b'):
            buf = iomod.BytesIO()
            Image.new('RGB', (side, 900), (30, 90, 200)).save(buf, 'PNG')
            if label == 'a':
                raw_a = buf.getvalue()
            else:
                raw_b = buf.getvalue()

        window = self._open(ImageToBase64Window)
        self.assertTrue(window._adopt_raw(raw_a, source='a.png'))
        first = window._preview_master()
        self.assertIsNot(first, window._image, '样本没触发主图缩小，用例失效')
        self.assertIs(window._preview_master(), first, '主图没被缓存')

        self.assertTrue(window._adopt_raw(raw_b, source='b.png'))
        second = window._preview_master()
        self.assertIsNot(second, first, '换图后主图缓存没失效')
        self.assertLessEqual(max(second.size), TW.PREVIEW_MASTER_BOX)

    def test_small_image_is_not_upscaled_by_master(self):
        """小图不该被主图放大——那会让预览变模糊。"""
        window = self._open(ImageToBase64Window)
        window._adopt_raw(_png_bytes((40, 30)), source='s.png')
        self.assertEqual(window._preview_master().size, (40, 30))

    def test_clear_releases_master_cache(self):
        """清空要连主图缓存一起放掉。

        缓存是 ``(原图, 主图)`` 元组，置空 ``_image`` 并不解除元组对原图的
        引用——不清就是「界面清空了，几 MB 内存还压着」，大图上很可观。
        """
        window = self._open(ImageToBase64Window)
        window._adopt_raw(_png_bytes((80, 60)), source='a.png')
        self.assertIsNotNone(window._preview_master_img)
        window._clear()
        self.assertIsNone(window._preview_master_img, '清空后主图缓存还占着内存')

    def test_clipboard_image_encodes_at_full_resolution(self):
        """剪贴板来的图（没有原始字节）也必须按**原尺寸**编码，不能被预览主图降采样。

        这条覆盖 ``_analyze`` 的 ``else`` 分支（``_image_raw is None`` →
        ``encode_image(self._image)``）。``_adopt_raw``（文件路径）总会设
        ``_image_raw``，所以文件路径永远走不到这个分支——而它恰恰最容易被
        「顺手拿主图省点内存」改坏。改坏后图变小、体积变小、**没有任何报错**，
        用户只会觉得「怎么糊了」。

        判据用产物解码回来的像素尺寸，比任何中间变量都直接。
        """

        from PIL import Image
        side = TW.PREVIEW_MASTER_BOX + 400
        big = Image.new('RGB', (side, 900), (200, 80, 40))

        window = self._open(ImageToBase64Window)
        # 直接走「剪贴板」形态：只有 PIL 图，没有原始字节
        window._adopt_image(big, source='剪贴板', raw=None, fmt='PNG')
        window.win.update()

        master = window._preview_master()
        self.assertIsNot(master, big, '没触发主图缩小，用例失去判别力')
        self.assertLessEqual(max(master.size), TW.PREVIEW_MASTER_BOX)

        decoded = image_b64.decode_base64(window._payload)
        self.assertEqual((decoded.width, decoded.height), (side, 900),
                         '编码用了预览主图，产物被静默降采样')

    def test_encoding_still_uses_original_image(self):
        """**主图只服务显示**，编码必须仍走原图/原始字节。

        这条是本组修复里最要紧的不变量：为了提速引入的中间图一旦参与编码，
        产物就会被悄悄降采样，而界面上一切正常、状态栏体积也「看起来对」。

        样本要**大于** ``PREVIEW_MASTER_BOX``，否则主图就是原图本身，
        identity 断言恒成立、失去判别力（第一版用 300×200 正是这样假通过的）。
        """
        import io as iomod

        from PIL import Image
        big = Image.new('RGB', (TW.PREVIEW_MASTER_BOX + 400, 1200), (90, 120, 200))
        buf = iomod.BytesIO()
        big.save(buf, 'PNG')
        raw = buf.getvalue()

        window = self._open(ImageToBase64Window)
        self.assertTrue(window._adopt_raw(raw, source='big.png'))
        window.win.update()

        master = window._preview_master()
        self.assertIsNot(master, window._image,
                         '样本没触发主图缩小，用例失去判别力')
        self.assertLessEqual(max(master.size), TW.PREVIEW_MASTER_BOX)
        # 产物必须是**原始字节**的 Data URI，不是被缩过的图重编出来的
        self.assertEqual(_data_uri(raw), window._payload,
                         '产物不是原始字节的 base64——中间图被拿去编码了')

    def test_text_tools_copy_verbatim(self):
        """文本类工具没覆写 ``_copy_content``，复制必须与显示逐字一致。

        这条挡的是「把 base64 的折行处理顺手加进基类」——那会让 JSON 结果的
        缩进换行全部消失，是肉眼可见但很容易漏测的破坏。
        """
        window = self._open(JsonParamWindow)
        self._type(window, '{"a":1,"b":[1,2]}')
        window.win.update()
        displayed = window.output.get('1.0', 'end-1c')
        self.assertIn('\n', displayed, 'JSON 结果没换行，用例失效')
        self.assertEqual(window._copy_content(displayed), displayed)

    def _tmpdir(self):
        import tempfile
        if not hasattr(self, '_tmp_ctx'):
            self._tmp_ctx = tempfile.TemporaryDirectory()
        return self._tmp_ctx.name

    def _run_with_save_path(self, window, path):
        """在假 ``filedialog`` 下执行 window.export()，返回固定路径。"""
        import sys as sysmod
        import types
        from unittest import mock

        fake = types.ModuleType('tkinter.filedialog')
        fake.asksaveasfilename = lambda **kw: path
        with mock.patch.dict(sysmod.modules, {'tkinter.filedialog': fake}), \
                mock.patch.object(tk, 'filedialog', fake, create=True):
            window.export()


if __name__ == '__main__':
    unittest.main()
