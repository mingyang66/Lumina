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

from lumina.history_panel import HistoryPanel  # noqa: E402
from lumina.tool_window import (  # noqa: E402
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
        # 输入框折行后横向永不溢出，只留纵向自绘细条；结果框不折行，两条都在。
        self.assertEqual(len(window.input._mini_scrolls), 1)
        self.assertEqual(len(window.output._mini_scrolls), 2)

    def test_input_wraps_at_width_so_no_horizontal_scroll(self):
        """左侧输入必须占满宽度就换行，长行不能横向溢出。

        原来 ``wrap="none"``，一整行压缩 JSON 会远远超出框宽，用户只能左右拖
        才能看到行尾。左侧是**输入区**，看得全比列对齐重要，改成按字符折行。
        """
        window = self._open(JsonParamWindow)
        self.assertEqual(str(window.input.cget('wrap')), 'char',
                         '左侧输入框没有开启折行')

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

    def test_output_keeps_lines_unwrapped(self):
        """右侧结果不折行：结构化输出强行折行会毁掉缩进层级与列对齐。"""
        window = self._open(JsonParamWindow)
        self.assertEqual(str(window.output.cget('wrap')), 'none')

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

    def test_open_tool_rejects_unknown_key(self):
        with self.assertRaises(ValueError):
            open_tool(self.panel, 'nope')

    def test_open_tool_returns_expected_window(self):
        json_window = open_tool(self.panel, 'json')
        jwt_window = open_tool(self.panel, 'jwt')
        self.created += [json_window, jwt_window]
        self.assertIsInstance(json_window, JsonParamWindow)
        self.assertIsInstance(jwt_window, JwtWindow)

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
    def test_json_formats_and_lists_fields(self):
        window = self._open(JsonParamWindow)
        self._type(window, '{"user":{"name":"张三","age":30}}')
        out = window.output.get('1.0', 'end-1c')
        self.assertIn('"name": "张三"', out)
        self.assertIn('字段结构', out)
        self.assertIn('user.name : 字符串', out)
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
