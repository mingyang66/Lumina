"""顶栏「工具」下拉里的独立小工具窗口：JSON 参数 / JWT 解析。

两个工具共用同一套无边框外壳（左侧输入、右侧结果、实时解析、一键复制），
只替换「如何解析」。新增工具只需实现一个 ``_analyze``。

设计取舍：
- **不画系统标题栏，但顶部要显示工具名**。标题以次级文字色、小一号字号写在
  左上角信号条之后，并配一道发丝线隔开——它承担「我在哪个工具里」的识别，
  但绝不与输入内容抢视线。**仍不放「输入」「结果」这类分区小标题**，那两块
  靠左侧色条（输入=强调蓝、结果=青）与可编辑/只读状态区分。
- **实时解析**（输入变化即刷新，带防抖），不设「执行」按钮——这类小工具的
  价值就在即时反馈，多按一次按钮纯属摩擦。
- **只读展示**，不写剪贴板历史、不改数据库。产物永远由用户主动点「复制」
  才落到剪贴板，避免把调试中间态污染进历史列表。
- 输入框与结果框都用等宽字体：JSON/JWT 是结构化文本，等宽才能对齐看。
- **失败不弹模态框**：解析错误直接显示在状态栏，用户改一个字符就能重试。

外壳实现要点（Windows）：
- ``overrideredirect(True)`` 去掉原生标题栏；窗口只剩一条 34px 的操作条，
  左端是纯几何「信号条」标识 + 拖动区，右端是绘制出来的关闭按钮。
- 圆角与描边用 PIL 画在 Canvas 上（2 倍超采样再缩放，边缘才不糊），四角
  裁剪交给 ``SetWindowRgn``——比透明色键稳：窗口缩放时不会漏出桌面底色。
- 拖动自己算（``_drag_press`` / ``_drag_move``），**绝不用 Tk 原生
  ``menu.post`` 或系统菜单**：Windows 下会进模态跟踪循环把 UI 线程卡死。
"""

import ctypes
import os
from tkinter import TclError

from . import jwt_tools
from .json_tools import transform_json

# 等宽字体：Windows 上 Consolas 覆盖 ASCII 与数字对齐；中文回退由 Tk 处理。
MONO_FAMILY = "Consolas"

# 输入变化到重新解析的等待时间。paste 大段 JSON 时逐字符触发会明显卡顿。
DEBOUNCE_MS = 180

# ---------- 外壳尺寸 ----------
SHELL_RADIUS = 12          # 外圆角半径（与 SetWindowRgn 共用）
SHELL_BORDER = 1           # 描边宽度
CONTENT_INSET = 8          # 内容与外壳边缘的间距，让描边与圆角露出来
RAIL_HEIGHT = 30           # 顶部操作条：只承载拖动与关闭
FOOT_HEIGHT = 34           # 底部状态 + 操作按钮
GAP = 10                   # 左右两栏之间的缝隙
MIN_W, MIN_H = 660, 440    # 允许缩到的最小尺寸

# 结果里的段落标记。用等宽方块而不是「— xxx —」，更接近终端观感。
SECTION_MARK = "▍"

# ---------- Win32 窗口扩展样式 ----------
# 无边框（overrideredirect）会让 Tk 给顶层窗口打上 WS_EX_TOOLWINDOW，
# 该标志的语义就是「不进任务栏、不进 Alt+Tab」。要让它像普通窗口一样
# 出现在任务栏，必须换成 WS_EX_APPWINDOW。
GWL_EXSTYLE = -20
GWL_STYLE = -16
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_SYSMENU = 0x00080000
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020


class _MiniScroll:
    """极细自绘滚动条。

    Tk 原生 ``Scrollbar`` 由系统主题绘制，``bg`` / ``troughcolor`` 在 Windows
    下**完全不生效**——深色窗口里会突兀地出现一条白槽（实测像素确认为
    ``#F0F0F0``）。这里用 Canvas 自绘一根圆头细条，只保留轨道色与滑块色，
    并保持 ``set``/``command`` 协议，可直接接到 Text 的 ``yscrollcommand``。
    """

    THICKNESS = 3          # 滑块粗细
    LANE = 10              # 轨道占位宽度（含留白）

    def __init__(self, tk, parent, orient, command, theme, accent):
        self.orient = orient
        self.command = command
        self._first = 0.0
        self._last = 1.0
        self._drag_from = None
        size = (self.LANE, 1) if orient == "vertical" else (1, self.LANE)
        self.canvas = tk.Canvas(
            parent, width=size[0], height=size[1], bg=theme["card_bg"],
            highlightthickness=0, bd=0, cursor="hand2")
        self._idle = theme["label3"]
        self._hot = accent
        self.canvas.bind("<Configure>", lambda _e: self._redraw())
        self.canvas.bind("<Enter>", lambda _e: self._redraw(hot=True))
        self.canvas.bind("<Leave>", lambda _e: self._redraw())
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._motion)
        self.canvas.bind("<ButtonRelease-1>", lambda _e: setattr(
            self, "_drag_from", None))

    # ---------- 协议 ----------
    def set(self, first, last):
        """供 ``Text(yscrollcommand=...)`` 调用。"""
        self._first, self._last = float(first), float(last)
        self._redraw()

    # ---------- 绘制 ----------
    def _length(self):
        return (self.canvas.winfo_height() if self.orient == "vertical"
                else self.canvas.winfo_width())

    def _thumb_span(self):
        total = max(1, self._length())
        start = int(self._first * total)
        end = int(self._last * total)
        if end - start < 18:            # 太短就抓不住，给个最小可点长度
            end = min(total, start + 18)
        return start, end

    def _redraw(self, hot=False):
        self.canvas.delete("thumb")
        if self._first <= 0.0 and self._last >= 1.0:
            return                      # 全部可见时不留滑块，界面更干净
        total = max(1, self._length())
        color = self._hot if hot else self._idle
        half = self.THICKNESS // 2
        lo, hi = half + 1, total - half - 1
        start, end = (max(lo, min(hi, v)) for v in self._thumb_span())
        if end <= start:
            return
        if self.orient == "vertical":
            coords = (self.LANE // 2, start, self.LANE // 2, end)
        else:
            coords = (start, self.LANE // 2, end, self.LANE // 2)
        self.canvas.create_line(
            *coords, fill=color, width=self.THICKNESS, capstyle="round",
            tags="thumb")

    # ---------- 拖动 ----------
    def _press(self, event):
        pos = event.y if self.orient == "vertical" else event.x
        start, end = self._thumb_span()
        if not start <= pos <= end:
            # 点轨道空白：把该位置滚到视口中央，再接着拖
            fraction = pos / max(1, self._length())
            self.command("moveto", max(0.0, fraction - (self._last - self._first) / 2))
            start, end = self._thumb_span()
        self._drag_from = (pos, start, end)

    def _motion(self, event):
        if not self._drag_from:
            return
        origin, start, _end = self._drag_from
        pos = event.y if self.orient == "vertical" else event.x
        # 把滑块位移换算成整体可视区位移，再交给 Text 的 moveto。
        span = max(1, self._last - self._first)
        fraction = (start + (pos - origin)) / max(1, self._length())
        self.command("moveto", max(0.0, min(1.0 - span, fraction)))


class _ToolWindow:
    """工具窗口骨架。子类只需实现 ``title``/``placeholder``/``_analyze``。"""

    # 工具名。同时作为**窗口标题**（任务栏 / Alt+Tab 显示的名字）与
    # **顶栏左上角的可见标题**。
    title = "工具"
    # 输入为空时的提示，替代原来写在标题栏下方的那行说明文字。
    placeholder = ""
    width = 920
    height = 640

    def __init__(self, panel):
        self.panel = panel
        self.ui = panel.ui
        self.tk = panel.tk
        self._closed = False
        self._after_id = None
        self._shell_job = None
        self._shell_image = None
        self._drag_off = None
        self._resize_from = None
        self._maxed = False
        self._normal_geom = None
        self._min_geom = None
        self._chrome_id = None
        # 为真表示「窗口正处于最小化，无边框还没装回去」，由 <Map> 负责收尾
        self._pending_chrome = False
        theme = panel._theme()
        self.T, self.dark = theme if isinstance(theme, tuple) else (theme, False)
        self.win = self.tk.Toplevel(panel.win)
        self.win.title(self.title)      # 仅任务栏标识用；无边框窗口不显示它
        self._build_shell()
        self._center()
        self._build()
        # 无边框窗口收不到 WM_DELETE_WINDOW 的标题栏按钮，Esc 是唯一的键盘出口。
        self.win.bind("<Escape>", lambda _e: self.close())
        # 首次刷新也记进 _after_id：否则窗口在 30ms 内就被关掉时，这个定时器
        # 不在 close() 的取消范围内，Tk 会在销毁后报 invalid command name。
        self._after_id = self.win.after(30, self._refresh)
        self.win.focus_force()
        self.input.focus_set()

    # ---------- 外壳 ----------
    def _build_shell(self):
        tk = self.tk
        self.win.overrideredirect(True)
        self.win.configure(bg=self.T["window_bg"])
        self._follow_topmost()
        # 首次构造时窗口还没映射，Tk 会在映射那一刻才应用 overrideredirect 的
        # 扩展样式并把 WS_EX_TOOLWINDOW 打上，所以构建期设置会被覆盖（实测：
        # 这里改完读回来仍是 0x80）。必须等 <Map> 之后再改一次。
        self.win.bind("<Map>", self._on_map_claim_taskbar, add="+")
        self._claim_taskbar()
        self.shell = tk.Canvas(self.win, bg=self.T["window_bg"],
                               highlightthickness=0, bd=0)
        self.shell.pack(fill="both", expand=True)
        self._paint_shell(self.width, self.height)
        self.shell.bind("<Configure>", self._on_shell_configure)
        self._build_rail()

    def _on_map_claim_taskbar(self, event):
        """窗口真正显示后重新声明任务栏归属。

        ``<Map>`` 里 Tk 已经把 overrideredirect 对应的扩展样式应用完了，
        此时改才不会被覆盖。

        **这里同时负责「最小化唤回」的收尾**：最小化期间窗口必须保持受管
        状态（见 ``_minimize``），用户点任务栏唤回才会重新触发 ``<Map>``，
        此刻才把无边框装回去。判断依据是 ``wm state``——只有 normal 才恢复，
        避免在 iconic 阶段又把窗口变成悬浮碎片。
        """
        if event.widget is not self.win or self._closed:
            return
        if self._pending_chrome:
            self._restore_chrome()
        else:
            self._claim_taskbar()

    def _claim_taskbar(self):
        """让无边框窗口出现在任务栏与 Alt+Tab 里。

        **根因**：Tk 在 ``overrideredirect(True)`` 时会给顶层窗口加上
        ``WS_EX_TOOLWINDOW``（0x80）——实测普通 Toplevel 是 ``0x100``，无边框
        变成 ``0x80 | WS_POPUP``。带这个扩展样式的窗口按设计就不进任务栏、
        不进 Alt+Tab，于是**最小化之后用户没有任何入口把它找回来**。

        解法：给顶层 HWND 加上 ``WS_EX_APPWINDOW``（0x40000）并去掉
        ``WS_EX_TOOLWINDOW``。注意 ``winfo_id()`` 返回的是内层 ``TkChild``，
        真正决定任务栏归属的是取不到父窗口的那层 ``TkTopLevel``，必须先向上
        找。改完还要 ``SetWindowPos`` 带 ``SWP_FRAMECHANGED`` 才会让任务栏
        立刻刷新，否则要等窗口下次显隐才生效。
        """
        self._taskbar_hwnd = None
        if os.name != "nt":
            return
        hwnd = self._top_level_hwnd()
        if not hwnd:
            return
        try:
            ex_style = self._read_exstyle(hwnd)
            wanted = (ex_style | WS_EX_APPWINDOW) & ~WS_EX_TOOLWINDOW
            if wanted != ex_style:
                self._write_exstyle(hwnd, wanted)
                # 改扩展样式不会自动重算窗口框架/任务栏，必须显式通知。
                # 两个坑都要避开：
                # ① argtypes 必须声明，否则 64 位下后续 int 参数按 32 位传；
                # ② hwndInsertAfter **不能传 0**（CW_USEDEFAULT 语义），实测
                #    会把窗口挪到 (0,0)。传自身 + SWP_NOZORDER 才保持原位。
                set_pos = ctypes.windll.user32.SetWindowPos
                set_pos.restype = ctypes.c_bool
                set_pos.argtypes = [
                    ctypes.c_void_p, ctypes.c_void_p,
                    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                    ctypes.c_uint]
                set_pos(
                    ctypes.c_void_p(hwnd), ctypes.c_void_p(hwnd), 0, 0, 0, 0,
                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER
                    | SWP_NOACTIVATE | SWP_FRAMECHANGED)
            self._taskbar_hwnd = hwnd
        except Exception:
            self._taskbar_hwnd = None

    @staticmethod
    def _read_exstyle(hwnd):
        getter = ctypes.windll.user32.GetWindowLongPtrW
        getter.restype = ctypes.c_void_p
        getter.argtypes = [ctypes.c_void_p, ctypes.c_int]
        return getter(ctypes.c_void_p(hwnd), GWL_EXSTYLE) or 0

    @staticmethod
    def _write_exstyle(hwnd, value):
        setter = ctypes.windll.user32.SetWindowLongPtrW
        setter.restype = ctypes.c_void_p
        setter.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        setter(ctypes.c_void_p(hwnd), GWL_EXSTYLE, ctypes.c_void_p(value))

    def _window_rect(self):
        """窗口的 Win32 屏幕矩形 ``(left, top, right, bottom)``。

        排查「最小化后飘在任务栏上」时用得到：正常最小化的窗口会移到
        ``(-32000, -32000)``，而悬浮碎片仍留在屏幕可见区域。``wm_geometry()``
        看不出这个差别——它对两者都返回最小化前的逻辑几何。
        """
        if os.name != "nt":
            return None
        hwnd = self._top_level_hwnd()
        if not hwnd:
            return None
        try:
            class _RECT(ctypes.Structure):
                _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                            ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

            rect = _RECT()
            getter = ctypes.windll.user32.GetWindowRect
            getter.restype = ctypes.c_bool
            getter.argtypes = [ctypes.c_void_p, ctypes.POINTER(_RECT)]
            if not getter(ctypes.c_void_p(hwnd), ctypes.byref(rect)):
                return None
            return (rect.left, rect.top, rect.right, rect.bottom)
        except Exception:
            return None

    def _top_level_hwnd(self):
        """沿父链找到最外层窗口句柄（任务栏归属由它决定）。"""
        if os.name != "nt":
            return None
        try:
            user32 = ctypes.windll.user32
            user32.GetParent.restype = ctypes.c_void_p
            user32.GetParent.argtypes = [ctypes.c_void_p]
            hwnd = self.win.winfo_id()
            for _ in range(6):
                parent = user32.GetParent(ctypes.c_void_p(hwnd))
                if not parent:
                    break
                hwnd = parent
            return hwnd
        except Exception:
            return None

    def _follow_topmost(self):
        """跟随主面板的置顶状态：面板常驻最前时，工具窗口别被它压住。"""
        try:
            self.win.attributes(
                "-topmost", bool(self.panel.win.attributes("-topmost")))
        except (TclError, AttributeError, TypeError):
            pass

    def _center(self):
        """把窗口摆到屏幕偏上居中的位置。

        **不要在这里先 update_idletasks()**：窗口此时尚未映射，Tk 会用
        「请求尺寸」（约 378x265）去计算，而我们随后设置的几何在映射后的
        第一轮事件循环里会被系统重新摆放——实测位置直接归零成 ``+0+0``。
        直接按屏幕尺寸算出目标值一次写完，等窗口映射就位。
        """
        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        w = min(self.width, max(MIN_W, sw - 80))
        h = min(self.height, max(MIN_H, sh - 120))
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 3)
        self.win.geometry(f"{w}x{h}+{x}+{y}")
        self._apply_round_region(w, h)

    def _on_shell_configure(self, event):
        # 区域裁剪必须每次立刻更新，否则窗口变大后超出旧区域的部分会被裁掉。
        self._apply_round_region(event.width, event.height)
        if self._shell_job is not None:
            try:
                self.win.after_cancel(self._shell_job)
            except TclError:
                pass
        width, height = event.width, event.height
        self._shell_job = self.win.after(
            60, lambda: self._paint_shell(width, height))

    def _apply_round_region(self, width, height):
        """用 GDI 区域把窗口四角裁成圆角。非 Windows 退化为直角。"""
        if os.name != "nt":
            return
        try:
            gdi = ctypes.windll.gdi32
            user32 = ctypes.windll.user32
            gdi.CreateRoundRectRgn.restype = ctypes.c_void_p
            gdi.CreateRoundRectRgn.argtypes = [ctypes.c_int] * 6
            user32.SetWindowRgn.restype = ctypes.c_int
            user32.SetWindowRgn.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool]
            rgn = gdi.CreateRoundRectRgn(
                0, 0, int(width) + 1, int(height) + 1,
                SHELL_RADIUS * 2, SHELL_RADIUS * 2)
            if rgn:
                # 成功后区域所有权归系统，不能再 DeleteObject。
                user32.SetWindowRgn(
                    ctypes.c_void_p(self.win.winfo_id()), rgn, True)
        except Exception:
            pass

    def _paint_shell(self, width, height):
        """绘制外壳：圆角底 + 冷光描边 + 顶边强调扫描线。"""
        self._shell_job = None
        if self._closed:
            return
        width, height = max(1, int(width)), max(1, int(height))
        try:
            from PIL import Image, ImageDraw, ImageTk
        except Exception:  # 没有 Pillow 时退化为纯色窗口，功能不受影响
            return
        # 2 倍超采样再缩放，圆角边缘才有抗锯齿；超大窗口降回 1 倍省内存。
        scale = 2 if width * height <= 1_400_000 else 1
        image = Image.new("RGBA", (width * scale, height * scale), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        fill = (*self._rgb(self.T["window_bg"]), 255)
        draw.rounded_rectangle(
            [0, 0, width * scale - 1, height * scale - 1],
            radius=SHELL_RADIUS * scale, fill=fill,
            outline=(*self._rgb(self._edge()), 255),
            width=SHELL_BORDER * scale)
        # 顶边扫描线：从圆角直边段（x >= radius）起笔，避免溢出到被裁掉的角落。
        accent = self._rgb(self._accent())
        top = SHELL_BORDER * scale
        start = SHELL_RADIUS * scale
        span = max(1, int((width * scale - start) * 0.5))
        steps = max(1, min(180, span))
        for index in range(steps):
            x = start + int(index * span / steps)
            alpha = int(190 * (1 - index / steps) ** 1.8)
            if alpha <= 0:
                break
            draw.line([(x, top), (x, top + scale - 1)], fill=(*accent, alpha))
        if scale != 1:
            image = image.resize((width, height), Image.LANCZOS)
        self._shell_image = ImageTk.PhotoImage(image)
        self.shell.delete("shell_bg")
        self.shell.create_image(0, 0, image=self._shell_image,
                                anchor="nw", tags="shell_bg")
        self.shell.tag_lower("shell_bg")

    def _build_rail(self):
        """顶部操作条：左侧信号条 + 工具名，右侧最小化/最大化/关闭。

        标题用次级文字色（``label2``）而不是主文字色，字号也比正文小一号——
        它只用来告诉用户「这是哪个工具」，不该和输入内容抢视线。**不做成系统
        标题栏**：没有底框、没有分隔线，与自绘外壳融为一体。

        「输入 / 结果」这类**分区小标题仍然不出现**，那两块靠左侧色条
        （输入=强调蓝、结果=青）与可编辑/只读状态区分。
        """
        tk, T = self.tk, self.T
        rail = tk.Frame(self.shell, bg=T["window_bg"], height=RAIL_HEIGHT)
        rail.place(x=CONTENT_INSET, y=CONTENT_INSET,
                   width=-CONTENT_INSET * 2, relwidth=1.0, height=RAIL_HEIGHT)
        mark = tk.Canvas(rail, width=24, height=RAIL_HEIGHT, bg=T["window_bg"],
                         highlightthickness=0, bd=0, cursor="fleur")
        mark.pack(side="left")
        self._draw_mark(mark)

        # 工具名。用 Label 而不是 Canvas 文字，方便测试断言与后续改文案。
        self.title_label = tk.Label(
            rail, text=self.title, bg=T["window_bg"], fg=T["label2"],
            font=(self.panel.FONT_FAMILY, 9), anchor="w")
        self.title_label.pack(side="left", padx=(7, 0))

        # 标题右侧一道极短的发丝线，把标题与控件区分开，同时保持轻量
        divider = tk.Frame(rail, bg=T["hairline"], width=1, height=11)
        divider.pack(side="left", padx=(9, 0))

        for widget in (rail, mark, self.title_label):
            widget.bind("<ButtonPress-1>", self._drag_press)
            widget.bind("<B1-Motion>", self._drag_move)

        # pack 顺序即左右：先 pack 的在最左，所以按 关闭 → 最大化 → 最小化
        # 反向 pack，最终视觉顺序是 最小化 / 最大化 / 关闭（Windows 习惯）。
        self._close_btn = self._make_control_button(
            rail, "close", self.close,
            hover_bg=self._sys("red"), hover_fg="#FFFFFF")
        self._max_btn = self._make_control_button(
            rail, "maximize", self._toggle_maximize)
        self._min_btn = self._make_control_button(
            rail, "minimize", self._minimize)
        for button in (self._close_btn, self._max_btn, self._min_btn):
            button.pack(side="right", padx=(2, 0))

    def _draw_mark(self, canvas):
        """信号条：三道递增竖条，纯几何，不表达任何文案。"""
        accent = self._accent()
        edge = self.T["label3"]
        base = (RAIL_HEIGHT - 15) // 2
        for index, bar in enumerate((6, 10, 15)):
            x = 5 + index * 5
            canvas.create_rectangle(
                x, base + (15 - bar), x + 3, base + 15,
                fill=accent if index == 2 else edge, outline="")

    def _make_control_button(self, parent, kind, command, hover_bg=None,
                             hover_fg=None):
        """窗口控制按钮：纯线条图形（最小化=横线，最大化=方框，关闭=叉线）。

        与主面板标题栏保持同一套视觉语言，但这里只用 Canvas 画——无边框窗口
        的 rail 很矮，位图缩放在高 DPI 下会糊。
        默认悬停：底色转 ``fill_hover``、图形转强调色；关闭按钮覆盖为红底白叉。
        """
        tk, T = self.tk, self.T
        size = 24
        idle = T["label3"]
        normal_bg = T["window_bg"]
        canvas = tk.Canvas(parent, width=size, height=size, bg=normal_bg,
                           highlightthickness=0, bd=0, cursor="hand2")

        def paint(fg, bg):
            canvas.configure(bg=bg)
            canvas.delete("all")
            if kind == "minimize":
                canvas.create_line(8, 12, 16, 12, fill=fg, width=1.6,
                                   capstyle="round")
            elif kind == "maximize":
                # 已最大化时画「还原」图形（两个错开的小方框），否则单方框。
                if self._maxed:
                    canvas.create_rectangle(7, 10, 14, 17, outline=fg, width=1.6)
                    canvas.create_line(10, 7, 17, 7, 17, 14, fill=fg, width=1.6)
                else:
                    canvas.create_rectangle(8, 8, 16, 16, outline=fg, width=1.6)
            else:
                canvas.create_line(8, 8, 16, 16, fill=fg, width=1.6,
                                   capstyle="round")
                canvas.create_line(16, 8, 8, 16, fill=fg, width=1.6,
                                   capstyle="round")

        paint(idle, normal_bg)

        def on_enter(_event):
            paint(hover_fg or self._accent(), hover_bg or T["fill_hover"])

        def on_leave(_event):
            paint(idle, normal_bg)

        canvas.bind("<Enter>", on_enter)
        canvas.bind("<Leave>", on_leave)
        canvas.bind("<Button-1>", lambda _e: command())
        # 供最大化/还原切换时重画图形（方框 ↔ 双框）
        canvas.refresh = lambda: paint(idle, normal_bg)
        return canvas

    # ---------- 窗口控制 ----------
    def _minimize(self):
        """最小化到任务栏。

        无边框窗口**不能直接 iconify**——实测抛
        ``TclError: override-redirect flag is set``，必须先临时取消
        ``overrideredirect``。

        **关键：最小化期间绝不能把边框装回去。** 早先的实现用
        ``after(120, _restore_chrome)`` 在窗口还是 iconic 时就恢复无边框，
        结果窗口被摘掉任务栏按钮、矩形从屏幕外的 ``-32000`` 变成贴在任务栏
        上沿的 ``160x28`` 悬浮碎片——用户看到的就是「不在任务栏里，而在任务栏
        上面」。实测对照：保持受管状态时矩形是 ``(-32000,-32000)``、任务栏
        按钮仍在（15847px 变化）；恢复无边框后按钮消失（0px）。

        所以这里只放开边框并 iconify，**恢复无边框的动作为推迟到窗口真正被
        唤回**（``<Map>`` 里 ``_pending_chrome`` 为真时执行）。
        """
        if self._closed:
            return
        try:
            if not self._pending_chrome:
                self._min_geom = self.win.wm_geometry()
            self._pending_chrome = True
            self.win.overrideredirect(False)
            self.win.iconify()
        except TclError:
            self._pending_chrome = False
            self.win.overrideredirect(True)
            self._claim_taskbar()

    def _restore_chrome(self):
        """把无边框外壳装回去，并保证几何与任务栏归属都回到最小化前。

        只在窗口已经回到 normal 之后调用（由 ``<Map>`` 触发，或测试里手动调用）。
        此时 WM 曾给窗口套上边框和标题栏，尺寸会从 920x640 被撑成 936x679
        （实测），**必须显式回写 geometry**，光恢复标志位不够。
        """
        if self._chrome_id is not None:
            try:
                self.win.after_cancel(self._chrome_id)
            except TclError:
                pass
        self._chrome_id = None
        self._pending_chrome = False
        if self._closed:
            return
        try:
            if str(self.win.state()) == "iconic":
                # 还没被唤回，什么都不做——此刻装边框会把窗口变成悬浮碎片。
                # 留着 _pending_chrome，下次 <Map> 时再收尾。
                self._pending_chrome = True
                return
            self.win.overrideredirect(True)
            if self._min_geom:
                self.win.geometry(self._min_geom)
            # overrideredirect(True) 会重设扩展样式、把 WS_EX_TOOLWINDOW 加回来，
            # 不重新声明就又会从任务栏消失。
            self._claim_taskbar()
            self.win.lift()
        except TclError:
            pass

    def _toggle_maximize(self):
        """最大化到工作区 / 还原。避开任务栏，不做全屏覆盖。"""
        if self._maxed:
            self._maxed = False
            if self._normal_geom:
                self.win.geometry(self._normal_geom)
            self._max_btn.refresh()
            return
        self._normal_geom = self.win.geometry()
        self._maxed = True
        area = self._work_area()
        if area:
            x, y, w, h = area
            self.win.geometry(f"{w}x{h}+{x}+{y}")
        else:
            self.win.geometry(
                f"{self.win.winfo_screenwidth()}x{self.win.winfo_screenheight()}+0+0")
        self._max_btn.refresh()

    def _work_area(self):
        """屏幕可用区域（排除任务栏）；取不到时返回 None 由调用方兜底。"""
        if os.name != "nt":
            return None
        try:
            class _RECT(ctypes.Structure):
                _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                            ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

            rect = _RECT()
            if ctypes.windll.user32.SystemParametersInfoW(
                    0x0030, 0, ctypes.byref(rect), 0):
                return (rect.left, rect.top,
                        rect.right - rect.left, rect.bottom - rect.top)
        except Exception:
            pass
        return None

    # ---------- 拖动 / 缩放 ----------
    def _drag_press(self, event):
        self._drag_off = (event.x_root - self.win.winfo_x(),
                          event.y_root - self.win.winfo_y())

    def _drag_move(self, event):
        if self._drag_off:
            self.win.geometry(
                f"+{event.x_root - self._drag_off[0]}+{event.y_root - self._drag_off[1]}")

    def _make_grip(self, parent):
        """右下角缩放抓手：三道斜线，按住可改窗口尺寸。"""
        tk, T = self.tk, self.T
        size = 14
        canvas = tk.Canvas(parent, width=size, height=size, bg=T["window_bg"],
                           highlightthickness=0, bd=0, cursor="size_nw_se")
        color = T["label3"]
        for index in range(3):
            canvas.create_line(size - 3 - index * 4, size - 2,
                               size - 2, size - 3 - index * 4,
                               fill=color, width=1)
        canvas.bind("<ButtonPress-1>", self._grip_press)
        canvas.bind("<B1-Motion>", self._grip_move)
        return canvas

    def _grip_press(self, event):
        self._resize_from = (event.x_root, event.y_root,
                             self.win.winfo_width(), self.win.winfo_height())

    def _grip_move(self, event):
        if not self._resize_from:
            return
        x0, y0, w0, h0 = self._resize_from
        w = max(MIN_W, w0 + event.x_root - x0)
        h = max(MIN_H, h0 + event.y_root - y0)
        self.win.geometry(f"{w}x{h}")

    # ---------- 布局 ----------
    def _mono(self, size, weight="normal"):
        return (MONO_FAMILY, size, weight)

    def _build(self):
        T, tk = self.T, self.tk
        body_h = -(CONTENT_INSET * 2 + RAIL_HEIGHT + FOOT_HEIGHT + 1 + 8)
        body = tk.Frame(self.shell, bg=T["window_bg"])
        body.place(x=CONTENT_INSET, y=CONTENT_INSET + RAIL_HEIGHT,
                   relwidth=1.0, relheight=1.0,
                   width=-CONTENT_INSET * 2, height=body_h)
        body.columnconfigure(0, weight=1, uniform="col")
        body.columnconfigure(1, weight=1, uniform="col")
        body.rowconfigure(0, weight=1)

        self.input = self._make_text(
            body, editable=True, edge=self._accent(), wrap_mode="char")
        self.input.master.grid(row=0, column=0, sticky="nsew")
        self.output = self._make_text(
            body, editable=False, edge=self._sys("teal"))
        self.output.master.grid(row=0, column=1, sticky="nsew", padx=(GAP, 0))
        self.input.bind("<KeyRelease>", self._on_input)
        self.input.bind("<<Paste>>", lambda _e: self.win.after(1, self._refresh))

        foot = tk.Frame(self.shell, bg=T["hairline"])
        foot.place(x=CONTENT_INSET, rely=1.0, y=-CONTENT_INSET,
                   relwidth=1.0, width=-CONTENT_INSET * 2,
                   height=FOOT_HEIGHT + 1, anchor="sw")
        row = tk.Frame(foot, bg=T["window_bg"])
        row.pack(fill="both", expand=True, pady=(1, 0))
        # 状态灯：一个小圆点，随解析结果在 灰/绿/红 之间切换。这是原「标题
        # + 说明文字」的位置，用零文案的方式承载「当前状态」这一信息。
        self._lamp = tk.Canvas(row, width=12, height=12, bg=T["window_bg"],
                               highlightthickness=0, bd=0)
        self._lamp.pack(side="left", padx=(4, 4))
        self._lamp_id = self._lamp.create_oval(
            2, 2, 10, 10, fill=self.T["label3"], outline="")
        self.status = tk.Label(row, text="", bg=T["window_bg"], fg=T["label2"],
                               font=(self.panel.FONT_FAMILY, 9), anchor="w")
        self.status.pack(side="left", fill="x", expand=True, padx=(0, 8))
        # pack 顺序即左右：抓手最靠右，其后是 清空 / 复制结果。
        # 「复制结果」复制的是**右侧结果框里的文字**（用户看到的即所得）。
        # 曾短暂改叫「复制文字」，后又按用户要求改回「复制结果」。
        self._make_grip(row).pack(side="right", padx=(4, 0))
        self._ghost_button(row, "复制结果", self._copy_output)
        self._ghost_button(row, "清空", self._clear)

    def _make_text(self, parent, editable, edge, wrap_mode="none"):
        """返回 Text 控件；其 ``master`` 是带边框与滚动条的容器 Frame。

        ``edge`` 是左侧 2px 强调色条——这是区分「输入 / 结果」的唯一标记，
        刻意不用文字标题。

        ``wrap_mode`` 决定超宽行怎么处理：

        - ``"char"``（左侧输入用）：**占满宽度就按字符折行**，不产生横向滚动。
          JSON 里没有空格，``"word"`` 折行对一整行压缩 JSON 完全无效，只会
          继续横向溢出，所以必须按字符折。折行只影响显示，``get("1.0","end")``
          取到的仍是原始文本，不会在内容里插入换行。
        - ``"none"``（右侧结果用）：保持每行不折、配横向滚动条。结果是**给人
          读的结构化输出**，强行折行会把缩进层级和列对齐打乱，反而更难看清。
        """
        T, tk = self.T, self.tk
        wrapped = wrap_mode != "none"
        wrap = tk.Frame(parent, bg=T["hairline"])
        wrap.rowconfigure(1, weight=1)
        wrap.columnconfigure(1, weight=1)
        tk.Frame(wrap, bg=T["hairline"], height=1).grid(
            row=0, column=0, columnspan=3, sticky="ew")
        tk.Frame(wrap, bg=edge, width=2).grid(row=1, column=0, sticky="ns")
        text = tk.Text(
            wrap, bd=0, highlightthickness=0, bg=T["card_bg"], fg=T["label"],
            insertbackground=self._accent(), font=self._mono(10),
            wrap=wrap_mode, undo=editable, padx=10, pady=8,
            selectbackground=T["accent_soft"], selectforeground=T["label"])
        # 原生 Scrollbar 在深色下会露出白槽，统一换成自绘细条。
        ys = _MiniScroll(tk, wrap, "vertical", text.yview, T, self._accent())
        text.configure(yscrollcommand=ys.set)
        scrolls = [ys]
        # 折行后横向永远不溢出，横条只会白占一条 10px 的槽，直接不建。
        xs = None
        if not wrapped:
            xs = _MiniScroll(tk, wrap, "horizontal", text.xview, T, self._accent())
            text.configure(xscrollcommand=xs.set)
            scrolls.append(xs)
        # 挂在 Text 上便于测试与后续主题重绘取用（不参与对外 API）。
        text._mini_scrolls = tuple(scrolls)
        text.grid(row=1, column=1, sticky="nsew")
        ys.canvas.grid(row=1, column=2, sticky="ns")
        if xs is not None:
            xs.canvas.grid(row=2, column=1, sticky="ew")
        wrap.columnconfigure(2, minsize=_MiniScroll.LANE)
        wrap.rowconfigure(2, minsize=_MiniScroll.LANE)
        if not editable:
            text.configure(state="disabled")
        return text

    def _ghost_button(self, parent, label, command):
        """描边幽灵按钮：外框 1px，悬停转强调色。"""
        T, tk = self.T, self.tk
        box = tk.Frame(parent, bg=T["hairline"])
        btn = tk.Button(
            box, text=label, command=command, bd=0, relief="flat",
            bg=T["fill"], fg=T["label"],
            activebackground=T["accent_soft"], activeforeground=self._accent(),
            font=(self.panel.FONT_FAMILY, 9), padx=12, pady=3,
            cursor="hand2", highlightthickness=0)
        btn.pack(padx=1, pady=1)

        def enter(_event):
            box.configure(bg=self._accent())
            btn.configure(bg=T["accent_soft"], fg=self._accent())

        def leave(_event):
            box.configure(bg=T["hairline"])
            btn.configure(bg=T["fill"], fg=T["label"])

        btn.bind("<Enter>", enter)
        btn.bind("<Leave>", leave)
        box.pack(side="right", padx=(6, 0), pady=2)
        return box

    # ---------- 取色 ----------
    @staticmethod
    def _rgb(value):
        value = value.lstrip("#")
        return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))

    def _accent(self):
        return self._sys("blue")

    def _sys(self, name):
        return self.panel._sys(name)

    def _edge(self):
        """描边色：分隔线与强调色混色，深色下像一条冷光边。"""
        base = self._rgb(self.T["hairline"])
        accent = self._rgb(self._accent())
        return "#{:02X}{:02X}{:02X}".format(
            *(int(a * 0.45 + b * 0.55) for a, b in zip(base, accent)))

    # ---------- 文本读写 ----------
    def _get_input(self):
        return self.input.get("1.0", "end-1c")

    def _set_output(self, text):
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        if text:
            self.output.insert("1.0", text)
        self.output.configure(state="disabled")

    def _set_status(self, text, kind="normal"):
        color = self._status_color(kind)
        self.status.configure(text=text, fg=color)
        self._lamp.itemconfigure(self._lamp_id, fill=color)

    def _status_color(self, kind):
        return {"error": self._sys("red"),
                "ok": self._sys("green")}.get(kind, self.T["label2"])

    # ---------- 交互 ----------
    def _on_input(self, _event=None):
        if self._after_id is not None:
            try:
                self.win.after_cancel(self._after_id)
            except TclError:
                pass
        self._after_id = self.win.after(DEBOUNCE_MS, self._refresh)

    def _refresh(self):
        # 主动调用 _refresh 时（测试、_clear）可能还有一次防抖
        # 定时器在排队；不清掉它，close() 就找不到该定时器，销毁后 Tk 会报
        # invalid command name。定时器自身回调进来时这里是空操作。
        if self._after_id is not None:
            try:
                self.win.after_cancel(self._after_id)
            except TclError:
                pass
        self._after_id = None
        if self._closed:
            return
        raw = self._get_input()
        if not raw.strip():
            self._set_output("")
            self._set_status(self.placeholder or "等待输入")
            return
        try:
            text, status, kind = self._analyze(raw)
        except Exception as exc:  # 工具窗口不应把异常抛给用户
            self._set_output("")
            self._set_status(str(exc) or type(exc).__name__, "error")
            return
        self._set_output(text)
        self._set_status(status, kind)

    def _analyze(self, raw):
        """返回 ``(结果文本, 状态文本, 状态类型)``。子类实现。"""
        raise NotImplementedError

    def _copy_output(self):
        try:
            content = self.output.get("1.0", "end-1c")
            self.win.clipboard_clear()
            self.win.clipboard_append(content)
            self._set_status(f"已复制 {len(content)} 字符", "ok")
        except TclError as exc:
            self._set_status(f"复制失败：{exc}", "error")

    def _clear(self):
        self.input.delete("1.0", "end")
        self._refresh()

    def close(self):
        if self._closed:
            return
        self._closed = True
        for job in (self._after_id, self._shell_job, self._chrome_id):
            if job is not None:
                try:
                    self.win.after_cancel(job)
                except TclError:
                    pass
        self._after_id = None
        self._shell_job = None
        self._chrome_id = None
        self._pending_chrome = False
        try:
            self.win.destroy()
        except TclError:
            pass


class JsonParamWindow(_ToolWindow):
    """JSON 参数工具：格式化 + 字段清单 + 重复键告警。"""

    title = "JSON 参数"
    placeholder = "粘贴接口入参后自动格式化，并列出字段结构"

    def _analyze(self, raw):
        result = transform_json(raw, "format")
        parts = [result.text]
        summary = _field_summary(result.text)
        if summary:
            parts += ["", f"{SECTION_MARK} 字段结构", summary]
        status = f"合法 JSON · {len(result.text)} 字符"
        if result.duplicate_keys:
            status += " · 重复键：" + "、".join(result.duplicate_keys)
            return "\n".join(parts), status, "error"
        return "\n".join(parts), status, "ok"


def _field_summary(pretty_json):
    """把 JSON 摊平成 ``a.b[0] : 类型 = 值`` 列表，帮助快速看结构。

    只展开数组首元素（后续元素结构通常相同），并附上总数，避免长数组刷屏。
    """
    import json
    try:
        obj = json.loads(pretty_json)
    except json.JSONDecodeError:
        return ""

    lines = []

    def walk(node, prefix):
        if isinstance(node, dict):
            if not node:
                lines.append(f"{prefix or '(根)'} : 空对象 {{}}")
                return
            for key, value in node.items():
                walk(value, f"{prefix}.{key}" if prefix else key)
        elif isinstance(node, list):
            if not node:
                lines.append(f"{prefix} : 空数组 []")
                return
            walk(node[0], f"{prefix}[0]")
            if len(node) > 1:
                lines.append(f"{prefix} : 共 {len(node)} 项")
        else:
            kind = ("null" if node is None else
                    "bool" if isinstance(node, bool) else
                    "数字" if isinstance(node, (int, float)) else "字符串")
            sample = "" if node is None else f" = {node!r}"[:64]
            lines.append(f"{prefix} : {kind}{sample}")

    walk(obj, "")
    return "\n".join(lines)


class JwtWindow(_ToolWindow):
    """JWT 解析工具：解 header/payload，标注过期状态，**不验签**。"""

    title = "JWT 解析"
    placeholder = "粘贴 JWT 后自动解码（只解码，不验签）"

    def _analyze(self, raw):
        result = jwt_tools.parse_jwt(raw)
        parts = [
            f"{SECTION_MARK} Header", result.header_json,
            "", f"{SECTION_MARK} Payload", result.payload_json,
        ]
        if result.signature:
            parts += ["", f"{SECTION_MARK} Signature（未校验）", result.signature]
        else:
            parts += ["", f"{SECTION_MARK} Signature", "(无签名，alg=none 形式)"]

        times = jwt_tools.summarize_claims(result.claims)
        if times:
            parts += ["", f"{SECTION_MARK} 时间声明", "\n".join(times)]

        status_parts = [f"{result.segments} 段"]
        header_alg = ""
        try:
            import json
            header_alg = json.loads(result.header_json).get("alg") or ""
        except (json.JSONDecodeError, AttributeError):
            header_alg = ""
        if header_alg:
            status_parts.append(f"alg={header_alg}")
        state, kind = self._expiry_state(result.claims)
        if state:
            status_parts.append(state)
        return "\n".join(parts), " · ".join(status_parts), kind

    @staticmethod
    def _expiry_state(claims):
        """根据 exp 给出人话状态；时钟以本机时间为准。"""
        import datetime
        now = datetime.datetime.now().timestamp()
        exp = claims.get("exp")
        if isinstance(exp, (int, float)) and not isinstance(exp, bool):
            delta = exp - now
            if delta <= 0:
                return "已过期", "error"
            if delta < 3600:
                return f"{int(delta // 60)} 分钟后过期", "error"
            if delta < 86400:
                return f"{int(delta // 3600)} 小时后过期", "ok"
            return f"{int(delta // 86400)} 天后过期", "ok"
        return "", "normal"


# 工具注册表：顶栏下拉与测试都遍历它，新增工具只改这里。
TOOLS = (
    ("json", "JSON 参数", JsonParamWindow),
    ("jwt", "JWT 解析", JwtWindow),
)


def open_tool(panel, key):
    """按 key 打开对应工具窗口，返回实例；未知 key 抛 ValueError。"""
    for tool_key, _label, cls in TOOLS:
        if tool_key == key:
            return cls(panel)
    raise ValueError(f"未知工具：{key}")


__all__ = ["TOOLS", "JsonParamWindow", "JwtWindow", "open_tool"]
