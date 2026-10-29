"""任务栏图标必须**按尺寸分槽**设置。

对应缺陷：*「面板和弹框在桌面上展示后任务栏中的图标就消失了」*。

根因是 Tk 的 ``iconbitmap`` 不区分尺寸——它把 ico 里 32x32 那张塞进本该放
16x16 的 ``ICON_SMALL`` 槽位（实测三个槽位拿到同一个 HICON，``GetIconInfo``
读出来是 ``(32, 32)``）。任务栏按钮取 ``ICON_SMALL`` 绘制，尺寸对不上时就
画不出图标。正确做法是 ``LoadImageW`` 按尺寸各加载一次，``WM_SETICON`` 分别
写进 ``ICON_SMALL`` / ``ICON_BIG``。

需要真实 Tk 与 Win32；任一不可用时整体跳过。
"""
import ctypes
import os
import sys
import tkinter as tk
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lumina import app_icon  # noqa: E402

try:
    _PROBE = tk.Tk()
    _PROBE.withdraw()
    _PROBE.destroy()
    TK_OK = True
except Exception:  # pragma: no cover - 仅在无图形环境时触发
    TK_OK = False

SKIP_REASON = '本机 Tk 不可用'

WM_GETICON = 0x007F


class _ICONINFO(ctypes.Structure):
    _fields_ = [('fIcon', ctypes.c_int), ('xHotspot', ctypes.c_uint),
                ('yHotspot', ctypes.c_uint), ('hbmMask', ctypes.c_void_p),
                ('hbmColor', ctypes.c_void_p)]


class _BITMAP(ctypes.Structure):
    _fields_ = [('bmType', ctypes.c_long), ('bmWidth', ctypes.c_long),
                ('bmHeight', ctypes.c_long), ('bmWidthBytes', ctypes.c_long),
                ('bmPlanes', ctypes.c_ushort), ('bmBitsPixel', ctypes.c_ushort),
                ('bmBits', ctypes.c_void_p)]


def _icon_slots(hwnd):
    """读两个图标槽位的 HICON 句柄。"""
    user32 = ctypes.WinDLL('user32')
    user32.SendMessageW.restype = ctypes.c_ssize_t
    user32.SendMessageW.argtypes = [
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
    return tuple(
        user32.SendMessageW(ctypes.c_void_p(hwnd), WM_GETICON, index, 0)
        for index in (0, 1))


def _icon_pixels(hicon):
    """HICON 的实际位图尺寸 ``(w, h)``；失败返回 ``None``。"""
    if not hicon:
        return None
    user32 = ctypes.WinDLL('user32')
    gdi32 = ctypes.WinDLL('gdi32')
    user32.GetIconInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ICONINFO)]
    user32.GetIconInfo.restype = ctypes.c_bool
    gdi32.GetObjectW.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                 ctypes.POINTER(_BITMAP)]
    gdi32.GetObjectW.restype = ctypes.c_int
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]

    info = _ICONINFO()
    if not user32.GetIconInfo(ctypes.c_void_p(hicon), ctypes.byref(info)):
        return None
    size = None
    if info.hbmColor:
        bitmap = _BITMAP()
        if gdi32.GetObjectW(ctypes.c_void_p(info.hbmColor),
                            ctypes.sizeof(_BITMAP), ctypes.byref(bitmap)):
            size = (bitmap.bmWidth, bitmap.bmHeight)
        gdi32.DeleteObject(ctypes.c_void_p(info.hbmColor))
    if info.hbmMask:
        gdi32.DeleteObject(ctypes.c_void_p(info.hbmMask))
    return size


class TaskbarIconTests(unittest.TestCase):
    """守住「分尺寸设图标」这一条，否则任务栏画不出图。"""

    @classmethod
    def setUpClass(cls):
        if not TK_OK:
            raise unittest.SkipTest(SKIP_REASON)
        if os.name != 'nt':
            raise unittest.SkipTest('仅 Windows 有意义')

    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.win = tk.Toplevel(self.root)
        self.win.overrideredirect(True)
        self.win.geometry('300x200+150+150')
        self.win.deiconify()
        self.root.update()
        self.win.update()

    def tearDown(self):
        try:
            self.root.destroy()
        except Exception:
            pass

    def test_ico_contains_multiple_sizes(self):
        """ico 必须含 16/32/48/256 四种尺寸，否则分槽加载没有素材。"""
        path = app_icon.ensure_icon_file()
        self.assertTrue(path and os.path.exists(path), '没能生成 ico 文件')
        sizes = {tuple(size) for size in app_icon.ICON_SIZES}
        self.assertIn((16, 16), sizes)
        self.assertIn((32, 32), sizes)

    def test_icons_are_set_per_slot_size(self):
        """``ICON_SMALL`` 必须是 16x16、``ICON_BIG`` 必须是 32x32。

        这条直接拦 Tk ``iconbitmap`` 那套：它会把两个槽位塞成同一个 32x32。
        """
        self.assertTrue(app_icon.apply_window_icon(self.win))
        self.root.update()

        hwnd = app_icon._top_level_hwnd(self.win)
        small, big = _icon_slots(hwnd)
        self.assertNotEqual(small, 0, 'ICON_SMALL 槽位是空的，任务栏没有图标')
        self.assertNotEqual(big, 0, 'ICON_BIG 槽位是空的')
        self.assertEqual(_icon_pixels(small), (16, 16),
                         'ICON_SMALL 不是 16x16 —— 任务栏按钮会画不出图标')
        self.assertEqual(_icon_pixels(big), (32, 32),
                         'ICON_BIG 不是 32x32')

    def test_icon_handles_are_cached(self):
        """同一尺寸只加载一次，重复设置不能产生新句柄。

        每次显示都重设图标（HWND 可能变），若不缓存就是每次 ``LoadImageW``
        一个新句柄、旧的再没人释放，长时间使用会持续泄漏 GDI/USER 资源。
        """
        self.assertTrue(app_icon.apply_window_icon(self.win))
        self.root.update()
        hwnd = app_icon._top_level_hwnd(self.win)
        first = _icon_slots(hwnd)

        for _ in range(3):
            app_icon.apply_window_icon(self.win)
            self.root.update()

        self.assertEqual(_icon_slots(hwnd), first,
                         '重复设置图标产生了新句柄 → 每次显示都在泄漏')

    def test_icon_survives_show_hide_round_trip(self):
        """显隐往返后图标槽位不变——这是「显示后图标消失」的直接回归。"""
        self.assertTrue(app_icon.apply_window_icon(self.win))
        self.root.update()
        hwnd = app_icon._top_level_hwnd(self.win)
        before = _icon_slots(hwnd)

        for _ in range(2):
            self.win.withdraw()
            self.root.update()
            self.win.deiconify()
            self.root.update()

        self.assertEqual(_icon_slots(hwnd), before,
                         '显隐之后图标没了')


if __name__ == '__main__':
    unittest.main()
