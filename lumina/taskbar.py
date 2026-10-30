"""无边框窗口的任务栏行为：归属声明、显隐、最小化/还原。

主面板（``history_panel``）与工具弹框（``tool_window``）都要做同样四件事，
而它们踩过的坑完全一样，所以抽到这里统一实现：

1. **进任务栏**：``overrideredirect(True)`` 会让 Tk 打上 ``WS_EX_TOOLWINDOW``，
   语义就是「不进任务栏、不进 Alt+Tab」，最小化后用户没有入口找回窗口。
   要换成 ``WS_EX_APPWINDOW``。
2. **点任务栏按钮能切换最小化/还原**：无边框窗口是纯 ``WS_POPUP``，
   ``WS_MINIMIZEBOX`` / ``WS_MAXIMIZEBOX`` / ``WS_SYSMENU`` 全为 0，缺了它们
   点按钮只会激活窗口。这三个是**纯行为标志**，补上不会露出原生标题栏。
3. **设图标**：见 ``app_icon``——必须分尺寸 ``WM_SETICON``，``iconphoto`` 与
   ``iconbitmap`` 都不可靠。
4. **最小化**：Tk 的 ``iconify()`` 对无边框窗口直接抛 ``TclError``，早先的绕法
   是临时 ``overrideredirect(False)`` → ``iconify()`` → 再翻回来。**这条路是
   「显示后图标消失、点击切换失效」的根源**：每次翻转都会重建 HWND
   （实测 ``0x3a2e1480`` → ``0x559a07c8`` → ``0x1f40160c``），新窗口带着
   Tk 刚重设的 ``WS_EX_TOOLWINDOW`` 重新进任务栏，explorer 于是撤掉按钮。
   改用 Win32 ``ShowWindow(SW_MINIMIZE/SW_RESTORE)`` 后 HWND 全程不变，
   实测样式与图标在「显示 → 最小化 → 唤回」多轮往返后完全稳定。
"""
import ctypes
import os

from .app_icon import apply_window_icon

# ---------- Win32 常量 ----------
GWL_STYLE = -16
GWL_EXSTYLE = -20

WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_SYSMENU = 0x00080000
# WS_CAPTION = WS_BORDER | WS_DLGFRAME；补标志后必须仍为 0，否则会冒出原生标题栏
WS_CAPTION = 0x00C00000

WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020

SW_HIDE = 0
SW_SHOW = 5
SW_MINIMIZE = 6
SW_RESTORE = 9

# 补进 STYLE 的三个框位标志（合成一个常量，便于测试断言）
FRAME_FLAGS = WS_MINIMIZEBOX | WS_MAXIMIZEBOX | WS_SYSMENU


def top_level_hwnd(window):
    """沿父链找到最外层窗口句柄；非 Windows 返回 ``None``。

    ``winfo_id()`` 返回内层 ``TkChild``，决定任务栏归属的是取不到父窗口的那层
    ``TkTopLevel``。

    **不要缓存**：``overrideredirect`` 翻转会重建 HWND，缓存下来的旧句柄改的是
    已销毁的窗口，标志永远不会生效。
    """
    if os.name != "nt":
        return None
    try:
        user32 = ctypes.windll.user32
        user32.GetParent.restype = ctypes.c_void_p
        user32.GetParent.argtypes = [ctypes.c_void_p]
        hwnd = window.winfo_id()
        for _ in range(6):
            parent = user32.GetParent(ctypes.c_void_p(hwnd))
            if not parent:
                break
            hwnd = parent
        return hwnd
    except Exception:
        return None


def read_style(hwnd, index=GWL_STYLE):
    """读 ``GWL_STYLE`` / ``GWL_EXSTYLE``；失败返回 0。"""
    if os.name != "nt" or not hwnd:
        return 0
    try:
        getter = ctypes.windll.user32.GetWindowLongPtrW
        getter.restype = ctypes.c_void_p
        getter.argtypes = [ctypes.c_void_p, ctypes.c_int]
        return getter(ctypes.c_void_p(hwnd), index) or 0
    except Exception:
        return 0


def _write_style(hwnd, value, index):
    setter = ctypes.windll.user32.SetWindowLongPtrW
    setter.restype = ctypes.c_void_p
    setter.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    setter(ctypes.c_void_p(hwnd), index, ctypes.c_void_p(value))


def claim_taskbar(window):
    """让无边框窗口进任务栏、进 Alt+Tab，且点任务栏按钮能切换最小化/还原。

    做三件事：换扩展样式、补三个框位标志、设分尺寸图标。任一有变化才
    ``SetWindowPos`` 通知系统刷新。

    ``SetWindowPos`` 的两个坑：
    ① ``argtypes`` 必须声明，否则 64 位下后续 int 参数按 32 位传；
    ② ``hwndInsertAfter`` **不能传 0**（``CW_USEDEFAULT`` 语义，实测会把窗口
       挪到 ``(0, 0)``），传自身 + ``SWP_NOZORDER`` 才保持原位。
    """
    hwnd = top_level_hwnd(window)
    if not hwnd:
        return False
    try:
        changed = False

        ex_style = read_style(hwnd, GWL_EXSTYLE)
        wanted_ex = (ex_style | WS_EX_APPWINDOW) & ~WS_EX_TOOLWINDOW
        if wanted_ex != ex_style:
            _write_style(hwnd, wanted_ex, GWL_EXSTYLE)
            changed = True

        style = read_style(hwnd, GWL_STYLE)
        wanted = style | FRAME_FLAGS
        if wanted != style:
            _write_style(hwnd, wanted, GWL_STYLE)
            changed = True

        # 图标每次都要重设：HWND 若被重建（如 overrideredirect 翻转），
        # 旧的 WM_SETICON 结果不会跟着搬过去。
        if apply_window_icon(window):
            changed = True

        if changed:
            user32 = ctypes.windll.user32
            set_pos = user32.SetWindowPos
            set_pos.restype = ctypes.c_bool
            set_pos.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p,
                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                ctypes.c_uint]
            set_pos(
                ctypes.c_void_p(hwnd), ctypes.c_void_p(hwnd), 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE
                | SWP_FRAMECHANGED)
        return True
    except Exception:
        return False


def show_window(window, activate=True):
    """显示窗口（走 Win32，不经过 Tk 的映射流程）。

    Tk 的 ``deiconify()`` 会重新应用 ``overrideredirect`` 对应的扩展样式，把
    ``WS_EX_TOOLWINDOW`` 打回去；``ShowWindow`` 不碰样式，故窗口的样式与图标
    在显隐之间保持不变。

    **窗口处于最小化状态时必须用 ``SW_RESTORE`` 而不是 ``SW_SHOW``。**
    实测（无边框 Toplevel，``ShowWindow(SW_MINIMIZE)`` 之后）：

    ====================  ==================  ================
    调用的 API             ``IsIconic``        ``winfo_viewable``
    ====================  ==================  ================
    （最小化后）           True                0
    ``SW_SHOW``/``SW_SHOWNA``  **True（无效）**  0
    ``SW_RESTORE``         False               1
    ====================  ==================  ================

    也就是说 ``SW_SHOW`` 对最小化窗口**完全唤不回来**——窗口还挂在任务栏上
    但永远不会回到屏幕。ESC 改成「最小化」之后这条路径就成了主入口，必须走
    ``SW_RESTORE``。
    """
    hwnd = top_level_hwnd(window)
    if not hwnd:
        return False
    try:
        user32 = ctypes.windll.user32
        user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.ShowWindow.restype = ctypes.c_bool
        # 先判最小化：IsIconic 为真时 SW_SHOW 无效，必须 SW_RESTORE。
        user32.IsIconic.argtypes = [ctypes.c_void_p]
        user32.IsIconic.restype = ctypes.c_bool
        if user32.IsIconic(ctypes.c_void_p(hwnd)):
            ok = user32.ShowWindow(ctypes.c_void_p(hwnd), SW_RESTORE)
        else:
            ok = user32.ShowWindow(ctypes.c_void_p(hwnd),
                                   SW_SHOW if activate else 8)  # 8 = SW_SHOWNA
        # 显示后补一次归属：万一别处翻转过 overrideredirect，这里能兜住
        claim_taskbar(window)
        return bool(ok)
    except Exception:
        return False


def hide_window(window):
    """隐藏窗口（任务栏按钮随之消失）。

    注意这与「最小化」语义不同：``hide`` 是彻底不显示，``minimize_window``
    是留在任务栏供用户唤回。
    """
    hwnd = top_level_hwnd(window)
    if not hwnd:
        return False
    try:
        user32 = ctypes.windll.user32
        user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.ShowWindow.restype = ctypes.c_bool
        return bool(user32.ShowWindow(ctypes.c_void_p(hwnd), SW_HIDE))
    except Exception:
        return False


def minimize_window(window):
    """最小化到任务栏，**不翻转 overrideredirect、不重建 HWND**。

    Tk 的 ``iconify()`` 对无边框窗口抛 ``TclError``，早先靠临时取消
    ``overrideredirect`` 绕过——但那会重建 HWND，新窗口带着 Tk 重设的
    ``WS_EX_TOOLWINDOW`` 回来，任务栏按钮就没了。

    ``ShowWindow(SW_MINIMIZE)`` 没有这个限制，实测无边框窗口能正常最小化
    （矩形移到 ``-32000``），且 EX/STYLE/HICON 全程不变。
    """
    hwnd = top_level_hwnd(window)
    if not hwnd:
        return False
    try:
        user32 = ctypes.windll.user32
        user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.ShowWindow.restype = ctypes.c_bool
        return bool(user32.ShowWindow(ctypes.c_void_p(hwnd), SW_MINIMIZE))
    except Exception:
        return False


def restore_window(window):
    """从最小化还原（``SW_RESTORE``），同样不碰 ``overrideredirect``。"""
    hwnd = top_level_hwnd(window)
    if not hwnd:
        return False
    try:
        user32 = ctypes.windll.user32
        user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.ShowWindow.restype = ctypes.c_bool
        ok = user32.ShowWindow(ctypes.c_void_p(hwnd), SW_RESTORE)
        claim_taskbar(window)
        return bool(ok)
    except Exception:
        return False


def is_minimized(window):
    """窗口当前是否处于最小化状态。"""
    hwnd = top_level_hwnd(window)
    if not hwnd:
        return False
    try:
        user32 = ctypes.windll.user32
        user32.IsIconic.argtypes = [ctypes.c_void_p]
        user32.IsIconic.restype = ctypes.c_bool
        return bool(user32.IsIconic(ctypes.c_void_p(hwnd)))
    except Exception:
        return False
