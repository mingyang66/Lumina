"""主面板的任务栏归属与「最小化到任务栏」行为。

对应需求：*「剪贴板有没有可能修改为跟这个弹框一样可以在任务栏展示，
最小化后在任务栏找到」*。

历史上主面板的 ``_minimize`` 用的是 ``withdraw()``——那是**隐藏**，窗口从
任务栏彻底消失，用户再也找不回来。这里守住三件事：

1. 窗口必须带 ``WS_EX_APPWINDOW``、不带 ``WS_EX_TOOLWINDOW``，否则不进任务栏；
2. 最小化必须是真正的 ``iconic``（矩形移到 ``-32000``），而不是隐藏；
3. **最小化不能重建 HWND**。早先的实现临时翻转 ``overrideredirect`` 才能
   ``iconify()``，每次翻转都重建 HWND，新窗口带着 Tk 重设的
   ``WS_EX_TOOLWINDOW`` 回来，explorer 撤掉任务栏按钮、``WM_SETICON`` 的图标
   也一并丢失——用户看到的就是「显示后图标消失、点击切换失效」。现在走
   ``ShowWindow(SW_MINIMIZE)``，全程不碰 HWND。

需要真实 Tk 与 Win32；任一不可用时整体跳过。
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

try:
    _PROBE = tk.Tk()
    _PROBE.withdraw()
    _PROBE.destroy()
    TK_OK = True
except Exception:  # pragma: no cover - 仅在无图形环境时触发
    TK_OK = False

SKIP_REASON = '本机 Tk 不可用'

WM_GETICON = 0x007F


def _icon_slots(hwnd):
    """读窗口两个图标槽位（``ICON_SMALL`` / ``ICON_BIG``）的 HICON。

    **必须用独立 ``WinDLL`` 句柄**：``history_panel`` 已给模块级
    ``user32.GetWindowRect`` 设过自己的 RECT argtypes，共用句柄会让类型校验串味。
    """
    user32 = ctypes.WinDLL('user32')
    user32.SendMessageW.restype = ctypes.c_ssize_t
    user32.SendMessageW.argtypes = [
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
    return tuple(
        user32.SendMessageW(ctypes.c_void_p(hwnd), WM_GETICON, index, 0)
        for index in (0, 1))


class _FakeDB:
    """面板构造期会查库；给一个永远空的最小替身。"""

    def list_recent(self, *args, **kwargs):
        return []

    def count(self, *args, **kwargs):
        return 0

    def get(self, *args, **kwargs):
        return None

    def __getattr__(self, name):
        def _noop(*args, **kwargs):
            return None
        return _noop


class _FakeUI:
    def __init__(self):
        self.db = _FakeDB()
        self.config = {}
        self.panel_visible = False


def _window_rect(hwnd):
    """读窗口的 Win32 屏幕矩形。

    **必须用独立的 ``WinDLL`` 句柄**：``history_panel`` 已经给模块级的
    ``user32.GetWindowRect`` 设过它自己的 RECT argtypes，共用同一个句柄会让
    这里的 ``byref(RECT)`` 类型校验失败（实测 ``expected LP_RECT instance``）。
    """
    if os.name != 'nt':
        return None
    user32 = ctypes.WinDLL('user32')

    class RECT(ctypes.Structure):
        _fields_ = [('left', ctypes.c_long), ('top', ctypes.c_long),
                    ('right', ctypes.c_long), ('bottom', ctypes.c_long)]

    user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(RECT)]
    user32.GetWindowRect.restype = ctypes.c_bool
    rect = RECT()
    if not user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect)):
        return None
    return (rect.left, rect.top, rect.right, rect.bottom)


@unittest.skipUnless(TK_OK, SKIP_REASON)
@unittest.skipUnless(os.name == 'nt', '任务栏行为仅 Windows 有意义')
class PanelTaskbarTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.panel = HistoryPanel(self.root, tk, _FakeUI())
        self.panel.win.deiconify()
        self._pump(0.15)

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
        """跑一轮事件循环。无边框/最小化的状态迁移要等 Tk 处理完才有结果。"""
        self.panel.win.update()
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.panel.win.update()
            time.sleep(0.01)

    def _exstyle(self):
        return self.panel._read_exstyle(self.panel._taskbar_hwnd())

    def _style(self):
        return self.panel._read_style(self.panel._taskbar_hwnd())

    def _hicon(self):
        """读窗口当前的任务栏图标句柄（``WM_GETICON``），0 表示没有。"""
        user32 = ctypes.WinDLL('user32')
        user32.SendMessageW.restype = ctypes.c_ssize_t
        user32.SendMessageW.argtypes = [
            ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
        return user32.SendMessageW(
            ctypes.c_void_p(self.panel._taskbar_hwnd()), 0x007F, 0, 0)

    # ---------- 任务栏按钮的点击切换 ----------
    def test_window_style_allows_taskbar_toggle(self):
        """必须补 WS_MINIMIZEBOX / WS_MAXIMIZEBOX / WS_SYSMENU。

        无边框窗口是纯 ``WS_POPUP``，**三个框位标志全为 0**（实测
        ``STYLE=0x96000008``）。缺了它们，点任务栏按钮只会把窗口提到前台，
        不会做「点一下最小化、再点一下还原」——这正是用户反馈的现象。
        """
        style = self._style()
        self.assertTrue(style & self.panel.WS_MINIMIZEBOX,
                        '缺少 WS_MINIMIZEBOX，任务栏点击不会最小化')
        self.assertTrue(style & self.panel.WS_MAXIMIZEBOX,
                        '缺少 WS_MAXIMIZEBOX，任务栏点击不会做还原切换')
        self.assertTrue(style & self.panel.WS_SYSMENU,
                        '缺少 WS_SYSMENU，窗口缺少系统菜单语义')

    def test_style_fix_does_not_show_native_caption(self):
        """补框位标志不能把原生标题栏带出来。

        这是本次改动最需要守住的边界：用户要的是「无边框但像普通窗口一样
        可最小化」，不是「恢复系统标题栏」。``WS_CAPTION`` 由
        ``WS_BORDER | WS_DLGFRAME`` 组成，必须仍为 0。
        """
        style = self._style()
        caption = style & 0xC00000
        self.assertEqual(caption, 0,
                         f'补样式后冒出了原生标题栏（CAPTION=0x{caption:X}）')

    def test_taskbar_click_toggles_minimize_and_restore(self):
        """任务栏按钮要能「点一下最小化、再点一下还原」的完整往返。

        直接给窗口句柄投递 ``WM_SYSCOMMAND(SC_MINIMIZE)`` 模拟点击任务栏按钮，
        再 ``deiconify`` 模拟第二次点击。

        **注意**：单看「SC_MINIMIZE 之后是否 iconic」拦不住样式缺失——实测
        Tk 自己也会响应这条消息，所以光靠它会让
        ``test_window_style_allows_taskbar_toggle`` 之外的回归漏网。这条用例
        的价值在于守住**往返完整性**：最小化后还能还原、且还原后无边框和
        任务栏标志都还在。
        """
        user32 = ctypes.WinDLL('user32')
        user32.SendMessageW.restype = ctypes.c_ssize_t
        user32.SendMessageW.argtypes = [
            ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
        before = self.panel.win.geometry()

        user32.SendMessageW(
            ctypes.c_void_p(self.panel._taskbar_hwnd()), 0x0112, 0xF020, 0)
        self._pump(0.3)
        self.assertEqual(str(self.panel.win.state()), 'iconic',
                         '收到 SC_MINIMIZE 后窗口没有最小化')

        self.panel.win.deiconify()
        self._pump(0.3)
        self.assertEqual(str(self.panel.win.state()), 'normal',
                         '第二次点击没有把窗口还原')
        self.assertTrue(self.panel.win.overrideredirect(),
                        '还原后无边框丢了')
        self.assertEqual(self.panel.win.geometry(), before, '还原后几何不一致')
        ex_style = self._exstyle()
        self.assertTrue(ex_style & self.panel.WS_EX_APPWINDOW,
                        '还原后任务栏标志丢了')

    # ---------- 任务栏图标 ----------
    def test_window_has_taskbar_icon(self):
        """窗口必须真的挂上图标，否则任务栏显示系统默认图。

        ``iconphoto`` 对无边框窗口完全无效（实测 ``WM_GETICON`` 三个槽位全是
        0），只有 ``iconbitmap(<ico>)`` 生效。这里用 ``WM_GETICON`` 直接确认
        窗口层拿到了非零 HICON。
        """
        self._pump(0.1)
        self.assertNotEqual(self._hicon(), 0,
                            '任务栏没有图标（WM_GETICON 为 0）')

    # ---------- 双击顶栏最大化 ----------
    def test_double_click_title_bar_toggles_maximize(self):
        """双击标题栏必须切换最大化/还原（Windows 原生习惯）。

        标题栏里除了几个按钮还有大片空白，点空白时 ``<ButtonPress-1>`` 由容器
        ``title_bar`` 自己接收，所以 ``<Double-Button-1>`` 绑在容器上即可。

        验证方式分两段，不依赖 Tk 的连击计时（阈值 500ms，靠 sleep 对齐既慢
        又不稳）：先确认容器真的绑了处理函数，再直接调用它验证方向正确。
        """
        # 从标题栏按钮反查容器，避免依赖内部属性名
        title_bar = self.panel._settings_btn.master
        self.assertTrue(title_bar.bind('<Double-Button-1>'),
                        '标题栏没有绑定双击')

        self.panel._maxed = False
        self.panel._normal_geom = '800x600+60+60'
        self.assertEqual(self.panel._hdr_double(), 'break')
        self.assertTrue(self.panel._maxed, '第一次双击没有最大化')
        self.panel._hdr_double()
        self.assertFalse(self.panel._maxed, '第二次双击没有还原')

    def test_double_click_clears_drag_anchor(self):
        """双击后必须清掉拖动锚点，否则最大化窗口会被后续拖动带偏。"""
        self.panel._drag_off = (123, 456)
        self.panel._hdr_double()
        self.assertIsNone(self.panel._drag_off,
                          '双击后拖动锚点还在，最大化窗口会被拖走')
        self.panel._maxed = False

    # ---------- 任务栏归属 ----------
    def test_panel_claims_taskbar_instead_of_toolwindow(self):
        """无边框窗口必须带 WS_EX_APPWINDOW，否则不进任务栏。

        Tk 在 ``overrideredirect(True)`` 时打的是 ``WS_EX_TOOLWINDOW``（0x80），
        语义就是「不进任务栏、不进 Alt+Tab」。主面板也走无边框自绘，所以必须
        跟工具弹框一样把标志换掉。
        """
        ex_style = self._exstyle()
        self.assertTrue(ex_style & self.panel.WS_EX_APPWINDOW,
                        '缺少 WS_EX_APPWINDOW，面板不会出现在任务栏')
        self.assertFalse(ex_style & self.panel.WS_EX_TOOLWINDOW,
                         '残留 WS_EX_TOOLWINDOW，任务栏仍会隐藏面板')

    def test_taskbar_hwnd_is_not_cached(self):
        """句柄不能缓存：``overrideredirect`` 切换会重建 HWND。

        实测切换前后句柄是 ``0x24330E74`` → ``0x72780598`` → ``0x24340E74``。
        缓存旧句柄等于给一个已销毁的窗口改样式，任务栏标志永远不生效。
        """
        first = self.panel._taskbar_hwnd()
        self.panel.win.overrideredirect(False)
        self._pump(0.05)
        second = self.panel._taskbar_hwnd()
        self.panel.win.overrideredirect(True)
        self.panel._claim_taskbar()
        self._pump(0.05)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertNotEqual(first, second,
                            '切换 overrideredirect 后句柄没变，说明取的是缓存值')

    # ---------- 最小化 ----------
    def test_apply_mode_declares_taskbar_ownership(self):
        """``apply_mode`` 必须自己调用 ``_claim_taskbar``，不能靠 ``<Map>`` 兜底。

        这条用例隔离 ``apply_mode`` 里那一行 ``_claim_taskbar()``。

        为什么用 spy 而不是断言扩展样式：实测发现任何
        ``overrideredirect`` 翻转都会触发一次 ``<Map>``（``False`` → ``True``
        等同于重新映射），于是构造期绑定的 ``_on_map_claim_taskbar`` 会顺手
        把样式修好，**把这一行的缺失完全掩盖掉**——把它注释掉整套测试仍然全绿
        （已实测）。所以只能直接验证调用契约。

        **实测规律**：``overrideredirect(True)`` 只在**从 False 翻到 True** 时
        才重新打上 ``WS_EX_TOOLWINDOW``；重复设 True 是空操作（EX 从
        ``0x40000`` 原样不动）。``show()`` 每次都走 ``apply_mode()``，而窗口
        此时已经映射，不该指望 ``<Map>`` 再来一次。
        """
        self._pump(0.1)
        calls = []
        original = self.panel._claim_taskbar

        def spy():
            calls.append(1)
            return original()

        self.panel._claim_taskbar = spy
        try:
            self.panel.apply_mode()
        finally:
            self.panel._claim_taskbar = original

        self.assertTrue(calls,
                        'apply_mode 没有调用 _claim_taskbar —— 窗口已映射时'
                        '没有 <Map> 兜底，面板会掉出任务栏')

        # 顺带确认收尾状态是对的
        self._pump(0.1)
        ex_style = self._exstyle()
        self.assertTrue(ex_style & self.panel.WS_EX_APPWINDOW)
        self.assertFalse(ex_style & self.panel.WS_EX_TOOLWINDOW)

    def test_minimize_iconifies_instead_of_withdrawing(self):
        """最小化必须是真 iconify，不能是 withdraw。

        ``withdraw`` 是隐藏：窗口从任务栏消失，用户再也找不回来。这是本次
        修复的核心——用户原话「最小化后在任务栏找到」。
        """
        self.panel._minimize()
        self._pump(0.3)
        self.assertEqual(str(self.panel.win.state()), 'iconic',
                         '最小化后窗口不是 iconic 状态，withdraw 会让它从任务栏消失')

    def test_minimized_window_moves_offscreen(self):
        """最小化后矩形必须是 -32000，而不是贴在任务栏上沿的悬浮碎片。"""
        self.panel._minimize()
        self._pump(0.3)
        rect = _window_rect(self.panel._taskbar_hwnd())
        self.assertIsNotNone(rect)
        left, top = rect[0], rect[1]
        self.assertLess(left, -10000,
                        f'最小化后没有移到屏幕外，left={left} → 会显示成任务栏上的碎片')
        self.assertLess(top, -10000,
                        f'最小化后没有移到屏幕外，top={top} → 会显示成任务栏上的碎片')

    def test_minimize_does_not_rebuild_hwnd(self):
        """最小化必须**不重建 HWND**——这是「显示后图标消失」的根因。

        早先 ``_minimize`` 走 ``overrideredirect(False)`` → ``iconify()``，唤回时
        再翻回来。实测每次翻转都重建 HWND（``0x3a2e1480`` → ``0x559a07c8`` →
        ``0x1f40160c``），新窗口带着 Tk 重设的 ``WS_EX_TOOLWINDOW`` 回来，
        explorer 于是撤掉任务栏按钮、``WM_SETICON`` 设的图标也一并丢失。

        现在走 ``ShowWindow(SW_MINIMIZE)``：HWND、扩展样式、框位标志、图标
        句柄全程不变。
        """
        hwnd_before = self.panel._taskbar_hwnd()
        ex_before = self._exstyle()
        style_before = self._style()
        icons_before = _icon_slots(hwnd_before)

        self.panel._minimize()
        self._pump(0.3)
        self.assertEqual(str(self.panel.win.state()), 'iconic')

        self.assertEqual(self.panel._taskbar_hwnd(), hwnd_before,
                         '最小化后 HWND 变了 → 图标与框位标志不会跟着搬过去')
        self.assertEqual(self._exstyle(), ex_before,
                         '最小化期间扩展样式变了 → 任务栏按钮会被撤掉')
        # STYLE 不逐位比较：最小化时系统会置 WS_MINIMIZE (0x20000000)，
        # 这是正常语义。真正要守的是三个框位标志还在。
        self.assertEqual(self._style() & taskbar.FRAME_FLAGS,
                         style_before & taskbar.FRAME_FLAGS,
                         '最小化期间框位标志丢了 → 点任务栏按钮不会切换')
        self.assertEqual(_icon_slots(hwnd_before), icons_before,
                         '最小化期间图标槽位变了 → 任务栏图标消失')

    # ---------- 唤回 ----------
    def test_summon_restores_geometry_and_taskbar(self):
        """唤回后：几何还原、任务栏标志与图标仍在，且 HWND 没变。"""
        before = self.panel.win.geometry()
        hwnd_before = self.panel._taskbar_hwnd()
        ex_before = self._exstyle()
        icons_before = _icon_slots(hwnd_before)

        self.panel._minimize()
        self._pump(0.3)
        self.panel._restore_chrome()
        self._pump(0.3)

        self.assertEqual(str(self.panel.win.state()), 'normal')
        self.assertEqual(self.panel._taskbar_hwnd(), hwnd_before,
                         '唤回后 HWND 变了，说明翻转了 overrideredirect')
        self.assertTrue(self.panel.win.overrideredirect(),
                        '唤回后无边框丢了，窗口会露出系统标题栏')
        self.assertEqual(self.panel.win.geometry(), before,
                         '唤回后几何没有还原')

        self.assertEqual(self._exstyle(), ex_before,
                         '唤回后任务栏标志变了，再次最小化就找不回来')
        self.assertEqual(_icon_slots(hwnd_before), icons_before,
                         '唤回后图标变了')

    # ---------- ESC = 最小化到任务栏 ----------
    def test_escape_minimizes_instead_of_hiding(self):
        """ESC 必须是**最小化**，不是隐藏，也不是关闭。

        用户原话：「现在按 ESC 键剪贴板和弹框都是直接关闭了，改成最小化到
        任务栏」。三条语义要分清：

        * 最小化 → 窗口留在任务栏，能点回来（ESC 走这条）
        * 隐藏 → 从任务栏彻底消失（``hide``，截图前让位用）
        * 关闭 → 销毁窗口（``close``）

        判据是 ``IsIconic`` 为真，而**不能**用 ``winfo_viewable()``——
        最小化后它也会变成 0，和隐藏分不开。

        用例走**真实按键**而非直接调 ``self.panel.escape()``：后者只测方法体，
        绑定改回 ``hide()`` 也照样全绿（反向验证时确认过）。
        """
        self.assertTrue(self.panel.is_visible())
        self.assertFalse(self.panel.is_minimized())

        self.panel.win.focus_force()
        self._pump(0.1)
        self.panel.win.event_generate('<Escape>')
        self._pump(0.35)

        self.assertTrue(self.panel.is_minimized(),
                        'ESC 后窗口没有最小化到任务栏')
        self.assertEqual(str(self.panel.win.state()), 'iconic',
                         'ESC 后不是 iconic —— 可能走成了 hide/withdraw')
        self.assertFalse(self.panel.is_visible(),
                         'ESC 后 winfo_viewable 仍为真，与最小化的实测行为不符')

    def test_escape_keeps_window_alive_and_in_taskbar(self):
        """最小化不能丢任务栏归属：HWND、扩展样式、图标都要原样。"""
        hwnd_before = self.panel._taskbar_hwnd()
        ex_before = self._exstyle()
        icons_before = _icon_slots(hwnd_before)

        self.panel.escape()
        self._pump(0.35)

        self.assertEqual(self.panel._taskbar_hwnd(), hwnd_before,
                         'ESC 最小化后 HWND 变了 → 任务栏按钮会消失')
        self.assertEqual(self._exstyle(), ex_before,
                         'ESC 最小化后扩展样式变了')
        self.assertEqual(_icon_slots(hwnd_before), icons_before,
                         'ESC 最小化后任务栏图标丢了')

    def test_escape_updates_panel_visible_flag(self):
        """``ui.panel_visible`` 必须跟着变 False。

        最小化后窗口真的看不见了（``winfo_viewable`` 为 0）。若这个跨线程
        标志还停在 True，截图前的「隐藏面板」会以为面板开着、托盘勾选状态
        也会骗人。
        """
        # setUp 里只 deiconify 了，没走 show()，panel_visible 仍是初始 False。
        # 要测「最小化把它翻回 False」，得先让它为 True。
        self.panel.show(None)
        self._pump(0.2)
        self.assertTrue(self.panel.ui.panel_visible, '前置条件：show() 后应为 True')
        self.panel.escape()
        self._pump(0.3)
        self.assertFalse(self.panel.ui.panel_visible,
                         'ESC 最小化后 panel_visible 仍为 True')

    def test_show_restores_minimized_window_via_restore_not_show(self):
        """唤回最小化窗口必须用 ``SW_RESTORE``，``SW_SHOW`` 是无效的。

        实测（无边框 Toplevel）：``SW_MINIMIZE`` 之后调 ``SW_SHOW``，``IsIconic``
        仍为 True、``winfo_viewable`` 仍为 0——窗口挂在任务栏上永远回不来。
        这条是 ESC 改成最小化之后**最容易踩**的坑：只改 ESC 不修 ``show_window``
        的话，按一下 ESC 就再也唤不回来了。
        """
        self.panel.escape()
        self._pump(0.35)
        self.assertTrue(self.panel.is_minimized())

        self.panel.show(None)
        self._pump(0.4)

        self.assertFalse(self.panel.is_minimized(),
                         'show() 没能把最小化窗口唤回 —— SW_SHOW 对最小化无效，'
                         '必须走 SW_RESTORE')
        self.assertTrue(self.panel.is_visible(), '唤回后窗口仍不可见')
        self.assertTrue(self.panel.ui.panel_visible,
                        '唤回后 panel_visible 没恢复成 True')

    def test_show_after_minimize_keeps_maximized_geometry(self):
        """「最大化 → ESC → 唤回」必须回来还是最大化，不能被重新居中。

        ``show()`` 原本无条件调 ``apply_mode()``，它会按屏幕尺寸重算几何并把
        窗口摆回正中。最小化还原时这属于多余动作，实测把 ``1920x1032`` 冲成
        ``1160x720``。所以 ``show()`` 里要先判 ``is_minimized()`` 再决定是否
        ``apply_mode()``。
        """
        self.panel._toggle_maximize()
        self._pump(0.3)
        maxed = self.panel.win.wm_geometry()
        self.assertTrue(maxed.startswith('1920'), f'没进入最大化：{maxed}')

        self.panel.escape()
        self._pump(0.35)
        self.panel.show(None)
        self._pump(0.4)

        self.assertEqual(self.panel.win.wm_geometry(), maxed,
                         'ESC 往返后最大化几何被 apply_mode 冲掉了')


if __name__ == '__main__':
    unittest.main()
