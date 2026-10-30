"""顶栏「工具」下拉里的独立小工具窗口：JSON 格式化 / JWT 解密。

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
- **JSON 窗口的转换菜单（格式化 / 压缩 / 展开转义 / 恢复原文）从剪贴板详情栏
  平移过来**，走同一个 ``json_popup.JsonPopup``、同一套文案与图标。用户原话
  「JSON格式化弹框将剪贴板中的JSON下拉框平移过去」——是「平移」不是「另做一套」，
  文案、顺序、图标都照抄 ``DetailPane._json_menu()``，不要另创措辞。
  **「Java 转义」是这里独有的第五种**（用户原话「json弹框工具新增 JSON参数化
  能力」，功能照做、展示名后来按官方术语改成了「Java 转义」），把 JSON 转成
  Java 字符串字面量内容；详情栏那个下拉不加它。

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

from . import jwt_tools, taskbar
from .json_modes import ESCAPE_JAVA_NOTICE, MODE_LABELS, build_menu_items
from .json_tools import transform_json

# 等宽字体：Windows 上 Consolas 覆盖 ASCII 与数字对齐；中文回退由 Tk 处理。
MONO_FAMILY = "Consolas"

# 输入变化到重新解析的等待时间。paste 大段 JSON 时逐字符触发会明显卡顿。
DEBOUNCE_MS = 180

# ---------- 外壳尺寸 ----------
# 窗口四角是**尖角**，与剪贴板主面板一致（主面板 `_make_rounded_bg` 只画矩形，
# 从不调 `SetWindowRgn` 做圆角）。曾是半径 12 的圆角，两个窗口并排看风格割裂，
# 已统一成直角。**不要再加回圆角**：那会让描边沿弧线走、视觉比主面板软一档。
SHELL_BORDER = 1           # 描边宽度
CONTENT_INSET = 8          # 内容与外壳边缘的间距，让描边露出来
RAIL_HEIGHT = 30           # 顶部操作条：只承载拖动与关闭
FOOT_HEIGHT = 34           # 底部状态 + 操作按钮
GAP = 10                   # 左右两栏之间的缝隙
MIN_W, MIN_H = 660, 440    # 允许缩到的最小尺寸
# 窗口四边 / 四角的可拖拽热区宽度。取 6px 而不是 Windows 常见的 8px，是因为
# 内容区从 CONTENT_INSET(8) 起笔——热区必须完全落在这一圈「只有外壳」的
# 缝隙里，否则会盖住正文响应区，用户点边框附近的文字时变成改尺寸。
RESIZE_EDGE = 6

# 结果里的段落标记。用等宽方块而不是「— xxx —」，更接近终端观感。
SECTION_MARK = "▍"

# JSON 转换模式的展示名。**唯一来源是 ``json_modes.MODE_LABELS``**，这里只做
# 再导出，便于 ``tool_window.JSON_MODE_LABELS`` 这个既有引用点继续可用。
#
# 历史：这段文案曾在 ``history_panel`` 与 ``tool_window`` 各写一份，靠
# ``test_json_mode_labels_match_detail_pane`` 钉住不许漂移。那是用测试补架构缺口
# ——用户要求两边同步后，改成从 ``json_modes`` 单一来源取。
JSON_MODE_LABELS = MODE_LABELS

# ---------- Win32 窗口扩展样式 ----------
# 具体实现统一在 lumina/taskbar.py（与主面板共用），这里只做常量再导出，
# 便于测试直接按名字断言。
#
# 无边框（overrideredirect）会让 Tk 给顶层窗口打上 WS_EX_TOOLWINDOW，
# 该标志的语义就是「不进任务栏、不进 Alt+Tab」。要让它像普通窗口一样
# 出现在任务栏，必须换成 WS_EX_APPWINDOW。
#
# WS_MINIMIZEBOX / WS_MAXIMIZEBOX / WS_SYSMENU 是**纯行为标志**：无边框窗口
# 是纯 WS_POPUP，三个框位标志全为 0，缺了它们点任务栏按钮只会激活窗口，
# 不会「点一下最小化、再点一下还原」。补上后 Windows 自己接管该交互，
# 实测**不会**因此露出原生标题栏。
GWL_EXSTYLE = taskbar.GWL_EXSTYLE
GWL_STYLE = taskbar.GWL_STYLE
WS_EX_TOOLWINDOW = taskbar.WS_EX_TOOLWINDOW
WS_EX_APPWINDOW = taskbar.WS_EX_APPWINDOW
WS_MINIMIZEBOX = taskbar.WS_MINIMIZEBOX
WS_MAXIMIZEBOX = taskbar.WS_MAXIMIZEBOX
WS_SYSMENU = taskbar.WS_SYSMENU
SWP_NOSIZE = taskbar.SWP_NOSIZE
SWP_NOMOVE = taskbar.SWP_NOMOVE
SWP_NOZORDER = taskbar.SWP_NOZORDER
SWP_NOACTIVATE = taskbar.SWP_NOACTIVATE
SWP_FRAMECHANGED = taskbar.SWP_FRAMECHANGED


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
        self._resize_edges = {}
        self._maxed = False
        self._normal_geom = None
        self._min_geom = None
        self._chrome_id = None
        # 任何自绘下拉（目前只有 JSON 那个）都挂在这里，close()/最小化时统一收掉。
        self._popup = None
        theme = panel._theme()
        self.T, self.dark = theme if isinstance(theme, tuple) else (theme, False)
        self.win = self.tk.Toplevel(panel.win)
        self.win.title(self.title)      # 仅任务栏标识用；无边框窗口不显示它
        self._build_shell()
        self._center()
        self._build()
        # ESC = 最小化到任务栏（用户要求，与主面板一致）。关闭仍走顶栏那个叉。
        self.win.bind("<Escape>", lambda _e: self.escape())
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
        # 任务栏归属与图标。构造期窗口还没映射，Tk 会在映射那一刻才应用
        # overrideredirect 的扩展样式并把 WS_EX_TOOLWINDOW 打上，所以构建期
        # 这里设的会被覆盖——必须等 <Map> 之后再改一次（见 _on_map_claim_taskbar）。
        self.win.bind("<Map>", self._on_map_claim_taskbar, add="+")
        self._claim_taskbar()
        self.shell = tk.Canvas(self.win, bg=self.T["window_bg"],
                               highlightthickness=0, bd=0)
        self.shell.pack(fill="both", expand=True)
        self._paint_shell(self.width, self.height)
        self.shell.bind("<Configure>", self._on_shell_configure)
        self._build_rail()
        # 缩放热区最后建：它要压在 rail 之上（顶边热区与 rail 顶沿重叠 1~2px），
        # 否则顶边那一条永远拖不动。
        self._make_resize_edges()

    def _on_map_claim_taskbar(self, event):
        """窗口真正显示后重新声明任务栏归属。

        ``<Map>`` 里 Tk 已经把 overrideredirect 对应的扩展样式应用完了，
        此时改才不会被覆盖。

        最小化已改为 Win32 ``ShowWindow(SW_MINIMIZE)``，不再翻转
        ``overrideredirect``，所以这里**没有**「最小化后收尾还原」的分支了。
        """
        if event.widget is not self.win or self._closed:
            return
        self._claim_taskbar()

    def _claim_taskbar(self):
        """声明任务栏归属并补框位标志，见 ``taskbar.claim_taskbar``。"""
        self._taskbar_hwnd = taskbar.top_level_hwnd(self.win)
        return taskbar.claim_taskbar(self.win)

    @staticmethod
    def _read_exstyle(hwnd):
        return taskbar.read_style(hwnd, taskbar.GWL_EXSTYLE)

    @staticmethod
    def _read_style(hwnd):
        return taskbar.read_style(hwnd, taskbar.GWL_STYLE)

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
        """沿父链找到最外层窗口句柄，见 ``taskbar.top_level_hwnd``。"""
        return taskbar.top_level_hwnd(self.win)

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
        """把窗口裁剪成**直角矩形**，与剪贴板主面板保持一致。

        早先这里用 ``CreateRoundRectRgn`` 裁成半径 ``SHELL_RADIUS`` 的圆角。
        但主面板（剪贴板）是纯直角窗口（``_make_rounded_bg`` 只画矩形填充，
        从不调 ``SetWindowRgn``），两个窗口并排看风格割裂，所以统一成尖角。

        仍保留 ``SetWindowRgn`` 调用：它在这里不产生圆角，只负责把窗口区域
        钉成精确的 ``width x height``，避免窗口尺寸与内容之间出现 1px 缝隙。
        **不能直接删掉这层调用**——``_paint_shell`` 画的外壳是按客户区尺寸
        绘制的，缺了区域裁剪时 DWM 会在边缘留一圈未绘制的黑边。
        方法名沿用旧名，是为了不动既有的调用点与测试。
        """
        if os.name != "nt":
            return
        try:
            gdi = ctypes.windll.gdi32
            user32 = ctypes.windll.user32
            gdi.CreateRectRgn.restype = ctypes.c_void_p
            gdi.CreateRectRgn.argtypes = [ctypes.c_int] * 4
            user32.SetWindowRgn.restype = ctypes.c_int
            user32.SetWindowRgn.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool]
            rgn = gdi.CreateRectRgn(0, 0, int(width) + 1, int(height) + 1)
            if rgn:
                # 成功后区域所有权归系统，不能再 DeleteObject。
                user32.SetWindowRgn(
                    ctypes.c_void_p(self.win.winfo_id()), rgn, True)
        except Exception:
            pass

    def _paint_shell(self, width, height):
        """绘制外壳：直角底 + 冷光描边 + 顶边强调扫描线。

        四角是**尖角**，与剪贴板主面板一致（主面板的 ``_make_rounded_bg``
        从来只画矩形）。这里不用 ``rounded_rectangle`` 是为了让描边贴着
        真正的窗口边界——圆角时描边要沿弧线走，视觉上比主面板「软」一档。
        """
        self._shell_job = None
        if self._closed:
            return
        width, height = max(1, int(width)), max(1, int(height))
        try:
            from PIL import Image, ImageDraw, ImageTk
        except Exception:  # 没有 Pillow 时退化为纯色窗口，功能不受影响
            return
        # 2 倍超采样再缩放，描边边缘才有抗锯齿；超大窗口降回 1 倍省内存。
        scale = 2 if width * height <= 1_400_000 else 1
        image = Image.new("RGBA", (width * scale, height * scale), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        fill = (*self._rgb(self.T["window_bg"]), 255)
        draw.rectangle(
            [0, 0, width * scale - 1, height * scale - 1],
            fill=fill, outline=(*self._rgb(self._edge()), 255),
            width=SHELL_BORDER * scale)
        # 顶边扫描线：直角窗口没有「被裁掉的角落」，所以从最左边起笔，
        # 与外壳左上角严格对齐。圆角版本要从 x >= radius 起笔避免溢出到
        # 弧外，现在不需要了。
        accent = self._rgb(self._accent())
        top = SHELL_BORDER * scale
        start = 0
        span = max(1, int(width * scale * 0.5))
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
            widget.bind("<Double-Button-1>", self._rail_double)
            # 双击顶栏切换最大化/还原，与 Windows 原生行为一致。
            # 三个控件各自要绑：Double 事件只会送到「收到第一次 press」的那个
            # 控件，绑在 rail 上无法覆盖 mark / title_label 这两块。

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
        """最小化到任务栏，**不翻转 overrideredirect、不重建 HWND**。

        Tk 的 ``iconify()`` 对无边框窗口抛
        ``TclError: override-redirect flag is set``。早先的绕法是临时
        ``overrideredirect(False)`` → ``iconify()`` → 唤回时再翻回来，但这
        **每次翻转都重建 HWND**（实测 ``0x3a2e1480`` → ``0x559a07c8`` →
        ``0x1f40160c``），新窗口带着 Tk 重设的 ``WS_EX_TOOLWINDOW`` 回来，
        explorer 于是撤掉任务栏按钮——用户看到的就是「显示后图标消失、
        点击切换失效」。

        现在改用 ``ShowWindow(SW_MINIMIZE)``：实测无边框窗口能正常最小化
        （矩形移到 ``-32000``），且 EX/STYLE/HICON 全程不变。
        """
        if self._closed:
            return
        # 先收起下拉：弹窗是独立 Toplevel，窗口最小化后它会孤零零飘在桌面上。
        self._close_popup()
        self._min_geom = self.win.wm_geometry()
        if taskbar.minimize_window(self.win):
            return
        try:
            self.win.withdraw()
        except TclError:
            pass

    def is_minimized(self):
        """窗口当前是否最小化在任务栏里（不是隐藏、也不是关闭）。"""
        if self._closed:
            return False
        return taskbar.is_minimized(self.win)

    def escape(self):
        """ESC：最小化到任务栏（不再是关闭）。

        用户原话：「现在按 ESC 键剪贴板和弹框都是直接关闭了，改成最小化到
        任务栏」。与顶栏最小化按钮完全一致，所以复用 ``_minimize``。

        **关闭仍然是 ``close()``**（顶栏那个叉），销毁 Tk 窗口、释放定时器。
        两者不要混：最小化的窗口还挂在任务栏上，用户点一下就能回来。
        """
        self._minimize()
        return "break"

    def summon(self):
        """把最小化的窗口唤回屏幕（``SW_RESTORE``）。

        ``SW_SHOW`` 对最小化窗口无效——实测调完 ``IsIconic`` 仍为 True，
        窗口挂在任务栏上永远回不来。所以走 ``restore_window``，它内部是
        ``SW_RESTORE``，同时会保住最小化前的几何（含最大化状态）。
        """
        if self._closed:
            return False
        if not taskbar.restore_window(self.win):
            try:
                self.win.deiconify()
            except TclError:
                return False
        try:
            self.win.lift()
            self.win.focus_force()
        except TclError:
            pass
        return True

    def _restore_chrome(self):
        """从最小化还原（``SW_RESTORE``），不碰 ``overrideredirect``。

        保留这个名字是为了兼容既有调用点与测试；现在它只负责「还原 + 补一次
        任务栏归属」，不再需要重装边框、也不再需要回写 geometry——HWND 从头
        到尾没变过。
        """
        if self._chrome_id is not None:
            try:
                self.win.after_cancel(self._chrome_id)
            except TclError:
                pass
        self._chrome_id = None
        if self._closed:
            return
        if not taskbar.restore_window(self.win):
            try:
                self.win.deiconify()
            except TclError:
                pass
        if self._min_geom:
            self.win.geometry(self._min_geom)


    def _toggle_maximize(self):
        """最大化到工作区 / 还原。避开任务栏，不做全屏覆盖。"""
        # 最大化/还原会整体换掉几何，残留的缩放起点必须作废——否则接下来
        # 一晃鼠标（哪怕没按）都会按旧起点重算尺寸。
        self._resize_from = None
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

    def _rail_double(self, _event=None):
        """双击顶栏切换最大化/还原（Windows 原生习惯）。

        先清掉拖动锚点：最大化之后窗口贴满工作区，残留的锚点会让接下来的
        ``<B1-Motion>`` 把窗口按旧偏移拖走。
        """
        self._drag_off = None
        self._toggle_maximize()
        return "break"

    def _drag_move(self, event):
        if self._drag_off:
            self.win.geometry(
                f"+{event.x_root - self._drag_off[0]}+{event.y_root - self._drag_off[1]}")

    def _make_resize_edges(self):
        """在窗口四边铺一圈透明的缩放热区，实现八向拖拽改尺寸。

        **为什么不用 Win32 那套（``WS_THICKFRAME`` + ``WM_NCHITTEST``）**：
        主面板用的是它，但工具弹框的自绘外壳扛不住 —— 实测给 ``winfo_id()``
        那个 TkChild 加上 ``WS_THICKFRAME`` 后：

        1. 客户区从 920x640 被压成 **906x626**（14px 非客户区）；
        2. ``PrintWindow`` 抓到的最外圈像素由干净的圆角强调色变成
           ``(227,227,227)`` / ``(255,255,255)`` —— 系统灰边露了出来，
           与自绘的 ``SetWindowRgn`` 圆角打架。

        本窗口是「整块 Canvas 自绘 + 圆角裁剪」的形态，露系统边框就毁了观感，
        所以改用纯 Tk 的热区：不动任何窗口样式，尺寸与外观零变化。

        热区必须压在**内容区之外**那一圈（``CONTENT_INSET = 8``）：正文、
        顶栏、底栏都是从这个内边距往里排的，所以 6px 宽的热区不会盖住它们。
        角上的热区用 ``RESIZE_EDGE * 2`` 见方，比单边更宽——角落本来就该
        更好抓。
        """
        tk = self.tk
        width = RESIZE_EDGE
        corner = RESIZE_EDGE * 2
        # 角热区从**真正的角**起算（place 在 0,0 / 右上等位置）。四角是尖角，
        # 整块 12x12 方框都是真实存在的像素，不像圆角时那样有个被裁掉的扇形。
        edges = (
            ("n", {"x": corner, "y": 0, "relwidth": 1.0, "width": -corner * 2,
                   "height": width}, "sb_v_double_arrow"),
            ("s", {"x": corner, "rely": 1.0, "y": -width, "relwidth": 1.0,
                   "width": -corner * 2, "height": width}, "sb_v_double_arrow"),
            ("w", {"x": 0, "y": corner, "width": width, "relheight": 1.0,
                   "height": -corner * 2}, "sb_h_double_arrow"),
            ("e", {"relx": 1.0, "x": -width, "y": corner, "width": width,
                   "relheight": 1.0, "height": -corner * 2},
             "sb_h_double_arrow"),
            ("nw", {"x": 0, "y": 0, "width": corner, "height": corner},
             "size_nw_se"),
            ("ne", {"relx": 1.0, "x": -corner, "y": 0, "width": corner,
                    "height": corner}, "size_ne_sw"),
            ("sw", {"x": 0, "rely": 1.0, "y": -corner, "width": corner,
                    "height": corner}, "size_ne_sw"),
            ("se", {"relx": 1.0, "x": -corner, "rely": 1.0, "y": -corner,
                    "width": corner, "height": corner}, "size_nw_se"),
        )
        self._resize_edges = {}
        for name, geometry, cursor in edges:
            zone = tk.Frame(self.shell, bg=self.T["window_bg"], cursor=cursor,
                            highlightthickness=0, bd=0)
            zone.place(**geometry)
            zone.bind("<Enter>", lambda _e, n=name: self._edge_enter(n))
            zone.bind("<Leave>", lambda _e, n=name: self._edge_leave(n))
            zone.bind("<ButtonPress-1>", lambda e, n=name: self._edge_press(e, n))
            zone.bind("<B1-Motion>", self._edge_move)
            zone.bind("<ButtonRelease-1>", self._edge_release)
            self._resize_edges[name] = zone
        return self._resize_edges

    def _edge_enter(self, name):
        """鼠标进入边缘时给一道极淡的高亮，暗示「这里能拖」。

        不做常驻描边：无边框窗口的边界本来就靠外壳的冷光描边表达，再加一层
        会显得毛糙。只在悬停时轻微提亮，符合「可发现但不喧哗」。
        """
        if self._closed:
            return
        zone = self._resize_edges.get(name)
        if zone is not None:
            zone.configure(bg=self.T["fill_hover"])

    def _edge_leave(self, name):
        zone = self._resize_edges.get(name)
        if zone is not None:
            zone.configure(bg=self.T["window_bg"])

    def _edge_press(self, event, name):
        """记下起点：窗口几何 + 鼠标屏幕坐标 + 拖的是哪条边/哪个角。"""
        # 先无条件清掉旧状态。最大化时不能缩放，但如果只是 return 而不清空，
        # 上一次拖拽遗留的 ``_resize_from`` 会被紧接着的 ``<B1-Motion>``
        # 拿来用——实测最大化窗口一拖就缩成 MIN_W。
        self._resize_from = None
        if self._maxed:
            return "break"      # 最大化时不允许改尺寸，与原生窗口一致
        self._resize_from = {
            "edge": name,
            "x": event.x_root,
            "y": event.y_root,
            "geom": (self.win.winfo_width(), self.win.winfo_height(),
                     self.win.winfo_x(), self.win.winfo_y()),
        }
        return "break"

    def _edge_move(self, event):
        """按边名决定要改宽/高、还是同时改宽高与原点。

        左侧和上侧缩放必须**同时移动原点**，否则看起来是「窗口在飞」而不是
        「边在动」。移动量先算出来再夹紧，保证最小尺寸时原点也停在正确位置。
        """
        state = self._resize_from
        if not state or self._closed or self._maxed:
            return
        name = state["edge"]
        w0, h0, x0, y0 = state["geom"]
        dx = event.x_root - state["x"]
        dy = event.y_root - state["y"]

        w, h, x, y = w0, h0, x0, y0
        if "e" in name:
            w = max(MIN_W, w0 + dx)
        if "w" in name:
            w = max(MIN_W, w0 - dx)
            # 夹紧后原点要跟着「实际改掉的宽度」走，不能直接用 dx，
            # 否则缩到下限后继续往右拖，窗口会开始往左漂。
            x = x0 + (w0 - w)
        if "s" in name:
            h = max(MIN_H, h0 + dy)
        if "n" in name:
            h = max(MIN_H, h0 - dy)
            y = y0 + (h0 - h)
        self.win.geometry(f"{w}x{h}+{x}+{y}")

    def _edge_release(self, _event=None):
        self._resize_from = None

    def _make_grip(self, parent):
        """右下角缩放抓手：三道斜线，纯视觉提示（真正的拖拽由边缘热区接管）。

        早先它是唯一的缩放入口，但它被 pack 在底部状态栏里，实测位置偏到
        「右边界内 8px、下边界内 18px」，离真正的窗口角有明显空档，用户
        根本抓不到。现在八向热区铺满四边，这个抓手退回纯装饰：告诉用户
        「这个角可以拉」，不再承担事件。
        """
        tk, T = self.tk, self.T
        size = 14
        canvas = tk.Canvas(parent, width=size, height=size, bg=T["window_bg"],
                           highlightthickness=0, bd=0, cursor="size_nw_se")
        color = T["label3"]
        for index in range(3):
            canvas.create_line(size - 3 - index * 4, size - 2,
                               size - 2, size - 3 - index * 4,
                               fill=color, width=1)
        # 抓手本身也接一下事件，免得手指正好落在这 14px 上时反而不能拖。
        canvas.bind("<ButtonPress-1>", lambda e: self._edge_press(e, "se"))
        canvas.bind("<B1-Motion>", self._edge_move)
        canvas.bind("<ButtonRelease-1>", self._edge_release)
        return canvas

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

        self.input = self._make_text(body, editable=True, edge=self._accent())
        self.input.master.grid(row=0, column=0, sticky="nsew",
                               padx=(0, GAP // 2))
        self.output = self._make_text(body, editable=False, edge=self._sys("teal"))
        # **两栏正文必须等宽。** 这里曾用 ``padx=(GAP, 0)`` 给右栏让出缝隙，代价是
        # 右栏内容区比左栏窄 10px（实测 440 vs 430）——同样的文本在窄栏里更早折行，
        # 实测 60 个字符的字符串左栏 1 屏行、右栏 2 屏行，正好违背「右侧和左侧保持
        # 一致」。改成两栏各让一半缝隙，宽度完全相等。视觉缝隙仍是 ``GAP``。
        self.output.master.grid(row=0, column=1, sticky="nsew",
                                padx=(GAP // 2, 0))
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
        # 子类专属的下拉入口（目前只有 JSON 工具的「JSON ▾」）。放在最后 pack，
        # 于是它落在最左——右侧那三个（清空/复制结果/抓手）的位置不变。
        self._build_footer_menu(row)

    def _build_footer_menu(self, parent):
        """底部左侧的下拉入口，默认没有。子类按需覆盖。

        单独留一个钩子而不是把下拉写进 ``_build``：JWT 那个工具没有可转换的
        模式，多一个空的下拉只会让人以为漏了什么。
        """

    def _close_popup(self):
        """收起自绘下拉（若有）。幂等，可被最小化/关闭路径无条件调用。"""
        popup, self._popup = self._popup, None
        if popup is not None:
            try:
                popup.close()
            except Exception:
                # 收菜单是尽力而为：这里失败也不能连累主流程（最小化/关闭）。
                pass

    def _make_text(self, parent, editable, edge):
        """返回 Text 控件；其 ``master`` 是带边框与滚动条的容器 Frame。

        ``edge`` 是左侧 2px 强调色条——这是区分「输入 / 结果」的唯一标记，
        刻意不用文字标题。

        **两侧都按字符折行（``wrap="char"``），行为与左栏完全一致。**
        用户原话：「工具弹框右侧和左侧如果超过当前长度自动换行，右侧和左侧保持
        一致」。早先右栏刻意 ``wrap="none"`` 配横向滚动条，理由是「结果是给人读
        的结构化输出，折行会打乱缩进列对齐」；但那个取舍的代价是长行必须左右拖
        才能读到行尾，用户明确要求放弃它、改成两侧统一自动换行。

        用 ``"char"`` 而不是 ``"word"``：JSON 文本里没有空格，一整行压缩 JSON 交给
        ``"word"`` 只会继续横向溢出。折行只影响显示，``get("1.0","end")`` 取到的
        仍是原始文本，不会在内容里插入换行符。

        两侧都折行后横向永不溢出，所以**不再建横向滚动条**——它只会白占一条
        10px 的槽（连轨道都画不出滑块）。
        """
        T, tk = self.T, self.tk
        wrap = tk.Frame(parent, bg=T["hairline"])
        wrap.rowconfigure(1, weight=1)
        wrap.columnconfigure(1, weight=1)
        tk.Frame(wrap, bg=T["hairline"], height=1).grid(
            row=0, column=0, columnspan=3, sticky="ew")
        tk.Frame(wrap, bg=edge, width=2).grid(row=1, column=0, sticky="ns")
        text = tk.Text(
            wrap, bd=0, highlightthickness=0, bg=T["card_bg"], fg=T["label"],
            insertbackground=self._accent(), font=self._mono(10),
            wrap="char", undo=editable, padx=10, pady=8,
            selectbackground=T["accent_soft"], selectforeground=T["label"])
        # 原生 Scrollbar 在深色下会露出白槽，统一换成自绘细条。
        ys = _MiniScroll(tk, wrap, "vertical", text.yview, T, self._accent())
        text.configure(yscrollcommand=ys.set)
        # 挂在 Text 上便于测试与后续主题重绘取用（不参与对外 API）。
        text._mini_scrolls = (ys,)
        text.grid(row=1, column=1, sticky="nsew")
        ys.canvas.grid(row=1, column=2, sticky="ns")
        # **行列 minisize 只给真正用到的那一行**：原先无条件
        # ``rowconfigure(2, minsize=_MiniScroll.LANE)``，折行后第 2 行没有任何控件，
        # 仍被撑出 10px 空白（实测容器 551 高、Text 只有 540）。
        wrap.columnconfigure(2, minsize=_MiniScroll.LANE)
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
        # 显式收掉下拉。**这条在实测中是冗余的**：``JsonPopup`` 自己绑了父窗口的
        # ``<Destroy>``，销毁时会 close 并回调 ``_popup_closed`` 把引用清掉。
        # 保留它是为了不依赖那条隐式链路——「弹窗飘在桌面上」是最难发现的缺陷
        # 之一，多一行确定性的收尾比省一行代码划算。最小化那条才是必需的：
        # 最小化不销毁窗口，弹窗会真的孤零零留在屏幕上。
        self._close_popup()
        # 从 panel 的实例表里摘掉自己：否则下次 open_tool 会拿到一个已销毁的
        # 实例（虽然那里也会判 winfo_exists 兜底，但留着是内存泄漏）。
        registry = getattr(self.panel, "_tool_windows", None)
        if isinstance(registry, dict):
            for key, window in list(registry.items()):
                if window is self:
                    registry.pop(key, None)
        for job in (self._after_id, self._shell_job, self._chrome_id):
            if job is not None:
                try:
                    self.win.after_cancel(job)
                except TclError:
                    pass
        self._after_id = None
        self._shell_job = None
        self._chrome_id = None
        try:
            self.win.destroy()
        except TclError:
            pass


class JsonParamWindow(_ToolWindow):
    """JSON 格式化工具：格式化 / 压缩 / 展开转义 / Java 转义 / 恢复原文 + 重复键告警。

    结果栏**只放转换后的结果本身**，不附字段结构摘要（用户明确要求去掉）。

    下拉里的前三种转换是从剪贴板详情栏**平移**过来的：同一个 ``JsonPopup``、
    同一套文案与图标、同样的 ``selected`` 勾选语义。区别只在于结果写到本窗口
    右侧，而不是详情栏的预览区。**「Java 转义」是本窗口独有的第五种**（用户原话
    「json弹框工具新增 JSON参数化 能力」，功能保留、展示名后来改成更标准的说法），
    详情栏那边不动。
    """

    title = "JSON 格式化"
    placeholder = "粘贴接口入参后自动格式化"

    def __init__(self, panel):
        # 默认模式：格式化。剪贴板详情栏的默认是「原文」，因为那里展示的是历史
        # 记录本身；这个窗口生来就是为了看格式化结果，换成原文反而莫名其妙。
        self._mode = "format"
        super().__init__(panel)

    # ---------- 下拉 ----------
    def _build_footer_menu(self, parent):
        """底部左下角的「JSON ▾」入口，与详情栏那颗按钮同款同文案。"""
        T, tk = self.T, self.tk
        box = tk.Frame(parent, bg=T["hairline"])
        btn = tk.Button(
            box, text="JSON ▾", command=self._json_menu, bd=0, relief="flat",
            bg=T["fill"], fg=T["label"],
            activebackground=T["accent_soft"], activeforeground=self._accent(),
            font=(self.panel.FONT_FAMILY, 9), padx=10, pady=3,
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
        # pack 在最后 -> 落在最左，右侧的 清空 / 复制结果 / 抓手 位置不变。
        box.pack(side="left", padx=(8, 0), pady=2)
        self._json_anchor = btn

    def _json_menu(self):
        """弹出转换菜单。**再次点击同一按钮即收起**。

        items 由 ``json_modes.build_menu_items`` 生成——与剪贴板详情栏**同一份
        代码**，两边菜单因此必然一致（用户要求「两边同步」）。

        用 ``json_popup.JsonPopup`` 而不是 Tk 原生 ``menu.post``：后者在 Windows
        下会进模态跟踪循环，把 UI 线程卡死（这条教训主面板已经付过代价）。
        """
        from .json_popup import JsonPopup
        # 已经开着就收起。判 isinstance 而不是判非空，是因为将来可能还有别种菜单。
        if isinstance(self._popup, JsonPopup):
            self._close_popup()
            return
        self._close_popup()
        # busy 恒 False：本窗口同步执行，不存在「转换进行中」这个状态。
        items = build_menu_items(
            self._set_mode, self._restore_original, self._mode,
            busy=False, can_restore=self._mode is not None)
        self._popup = JsonPopup(
            self.tk, self.win, items, self.T, dpi=self._dpi())
        self._popup.popup(self._json_anchor, on_close=self._popup_closed)

    def _popup_closed(self):
        """弹窗自己关掉时（点外部、按 ESC）把引用清干净。"""
        self._popup = None

    def _dpi(self):
        """取主面板的缩放系数；替身面板（测试）没有这个属性时退回 1.0。"""
        value = getattr(self.panel, "_dpi", 1.0)
        try:
            return max(0.5, min(3.0, float(value)))
        except (TypeError, ValueError):
            return 1.0

    def _set_mode(self, mode):
        """切到某个转换模式并立刻重算。剪贴板详情栏同一套语义。"""
        if self._mode == mode:
            return
        self._mode = mode
        self._refresh()

    def _restore_original(self):
        """恢复原文：不传给 ``transform_json``（它明确拒绝 restore）。

        这里「原文」指用户粘进左栏的内容本身，不需要回溯任何历史状态。
        """
        self._mode = None
        self._refresh()

    # ---------- 解析 ----------
    def _analyze(self, raw):
        if self._mode is None:
            # 原文态：原样回显，不做任何结构改动。
            return raw, f"原文 · {len(raw)} 字符", "ok"
        result = transform_json(raw, self._mode)
        # **结果栏只放转换后的结果本身，不再附字段结构摘要。**
        # 用户原话：「JSON格式化右侧的字段结构就不要了去掉」。早先格式化视图会在
        # 末尾追加一段 ``▍ 字段结构`` 摊平清单（``a.b[0] : 字符串 = 'x'``），本意是
        # 帮用户快速看结构；但右侧本来就是完整且已缩进对齐的 JSON，再多一段摘要在
        # 长文档下反而把真正的结果推下去。**这是用户偏好，不要以「信息更全」为由
        # 加回来。**
        status = f"{JSON_MODE_LABELS[self._mode]} · {len(result.text)} 字符"
        if self._mode == "escape_java":
            # 转义产物是**字面量内容**，不含外层引号——不写明的话用户很可能
            # 直接粘进 ``setPayload(xxx)`` 得到编译错误。这句提示是功能的一部分。
            # 文案取自 ``json_modes.ESCAPE_JAVA_NOTICE``，与详情栏共用一份，
            # 免得两侧说法漂移。
            status += " · " + ESCAPE_JAVA_NOTICE
        if result.duplicate_keys:
            status += " · 重复键：" + "、".join(result.duplicate_keys)
            return result.text, status, "error"
        return result.text, status, "ok"


class JwtWindow(_ToolWindow):
    """JWT 解密工具：解 header/payload，标注过期状态，**不验签**。"""

    title = "JWT 解密"
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
    ("json", "JSON 格式化", JsonParamWindow),
    ("jwt", "JWT 解密", JwtWindow),
)


def open_tool(panel, key):
    """按 key 打开对应工具窗口，返回实例；未知 key 抛 ValueError。

    **同一工具只保留一个窗口实例。** ESC 改成「最小化」之后这条变重要了：
    最小化的窗口仍在任务栏上，若再从工具菜单点一次就新建第二个，用户会看到
    两个同名窗口、且各自保存着不同的输入内容。所以先查 ``panel._tool_windows``
    里有没有还活着的实例——有就只唤回它（最小化状态走 ``SW_RESTORE``），
    不新建。
    """
    registry = getattr(panel, "_tool_windows", None)
    if registry is None:
        registry = {}
        panel._tool_windows = registry

    existing = registry.get(key)
    if existing is not None:
        # 窗口被用户点叉关掉后 Tk 控件已销毁，实例不能再用，直接清掉重建。
        try:
            alive = not existing._closed and bool(existing.win.winfo_exists())
        except Exception:
            alive = False
        if alive:
            existing.summon()
            return existing
        registry.pop(key, None)

    for tool_key, _label, cls in TOOLS:
        if tool_key == key:
            window = cls(panel)
            registry[key] = window
            return window
    raise ValueError(f"未知工具：{key}")


__all__ = ["TOOLS", "JsonParamWindow", "JwtWindow", "open_tool"]
