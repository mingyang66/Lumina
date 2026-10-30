"""工具窗口的 UI 集成测试：外壳、主题、实时解析、复制、关闭。

需要真实 Tk；TTY 环境无 Tk 时整体跳过，避免 CI 报错。
"""
import ctypes
import os
import sys
import time
import tkinter as tk
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lumina import taskbar  # noqa: E402
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
    def test_tool_registry_has_two_tools(self):
        keys = [key for key, _label, _cls in TOOLS]
        self.assertEqual(keys, ['json', 'jwt'])

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

    def test_tool_names_use_current_wording(self):
        """工具名措辞：**JSON 格式化** / **JWT 解密**。

        用户原话：「工具下拉框选项JSON参数-改为 JSON格式化  JWT解析-JWT解密」。
        这是用户偏好而非技术判断（「解密」严格说只解码不验签，但那是文案选择），
        别再以"更准确"为理由改回「参数」「解析」。
        """
        labels = {key: label for key, label, _cls in TOOLS}
        self.assertEqual(labels['json'], 'JSON 格式化')
        self.assertEqual(labels['jwt'], 'JWT 解密')

    def test_open_tool_rejects_unknown_key(self):
        with self.assertRaises(ValueError):
            open_tool(self.panel, 'nope')

    def test_open_tool_returns_expected_window(self):
        json_window = open_tool(self.panel, 'json')
        jwt_window = open_tool(self.panel, 'jwt')
        self.created += [json_window, jwt_window]
        self.assertIsInstance(json_window, JsonParamWindow)
        self.assertIsInstance(jwt_window, JwtWindow)

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
        import base64
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
        import base64
        import json as jsonlib
        def enc(obj):
            raw = jsonlib.dumps(obj).encode()
            return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')
        window = self._open(JwtWindow)
        self._type(window, f'{enc({"alg": "none"})}.{enc({"sub": "1"})}')
        self.assertIn('alg=none', window.output.get('1.0', 'end-1c'))


if __name__ == '__main__':
    unittest.main()
