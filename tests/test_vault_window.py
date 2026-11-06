"""密匣窗口的测试：两级结构、汇总、已到期高亮、增删、入口接线。

需要真实 Tk；无 Tk 环境整体跳过，与 ``test_tool_window`` 一致。
窗口由 ``tests/__init__.py`` 的全局挂钩设成全透明，跑测试不在屏幕上闪。
"""
import datetime as dt
import sys
import tkinter as tk
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lumina.database import Database  # noqa: E402
from lumina.vault_window import VaultWindow  # noqa: E402

try:
    _PROBE = tk.Tk()
    _PROBE.withdraw()
    _PROBE.destroy()
    TK_OK = True
except Exception:  # pragma: no cover - 仅在无图形环境时触发
    TK_OK = False

SKIP_REASON = "本机 Tk 不可用"

TODAY = dt.date(2026, 11, 5)


def _rgb(value):
    """``#RRGGBB`` → ``(r, g, b)``。用来按**颜色本身**判断色系，
    而不是按色值的名字——改个变量名骗不过这类断言。"""
    text = str(value).lstrip("#")
    return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))


def _hue(value):
    """返回色相角与饱和度；色系不能按红通道是否最高判断。"""
    r, g, b = (channel / 255 for channel in _rgb(value))
    top, bottom = max(r, g, b), min(r, g, b)
    delta = top - bottom
    if delta == 0:
        return 0.0, 0.0
    if top == r:
        hue = 60 * (((g - b) / delta) % 6)
    elif top == g:
        hue = 60 * ((b - r) / delta + 2)
    else:
        hue = 60 * ((r - g) / delta + 4)
    saturation = 0.0 if top == 0 else delta / top
    return hue, saturation


def _contrast(one, two):
    """两个 ``#RRGGBB`` 之间的 WCAG 对比度（1.0 ~ 21.0）。

    **这是判「这行字到底看不看得清」的唯一可靠判据**：色相、明度、饱和度单独
    看都不行——两个颜色可以色相完全不同却同样浅，叠在一起照样读不出来。
    4.5 是 WCAG AA 对普通文字的要求，小字与次要说明也使用此下限。
    """
    def _lum(value):
        out = []
        for channel in _rgb(value):
            c = channel / 255
            out.append(
                c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
        return 0.2126 * out[0] + 0.7152 * out[1] + 0.0722 * out[2]

    first, second = _lum(one), _lum(two)
    high, low = max(first, second), min(first, second)
    return (high + 0.05) / (low + 0.05)


def _red_distance(value):
    """色相到红色的角距离，处理 0°/360° 接缝。"""
    hue, _saturation = _hue(value)
    return min(hue, 360 - hue)


# 一组固定的示例数据。**基准日固定注入**，所以已到期/临近/正常的判定
# 不随跑测试的日期漂移。
SAMPLE = (
    # (银行, 存单名, 本金, 利率, 月数, 到期日)
    ("招商银行", "大额存单 3 年", 400000, 3.10, 36, "2027-03-15"),
    ("招商银行", "整存整取 6 个月", 50000, 1.55, 6, "2026-11-08"),
    ("工商银行", "整存整取 2 年", 300000, 2.75, 24, "2027-01-10"),
    ("建设银行", "整存整取 1 年", 100000, 2.10, 12, "2026-10-28"),
)

# 版式压力数据：**每一列都取最容易溢出的那个值**——
# 超长银行名与存单名（考验截断）、八位数金额、三位数已到期天数。
# 基准日同上，已到期天数因此是确定的（2026-08-01 距 2026-11-05 为 96 天）。
SAMPLE_TRUNCATION = (
    ("中国工商银行股份有限公司营业部", "三年期整存整取可转让大额存单",
     12345678, 3.55, 36, "2026-08-01"),
    ("中国农业银行股份有限公司营业部", "七天通知存款", 1234567, 1.55, 6,
     "2026-11-06"),
)


def _print_window(window):
    """用 PrintWindow 抓真实窗口像素，GDI 资源在失败路径也必须释放。"""
    import ctypes
    import struct
    from ctypes import wintypes

    from PIL import Image

    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    user32.GetWindowDC.argtypes = [wintypes.HWND]
    user32.GetWindowDC.restype = wintypes.HDC
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
    gdi32.SelectObject.restype = wintypes.HANDLE
    gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT,
                               wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
    gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    window.update()
    hwnd = user32.GetAncestor(window.winfo_id(), 2)
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise ctypes.WinError()
    width, height = rect.right - rect.left, rect.bottom - rect.top
    dc = user32.GetWindowDC(hwnd)
    memory = gdi32.CreateCompatibleDC(dc)
    bitmap = gdi32.CreateCompatibleBitmap(dc, width, height)
    previous = gdi32.SelectObject(memory, bitmap)
    try:
        if not user32.PrintWindow(hwnd, memory, 2):
            raise ctypes.WinError()
        header = ctypes.create_string_buffer(struct.pack(
            "<IiiHHIIiiII", 40, width, -height, 1, 32, 0, width * height * 4, 0, 0, 0, 0))
        pixels = ctypes.create_string_buffer(width * height * 4)
        if gdi32.GetDIBits(memory, bitmap, 0, height, pixels, header, 0) != height:
            raise ctypes.WinError()
        return (Image.frombytes("RGB", (width, height), pixels.raw, "raw", "BGRX"),
                (rect.left, rect.top))
    finally:
        gdi32.SelectObject(memory, previous)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory)
        user32.ReleaseDC(hwnd, dc)


# 同一银行内含普通、临近、历史到期与当天到期，已到期占奇偶两个位置。
SAMPLE_STRIPES = tuple(
    (bank, f"测试存单 {index}", 10000, 2.0, 12, end)
    for bank in ("测试银行甲", "测试银行乙")
    for index, end in enumerate(("2027-03-15", "2026-11-08", "2026-10-28",
                                 "2026-11-05", "2027-05-01")))


class FakePanel:
    """只提供密匣窗口需要的成员。"""

    FONT_FAMILY = "Microsoft YaHei UI"

    def __init__(self, root, tk_mod, db):
        self.tk = tk_mod
        self.db = db
        self.ui = type("U", (), {"config": {}, "db": db})()
        self.win = tk_mod.Toplevel(root)
        self.win.withdraw()

    def _theme(self):
        from lumina.history_panel import HistoryPanel
        # 固定浅色：断言里比较底色时，浅色的「白 vs 淡红」比深色更直观。
        return {k: v[0] for k, v in HistoryPanel.THEME.items()}, False

    def _sys(self, name):
        from lumina.history_panel import HistoryPanel
        return HistoryPanel.SYS_COLORS.get(
            name, HistoryPanel.SYS_COLORS["gray"])[0]


@unittest.skipUnless(TK_OK, SKIP_REASON)
class VaultWindowTests(unittest.TestCase):

    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.db = Database(":memory:")
        self.db.init_schema()
        self.panel = FakePanel(self.root, tk, self.db)
        self.windows = []

    def tearDown(self):
        for window in self.windows:
            window.close()
        # **显式关连接**：内存库也要关，否则 Python 退出时会在别的模块
        # （实测是 ``tokenize``）里冒出 ``ResourceWarning: unclosed database``，
        # 把真正的告警淹掉。
        self.db.close()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    # ---------- 夹具 ----------
    def make_window(self, rows=SAMPLE):
        window = VaultWindow(self.panel)
        self.windows.append(window)
        window._today = TODAY
        if rows:
            banks = {}
            for bank, name, principal, rate, months, end in rows:
                if bank not in banks:
                    banks[bank] = self.db.add_vault_bank(bank)
                self.db.add_vault_deposit(
                    banks[bank], name, principal, rate, months, end)
        window._reload()
        self.root.update()
        return window

    @staticmethod
    def grid_rows(window):
        """把清单里的控件按 ``(row, column)`` 整理出来。

        用 ``grid_info()`` 而不是 ``winfo_children()`` 的顺序——后者的顺序
        不保证与视觉顺序一致，按它断言会得到「顺序恰好对」的假绿。
        """
        rows = {}
        for child in window._table.winfo_children():
            if child.winfo_manager() != "grid" or not isinstance(child, tk.Label):
                continue
            info = child.grid_info()
            rows.setdefault(info["row"], {})[info["column"]] = child
        return rows

    @staticmethod
    def text_of(cell):
        return cell.cget("text")

    def rows_with(self, window, needle, column=3):
        """所有在 ``column`` 列含 ``needle`` 的行号。

        **按整张表找会选错行**：银行主行的「到期 / 倒计时」列显示的是它
        最近的存单，所以「剩 3 天」这类文字在主行和子行上都会出现。只按键
        找行会在「主行 vs 子行」这层选错，断言就落到错误的行上。
        """
        rows = self.grid_rows(window)
        return [index for index in sorted(rows)
                if needle in self.text_of(rows[index][column])]

    def deposit_row_with(self, window, needle):
        """**存单行**（有缩进的那些）里含 ``needle`` 的行号，唯一才返回。"""
        rows = self.grid_rows(window)
        found = [index for index in sorted(rows)
                 if self.text_of(rows[index][0]).startswith(" ")
                 and needle in self.text_of(rows[index][3])]
        self.assertEqual(len(found), 1,
                         f"应恰好有一行存单含 {needle}，实际 {found}")
        return found[0]

    def bank_row_of(self, window, name):
        """某家银行的**主行**行号。"""
        rows = self.grid_rows(window)
        found = [index for index in sorted(rows)
                 if name in self.text_of(rows[index][0])
                 and not self.text_of(rows[index][0]).startswith(" ")]
        self.assertEqual(len(found), 1, f"应恰好有一行主行是 {name}，实际 {found}")
        return found[0]

    def test_stripes_restart_per_bank_and_matured_overrides_both_parities(self):
        window = self.make_window(SAMPLE_STRIPES)
        for _ in range(3):
            rows = self.grid_rows(window)
            expected = [window.P[key] for key in (
                "row_bank_bg", "card", "row_alt", "matured_bg", "matured_bg", "card")]
            self.assertEqual(len(rows), 12)
            for index, cells in rows.items():
                self.assertEqual({cell.cget("bg") for cell in cells.values()},
                                 {expected[index % 6]})
            window._toggle_theme()
            self.root.update()

    def test_stripes_stay_stable_after_reload_and_other_bank_collapse(self):
        window = self.make_window(SAMPLE_STRIPES)

        def signature():
            return [(cells[0].cget("text"), cells[0].cget("bg"))
                    for cells in self.grid_rows(window).values()
                    if cells[0].cget("text").startswith(" ")]

        before = signature()
        window._reload()
        self.root.update()
        self.assertEqual(signature(), before)
        first = window._groups[0]["bank"]["id"]
        window._toggle_collapse(first)
        self.root.update()
        self.assertEqual(signature(), before[5:])
        window._toggle_collapse(first)
        self.root.update()
        self.assertEqual(signature(), before)

    def test_backgrounds_cover_whole_rows_without_changing_column_geometry(self):
        window = self.make_window(SAMPLE_STRIPES)
        for width in (660, 900, 1120):
            window.win.geometry(f"{width}x900")
            self.root.update()
            rows = self.grid_rows(window)
            backgrounds = [child for child in window._table.grid_slaves()
                           if isinstance(child, tk.Frame)]
            self.assertEqual(len(backgrounds), len(rows))
            for background in backgrounds:
                index = background.grid_info()["row"]
                cells = rows[index]
                self.assertEqual(background.winfo_x(), 0)
                self.assertEqual(background.winfo_width(), window._table.winfo_width())
                self.assertEqual(background.winfo_y(), cells[0].winfo_y())
                self.assertEqual(background.winfo_height(), cells[0].winfo_height())
                self.assertEqual(background.cget("bg"), cells[0].cget("bg"))
                for column, cell in cells.items():
                    head = window._table_head.grid_slaves(row=0, column=column)[0]
                    self.assertEqual(cell.winfo_rootx(), head.winfo_rootx())
                    self.assertEqual(cell.winfo_width(), head.winfo_width())
                self.assertLessEqual(cells[5].winfo_x() + cells[5].winfo_width(),
                                     window._canvas.winfo_width())

    @unittest.skipUnless(sys.platform == "win32", "PrintWindow 仅支持 Windows")
    def test_real_pixels_have_no_padding_seams_in_both_themes_and_after_scroll(self):
        from tests.silent_ui import make_visible, silence

        window = self.make_window(SAMPLE_STRIPES)
        try:
            make_visible(window.win)
            for _ in range(3):
                window.win.geometry("660x560")
                self.root.update()
                for scroll in (0.0, 1.0):
                    window._canvas.yview_moveto(scroll)
                    self.root.update()
                    image, origin = _print_window(window.win)
                    for index, cells in self.grid_rows(window).items():
                        y = cells[0].winfo_rooty() + cells[0].winfo_height() // 2
                        if not (window._canvas.winfo_rooty() <= y <
                                window._canvas.winfo_rooty() + window._canvas.winfo_height()):
                            continue
                        expected = _rgb(window.P[("row_bank_bg", "card", "row_alt",
                                                  "matured_bg", "matured_bg", "card")[index % 6]])
                        spans = [(window._table.winfo_rootx(), cells[0].winfo_rootx())]
                        spans.extend((cells[c].winfo_rootx() + cells[c].winfo_width(),
                                      cells[c + 1].winfo_rootx()) for c in range(5))
                        spans.append((cells[5].winfo_rootx() + cells[5].winfo_width(),
                                      window._table.winfo_rootx() + window._table.winfo_width()))
                        for left, right in spans:
                            self.assertGreater(right, left)
                            for x in range(left, right):
                                self.assertEqual(image.getpixel((x - origin[0], y - origin[1])),
                                                 expected, (window.dark, index, x, y))
                window._toggle_theme()
                self.root.update()
        finally:
            silence(window.win)

    def test_stripe_text_and_links_remain_readable(self):
        for palette in (VaultWindow.LIGHT_PALETTE, VaultWindow.DARK_PALETTE):
            for bg in ("card", "row_alt", "row_bank_bg"):
                for fg in ("title", "link", "urgent_fg", "ok_fg"):
                    self.assertGreaterEqual(_contrast(palette[fg], palette[bg]), 4.5,
                                            (fg, bg, palette[fg], palette[bg]))

    # ---------- 两级结构 ----------
    def test_bank_row_then_its_deposits(self):
        """主行在前、它的存单紧随其后。这是需求的核心版式。"""
        window = self.make_window()
        rows = self.grid_rows(window)
        ordered = [rows[r][0].cget("text") for r in sorted(rows)]
        self.assertIn("招商银行", ordered[0])
        self.assertIn("大额存单 3 年", ordered[1])
        self.assertIn("整存整取 6 个月", ordered[2])
        self.assertIn("工商银行", ordered[3])
        self.assertIn("整存整取 2 年", ordered[4])

    def test_deposit_rows_are_indented_visually(self):
        """子行必须有**可见的**层级差，不能只是数据上属于某家银行。

        两条判据：① 内容带缩进；② 主行底色与子行底色不同。
        只断言「数据分组对了」是不够的——分组对了但界面平铺，用户一样
        看不出哪几张存单属于哪家银行。
        """
        window = self.make_window()
        rows = self.grid_rows(window)
        bank_cell = rows[0][0]
        cert_cell = rows[1][0]
        self.assertTrue(self.text_of(cert_cell).startswith(" "),
                        "存单行内容要有缩进")
        self.assertFalse(self.text_of(bank_cell).startswith(" "),
                         "银行主行不该缩进")
        self.assertNotEqual(bank_cell.cget("bg"), cert_cell.cget("bg"),
                            "主行与子行底色必须不同，否则层级看不出来")

    def test_bank_row_is_bolder_than_deposit_row(self):
        """主行字重与子行不同——这是层级在字重上的第二条线索。"""
        window = self.make_window()
        rows = self.grid_rows(window)
        bank_font = rows[0][0].cget("font")
        cert_font = rows[1][0].cget("font")
        self.assertNotEqual(str(bank_font), str(cert_font))

    def test_bank_row_shows_aggregates(self):
        """主行放聚合值：笔数、本金合计、加权年化、利息合计。"""
        window = self.make_window()
        rows = self.grid_rows(window)
        self.assertIn("2 笔", self.text_of(rows[0][0]))
        self.assertEqual(self.text_of(rows[0][1]), "450,000")
        # 招行两笔：40万@3.10%/36月 + 5万@1.55%/6月
        # 利息 37200 + 387.5 = 37587.5 → 四舍五入到元
        self.assertEqual(self.text_of(rows[0][4]), "37,588")

    def test_deposit_row_shows_its_own_values(self):
        window = self.make_window()
        rows = self.grid_rows(window)
        self.assertIn("3 年", self.text_of(rows[1][0]))
        self.assertEqual(self.text_of(rows[1][1]), "400,000")
        self.assertEqual(self.text_of(rows[1][2]), "3.10%")
        self.assertEqual(self.text_of(rows[1][4]), "37,200")

    def test_bank_with_no_deposits_renders_a_row(self):
        """**没有存单的银行也要出现在清单里**。用户刚建完银行就该看到它；
        不显示的话他会以为建失败了，接着再建一次，得到重复记录。"""
        window = VaultWindow(self.panel)
        self.windows.append(window)
        window._today = TODAY
        self.db.add_vault_bank("空银行")
        window._reload()
        self.root.update()
        rows = self.grid_rows(window)
        texts = [rows[r][0].cget("text") for r in sorted(rows)]
        self.assertTrue(any("空银行" in t for t in texts), texts)
        self.assertTrue(any("暂无存单" in self.text_of(rows[r][3])
                            for r in rows))

    # ---------- 已到期高亮 ----------
    def test_matured_row_is_fully_highlighted(self):
        """已到期存单**整行**要醒目，不是只有日期那一格变色。

        断言的是「同一行的每一格底色都相等且不等于正常行底色」——
        只测日期那一格的话，「只给一格上色」这个缺陷会逃过。
        """
        window = self.make_window()
        rows = self.grid_rows(window)
        matured_row = self.deposit_row_with(window, "已到期")
        normal_row = self.deposit_row_with(window, "剩 130 天")
        matured_bgs = {rows[matured_row][c].cget("bg")
                       for c in rows[matured_row]}
        normal_bg = rows[normal_row][1].cget("bg")
        # 整行同色（含最后一列），且与正常行不同。
        self.assertEqual(len(matured_bgs), 1,
                         f"已到期行的底色不统一：{matured_bgs}")
        self.assertNotEqual(next(iter(matured_bgs)), normal_bg)

    def test_matured_date_text_differs_from_normal(self):
        window = self.make_window()
        rows = self.grid_rows(window)
        matured_row = self.deposit_row_with(window, "已到期")
        normal_row = self.deposit_row_with(window, "剩 130 天")
        self.assertNotEqual(rows[matured_row][3].cget("fg"),
                            rows[normal_row][3].cget("fg"))

    def test_matured_ochre_red_is_separate_from_interaction_blue(self):
        """状态赭红与交互蓝必须属于不同色系，不靠变量名判断。"""
        for palette in (VaultWindow.LIGHT_PALETTE, VaultWindow.DARK_PALETTE):
            self.assertLess(_red_distance(palette["matured_fg"]), 25)
            hue, saturation = _hue(palette["link"])
            self.assertTrue(195 <= hue <= 220)
            self.assertGreater(saturation, 0.3)
            self.assertNotEqual(palette["matured_fg"], palette["link"])

    def test_the_normal_colour_is_green(self):
        """正常档用绿色（色相约 100~170°）。"""
        for name, palette in (("LIGHT", VaultWindow.LIGHT_PALETTE),
                              ("DARK", VaultWindow.DARK_PALETTE)):
            hue, saturation = _hue(palette["ok_fg"])
            self.assertGreater(saturation, 0.4, f"{name} 的正常色饱和度太低")
            self.assertTrue(100 <= hue <= 170,
                            f"{name} 的正常色 {palette['ok_fg']} "
                            f"色相 {hue:.0f}° 不是绿色")

    def test_matured_and_urgent_use_different_colours(self):
        """已到期与临近到期必须是**两个**颜色。合成一个的话
        「还剩几天」和「已经到期了」这两件事就分不出来了。"""
        light = VaultWindow.LIGHT_PALETTE
        self.assertNotEqual(light["matured_fg"], light["urgent_fg"])
        dark = VaultWindow.DARK_PALETTE
        self.assertNotEqual(dark["matured_fg"], dark["urgent_fg"])

    def test_all_three_due_states_are_visible_on_their_own_background(self):
        """状态、正文、标题、交互与白底说明在实际底色上须达到 AA ≥4.5。"""
        for name, palette in (("LIGHT", VaultWindow.LIGHT_PALETTE),
                              ("DARK", VaultWindow.DARK_PALETTE)):
            cases = (
                ("已到期", palette["matured_fg"], palette["matured_bg"]),
                ("90 天内到期", palette["urgent_fg"], palette["card"]),
                ("正常", palette["ok_fg"], palette["card"]),
                ("正文", palette["title"], palette["card"]),
                ("次要说明", palette["muted"], palette["card"]),
                ("顶栏标题", palette["rail_fg"], palette["rail_bg"]),
                ("主按钮", palette["button_fg"], palette["button"]),
                ("主按钮按下", palette["button_fg"], palette["button_hot"]),
                ("临近主行", palette["urgent_fg"], palette["row_bank_bg"]),
                ("正常主行", palette["ok_fg"], palette["row_bank_bg"]),
                ("已到期主行", palette["matured_fg"], palette["row_bank_bg"]),
                ("已到期链接", palette["link"], palette["matured_bg"]),
                ("次按钮悬停", palette["link"], palette["head_bg"]),
                ("普通指标副行", palette["faint"], palette["metric_bg"]),
            )
            for label, fg, bg in cases:
                ratio = _contrast(fg, bg)
                self.assertGreaterEqual(
                    ratio, 4.5,
                    f"{name} 的「{label}」{fg} 在 {bg} 上对比度只有 "
                    f"{ratio:.2f}（需 ≥4.5），这行字读不清")

    def test_secondary_text_contrast_documents_confirmed_palette_limit(self):
        """指定浅色灰在非白底约 4.22–4.39，不能误报为 AA 达标。"""
        light = VaultWindow.LIGHT_PALETTE
        for key in ("muted", "faint"):
            for bg in ("surface", "head_bg", "matured_bg"):
                ratio = _contrast(light[key], light[bg])
                self.assertGreaterEqual(ratio, 4.2)
                self.assertLess(ratio, 4.5)
        dark = VaultWindow.DARK_PALETTE
        for key in ("muted", "faint"):
            for bg in ("surface", "head_bg", "metric_bg", "matured_bg"):
                self.assertGreaterEqual(_contrast(dark[key], dark[bg]), 4.5)

    def test_metric_cards_have_a_colour_strip(self):
        """四张指标卡的左侧 2px 色条真实存在，且各自有可见的颜色区分。"""
        window = self.make_window()
        strips = {}
        for key, item in window._metric_widgets.items():
            strip = item["strip"]
            self.assertTrue(strip.winfo_exists(), f"{key} 卡没有色条")
            self.assertEqual(
                int(strip.cget("width")), 2,
                f"{key} 的色条不是 2px")
            strips[key] = str(strip.cget("bg")).upper()
        self.assertEqual(
            len(set(strips.values())), 4,
            f"四张卡的色条应该四个颜色，实际 {strips}")
        self.assertEqual(strips["principal"], "#244A68",
                         "本金卡色条应使用确认的主蓝")

    def test_matured_countdown_says_the_number(self):
        """倒计时直接写「已到期 8 天」，用户不用自己减日期。"""
        window = self.make_window()
        row = self.deposit_row_with(window, "已到期")
        self.assertIn("已到期 8 天", self.text_of(self.grid_rows(window)[row][3]))

    def test_today_due_deposit_is_rendered_as_matured(self):
        """**当天到期的存单在界面上属于「已到期」那一档**（含当天口径）。

        上面那条查的是 8 天前到期的那笔，只覆盖 ``days < 0``。当天
        （``days == 0``）是口径改动真正的边界，得有独立的存单来钉：
        - 文案是「今天到期」而不是「剩 0 天」（后者读起来像已经错过了）；
        - 颜色与底色**与已到期一致**，与正常档不同；
        - 指标卡把它计入「已到期」而不是「90 天内到期」。

        **这条是被变异验证逼出来的**：把「当天」改回 ``urgent`` 档时，
        只有计算层测试失败，窗口层全绿——说明窗口层实际上没覆盖这个边界。
        """
        window = VaultWindow(self.panel)
        self.windows.append(window)
        window._today = TODAY                       # 基准日 2026-11-05
        bank_id = self.db.add_vault_bank("当天到期行")
        self.db.add_vault_deposit(bank_id, "今天到期存单", 10000, 2.0, 12,
                                  "2026-11-05")      # 恰好今天
        window._reload()
        self.root.update()

        row = self.deposit_row_with(window, "今天到期")
        cells = self.grid_rows(window)[row]
        self.assertEqual(self.text_of(cells[3]), "2026-11-05 · 今天到期")

        # 与已到期同档：日期色、整行底色都与正常行不同。
        window2 = self.make_window()                # 含已到期（8 天）与正常行
        rows2 = self.grid_rows(window2)
        matured = rows2[self.deposit_row_with(window2, "已到期")]
        normal = rows2[self.deposit_row_with(window2, "剩 130 天")]
        self.assertEqual(cells[3].cget("fg"), matured[3].cget("fg"),
                         "当天到期的日期色应与已到期一致")
        self.assertNotEqual(cells[3].cget("fg"), normal[3].cget("fg"))
        self.assertEqual(cells[1].cget("bg"), matured[1].cget("bg"),
                         "当天到期应整行铺已到期底色")
        self.assertNotEqual(cells[1].cget("bg"), normal[1].cget("bg"))

        # 指标卡：计入「已到期」，不计入「90 天内到期」。
        self.assertEqual(
            window._metric_widgets["matured"]["value"].cget("text"), "1 笔")
        self.assertEqual(
            window._metric_widgets["soon"]["value"].cget("text"), "0 笔")

    def test_urgent_but_not_matured_uses_a_third_colour(self):
        """临近到期（非已到期）不该和已到期同色，也不该和正常同色——
        三档状态要能一眼分开。

        **这条抓到过一个真实缺陷**：``_grid_row`` 曾判 ``if urgent:``，
        而 ``"normal"`` 是非空字符串、为真，于是正常状态也被涂成警示色，
        三档实际只有两档可见。只比较「已到期 vs 正常」的测试发现不了它。
        """
        window = self.make_window()
        rows = self.grid_rows(window)
        urgent = rows[self.deposit_row_with(window, "剩 3 天")][3].cget("fg")
        matured = rows[self.deposit_row_with(window, "已到期")][3].cget("fg")
        normal = rows[self.deposit_row_with(window, "剩 130 天")][3].cget("fg")
        self.assertNotEqual(urgent, matured)
        self.assertNotEqual(urgent, normal)
        self.assertNotEqual(matured, normal)

    def test_normal_due_date_uses_the_positive_colour(self):
        """正常状态用**正向的绿色**（本轮重渲染新增的一档）。

        原方案正常档不上色（等于这一档没有信号）。这条同时钉住「它不再是
        主文字色」——改回不上色会让它等于 ``title``，测试就会失败。
        """
        window = self.make_window()
        rows = self.grid_rows(window)
        normal_row = self.deposit_row_with(window, "剩 130 天")
        normal_fg = rows[normal_row][3].cget("fg")
        self.assertEqual(normal_fg, window.P["ok_fg"])
        self.assertNotEqual(normal_fg, window.P["title"])

    def test_urgent_row_is_not_given_the_matured_background(self):
        """临近到期**不该**铺已到期那种底色——否则「整行变色」就不再意味着
        「已经到期」，那个视觉信号会失效。"""
        window = self.make_window()
        rows = self.grid_rows(window)
        urgent_row = self.deposit_row_with(window, "剩 3 天")
        normal_row = self.deposit_row_with(window, "剩 130 天")
        self.assertEqual(rows[urgent_row][1].cget("bg"), window.P["row_alt"])
        self.assertEqual(rows[normal_row][1].cget("bg"), window.P["card"])
        self.assertNotEqual(rows[urgent_row][1].cget("bg"), window.P["matured_bg"])

    # ---------- 指标卡 ----------
    def test_metrics_show_expected_numbers(self):
        window = self.make_window()
        m = window._metric_widgets
        # 本金 85 万；本息 906187.5 退到副行（「本金」卡的副行是「本息 ¥…」）。
        self.assertEqual(m["principal"]["value"].cget("text"), "¥850,000")
        self.assertIn("906,188", m["principal"]["sub"].cget("text"))
        self.assertEqual(m["interest"]["value"].cget("text"), "¥56,188")
        self.assertIn("2.92%", m["interest"]["sub"].cget("text"))
        self.assertEqual(m["soon"]["value"].cget("text"), "2 笔")
        self.assertEqual(m["matured"]["value"].cget("text"), "1 笔")

    def test_metrics_are_rounded_not_raw_floats(self):
        """指标卡上的数字要四舍五入到**整元**。带小数的一串数字在卡片上
        会溢出，读起来也不像金额。"""
        window = self.make_window()
        for key in ("principal", "interest"):
            text = window._metric_widgets[key]["value"].cget("text")
            self.assertNotIn(".", text, f"{key} 出现了小数：{text}")

    def test_metrics_update_after_a_deposit_is_added(self):
        """指标卡必须是数据的函数。这条防的是「加了数据但忘了刷指标卡」——
        只测初始渲染的话，漏刷的缺陷完全看不出来。"""
        window = self.make_window()
        before = window._metric_widgets["principal"]["value"].cget("text")
        bank_id = self.db.list_vault_banks()[0]["id"]
        self.db.add_vault_deposit(bank_id, "新增", 100000, 2.0, 12,
                                 "2027-06-01")
        window._reload()
        self.root.update()
        after = window._metric_widgets["principal"]["value"].cget("text")
        self.assertNotEqual(before, after)

    def test_empty_database_is_not_a_crash(self):
        window = self.make_window(rows=())
        self.assertEqual(
            window._metric_widgets["principal"]["value"].cget("text"), "¥0")
        texts = [c.cget("text") for c in window._table.winfo_children()
                 if c.winfo_manager() == "grid"]
        self.assertTrue(any("还没有记录" in t for t in texts), texts)

    def test_metric_card_labels_match_the_requirements(self):
        """四张卡的标题就是用户确认过的那四个词。

        这条单独钉文案：「本息合计 → 本金」「已逾期 → 已到期」是用户
        逐字给的，改错一个字（比如「已过期」「到期」）都不算完成。
        """
        window = self.make_window()
        labels = [item["caption"].cget("text")
                  for item in window._metric_widgets.values()]
        self.assertIn("本金", labels)
        self.assertIn("已到期", labels)
        self.assertNotIn("本息合计", labels)
        self.assertNotIn("已逾期", labels)
        # **按控件取标题，不按 ``winfo_children()[0]`` 取**：卡片里现在第一个
        # 子控件是左侧色条（``place`` 进去的 Frame），它没有 ``-text`` 选项，
        # 按位置取会拿到它并抛 ``unknown option "-text"``。位置会随下一次改动
        # 再变，标题的位置也不会变——所以断言要钉在**语义明确的那个控件**上。
        self.assertEqual(len(labels), 4, f"应有四张卡的标题，实际 {labels}")

    # ---------- 折叠 ----------
    def test_collapse_hides_exactly_that_banks_deposits(self):
        window = self.make_window()
        full = len(self.grid_rows(window))
        banks = {b["name"]: b["id"] for b in self.db.list_vault_banks()}
        window._toggle_collapse(banks["招商银行"])
        self.root.update()
        collapsed = self.grid_rows(window)
        # 招行有 2 张存单 → 少 2 行；其他银行的存单不受影响。
        self.assertEqual(len(collapsed), full - 2)
        remaining = [collapsed[r][0].cget("text") for r in sorted(collapsed)]
        self.assertTrue(any("工商银行" in t for t in remaining))
        self.assertFalse(any("大额存单" in t for t in remaining))

    def test_collapse_arrow_reflects_state(self):
        """折叠状态要有可见的指示（箭头方向），否则用户不知道这家银行
        是「折叠起来了」还是「真的没有存单」。"""
        window = self.make_window()
        banks = {b["name"]: b["id"] for b in self.db.list_vault_banks()}
        rows = self.grid_rows(window)
        expanded_arrow = self.text_of(rows[0][0])[0]
        window._toggle_collapse(banks["招商银行"])
        self.root.update()
        rows = self.grid_rows(window)
        collapsed_arrow = self.text_of(rows[0][0])[0]
        self.assertNotEqual(expanded_arrow, collapsed_arrow)

    def test_collapse_toggles_back(self):
        window = self.make_window()
        banks = {b["name"]: b["id"] for b in self.db.list_vault_banks()}
        full = len(self.grid_rows(window))
        window._toggle_collapse(banks["招商银行"])
        window._toggle_collapse(banks["招商银行"])
        self.root.update()
        self.assertEqual(len(self.grid_rows(window)), full)

    # ---------- 删除 ----------
    def test_delete_deposit_removes_it_from_the_list(self):
        window = self.make_window()
        target = next(d for d in self.db.list_vault_deposits()
                      if d["name"] == "大额存单 3 年")
        window.db.delete_vault_deposit(target["id"])
        window._reload()
        self.root.update()
        all_text = [c.cget("text") for r in self.grid_rows(window).values()
                    for c in r.values()]
        self.assertFalse(any("大额存单 3 年" in t for t in all_text))

    def test_delete_bank_removes_its_deposits(self):
        """删银行要连存单一起删，不能留下孤儿记录——那些记录在界面上会
        变成「未归银行」，但用户其实是要把它们删掉。"""
        window = self.make_window()
        banks = {b["name"]: b["id"] for b in self.db.list_vault_banks()}
        window.db.delete_vault_bank(banks["招商银行"])
        window._reload()
        self.root.update()
        names = [d["name"] for d in self.db.list_vault_deposits()]
        self.assertNotIn("大额存单 3 年", names)
        self.assertNotIn("整存整取 6 个月", names)
        self.assertIn("整存整取 2 年", names)

    def test_orphan_deposits_are_still_shown(self):
        """孤儿存单（``bank_id`` 指向不存在的银行）必须仍然渲染出来。
        数据在而看不见是最难排查的一类问题。"""
        window = VaultWindow(self.panel)
        self.windows.append(window)
        window._today = TODAY
        self.db.add_vault_bank("招商银行")
        self.db.add_vault_deposit(999, "幽灵存单", 1000, 1.0, 12,
                                 "2027-01-01")
        window._reload()
        self.root.update()
        texts = [c.cget("text") for r in self.grid_rows(window).values()
                 for c in r.values()]
        # **用子串而不是相等**：存单行的文本带缩进（层级差就靠它），
        # 相等比较必然找不到——那是测试写错了，不是功能不对。
        self.assertTrue(any("幽灵存单" in t for t in texts), texts)
        self.assertTrue(any("未归银行" in t for t in texts), texts)

    def test_orphan_group_has_no_dead_collapse_arrow(self):
        """孤儿分组**不给折叠箭头**：它的 bank_id 是 None，点上去只会落进
        早退分支，用户看到的是一个有箭头但点不动的行。"""
        window = VaultWindow(self.panel)
        self.windows.append(window)
        window._today = TODAY
        self.db.add_vault_bank("招商银行")
        self.db.add_vault_deposit(999, "幽灵存单", 1000, 1.0, 12,
                                 "2027-01-01")
        window._reload()
        self.root.update()
        rows = self.grid_rows(window)
        orphan_row = next(r for r in rows if "未归银行" in self.text_of(rows[r][0]))
        self.assertNotIn(self.text_of(rows[orphan_row][0])[0], "▸▾")
        self.assertEqual(self.text_of(rows[orphan_row][5]), "")

    # ---------- 交互 ----------
    def test_bank_name_click_toggles_collapse(self):
        """主行的第一格真的接上了折叠事件（不是只有 `_toggle_collapse`
        这个方法存在）。用 ``event_generate`` 打真实点击。"""
        window = self.make_window()
        full = len(self.grid_rows(window))
        cell = self.grid_rows(window)[0][0]
        cell.event_generate("<Button-1>", x=4, y=4)
        self.root.update()
        self.assertLess(len(self.grid_rows(window)), full)

    def test_scroll_binding_is_on_this_window_not_global(self):
        """滚轮必须绑在**本窗口**上。

        ``bind_all`` 是全局的，会把主面板的滚动一起劫持——实测那种
        「在密匣里滚，剪贴板列表跟着动」的现象就是这么来的。这里断言
        本窗口有绑定，且 ``all`` 上没有新增的 MouseWheel 绑定。
        """
        window = self.make_window()
        self.assertTrue(window.win.bind("<MouseWheel>"),
                        "窗口级 MouseWheel 绑定缺失，滚轮会失效")
        for sequence in ("<MouseWheel>",):
            global_bind = window.win.bind_all(sequence)
            self.assertFalse(
                global_bind, f"不该有全局 {sequence} 绑定：{global_bind}")

    def test_column_count_matches_the_header(self):
        """每一行的单元格数必须与表头列数一致。少了或多了都会让整张表
        错位——而错位在几何断言里不一定看得出来（列宽是固定的）。"""
        window = self.make_window()
        columns = len(window._columns)
        for index, cells in self.grid_rows(window).items():
            self.assertEqual(len(cells), columns, f"第 {index} 行列数不对")

    def test_row_data_shorter_than_columns_raises(self):
        """守卫：``_grid_row`` 拿到的列数不对时要**明确报错**。

        这条钉的是一个真实发生过的缺陷：``_grid_row`` 里循环变量与列标题
        撞名，导致每行文字被渲染成了列标题（界面看起来像表头重复了很多遍）。
        加这条长度校验之后，那类错误会在构造期就暴露。
        """
        window = self.make_window()
        with self.assertRaises(ValueError):
            window._grid_row(window._table, 99, ["只有一个"],
                             bg="#FFFFFF", height=20)

    def test_no_row_renders_header_text_as_data(self):
        """**防那次的回归**：任何一行的第一格都不该正好等于表头标题。

        之前 ``_grid_row`` 的解包把入参覆盖了，结果每行第一格是
        「银行 / 存单」。这条直接扫渲染结果，比断言「某个数字出现」更难被
        巧合满足。
        """
        window = self.make_window()
        header = self.text_of(
            window._table_head.grid_slaves(row=0, column=0)[0])
        for index, cells in self.grid_rows(window).items():
            for column, cell in cells.items():
                self.assertNotEqual(
                    self.text_of(cell), header,
                    f"第 {index} 行第 {column} 列渲染成了表头文字")

    # ---------- 状态与提示 ----------
    def test_hint_states_the_interest_formula(self):
        """利息口径必须写在界面上。用户特意确认过按单利按月份折算，
        把它写出来省得他每次对不上都来问。

        **窗口缩窄后这句话从操作条挪到了表格下方的脚注**（``_footnote``）：
        半宽窗口里操作条放不下 341px 的说明文字（实测），硬留会把两个按钮
        挤到换行。所以这条测试查的是脚注，操作条只留计数。
        """
        window = self.make_window()
        footnote = window._footnote.cget("text")
        self.assertIn("单利", footnote)
        self.assertIn("12", footnote)
        # 操作条仍然要有计数（定位信息没丢，只是换了个位置）。
        self.assertIn("3 家银行", window._hint.cget("text"))

    def test_load_failure_is_reported_not_raised(self):
        """读库失败要在界面上说清，而不是把异常抛进 Tk 回调变成一行堆栈。

        这里把 ``list_vault_banks`` 打坏，断言窗口仍然建得起来且给出了提示。
        """
        window = VaultWindow(self.panel)
        self.windows.append(window)
        window._today = TODAY

        def boom():
            raise RuntimeError("模拟读库失败")

        original = self.db.list_vault_banks
        self.db.list_vault_banks = boom
        try:
            window._reload()          # 不该抛
        finally:
            self.db.list_vault_banks = original
        self.root.update()
        self.assertIn("读取本地数据失败", window._load_error)
        texts = [c.cget("text") for c in window._table.winfo_children()
                 if c.winfo_manager() == "grid"]
        self.assertTrue(any("读取本地数据失败" in t for t in texts), texts)


@unittest.skipUnless(TK_OK, SKIP_REASON)
class VaultLayoutTests(unittest.TestCase):
    """窄版窗口的版式约束。

    用户要求「宽度缩短大概一半，高度扩充点」，缩窄牵动了三件容易复发的事：

    1. **表头与数据逐列对齐** —— 表头和数据是**两个独立的 grid 容器**，
       各自按自己的内容算列宽；表头「本金」24px、数据「400,000」45px，
       不把「Label 装饰 + 单元格内边距」并进 ``minsize`` 就会逐列错位
       4~10px。这个缺陷在缩窄**之前**就存在（900px 宽下同样错位）。
    2. **内容不得撑开列** —— ``grid`` 的 ``minsize`` 只是下限，内容更宽时列
       会被撑大；长银行名曾把内嵌区从 600px 撑到 664px，**「删除」列被顶出
       画布，用户点不到**，而且不报任何错。
    3. **宽度预算** —— 窗口宽必须 ≥ 列宽 + 装饰 + 内边距 + 卡片损耗，
       低于这个值就是上面那条缺陷。

    三条都是「几何实测」，不是读源码常量：常量对不上界面时，只有实测能发现。
    """

    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.db = Database(":memory:")
        self.db.init_schema()
        self.panel = FakePanel(self.root, tk, self.db)
        self.windows = []

    def tearDown(self):
        for window in self.windows:
            window.close()
        self.db.close()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def make_window(self, rows, width=None):
        window = VaultWindow(self.panel)
        self.windows.append(window)
        window._today = TODAY
        banks = {}
        for bank, name, principal, rate, months, end in rows:
            if bank not in banks:
                banks[bank] = self.db.add_vault_bank(bank)
            self.db.add_vault_deposit(
                banks[bank], name, principal, rate, months, end)
        if width:
            window.win.geometry(f"{width}x700")
        window._reload()
        self.root.update()
        self.root.update_idletasks()
        return window

    def test_default_width_fits_the_columns(self):
        """默认宽度下，内嵌区不得超出画布。

        超出时「删除 / 删银行」列被顶到可视区外——**能渲染、但点不到**，
        界面上看不出异常。这条是窄版宽度的**下限守卫**：把 ``width`` 调到
        628 以下，这里会失败。
        """
        window = self.make_window(SAMPLE_TRUNCATION)
        inner = window._table.winfo_reqwidth()
        canvas = window._canvas.winfo_width()
        self.assertLessEqual(
            inner, canvas,
            f"内嵌区需 {inner}px 而画布只有 {canvas}px，"
            f"右侧「删除」列会被顶出可视区")

    def test_min_width_also_fits(self):
        """缩到最小尺寸也要放得下——用户可以手动拖窄。"""
        window = self.make_window(SAMPLE_TRUNCATION)
        window.win.geometry(f"{VaultWindow.min_width}x700")
        self.root.update()
        window._reload()
        self.root.update()
        window.win.update_idletasks()
        self.assertLessEqual(window._table.winfo_reqwidth(),
                             window._canvas.winfo_width(),
                             "最小宽度下内嵌区超出画布")

    def test_header_and_body_columns_align(self):
        """表头与数据的每一列左缘必须一致。

        **这是缩窄前就存在的缺陷**（900px 宽下表头 x=512 而数据 x=440），
        所以断言不看窗口宽度、也不放宽容差：差 1px 也是错位。
        """
        window = self.make_window(SAMPLE)
        head_x = {int(c.grid_info()["column"]): c.winfo_x()
                  for c in window._table_head.grid_slaves()}
        body_x = {}
        for cell in window._table.grid_slaves():
            if not isinstance(cell, tk.Label):
                continue
            body_x.setdefault(int(cell.grid_info()["column"]),
                              cell.winfo_x())
        for column in sorted(set(head_x) & set(body_x)):
            self.assertEqual(
                head_x[column], body_x[column],
                f"第 {column} 列表头 x={head_x[column]} 而数据 x={body_x[column]}")

    def test_long_names_do_not_widen_the_table(self):
        """超长银行名不得撑开内嵌区。

        两次各用一个**独立的内存库**：同一个库上追加数据会让第二条把第一条
        的行也渲染出来，``reqwidth`` 的比较就不公平了（行数不同本身会改变
        宽度）。比较的是「同样一行，长名 vs 短名」下的内嵌区宽度。
        """
        def width_for(rows):
            db = Database(":memory:")
            db.init_schema()
            panel = FakePanel(self.root, tk, db)
            window = VaultWindow(panel)
            self.windows.append(window)
            window._today = TODAY
            bank_id = db.add_vault_bank(rows[0][0])
            db.add_vault_deposit(bank_id, "存单", 400000, 2.9, 36,
                                 "2026-11-08")
            window._reload()
            self.root.update()
            self.root.update_idletasks()
            width = window._table.winfo_reqwidth()
            db.close()
            return width

        short = width_for([("招商银行",)])
        long_ = width_for([("中国工商银行股份有限公司营业部",)])
        self.assertEqual(
            short, long_,
            f"长名字把内嵌区从 {short}px 撑到 {long_}px，说明列被内容撑开了")

    def test_long_name_is_truncated_with_a_hint(self):
        """放不下的名字要**显式截断**（加省略号），而不是让 grid 硬挤。

        同时确认截断不是「静默丢字」：``_fit_text`` 只在该格确实放不下时
        动手，放得下时原样返回。
        """
        window = self.make_window(SAMPLE_TRUNCATION)
        long_name = "中国工商银行股份有限公司营业部"
        self.assertTrue(window._fit_text(long_name, "bank").endswith("…"),
                        "长名字没有被截断")
        self.assertEqual(window._fit_text("招商银行", "bank"), "招商银行",
                         "短名字不该被动")

    def test_critical_columns_are_never_truncated(self):
        """金额 / 年化 / 到期 / 操作**四列不得截断**。

        这几列是机器算出来的值，宽度已按实测最坏值给足：
        到期列放得下「2026-08-01 · 已到期 966 天」（154px），金额列放得下
        「12,345,678」（62px）。被截断说明列宽常量被改小了。

        **文案改长时这条会失败**——那正是它的价值：「已到期」比「逾期」
        长 12px（142 → 154），到期列必须同步从 144 提到 156。
        """
        window = self.make_window(SAMPLE_TRUNCATION)
        for key, text in (("due", "2026-08-01 · 已到期 966 天"),
                          ("principal", "12,345,678"),
                          ("interest", "12,345,678"),
                          ("rate", "3.55%"),
                          ("action", "删银行")):
            self.assertNotIn(
                "…", window._fit_text(text, key),
                f"{key} 列放不下 {text!r}，列宽常量需要调大")

    def test_width_budget_covers_the_column_sum(self):
        """窗口宽必须 ≥ 列宽合计 + 装饰 + 内边距 + 卡片损耗。

        这条是**纯算术**的守卫，补在两条几何测试前面：几何测试要在真实
        Tk 里量，而这条在任何环境下都能跑。改列宽常量或改窗口宽时，
        它先一步指出预算已经被撑破。
        """
        from lumina.vault_window import CELL_PADX, LABEL_CHROME
        window = self.make_window(SAMPLE)
        column_sum = sum(window._widths.values())
        need = column_sum + LABEL_CHROME * 6 + 2 * CELL_PADX * 6
        self.assertGreaterEqual(
            VaultWindow.width, need + 28,
            f"列宽合计 {column_sum} 需要窗口至少 {need + 28}px，"
            f"而 width={VaultWindow.width}")
        self.assertGreaterEqual(VaultWindow.min_width, VaultWindow.width,
                                "min_width 不该小于 width，否则用户拖窄后"
                                "「删除」列会被顶出可视区")

    def test_metrics_are_a_two_by_two_grid(self):
        """四张指标卡必须排成 2×2。

        原来是一行四张（1180px 窗口放得下）。缩窄后一行四张每张只剩约
        130px，而 15pt 的「¥12,345,678」就要 120px，必然截断。
        """
        window = self.make_window(SAMPLE)
        grid = {key: (int(item["card"].grid_info()["row"]),
                      int(item["card"].grid_info()["column"]))
                for key, item in window._metric_widgets.items()}
        self.assertEqual(len({r for r, _c in grid.values()}), 2,
                         f"指标卡应有 2 行，实际 {grid}")
        self.assertEqual(len({c for _r, c in grid.values()}), 2,
                         f"指标卡应有 2 列，实际 {grid}")

    def test_metric_sub_line_has_visible_height(self):
        """指标卡的副行（「本息 ¥…」「资金加权年化 …」）必须有可见高度。

        **卡高写 62 时这行会被压成 0 高**——不报错、只是整行字凭空消失，
        「本息 / 资金加权年化 / 本金」这些说明全部不见。三行实测要 70px，
        所以卡高必须 ≥ 70。判据用 ``winfo_height`` 而不是 ``reqwidth``：
        文字宽度一直是对的，被压掉的是**高度**。
        """
        window = self.make_window(SAMPLE)
        self.assertGreaterEqual(
            window._metric_widgets["principal"]["card"].winfo_height(),
            70, "指标卡不够高，副行会被压成 0 高")
        for key, item in window._metric_widgets.items():
            self.assertGreater(
                item["sub"].winfo_height(), 1,
                f"{key} 卡副行没有可见高度（被压没了）")


@unittest.skipUnless(TK_OK, SKIP_REASON)
class VaultPaletteChromeTests(unittest.TestCase):
    """蓝灰外观：顶栏与外壳同色、交互蓝与状态赭红分离。

    这组测试守的是「整窗外壳色只在一个地方定义」这件事。``_surface_bg()``
    这个色散落在窗口本体、自绘外壳、正文容器、底栏抓手、**以及那圈透明缩放
    热区**上；热区还各自有 ``_edge_enter`` / ``_edge_leave`` 两次改色。
    任何一处漏改，外壳上就会出现一圈与周围不同色的补丁——**透明热区、
    不报错、不闪**，只是看起来像「边缘有一道脏边」，肉眼很难定位。

    所以判据不能只查「源码里有一句 ``_surface_bg``」，要**把那圈热区一个个
    读出来比色**。
    """

    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.db = Database(":memory:")
        self.db.init_schema()
        self.panel = FakePanel(self.root, tk, self.db)
        self.windows = []

    def tearDown(self):
        for window in self.windows:
            window.close()
        self.db.close()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def make_window(self, dark=False, rows=SAMPLE):
        panel = self.panel
        if dark:
            panel = FakePanel(self.root, tk, self.db)
            panel._theme = lambda: (
                {k: v[1] for k, v in _history_theme().items()}, True)
        window = VaultWindow(panel)
        self.windows.append(window)
        window._today = TODAY
        banks = {}
        for bank, name, principal, rate, months, end in rows:
            if bank not in banks:
                banks[bank] = self.db.add_vault_bank(bank)
            self.db.add_vault_deposit(
                banks[bank], name, principal, rate, months, end)
        window._reload()
        self.root.update()
        return window

    def test_theme_button_roundtrip_preserves_window_and_data(self):
        window = self.make_window()
        identity = window.win.winfo_id()
        bank_id = window._groups[0]["bank"]["id"]
        window._toggle_collapse(bank_id)
        self.root.update()
        data = window._data.copy()
        banks = [dict(row) for row in self.db.list_vault_banks()]
        deposits = [dict(row) for row in self.db.list_vault_deposits()]
        geometry = window.win.geometry()
        rows = len(VaultWindowTests.grid_rows(window))
        window._theme_button.event_generate("<Button-1>")
        window.win.update()
        self.assertTrue(window.dark)
        self.assertEqual(window.win.winfo_id(), identity)
        self.assertEqual(window._data, data)
        self.assertEqual(window.title_label.master.cget("bg"), window.P["rail_bg"])
        self.assertEqual(window._scroll.canvas.cget("bg"), window.P["card"])
        window._theme_button.event_generate("<Button-1>")
        window.win.update()
        self.assertFalse(window.dark)
        self.assertEqual(window._data, data)
        self.assertEqual(window.win.winfo_id(), identity)
        self.assertEqual(window.win.geometry(), geometry)
        self.assertEqual(window._collapsed, {bank_id})
        self.assertEqual(len(VaultWindowTests.grid_rows(window)), rows)
        self.assertEqual([dict(row) for row in self.db.list_vault_banks()], banks)
        self.assertEqual([dict(row) for row in self.db.list_vault_deposits()], deposits)
        self.assertEqual(window._theme_button.cget("text"), "切换深色")

    def test_resize_corners_never_cover_rail(self):
        for dark in (False, True):
            window = self.make_window(dark=dark)
            rail = window.title_label.master
            for zone in window._resize_edges.values():
                left = max(zone.winfo_x(), rail.winfo_x())
                top = max(zone.winfo_y(), rail.winfo_y())
                right = min(zone.winfo_x() + zone.winfo_width(),
                            rail.winfo_x() + rail.winfo_width())
                bottom = min(zone.winfo_y() + zone.winfo_height(),
                             rail.winfo_y() + rail.winfo_height())
                self.assertFalse(right > left and bottom > top,
                                 "缩放热区覆盖标题栏")

    def test_metric_text_fully_fits_at_multiple_font_scales(self):
        original_scale = self.root.tk.call("tk", "scaling")
        try:
            for scale in (1.0, 1.5, 2.0):
                self.root.tk.call("tk", "scaling", scale)
                window = self.make_window()
                window.win.geometry("660x900")
                window.win.update()
                for item in window._metric_widgets.values():
                    previous_bottom = 0
                    for name in ("caption", "value", "sub"):
                        label = item[name]
                        self.assertGreaterEqual(label.winfo_height(), label.winfo_reqheight())
                        self.assertGreaterEqual(label.winfo_y(), previous_bottom)
                        previous_bottom = label.winfo_y() + label.winfo_height()
                    self.assertLessEqual(previous_bottom, item["card"].winfo_height() - 1)
        finally:
            self.root.tk.call("tk", "scaling", original_scale)

    def test_bank_text_is_measured_in_its_actual_bold_font(self):
        from tkinter import font as tkfont
        window = self.make_window(rows=[
            ("中国工商银行股份有限公司超长银行名称", "定期", 99999999,
             3.12, 12, "2027-12-31")])
        for label in window._table.grid_slaves(row=0):
            if not isinstance(label, tk.Label):
                continue
            column = int(label.grid_info()["column"])
            key = window._columns[column][0]
            font = tkfont.Font(font=label.cget("font"))
            self.assertLessEqual(font.measure(label.cget("text")), window._widths[key])

    def test_the_scroll_gutter_matches_the_card_not_the_base_theme(self):
        """独立 Canvas 的轨道与滑块显式跟随 P，浅深主题都不回退到系统色。"""
        for dark in (False, True):
            window = self.make_window(dark=dark)
            scroll = window._scroll
            gutter = scroll.canvas
            self.assertEqual(
                str(gutter.cget("bg")).upper(),
                str(window.P["card"]).upper(),
                f"dark={dark} 的滚动条轨道底色 {gutter.cget('bg')} "
                f"没跟着卡片色 {window.P['card']}")
            # 滑块色同样不该留在基类主题的冷灰上。
            self.assertEqual(
                str(scroll._idle).upper(),
                str(window.P["faint"]).upper(),
                f"dark={dark} 的滑块色还是基类主题的冷灰")

    def test_rail_and_title_share_surface_in_both_themes(self):
        for dark in (False, True):
            window = self.make_window(dark=dark)
            expected = "#111923" if dark else "#F4F6F8"
            for widget in (window.title_label.master, window.title_label,
                           window._theme_button):
                self.assertEqual(widget.cget("bg"), expected)
            self.assertEqual(window.title_label.cget("fg"), window.P["title"])
            self.assertGreaterEqual(_contrast(window.title_label.cget("fg"), expected), 4.5)
            mark = next(child for child in window.title_label.master.winfo_children()
                        if child.winfo_class() == "Canvas" and child is not window._min_btn
                        and child is not window._max_btn and child is not window._close_btn)
            colors = {mark.itemcget(item, "fill") for item in mark.find_all()}
            self.assertEqual(colors, {window.P["muted"], window.P["link"]})

    def test_controls_hover_and_leave_use_local_palette(self):
        """实际触发悬停，验证底色与线条色同时切换并恢复。"""
        for dark in (False, True):
            window = self.make_window(dark=dark)
            for name in ("_min_btn", "_max_btn", "_close_btn"):
                button = getattr(window, name)
                close = name == "_close_btn"
                states = (
                    (None, "surface", "muted"),
                    ("<Enter>", "matured_bg" if close else "head_bg",
                     "matured_fg" if close else "title"),
                    ("<Leave>", "surface", "muted"),
                )
                for event, bg, fg in states:
                    if event:
                        button.event_generate(event, when="now")
                        self.root.update()
                    self.assertEqual(button.cget("bg"), window.P[bg])
                    colors = {button.itemcget(item, "fill" if button.type(item) == "line"
                                             else "outline") for item in button.find_all()}
                    self.assertEqual(colors, {window.P[fg]})
                    # 控制图形非正文；确认的浅色 muted/surface 为 4.39。
                    self.assertGreaterEqual(_contrast(window.P[fg], button.cget("bg")), 3.0)

    def test_theme_button_hover_restores_rail_colours(self):
        for dark in (False, True):
            window = self.make_window(dark=dark)
            button = window._theme_button
            button.event_generate("<Enter>", when="now")
            self.root.update()
            self.assertEqual(button.cget("bg"), window.P["head_bg"])
            self.assertEqual(button.cget("fg"), window.P["title"])
            button.event_generate("<Leave>", when="now")
            self.root.update()
            self.assertEqual(button.cget("bg"), window.P["surface"])
            self.assertEqual(button.cget("fg"), window.P["rail_fg"])

    def test_the_resize_zones_paint_the_same_colour_as_the_shell(self):
        """八向缩放热区的底色必须与外壳底色一致，且悬停后也还原成它。

        热区是 ``place`` 在最外 6px 的**透明 Frame**：它与外壳不同色时，
        用户看到的就是一圈描边内侧的脏边。``_edge_leave`` 是第二个改色点，
        两个都要对。
        """
        for dark in (False, True):
            window = self.make_window(dark=dark)
            surface = window.P["surface"]
            zones = window._resize_edges
            self.assertEqual(len(zones), 8, f"应有八个热区，实际 {list(zones)}")
            for name, zone in zones.items():
                self.assertEqual(zone.cget("bg"), surface)
                if len(name) == 2:
                    self.assertEqual((zone.winfo_width(), zone.winfo_height()), (8, 8))
                for event in ("<Enter>", "<Leave>"):
                    zone.event_generate(event, when="now")
                    self.root.update()
                    self.assertEqual(zone.cget("bg"), surface,
                                     f"{name} 在 {event} 后露出非 surface 色")

    def test_the_shell_and_body_share_the_surface_colour(self):
        """外壳画布、正文容器、窗口本体三者底色必须一致。

        三者分别建在 ``_build_shell`` / ``_build`` 两处，是「整窗换底」时
        最典型的漏改点：漏掉 ``self.win`` 就会在四角露出灰边（窗口本体在
        外壳之外那一圈）。
        """
        window = self.make_window()
        surface = str(window._surface_bg()).upper()
        for label, widget in (("窗口本体", window.win),
                              ("外壳画布", window.shell),
                              ("正文容器", window._body)):
            self.assertEqual(
                str(widget.cget("bg")).upper(), surface,
                f"{label} 的底色与 ``_surface_bg()`` 不一致")
        # 八个缩放热区也压在最外那一圈上，同样要一致。
        for name, zone in window._resize_edges.items():
            self.assertEqual(
                str(zone.cget("bg")).upper(), surface,
                f"{name} 热区压在外壳最外圈上，底色必须与外壳一致")

    def test_primary_button_press_and_release_use_confirmed_blue(self):
        for dark in (False, True):
            window = self.make_window(dark=dark)
            calls = []
            box = window._primary_button(window._hint.master, "测试", lambda: calls.append(1))
            self.root.update()
            blue = "#75B5E8" if dark else "#244A68"
            self.assertEqual(box.cget("bg"), blue)
            self.assertEqual(box.label_widget.cget("fg"), window.P["button_fg"])
            box.label_widget.event_generate("<ButtonPress-1>", when="now")
            self.root.update()
            self.assertEqual(calls, [1])
            for widget in (box, box.label_widget):
                self.assertEqual(widget.cget("bg"), window.P["button_hot"])
            box.label_widget.event_generate("<ButtonRelease-1>", when="now")
            self.root.update()
            for widget in (box, box.label_widget):
                self.assertEqual(widget.cget("bg"), blue)

    def test_secondary_button_hover_uses_head_not_matured_background(self):
        for dark in (False, True):
            window = self.make_window(dark=dark)
            calls = []
            box = window._text_button(window._hint.master, "测试", lambda: calls.append(1))
            self.root.update()
            inner = box.winfo_children()[0]
            self.assertEqual(box.cget("bg"), window.P["link"])
            self.assertEqual(inner.cget("fg"), window.P["link"])
            self.assertEqual(inner.cget("activebackground"), window.P["head_bg"])
            inner.event_generate("<Enter>", when="now")
            self.root.update()
            self.assertEqual(inner.cget("bg"), window.P["head_bg"])
            self.assertNotEqual(inner.cget("bg"), window.P["matured_bg"])
            inner.invoke()
            self.assertEqual(calls, [1])
            inner.event_generate("<Leave>", when="now")
            self.root.update()
            self.assertEqual(inner.cget("bg"), window.P["surface"])

    def test_table_links_stay_blue_even_on_matured_rows(self):
        for dark in (False, True):
            window = self.make_window(dark=dark)
            rows = VaultWindowTests.grid_rows(window)
            self.assertEqual({cells[5].cget("text") for cells in rows.values()},
                             {"删除", "删银行"})
            for cells in rows.values():
                self.assertEqual(cells[5].cget("fg"), window.P["link"])
                self.assertGreaterEqual(_contrast(cells[5].cget("fg"), cells[5].cget("bg")), 4.5)
                if "已到期" in cells[3].cget("text"):
                    self.assertEqual(cells[3].cget("fg"), window.P["matured_fg"])
                    self.assertNotEqual(cells[5].cget("fg"), cells[3].cget("fg"))

    def test_metric_and_table_borders_match_shell_border(self):
        for dark in (False, True):
            window = self.make_window(dark=dark)
            border = "#334155" if dark else "#DCE2E8"
            self.assertEqual(window._edge(), border)
            self.assertEqual(window.P["metric_border"], border)
            self.assertEqual(window._table_head.master.cget("highlightbackground"), border)
            for key, item in window._metric_widgets.items():
                self.assertEqual(item["card"].cget("highlightbackground"), border)
                self.assertEqual(item["strip"].cget("bg"), window.P[f"strip_{key}"])
                bg = window.P["matured_bg" if key == "matured" else "card"]
                self.assertEqual(item["card"].cget("bg"), bg)
                if key in ("principal", "interest"):
                    self.assertEqual(item["value"].cget("fg"), window.P["title"])
            self.assertEqual(window._table_head.cget("bg"), window.P["head_bg"])
            rows = VaultWindowTests.grid_rows(window)
            self.assertEqual(rows[0][0].cget("bg"), window.P["row_bank_bg"])
            self.assertNotEqual(rows[0][0].cget("bg"), window.P["head_bg"])
            self.assertEqual(rows[1][0].cget("bg"), window.P["card"])
            # 读自绘图片的数据，不截图；取无顶边强调线干扰的右侧描边。
            window._paint_shell(window.win.winfo_width(), window.win.winfo_height())
            rgb = window.win.tk.call(str(window._shell_image), "get",
                                     window.win.winfo_width() - 1,
                                     window.win.winfo_height() // 2)
            # 基类使用 2 倍超采样，外缘会与 surface 混合少量像素。
            self.assertLessEqual(max(abs(actual - expected)
                                     for actual, expected in zip(map(int, rgb), _rgb(border))), 3)


def _history_theme():
    from lumina.history_panel import HistoryPanel
    return HistoryPanel.THEME


@unittest.skipUnless(TK_OK, SKIP_REASON)
class VaultEntryPointTests(unittest.TestCase):
    """「密匣」菜单项的接线。

    这条测试**刻意不建真实的 ``HistoryPanel``**（它的构建成本很高），而是
    断言源码层面的接线：菜单项的命令指向 ``_open_vault_box``，而那个方法
    真的会去建 ``VaultWindow``。之前它只设了一句状态栏提示，所以这里同时
    钉住「提示文案已经不在了」。
    """

    def _source(self):
        return (ROOT / "lumina" / "history_panel.py").read_text(
            encoding="utf-8")

    def test_vault_menu_item_is_wired(self):
        source = self._source()
        self.assertIn('command=lambda: self._popup_action(self._open_vault_box)',
                      source)

    def test_open_vault_box_instantiates_the_window(self):
        """光有 ``VaultWindow`` 这个名字不算数——只查名字的话，**只留一句
        import 而不真正构造**也能过（这正是本轮变异验证里逃掉的那一项）。
        所以这里钉的是「真的调了构造函数」和「真的登记进了实例表」，
        两件事缺一，菜单点下去都不会有窗口。"""
        source = self._source()
        method = source.split("def _open_vault_box")[1].split("\n    def ")[0]
        self.assertIn("from .vault_window import VaultWindow", method)
        self.assertIn("VaultWindow(self)", method)
        self.assertIn('registry["vault"] = window', method)

    def test_placeholder_message_is_gone(self):
        """「密匣功能开发中」这句占位提示必须已经删掉——留着它意味着
        菜单项还会走那条什么都不做的路径。"""
        self.assertNotIn("密匣功能开发中", self._source())

    def test_open_vault_box_does_not_assume_registry_exists(self):
        """实例表是**懒创建**的（``open_tool`` 里用 ``getattr`` 兜底），
        所以 ``_open_vault_box`` 不能直接取 ``self._tool_windows``——
        密匣常常是这一天里第一个被打开的窗口，那时属性还不存在。"""
        source = self._source()
        method = source.split("def _open_vault_box")[1].split("\n    def ")[0]
        self.assertIn('getattr(self, "_tool_windows", None)', method)
        self.assertNotIn("registry = self._tool_windows\n", method)

    def test_vault_is_not_in_the_tools_registry(self):
        """密匣**不该**出现在 ``TOOLS`` 里：那个注册表喂的是顶栏「工具」
        下拉，密匣的入口在「隐域」下面。混进去会让工具菜单多出一项。"""
        from lumina.tool_window import TOOLS
        keys = [key for key, _label, _cls in TOOLS]
        self.assertNotIn("vault", keys)
        labels = [label for _key, label, _cls in TOOLS]
        self.assertNotIn("密匣", labels)


@unittest.skipUnless(TK_OK, SKIP_REASON)
class DepositDialogTests(unittest.TestCase):
    """新增存单对话框的校验路径。

    对话框走 ``vault_tools.build_deposit``，所以这里测的是「界面上填错 →
    对话框里的那句话对不对」以及「校验失败时不关窗」。
    """

    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.db = Database(":memory:")
        self.db.init_schema()
        self.panel = FakePanel(self.root, tk, self.db)
        self.windows = []

    def tearDown(self):
        for window in self.windows:
            window.close()
        self.db.close()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _dialog(self):
        from lumina.vault_window import _DepositDialog
        window = VaultWindow(self.panel)
        self.windows.append(window)
        window._today = TODAY
        self.db.add_vault_bank("招商银行")
        window._reload()
        self.root.update()
        return window, _DepositDialog(window)

    def test_dialog_colours_and_tooltip_follow_both_themes(self):
        """实际构造对话框与提示气泡，检查普通交互不借用状态底色。"""
        from lumina.vault_window import _DepositDialog
        for dark in (False, True):
            window = VaultWindow(self.panel)
            self.windows.append(window)
            self.db.add_vault_bank("测试银行")
            window._reload()
            if dark:
                window._toggle_theme()
            dialog = _DepositDialog(window)
            self.root.update()
            P = window.P
            self.assertEqual(dialog.win.cget("bg"), P["surface"])
            self.assertEqual(dialog.error.cget("fg"), P["matured_fg"])
            option = next(child for child in dialog.win.winfo_children()
                          if isinstance(child, tk.Menubutton))
            self.assertEqual(option.cget("activebackground"), P["head_bg"])
            buttons = [button for child in dialog.win.winfo_children()
                       if isinstance(child, tk.Frame)
                       for button in child.winfo_children() if isinstance(button, tk.Button)]
            add = next(button for button in buttons if button.cget("text") == "添加")
            cancel = next(button for button in buttons if button.cget("text") == "取消")
            self.assertEqual(add.cget("bg"), P["button"])
            self.assertEqual(add.cget("fg"), P["button_fg"])
            self.assertEqual(add.cget("activeforeground"), P["button_fg"])
            self.assertEqual(cancel.cget("activebackground"), P["head_bg"])
            entry = dialog.entries["amount"]
            self.assertEqual(entry.cget("insertbackground"), P["link"])
            entry.event_generate("<Enter>", when="now")
            self.root.update()
            tips = [child for child in entry.winfo_children() if isinstance(child, tk.Toplevel)]
            self.assertEqual(len(tips), 1)
            label = tips[0].winfo_children()[0]
            self.assertEqual(label.cget("fg"), P["muted"])
            self.assertEqual(label.cget("highlightbackground"), P["border"])
            self.assertGreaterEqual(_contrast(label.cget("fg"), label.cget("bg")), 4.5)
            entry.event_generate("<Leave>", when="now")
            self.root.update()
            self.assertFalse(entry.winfo_children())
            cancel.invoke()
            self.assertFalse(dialog.win.winfo_exists())
            self.assertIsNone(dialog.result)

    def test_valid_input_produces_a_record(self):
        _window, dialog = self._dialog()
        dialog.entries["name"].delete(0, "end")
        dialog.entries["name"].insert(0, "大额存单 3 年")
        dialog.entries["amount"].insert(0, "40万")
        dialog.entries["rate"].insert(0, "3.10")
        dialog.entries["term"].insert(0, "3年")
        dialog.entries["date"].insert(0, "2027-03-15")
        dialog._submit()
        self.assertIsNotNone(dialog.result)
        self.assertEqual(dialog.result["principal"], 400000.0)
        self.assertEqual(dialog.result["months"], 36)
        self.assertEqual(dialog.result["end_date"], "2027-03-15")

    def test_invalid_input_keeps_the_dialog_open(self):
        """**校验失败不能关对话框**。用户填了六个字段，关掉就得全部重填，
        这是最容易被忽略又最招人烦的那种体验缺陷。"""
        _window, dialog = self._dialog()
        dialog.entries["name"].insert(0, "存单")
        dialog.entries["amount"].insert(0, "四万")     # 故意写错
        dialog.entries["rate"].insert(0, "3.10")
        dialog.entries["term"].insert(0, "3年")
        dialog.entries["date"].insert(0, "2027-03-15")
        dialog._submit()
        self.assertIsNone(dialog.result)
        self.assertTrue(dialog.win.winfo_exists(), "对话框被关掉了")
        self.assertTrue(dialog.error.cget("text"), "没有给出错误提示")

    def test_error_message_points_at_the_bad_field(self):
        """错误提示要指到具体哪个字段。只说「输入无效」等于让用户在六个
        字段之间反复试。"""
        _window, dialog = self._dialog()
        dialog.entries["name"].insert(0, "存单")
        dialog.entries["amount"].insert(0, "1000")
        dialog.entries["rate"].insert(0, "3.10")
        dialog.entries["term"].insert(0, "3年")
        dialog.entries["date"].insert(0, "2027-13-45")   # 月份非法
        dialog._submit()
        message = dialog.error.cget("text")
        # 断言的是**字段名**「到期日」而不是泛泛的「日期」：前者能直接指出
        # 该改哪一格，后者还要用户在六个字段里找哪个跟日期有关。
        self.assertIn("到期日", message, message)

    def _set(self, dialog, **values):
        for key, value in values.items():
            dialog.variables[key].set(value)
        self.root.update()

    def test_comfortable_geometry_and_permanent_examples(self):
        window, dialog = self._dialog()
        self.root.update()
        self.assertEqual(dialog.win.winfo_width(), 500)
        self.assertEqual(dialog.entries["name"].get(), "定期存款")
        self.assertEqual(dialog.entries["start"].get(), TODAY.isoformat())
        self.assertEqual(dialog.win.focus_get(), dialog.entries["amount"])
        for key, entry in dialog.entries.items():
            self.assertGreaterEqual(entry.winfo_height(), 30)
            self.assertLessEqual(entry.winfo_rootx() + entry.winfo_width(),
                                 dialog.win.winfo_rootx() + 500 - 20)
            helper = dialog.field_hints[key]
            self.assertTrue(helper.cget("text"))
            self.assertGreater(helper.winfo_height(), 1)
            self.assertEqual(entry.cget("bg"), window.P["card"])
            self.assertGreaterEqual(_contrast(helper.cget("fg"), helper.cget("bg")), 4.0)
        self._set(dialog, amount="40万")
        self.assertIn("400,000.00", dialog.field_hints["amount"].cget("text"))
        self.assertEqual(dialog.entries["amount"].get(), "40万")

    def test_shortcuts_real_clicks_keep_custom_term_and_manual_date(self):
        _window, dialog = self._dialog()
        self._set(dialog, date="2030-07-15")
        for months, button in dialog.term_buttons.items():
            button.event_generate("<Button-1>", when="now")
            self.root.update()
            self.assertEqual(dialog.entries["term"].get(), str(months))
            self.assertEqual(dialog.entries["date"].get(), "2030-07-15")
        self._set(dialog, term="18个月", start="2024-01-31")
        self.assertEqual(dialog.entries["date"].get(), "2030-07-15")
        self.assertEqual(dialog.entries["term"].get(), "18个月")

    def test_explicit_calculate_real_event_month_end_and_leap_year(self):
        _window, dialog = self._dialog()
        for start, term, expected in (("2024-01-31", "1", "2024-02-29"),
                                      ("2023-01-31", "1", "2023-02-28"),
                                      ("2024-02-29", "12", "2025-02-28"),
                                      ("2026-12-31", "6", "2027-06-30")):
            self._set(dialog, start=start, term=term, date="2030-01-01")
            dialog.calculate_button.event_generate("<Button-1>", when="now")
            self.root.update()
            self.assertEqual(dialog.entries["date"].get(), expected)
        self._set(dialog, start="2024-02-30")
        before = dialog.entries["date"].get()
        dialog.calculate_button.event_generate("<Button-1>", when="now")
        self.root.update()
        self.assertEqual(dialog.entries["date"].get(), before)
        self.assertEqual(dialog._error_field, "start")

    def test_preview_updates_without_errors_or_database_writes(self):
        _window, dialog = self._dialog()
        self._set(dialog, amount="40万", rate="3.10", term="36")
        self.assertEqual(dialog.preview.cget("text"),
                         "预计利息 ¥37,200.00   本息 ¥437,200.00")
        for values in ({"rate": "nan"}, {"amount": "四万"},
                       {"rate": "", "term": ""}):
            self._set(dialog, **values)
            self.assertNotIn("37,200", dialog.preview.cget("text"))
            self.assertEqual(dialog.error.cget("text"), "")
        self.assertEqual(self.db.list_vault_deposits(), [])

    def test_invalid_submission_marks_and_focuses_each_field_keeps_values(self):
        window, dialog = self._dialog()
        valid = {"name": "存单", "amount": "40万", "rate": "3.10",
                 "term": "36", "date": "2027-01-01"}
        for key in valid:
            self._set(dialog, **valid)
            self._set(dialog, **{key: ""})
            before = dialog._values()
            dialog._submit()
            self.root.update()
            self.assertEqual(dialog._values(), before)
            self.assertTrue(dialog.win.winfo_exists())
            self.assertEqual(dialog._error_field, key)
            self.assertEqual(dialog.win.focus_get(), dialog.entries[key])
            self.assertEqual(dialog.entries[key].cget("highlightcolor"), window.P["matured_fg"])
        self._set(dialog, **valid)
        dialog.bank_var.set("不存在")
        dialog._submit()
        self.root.update()
        self.assertEqual(dialog._error_field, "bank")
        self.assertEqual(dialog.win.focus_get(), dialog.bank_option)

    def test_keyboard_tab_enter_and_escape(self):
        _window, dialog = self._dialog()
        for index, widget in enumerate(dialog._tab_order):
            widget.focus_force()
            self.root.update()
            widget.event_generate("<Tab>", when="now")
            self.root.update()
            self.assertEqual(dialog.win.focus_get(),
                             dialog._tab_order[(index + 1) % len(dialog._tab_order)])
        self._set(dialog, amount="40万", rate="3.10", term="36", date="2027-01-01")
        dialog.entries["amount"].focus_force()
        dialog.entries["amount"].event_generate("<Return>", when="now")
        self.root.update()
        self.assertIsNotNone(dialog.result)
        from lumina.vault_window import _DepositDialog
        second = _DepositDialog(_window)
        second.win.update()
        second.entries["amount"].event_generate("<Escape>", when="now")
        self.root.update()
        self.assertIsNone(second.result)
        self.assertFalse(second.win.winfo_exists())

    def test_bank_dropdown_lists_banks(self):
        _window, dialog = self._dialog()
        self.assertEqual(dialog.bank_names, ["招商银行"])
        self.assertEqual(dialog.bank_var.get(), "招商银行")


@unittest.skipUnless(TK_OK, SKIP_REASON)
class DepositWritePathTests(unittest.TestCase):
    """「新增存单」这条路必须**真的把数据写进数据库**。

    这组测试守的是一个曾经真实存在的缺陷：``_add_deposit`` 建完
    ``_DepositDialog`` 就直接读 ``dialog.result``，而对话框的构造函数只把
    界面搭好就返回——用户还没来得及输入任何东西，``result`` 必然是 ``None``。
    于是 ``add_vault_deposit`` 从来没被调用过，**存单一条都写不进库**，
    而界面上不报任何错（用户以为存了，关掉窗口再打开就没了）。

    真实库上留下的痕迹很典型：``vault_bank`` 有行（银行走
    ``simpledialog.askstring``，它自带模态循环所以能存）、``vault_deposit``
    恒为 0。

    **判据必须落在「库里真的有这条记录」上**，不能只断言「调了
    ``add_vault_deposit``」——那样的桩件测试在缺陷版本上也是绿的。
    """

    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.db = Database(":memory:")
        self.db.init_schema()
        self.panel = FakePanel(self.root, tk, self.db)
        self.windows = []
        self.db.add_vault_bank("招商银行")

    def tearDown(self):
        for window in self.windows:
            window.close()
        self.db.close()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _window(self):
        window = VaultWindow(self.panel)
        self.windows.append(window)
        window._today = TODAY
        window._reload()
        self.root.update()
        return window

    def _patch(self, cls):
        """把窗口模块里的 ``_DepositDialog`` 换成 ``cls``，返回还原函数。"""
        import lumina.vault_window as module
        original = module._DepositDialog
        module._DepositDialog = cls
        return lambda: setattr(module, "_DepositDialog", original)

    def test_auto_submitted_dialog_writes_a_row(self):
        """**端到端**：真实对话框 → 填六个字段 → 点「添加」→ 库里多一行。

        用的是真的 ``_DepositDialog`` 子类，只把「用户手工输入」这一步用
        ``after`` 排进事件循环——**这正是模态等待要处理的那个场景**：
        ``_add_deposit`` 进 ``wait_window`` 后事件循环转动，对话框填完提交并
        销毁，``wait_window`` 返回，然后才读 ``result``。

        判据落在**库里真的有一行**上。只断言「调了 ``add_vault_deposit``」
        是没用的：缺陷版本里那句调用根本不存在，桩件照样能过。
        """
        import lumina.vault_window as module
        window = self._window()

        class AutoSubmit(module._DepositDialog):
            FILL = {"name": "大额存单 3 年", "amount": "40万",
                    "rate": "3.10", "term": "3年", "date": "2027-03-15"}

            def __init__(self, owner):
                super().__init__(owner)
                self.win.after(10, self._fill_and_submit)

            def _fill_and_submit(self):
                for key, value in self.FILL.items():
                    self.entries[key].delete(0, "end")
                    self.entries[key].insert(0, value)
                self._submit()

        restore = self._patch(AutoSubmit)
        try:
            window._add_deposit()
        finally:
            restore()
        self.root.update()

        rows = [dict(r) for r in self.db.list_vault_deposits()]
        self.assertEqual(len(rows), 1, "存单没有被写进数据库")
        self.assertEqual(rows[0]["name"], "大额存单 3 年")
        self.assertEqual(rows[0]["principal"], 400000.0)
        self.assertEqual(rows[0]["months"], 36)
        self.assertEqual(rows[0]["end_date"], "2027-03-15")
        self.assertEqual(window._last_deposit_bank_id, rows[0]["bank_id"])
        second_id = self.db.add_vault_bank("测试银行乙")
        window._last_deposit_bank_id = second_id
        window._reload()
        dialog = module._DepositDialog(window)
        self.assertEqual(dialog.bank_var.get(), "测试银行乙")
        dialog._cancel()
        self.assertEqual(window._last_deposit_bank_id, second_id)
        self.db.delete_vault_bank(second_id)
        window._reload()
        dialog = module._DepositDialog(window)
        self.assertEqual(dialog.bank_var.get(), "招商银行")
        dialog._cancel()
        self.assertEqual(self.panel.ui.config, {})

    def test_written_deposit_appears_in_the_list(self):
        """写进库之后界面要看得见它。

        「数据进库但界面不显示」也是「没写进去」的一种表现，所以这条
        跟着上一条走完同一条路径、再查渲染结果。
        """
        import lumina.vault_window as module
        window = self._window()

        class AutoSubmit(module._DepositDialog):
            def __init__(self, owner):
                super().__init__(owner)
                self.win.after(10, self._go)

            def _go(self):
                for key, value in {"name": "整存整取 2 年", "amount": "30万",
                                   "rate": "2.75", "term": "2年",
                                   "date": "2027-01-10"}.items():
                    self.entries[key].delete(0, "end")
                    self.entries[key].insert(0, value)
                self._submit()

        restore = self._patch(AutoSubmit)
        try:
            window._add_deposit()
        finally:
            restore()
        self.root.update()

        texts = [c.cget("text") for row in self.grid_rows(window).values()
                 for c in row.values()]
        self.assertTrue(any("整存整取 2 年" in t for t in texts), texts)
        # 指标卡也要跟着动（本金从 0 变成 30 万）。
        self.assertEqual(
            window._metric_widgets["principal"]["value"].cget("text"),
            "¥300,000")

    def test_cancelled_dialog_writes_nothing(self):
        """用户点「取消」或直接关窗，不该写库。

        ``result`` 是 ``None``，那条早退分支必须留——少了它会把 ``None``
        拆包成关键字参数、抛 ``TypeError``。
        """
        import lumina.vault_window as module
        window = self._window()

        class Cancel(module._DepositDialog):
            def __init__(self, owner):
                super().__init__(owner)
                self.win.after(10, self._cancel)

        restore = self._patch(Cancel)
        try:
            window._add_deposit()
        finally:
            restore()
        self.root.update()
        self.assertEqual(self.db.list_vault_deposits(), [])

    def test_a_broken_write_is_reported_not_swallowed(self):
        """写库失败要在界面上说清，不能静默吞掉。

        静默失败与「没写进去」对用户是同一件事，但排查难度差很远：
        前者连个错误都没有。
        """
        import lumina.vault_window as module
        window = self._window()

        class AutoSubmit(module._DepositDialog):
            def __init__(self, owner):
                super().__init__(owner)
                self.win.after(10, self._go)

            def _go(self):
                self.entries["name"].insert(0, "存单")
                self.entries["amount"].insert(0, "1000")
                self.entries["rate"].insert(0, "2.0")
                self.entries["term"].insert(0, "1年")
                self.entries["date"].insert(0, "2027-01-01")
                self._submit()

        def boom(**_kwargs):
            raise RuntimeError("模拟写库失败")

        original_add = self.db.add_vault_deposit
        self.db.add_vault_deposit = boom
        restore = self._patch(AutoSubmit)
        try:
            window._add_deposit()
        finally:
            restore()
            self.db.add_vault_deposit = original_add
        self.root.update()
        self.assertIn("新增存单失败", window._hint.cget("text"))
        self.assertIsNone(window._last_deposit_bank_id)

    def test_waits_for_the_dialog_before_reading_its_result(self):
        """``wait_window`` 等的必须是**这个**对话框的窗口，且在读 ``result`` 之前。

        等 ``self.win``（主窗口）会立刻返回——主窗口一直活着——读到的
        仍然是构造期的 ``None``，而代码里确实有 ``wait_window`` 这一句，
        光看源码很难发现等错了对象。

        **断言前先把 docstring 切掉**：函数说明里也会出现这两个词，
        不切的话 ``index()`` 找到的是注释里的位置，这条就变成在检查
        注释的书写顺序了。
        """
        import ast
        source = (ROOT / "lumina" / "vault_window.py").read_text(
            encoding="utf-8")
        tree = ast.parse(source)
        func = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.FunctionDef)
                    and node.name == "_add_deposit")
        # 用**行号切片**取出函数体，再按行丢掉 docstring。
        # 不用 ``ast.get_source_segment``：它要求节点带位置信息，
        # 而把 docstring 摘掉后重建的节点没有 lineno，会返回空串
        # ——断言就会变成「在空字符串里找关键词」，恒失败。
        lines = source.splitlines()[func.body[0].lineno - 1:func.end_lineno]
        if lines and lines[0].lstrip().startswith(('"""', "'''")):
            quote = lines[0].lstrip()[:3]
            if lines[0].lstrip().count(quote) >= 2:
                lines = lines[1:]
            else:
                end = next(i for i, line in enumerate(lines[1:], start=1)
                           if quote in line)
                lines = lines[end + 1:]
        code = "\n".join(lines)

        self.assertIn("self.win.wait_window(dialog.win)", code,
                      "必须等对话框自己的窗口关闭，而不是其他窗口")
        self.assertLess(code.index("wait_window"), code.index("dialog.result"),
                        "先读了 result 才等待，等于问一个还没输入的人要结果")

    @staticmethod
    def grid_rows(window):
        rows = {}
        for child in window._table.winfo_children():
            if child.winfo_manager() != "grid" or not isinstance(child, tk.Label):
                continue
            info = child.grid_info()
            rows.setdefault(info["row"], {})[info["column"]] = child
        return rows


if __name__ == "__main__":
    unittest.main()
