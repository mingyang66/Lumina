"""独立的闹钟设置页、桌面提醒与轻量调度器。

不修改剪贴板数据库；闹钟选项保存在应用 config.json 的 ``alarm_*`` 字段中。
调度仅使用主 Tk 线程的 ``after``，避免 Tk 跨线程调用。
"""
import calendar
import math
import tkinter as tk
import traceback
from datetime import date, datetime, timedelta

LEAD_MINUTES = (0, 1, 3, 5, 10, 15)
# 稍后提醒可选时长（分钟）。设置页与弹框按钮共用同一份清单。
SNOOZE_MINUTES = (1, 3, 5, 10, 15, 30)
REPEAT_LABELS = {"once": "单次", "daily": "每天", "weekdays": "工作日"}
REPEAT_VALUES = {value: key for key, value in REPEAT_LABELS.items()}
STYLE_LABELS = {"neon": "霓虹光环", "soft": "柔和星光", "minimal": "极简"}
STYLE_VALUES = {value: key for key, value in STYLE_LABELS.items()}
PALETTES = {
    "neon": ("#151326", "#24203d", "#a78bfa", "#ece9ff"),
    "soft": ("#14212b", "#203440", "#72d4d0", "#e5fbfa"),
    "minimal": ("#171717", "#282828", "#d3b8ff", "#f1edf8"),
}
DATE_FORMAT = "%Y-%m-%d"
DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"
TIME_FORMATS = ("%H:%M:%S", "%H:%M")


def parse_alarm_time(value):
    """Parse ``HH:MM:SS`` (legacy ``HH:MM`` is still accepted) into a ``time``."""
    text = str(value).strip()
    for pattern in TIME_FORMATS:
        try:
            return datetime.strptime(text, pattern).time()
        except ValueError:
            continue
    raise ValueError(f"无法识别的时间：{text or '（空）'}，格式应为 时:分:秒")


def month_grid(year, month):
    """Return the 42 dates (Monday-first) rendered for a month view."""
    first = date(year, month, 1)
    start = first - timedelta(days=first.weekday())
    return [start + timedelta(days=offset) for offset in range(42)]


def humanize_delta(delta):
    """Render a duration as a short Chinese phrase, e.g. ``1 小时 20 分钟``."""
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 60:
        return f"{seconds} 秒"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} 分钟"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} 小时 {minutes} 分钟" if minutes else f"{hours} 小时"
    days, hours = divmod(hours, 24)
    return f"{days} 天 {hours} 小时" if hours else f"{days} 天"


def format_seconds(seconds):
    """Total whole seconds for the ring centre, e.g. ``8100``."""
    return str(max(0, int(seconds)))


def next_alarm_occurrence(settings, now=None):
    """Return the next local alarm datetime, or None when disabled/invalid/past."""
    if not settings.get("alarm_enabled", False):
        return None
    now = now or datetime.now()
    try:
        alarm_time = parse_alarm_time(settings.get("alarm_time", "09:00"))
        repeat = settings.get("alarm_repeat", "once")
        if repeat == "once":
            alarm_date = datetime.strptime(
                str(settings.get("alarm_date", now.strftime(DATE_FORMAT))),
                DATE_FORMAT).date()
            target = datetime.combine(alarm_date, alarm_time)
            return target if target >= now else None
        if repeat not in ("daily", "weekdays"):
            return None
        for offset in range(8):
            day = now.date() + timedelta(days=offset)
            if repeat == "weekdays" and day.weekday() >= 5:
                continue
            target = datetime.combine(day, alarm_time)
            if target >= now:
                return target
    except (TypeError, ValueError):
        return None
    return None


class PillButton(tk.Canvas):
    """画布自绘的圆角按钮，保留 ``tk.Button`` 的常用子集 API。

    弹框原来用原生 ``tk.Button``，和主面板自绘的 pill 按钮不是一套视觉语言。
    这里换成自绘版本，同时保留 ``cget`` / ``configure`` / ``invoke`` /
    ``grid`` / ``grid_remove`` / ``winfo_manager``，调用方与既有测试都不用改。
    """

    def __init__(self, parent, text="", command=None, bg="#334155",
                 fg="#ffffff", hover=None, pressed=None, padx=12, pady=6,
                 font=("Microsoft YaHei UI", 9), dpi=1.0, radius=9):
        self._dpi = dpi or 1.0
        self._padx = max(6, int(round(padx * self._dpi)))
        self._pady = max(3, int(round(pady * self._dpi)))
        self._radius = max(4, int(round(radius * self._dpi)))
        self._font = font
        self._text = text
        self._command = command
        self._bg = bg
        self._fg = fg
        self._fg_active = fg
        self._hover = hover or bg
        self._pressed = pressed or hover or bg
        self._photos = {}
        self._state = "normal"
        self._text_w, self._text_h = self._measure()
        width, height = self._size()
        try:
            parent_bg = parent.cget("bg")
        except Exception:
            parent_bg = bg
        super().__init__(parent, width=width, height=height, bd=0,
                         highlightthickness=0, cursor="hand2", bg=parent_bg)
        self._draw(self._bg)
        self.bind("<Enter>", lambda _e: self._draw(self._hover))
        self.bind("<Leave>", lambda _e: self._draw(self._bg))
        self.bind("<ButtonPress-1>", lambda _e: self._draw(self._pressed))
        self.bind("<ButtonRelease-1>", self._on_release)

    # ---------- 兼容 tk.Button 的常用面 ----------
    def cget(self, key):
        if key == "text":
            return self._text
        if key == "bg":
            return self._bg
        if key == "fg":
            return self._fg
        if key == "command":
            return self._command
        return super().cget(key)

    def configure(self, **kwargs):
        redraw = False
        if "text" in kwargs:
            self._text = kwargs.pop("text")
            redraw = True
        if "command" in kwargs:
            self._command = kwargs.pop("command")
        if "bg" in kwargs:
            self._bg = kwargs.pop("bg")
            redraw = True
        if "fg" in kwargs:
            self._fg = kwargs.pop("fg")
            self._fg_active = self._fg
            redraw = True
        if "activebackground" in kwargs:
            self._hover = kwargs.pop("activebackground")
        if "activeforeground" in kwargs:
            self._fg_active = kwargs.pop("activeforeground")
        if "state" in kwargs:
            self._state = kwargs.pop("state")
        if kwargs:
            super().configure(**kwargs)
        if redraw:
            self._text_w, self._text_h = self._measure()
            width, height = self._size()
            super().configure(width=width, height=height)
            self._draw(self._bg)
        return self

    config = configure

    def invoke(self):
        if self._state != "disabled" and self._command is not None:
            return self._command()
        return None

    # ---------- 绘制 ----------
    def _measure(self):
        from tkinter import font as tkfont
        measured = tkfont.Font(font=self._font)
        return measured.measure(self._text or ""), measured.metrics("linespace")

    def _size(self):
        return (self._text_w + 2 * self._padx, self._text_h + 2 * self._pady)

    def _photo(self, color, width, height):
        key = (color, width, height)
        photo = self._photos.get(key)
        if photo is not None:
            return photo
        from PIL import Image, ImageDraw, ImageTk

        scale = 2
        image = Image.new("RGBA", (width * scale, height * scale),
                          (0, 0, 0, 0))
        ImageDraw.Draw(image).rounded_rectangle(
            (0, 0, width * scale - 1, height * scale - 1),
            radius=self._radius * scale, fill=color)
        photo = ImageTk.PhotoImage(
            image.resize((width, height), Image.LANCZOS), master=self)
        self._photos[key] = photo
        return photo

    def _draw(self, color):
        width, height = self._size()
        self.delete("all")
        self.create_image(0, 0, image=self._photo(color, width, height),
                          anchor="nw")
        hovered = color == self._hover and self._hover != self._bg
        self.create_text(width // 2, height // 2, text=self._text,
                         fill=self._fg_active if hovered else self._fg,
                         font=self._font)

    def _on_release(self, _event=None):
        self._draw(self._hover)
        return self.invoke()


def menu_theme(palette):
    """FlatMenu 的配色：从 palette 派生，弹框与设置页共用一份。"""
    _bg, card, accent, fg = palette
    return {
        "hairline": blend_color(card, accent, 0.30),
        "card_bg": card,
        "separator": blend_color(card, fg, 0.18),
        "label": fg,
        "accent": accent,
        "accent_soft": blend_color(card, accent, 0.30),
        "fill_hover": blend_color(card, accent, 0.22),
    }


class PickField(tk.Frame):
    """自绘下拉：左边显示当前值，点一下用 FlatMenu 选。

    原来用 ``ttk.Combobox``，但 Windows 的 ttk 字段恒为浅色，插在深色卡片里像
    白补丁；这里换成自绘，配色跟着 palette 走，行为与只读下拉一致。
    """

    def __init__(self, parent, var, values, palette, width=14, font=None,
                 dpi=1.0):
        _bg, card, accent, fg = palette
        self._dpi = dpi or 1.0
        self._field_bg = blend_color(card, "#000000", 0.45)
        super().__init__(parent, bg=self._field_bg, highlightthickness=1,
                         highlightbackground=blend_color(card, accent, 0.35),
                         highlightcolor=accent, takefocus=1)
        self._var = var
        self._values = list(values)
        self._palette = palette
        self._fg = fg
        self._font = font or ("Microsoft YaHei UI", 10)
        self._label = tk.Label(self, textvariable=var, anchor="w",
                               bg=self._field_bg, fg=fg, font=self._font,
                               padx=self._s(8), pady=self._s(4), width=width)
        self._label.pack(side="left", fill="x", expand=True)
        self._arrow = tk.Label(self, text="▾", bg=self._field_bg,
                               fg=blend_color(fg, card, 0.35),
                               font=self._font, padx=self._s(8))
        self._arrow.pack(side="right")
        for widget in (self, self._label, self._arrow):
            widget.configure(cursor="hand2")
            widget.bind("<Button-1>", self._open)
        self.bind("<Return>", self._open)
        self.bind("<space>", self._open)
        self.bind("<FocusIn>", lambda _event: self._label.configure(fg=accent))
        self.bind("<FocusOut>",
                  lambda _event: self._label.configure(fg=self._fg))

    def _s(self, value):
        return int(round(value * self._dpi))

    def _open(self, _event=None):
        if not self._values:
            return "break"
        try:
            from .panel_menu import FlatMenu
            items = [(value, (lambda chosen=value: self._choose(chosen)))
                     for value in self._values]
            menu = FlatMenu(tk, self.winfo_toplevel(), items,
                            theme=menu_theme(self._palette))
            menu.popup(self.winfo_rootx(),
                       self.winfo_rooty() + self.winfo_height())
        except Exception:
            traceback.print_exc()
        return "break"

    def _choose(self, value):
        self._var.set(value)


class AlarmScheduler:
    """One-second, UI-thread scheduler. ``config`` is the shared app config dict."""

    TICK_MS = 1000

    def __init__(self, master, config, save_settings, card_master=None,
                 on_open_settings=None, on_show_clipboard=None):
        self.master = master
        self.card_master = card_master or master
        self.config = config
        self.save_settings = save_settings
        self.on_open_settings = on_open_settings
        self.on_show_clipboard = on_show_clipboard
        self._job = None
        self._signature = None
        self._target = None
        self._early_shown = False
        self._ringing = False
        self._snooze_at = None
        self._overlay = None
        self._stopped = False
        self._internal_update = False

    @staticmethod
    def _settings_signature(config):
        return tuple(config.get(key) for key in (
            "alarm_enabled", "alarm_date", "alarm_time", "alarm_repeat"))

    def start(self):
        if self._job is None and not self._stopped:
            self._sync_target(datetime.now())
            self._tick()

    def stop(self):
        self._stopped = True
        if self._job is not None:
            try:
                self.master.after_cancel(self._job)
            except Exception:
                pass
            self._job = None
        if self._overlay is not None:
            self._overlay.destroy_without_callback()
            self._overlay = None

    def refresh(self):
        """Re-read config after the settings page saves, without waiting for a tick."""
        if not self._stopped and not self._internal_update:
            self._signature = None
            self._sync_target(datetime.now())

    def _sync_target(self, now):
        signature = self._settings_signature(self.config)
        if signature != self._signature:
            if self._overlay is not None:
                self._overlay.destroy_without_callback()
                self._overlay = None
            self._signature = signature
            self._target = next_alarm_occurrence(self.config, now)
            self._early_shown = False
            self._ringing = False
            self._snooze_at = None
            if self._target is not None:
                self._show("scheduled", self._target)
            else:
                self._show("idle", None)

    def _tick(self):
        self._job = None
        if self._stopped:
            return
        now = datetime.now()
        self._sync_target(now)
        target = self._target
        if target is not None:
            if self._snooze_at is not None and now >= self._snooze_at:
                self._snooze_at = None
                stage = "alarm" if self._ringing or now >= target else "early"
                self._show(stage, target)
            elif now >= target and not self._ringing:
                self._ringing = True
                self._show("alarm", target)
            elif (not self._early_shown and not self._ringing
                  and int(self.config.get("alarm_lead_minutes", 0) or 0) > 0
                  and now >= target - timedelta(
                      minutes=int(self.config.get("alarm_lead_minutes", 0)))):
                self._early_shown = True
                self._show("early", target)
        self._job = self.master.after(self.TICK_MS, self._tick)

    def _show(self, stage, target, collapsed=False):
        overlay = self._overlay
        if overlay is not None and overlay.alive():
            overlay.apply(stage, target, collapsed=collapsed)
            return
        self._overlay = AlarmAlertWindow(
            self.card_master, self.config, stage, target,
            on_acknowledge=self._acknowledge,
            on_snooze=self._snooze, persistent=True,
            on_open_settings=self.on_open_settings,
            on_show_clipboard=self.on_show_clipboard,
            start_collapsed=(stage == "idle" or collapsed))

    def _acknowledge(self, stage):
        if stage == "alarm":
            self._finish_occurrence()

    def _snooze(self, stage, minutes):
        snooze_at = datetime.now() + timedelta(minutes=minutes)
        self._snooze_at = snooze_at
        if stage == "alarm":
            self._ringing = True
        overlay = self._overlay
        if overlay is not None and overlay.alive():
            overlay.apply("snoozed", snooze_at, collapsed=True)
        else:
            self._show("snoozed", snooze_at, collapsed=True)

    def _finish_occurrence(self):
        self._snooze_at = None
        if self.config.get("alarm_repeat", "once") == "once":
            self._internal_update = True
            try:
                self.save_settings({"alarm_enabled": False})
            except Exception:
                pass
            finally:
                self._internal_update = False
            self._signature = self._settings_signature(self.config)
            self._target = None
            self._ringing = False
            self._show("idle", None, collapsed=True)
            return
        self._target = next_alarm_occurrence(
            self.config, datetime.now() + timedelta(seconds=1))
        self._early_shown = False
        self._ringing = False
        if self._target is not None:
            self._show("scheduled", self._target, collapsed=True)
        else:
            self._show("idle", None, collapsed=True)


def blend_color(first, second, ratio):
    """Mix two ``#rrggbb`` colors; ``ratio`` 0 keeps ``first`` and 1 gives ``second``."""
    ratio = min(1.0, max(0.0, ratio))
    channels = [
        round(int(first[index:index + 2], 16) * (1 - ratio)
              + int(second[index:index + 2], 16) * ratio)
        for index in (1, 3, 5)
    ]
    return "#" + "".join(f"{channel:02x}" for channel in channels)


def detect_dpi(window):
    """与 history_panel 同一口径：fpixels('1i')/96，夹在 0.8–3.0。"""
    try:
        return max(0.8, min(window.winfo_fpixels("1i") / 96.0, 3.0))
    except Exception:
        return 1.0


def monitor_work_area(window):
    """鼠标所在显示器的工作区 ``(x, y, w, h)``。

    取不到（无 Win32 API / 无图形会话）时退回主屏整屏。
    """
    try:
        import ctypes
        from ctypes import wintypes

        class _Rect(ctypes.Structure):
            _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                        ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

        class _MonitorInfo(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", _Rect),
                        ("rcWork", _Rect), ("dwFlags", wintypes.DWORD)]

        point = wintypes.POINT()
        if not ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
            raise OSError("GetCursorPos failed")
        monitor = ctypes.windll.user32.MonitorFromPoint(
            point, 2)  # MONITOR_DEFAULTTONEAREST
        info = _MonitorInfo()
        info.cbSize = ctypes.sizeof(_MonitorInfo)
        if not ctypes.windll.user32.GetMonitorInfoW(monitor,
                                                   ctypes.byref(info)):
            raise OSError("GetMonitorInfoW failed")
        rect = info.rcWork
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        if width > 0 and height > 0:
            return rect.left, rect.top, width, height
    except Exception:
        pass
    return (0, 0, window.winfo_screenwidth(), window.winfo_screenheight())


class AlarmAlertWindow:
    """无边框闹钟卡片：同心圆涟漪动效、右上角常驻、可折叠。"""

    WIDTH = 420
    CANVAS_WIDTH = 396
    # 高度演进：200 → 96（紧凑化）→ 144（用户要求涟漪再大）。
    # 144 让环的最大直径到 132，仍留 6px 上下余量；整卡约 297 高。
    CANVAS_HEIGHT = 144
    HANDLE_WIDTH = 100
    HANDLE_HEIGHT = 100
    ORB_RADIUS = 32
    ORB_MARGIN = 8
    ORB_KEY = "#010203"
    ALERT_ACCENT = "#FF7A6B"
    AUTO_COLLAPSE_MS = 3000
    MARGIN = 20
    # 标题栏里闹钟名称的最长可见字符数，超出用省略号（否则会把整卡撑宽）。
    CAPTION_MAX = 12
    SNOOZE_DEFAULT = 5
    WEEKDAYS = ("一", "二", "三", "四", "五", "六", "日")

    def __init__(self, master, config, stage, target,
                 on_acknowledge, on_snooze, persistent=False,
                 start_collapsed=False, on_open_settings=None,
                 on_show_clipboard=None):
        import tkinter as tk

        self.tk = tk
        self.master = master
        self.config = config
        self.stage = stage
        self.target = target
        self.on_acknowledge = on_acknowledge
        self.on_snooze = on_snooze
        self.persistent = persistent
        self.on_open_settings = on_open_settings
        self.on_show_clipboard = on_show_clipboard
        self.collapsed = False
        self._closed = False
        self._sound_job = None
        self._slide_job = None
        self._count_job = None
        self._animation_job = None
        self._auto_collapse_job = None
        self._handle_hover = False
        self._orb_photo = None
        self._tab_job = None
        self._tab_phase = 0.0
        self._clock_text = ""
        self._alert_job = None
        self._alert_ticks = 0
        self._phase = 0.0
        self._speed = 0.014
        self._interval = 130
        self._drag_origin = None
        self._drag_moved = False
        self._custom_position = False
        self._handle_drag_origin = None
        self._handle_dragged = False
        self._current_x = 0
        self._target_y = self.MARGIN
        self.bg, self.card, self.accent, self.fg = PALETTES.get(
            config.get("alarm_style", "neon"), PALETTES["neon"])
        self.border = blend_color(self.card, self.accent, 0.38)
        self.muted = blend_color(self.fg, self.card, 0.45)
        self.alert_border = blend_color(self.card, self.ALERT_ACCENT, 0.55)
        self.window = tk.Toplevel(master)
        self.window.withdraw()
        self._dpi = self._detect_dpi()
        self._canvas_w = self._s(self.CANVAS_WIDTH)
        self._canvas_h = self._s(self.CANVAS_HEIGHT)
        self._handle_w = self._s(self.HANDLE_WIDTH)
        self._handle_h = self._s(self.HANDLE_HEIGHT)
        self.window.overrideredirect(True)
        self.window.configure(bg=self.border)
        self.window.attributes("-topmost", True)
        self._orb_transparent = False
        try:
            self.window.attributes("-transparentcolor", self.ORB_KEY)
            self._orb_transparent = True
        except Exception:
            pass
        if self._orb_transparent:
            self.window.configure(bg=self.ORB_KEY)
        self.full_frame = tk.Frame(self.window, bg=self.border)
        self.full_frame.pack(fill="both", expand=True)
        inner = tk.Frame(self.full_frame, bg=self.card)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        header = tk.Frame(inner, bg=self.card)
        header.pack(fill="x", padx=14, pady=(10, 0))
        header_dot = tk.Canvas(header, width=6, height=6, bg=self.card,
                               highlightthickness=0, bd=0)
        self.header_dot = header_dot
        self.header_dot_item = header_dot.create_oval(
            1, 1, 5, 5, fill=self.accent, outline="")
        header_dot.pack(side="left", padx=(0, 6), pady=(3, 0))
        self.label_caption = tk.Label(
            header, text="", bg=self.card, fg=self.muted,
            font=("Segoe UI", 9, "bold"))
        self.label_caption.pack(side="left")
        close_label = tk.Label(header, text="×", bg=self.card, fg=self.muted,
                               font=("Segoe UI", 13), cursor="hand2")
        close_label.pack(side="right")
        close_label.bind("<Button-1>", self._dismiss_view)
        close_label.bind("<Enter>", lambda _event: close_label.configure(fg=self.fg))
        close_label.bind("<Leave>", lambda _event: close_label.configure(fg=self.muted))
        self.clock_label = tk.Label(
            header, text="", bg=self.card, fg=self.muted,
            font=("Consolas", 9))
        self.clock_label.pack(side="right", padx=(0, 12))
        self.settings_label = tk.Label(
            header, text="设置", bg=self.card, fg=self.muted,
            font=("Segoe UI", 9), cursor="hand2")
        if self.on_open_settings is not None:
            self.settings_label.bind("<Button-1>", self._open_settings)
            self.settings_label.bind(
                "<Enter>",
                lambda _event: self.settings_label.configure(fg=self.fg))
            self.settings_label.bind(
                "<Leave>",
                lambda _event: self.settings_label.configure(fg=self.muted))
        self.canvas = tk.Canvas(
            inner, width=self._canvas_w, height=self._canvas_h,
            bg=self.card, bd=0, highlightthickness=0)
        self.canvas.pack(fill="x", padx=self._s(10), pady=(self._s(2), 0))
        self.title_label = tk.Label(
            inner, text="", bg=self.card, fg=self.fg,
            font=("Segoe UI", 18, "bold"), justify="center",
            wraplength=self.WIDTH - 56)
        self.title_label.pack(pady=(4, 0))
        self.detail_label = tk.Label(
            inner, text="", bg=self.card, fg=self.muted,
            font=("Segoe UI", 10), justify="center",
            wraplength=self.WIDTH - 56)
        self.detail_label.pack(pady=(2, 6))
        footer = tk.Frame(inner, bg=self.card)
        footer.pack(pady=(0, 8))
        secondary_bg = blend_color(self.card, self.accent, 0.22)
        secondary_hover = blend_color(self.card, self.accent, 0.34)
        primary_fg = blend_color(self.card, "#000000", 0.82)
        primary_hover = blend_color(self.accent, "#ffffff", 0.18)
        self._secondary_bg = secondary_bg
        self._secondary_hover = secondary_hover
        self._primary_fg = primary_fg
        self.secondary = PillButton(
            footer, text="", command=lambda: self._snooze(self.snooze_minutes()),
            bg=secondary_bg, fg=self.fg, hover=secondary_hover,
            pressed=secondary_hover, padx=12, pady=6, dpi=self._dpi,
            font=("Microsoft YaHei UI", 9))
        self.secondary.grid(row=0, column=0, padx=(0, self._s(8)))
        self.secondary.bind("<Button-3>", self._snooze_menu)
        self.primary = PillButton(
            footer, text="", command=self._ack, bg=self.accent, fg=primary_fg,
            hover=primary_hover, pressed=primary_hover, padx=16, pady=6,
            dpi=self._dpi, font=("Microsoft YaHei UI", 9, "bold"))
        self.primary.grid(row=0, column=1)
        corner = self.ORB_KEY if self._orb_transparent else self.card
        self.handle_frame = tk.Canvas(
            self.window, width=self._handle_w, height=self._handle_h,
            bg=corner, highlightthickness=0, bd=0, cursor="hand2")
        self.handle_frame.bind("<ButtonPress-1>", self._handle_press)
        self.handle_frame.bind("<B1-Motion>", self._handle_drag)
        self.handle_frame.bind("<ButtonRelease-1>", self._handle_release)
        self.handle_frame.bind("<Enter>", self._handle_enter)
        self.handle_frame.bind("<Leave>", self._handle_leave)
        self._build_orb_photo()
        self.handle_frame.create_image(0, 0, image=self._orb_photo, anchor="nw")
        self._set_stage(stage, target)
        self.window.bind("<Escape>", self._dismiss_view)
        # 闹钟弹框上最常按的两个动作：Enter = 主按钮，S = 稍后提醒。
        self.window.bind("<Return>", lambda _event: self._invoke_primary())
        self.window.bind("<KP_Enter>", lambda _event: self._invoke_primary())
        self.window.bind("<s>", lambda _event: self._snooze_from_key())
        self.window.bind("<S>", lambda _event: self._snooze_from_key())
        self._enable_drag()
        if start_collapsed:
            self.full_frame.pack_forget()
            self.handle_frame.pack(fill="both", expand=True)
            self.collapsed = True
            self._update_countdown()
        self._place_top_right()
        self._start_animation()
        self._start_tab_animation()
        self._schedule_auto_collapse()
        if stage == "alarm":
            self.window.focus_force()

    def _detect_dpi(self):
        """与 history_panel 同一口径的 DPI 探测。"""
        return detect_dpi(self.window)

    def _s(self, value):
        """逻辑像素 → 当前 DPI 下的物理像素。"""
        return int(round(value * self._dpi))

    def _monitor_work_area(self):
        """鼠标所在显示器的工作区（实现见模块级 monitor_work_area）。"""
        return monitor_work_area(self.window)

    def _place_top_right(self):
        """Size to content, park off-screen, then slide in below the corner."""
        self.window.update_idletasks()
        mx, my, mw, _mh = self._monitor_work_area()
        if self.collapsed:
            width, height = self._handle_w, self._handle_h
            self._current_x = mx + mw + 8
            self._target_y = my + self._s(self.MARGIN)
            self.window.geometry(
                f"{width}x{height}+{self._current_x}+{self._target_y}")
            self.window.deiconify()
            self.window.lift()
            self._animate_x(max(
                mx, mx + mw - width - self._s(self.ORB_MARGIN)))
            return
        width = max(self._s(self.WIDTH), self.window.winfo_reqwidth())
        height = self.window.winfo_reqheight()
        self._current_x = mx + mw + 8
        self._target_y = my + self._s(self.MARGIN)
        self.window.geometry(f"{width}x{height}+{self._current_x}+{self._target_y}")
        self.window.deiconify()
        self.window.lift()
        self._animate_x(max(mx, mx + mw - width - self._s(self.MARGIN)))

    def _set_stage(self, stage, target):
        self.stage = stage
        self.target = target
        if stage == "alarm":
            self._apply_alert_style()
            self._start_alert_breath()
        else:
            self._restore_style()
            self._stop_alert_breath()
        if self.on_open_settings is not None:
            if stage == "alarm":
                self.settings_label.pack_forget()
            else:
                self.settings_label.pack(
                    side="right", before=self.clock_label, padx=(0, 10))
        if stage == "snoozed":
            label = self.config.get("alarm_label", "闹钟")
            self.label_caption.configure(text=self._fit_caption(label))
            self.title_label.configure(text="稍后提醒", fg=self.fg)
            self.detail_label.configure(text=f"{target:%H:%M:%S} 再次响铃")
            self.primary.configure(text="知道了", command=self._ack)
            self.secondary.grid_remove()
            self._cancel_sound()
            self._update_countdown()
            self._start_countdown()
            self._update_animation_speed()
            return
        if stage == "idle":
            self.label_caption.configure(text="闹钟服务")
            self.title_label.configure(text="闹钟未设置", fg=self.fg)
            self.detail_label.configure(
                text="从面板的「闹钟」入口添加一个提醒")
            self.primary.configure(text="知道了", command=self._ack)
            self.secondary.grid_remove()
            self._cancel_sound()
            self._update_countdown()
            self._start_countdown()
            self._update_animation_speed()
            return
        self.primary.configure(command=self._ack)
        label = self.config.get("alarm_label", "闹钟")
        weekday = self.WEEKDAYS[target.weekday()]
        titles = {"scheduled": "闹钟已设置", "early": "即将开始", "alarm": "时间到了"}
        self.label_caption.configure(text=self._fit_caption(label))
        self.title_label.configure(
            text=titles.get(stage, "闹钟"),
            fg=self.ALERT_ACCENT if stage == "alarm" else self.fg)
        self.detail_label.configure(
            text=f"响铃 {target.year}年{target.month}月{target.day}日 周{weekday}")
        if stage == "alarm":
            self.primary.configure(text="停止")
            self.secondary.configure(
                text=f"{self.snooze_minutes()} 分钟后提醒")
            self.secondary.grid()
            self._cancel_countdown()
            if self.config.get("alarm_sound", True) and self._sound_job is None:
                self._ring()
        else:
            self.primary.configure(text="知道了")
            if stage == "early":
                self.secondary.configure(
                    text=f"{self.snooze_minutes()} 分钟后提醒")
                self.secondary.grid()
            else:
                self.secondary.grid_remove()
            self._cancel_sound()
            self._update_countdown()
            self._start_countdown()
        self._update_animation_speed()

    def _apply_alert_style(self):
        alert_fg = blend_color(self.ALERT_ACCENT, "#000000", 0.80)
        self.primary.configure(
            bg=self.ALERT_ACCENT, activebackground=self.ALERT_ACCENT,
            fg=alert_fg, activeforeground=alert_fg)
        self.header_dot.itemconfigure(self.header_dot_item,
                                      fill=self.ALERT_ACCENT)
        self.full_frame.configure(bg=self.alert_border)

    def _restore_style(self):
        self.primary.configure(
            bg=self.accent, activebackground=self.accent,
            fg=self._primary_fg, activeforeground=self._primary_fg)
        self.header_dot.itemconfigure(self.header_dot_item, fill=self.accent)
        self.full_frame.configure(bg=self.border)

    def _start_alert_breath(self):
        self._stop_alert_breath()
        if self._closed or self.stage != "alarm":
            return
        self._alert_ticks = 0
        self._alert_breath()

    def _stop_alert_breath(self):
        if self._alert_job is not None:
            try:
                self.window.after_cancel(self._alert_job)
            except Exception:
                pass
            self._alert_job = None
        if self.stage != "alarm" and not self._closed:
            try:
                self.full_frame.configure(bg=self.border)
            except Exception:
                pass

    def _alert_breath(self):
        self._alert_job = None
        if self._closed or self.stage != "alarm" or self.collapsed:
            return
        self._alert_ticks += 1
        if self._alert_ticks <= 40:
            wave = 0.5 + 0.5 * math.sin(self._alert_ticks / 2.2)
            color = blend_color(self.border, self.alert_border,
                                0.35 + 0.65 * wave)
            self.full_frame.configure(bg=color)
            self._alert_job = self.window.after(60, self._alert_breath)
        else:
            self.full_frame.configure(bg=self.alert_border)

    def _stage_accent(self):
        return self.ALERT_ACCENT if self.stage == "alarm" else self.accent

    def _update_animation_speed(self):
        if self.stage == "alarm":
            self._speed, self._interval = 0.05, 60
        elif self.stage == "early":
            self._speed, self._interval = 0.03, 90
        else:
            self._speed, self._interval = 0.014, 130

    def _schedule_auto_collapse(self):
        self._cancel_auto_collapse()
        if (self.persistent and self.stage in ("scheduled", "early")
                and not self.collapsed and not self._closed):
            self._auto_collapse_job = self.window.after(
                self.AUTO_COLLAPSE_MS, self._auto_collapse)

    def _cancel_auto_collapse(self):
        if self._auto_collapse_job is not None:
            try:
                self.window.after_cancel(self._auto_collapse_job)
            except Exception:
                pass
            self._auto_collapse_job = None

    def _auto_collapse(self):
        self._auto_collapse_job = None
        if self._closed or self.collapsed or self.stage not in ("scheduled", "early"):
            return
        self.collapse()

    def _start_animation(self):
        if self._animation_job is None and not self._closed and not self.collapsed:
            self._animate()

    def _stop_animation(self):
        if self._animation_job is not None:
            try:
                self.window.after_cancel(self._animation_job)
            except Exception:
                pass
            self._animation_job = None

    def _start_tab_animation(self):
        if self._tab_job is None and self.collapsed and not self._closed:
            self._animate_tab()

    def _stop_tab_animation(self):
        if self._tab_job is not None:
            try:
                self.window.after_cancel(self._tab_job)
            except Exception:
                pass
            self._tab_job = None

    def _animate_tab(self):
        self._tab_job = None
        if self._closed or not self.collapsed:
            return
        self._tab_phase = (self._tab_phase + 0.03) % 1.0
        self._render_handle()
        self._tab_job = self.window.after(90, self._animate_tab)

    def _animate(self):
        self._animation_job = None
        if self._closed or self.collapsed:
            return
        canvas = self.canvas
        canvas.delete("all")
        now = datetime.now()
        clock = f"{now:%Y-%m-%d %H:%M:%S}"
        if clock != self._clock_text:
            self._clock_text = clock
            self.clock_label.configure(text=clock)
        cx, cy = self._canvas_w / 2, self._canvas_h / 2
        self._phase = (self._phase + self._speed) % 1.0
        for index in range(3):
            phase = (self._phase + index / 3.0) % 1.0
            # 半径 36–66：圆心在 72，最大 66 让环直径到 132（原来只有 92）。
            radius = self._s(36 + phase * 30)
            weight = max(0.08, 0.56 * (1.0 - phase))
            outline = blend_color(self.card, self._stage_accent(), weight)
            canvas.create_oval(cx - radius, cy - radius, cx + radius,
                               cy + radius, outline=outline, width=2)
        canvas.create_oval(cx - self._s(32), cy - self._s(32),
                           cx + self._s(32), cy + self._s(32),
                           fill=self._stage_accent(), outline="")
        if self.target is None:
            text = "休息"
            font = ("Segoe UI", 12, "bold")
        else:
            remaining = max(0, int((self.target - now).total_seconds()))
            text = format_seconds(remaining)
            size = 18
            if len(text) >= 4:
                size = 16
            if len(text) >= 5:
                size = 15
            if len(text) >= 6:
                size = 13
            if len(text) >= 7:
                size = 12
            if len(text) >= 8:
                size = 11
            font = ("Consolas", size, "bold")
        # 目标时间在详情行（"响铃 2026年11月18日 周三"），当前时间在头部时钟，
        # 画布底部原来那行两者都不是，纯占高度，压高度时先删掉。
        canvas.create_text(cx, cy, text=text, fill=self.bg, font=font)
        self._animation_job = self.window.after(self._interval, self._animate)

    def apply(self, stage, target, collapsed=False):
        """Switch stage on a live card, auto-expanding it when collapsed."""
        if self._closed:
            return
        self._set_stage(stage, target)
        if collapsed:
            self._cancel_auto_collapse()
            if not self.collapsed:
                self.collapse()
            else:
                self._render_handle()
                self._start_tab_animation()
            return
        if stage == "idle":
            self._cancel_auto_collapse()
            if not self.collapsed:
                self.collapse()
            else:
                self._render_handle()
                self._start_tab_animation()
            return
        if self.collapsed:
            self.expand()
        else:
            self._resize_to_content()
            self._start_animation()
        if stage == "alarm":
            self.window.focus_force()
        self.window.lift()
        if stage in ("scheduled", "early"):
            self._schedule_auto_collapse()
        else:
            self._cancel_auto_collapse()

    def collapse(self):
        """Shrink to a small tab hugging the right screen edge."""
        if self._closed or self.collapsed:
            return
        self._cancel_slide()
        self._cancel_auto_collapse()
        self._stop_animation()
        self.full_frame.pack_forget()
        self.handle_frame.pack(fill="both", expand=True)
        self.collapsed = True
        self._update_countdown()
        self._start_tab_animation()
        self.window.update_idletasks()
        if self._custom_position:
            x, y = self._clamp_to_screen(
                self._current_x, self._target_y,
                self._handle_w, self._handle_h)
            self._current_x, self._target_y = x, y
            self.window.geometry(
                f"{self._handle_w}x{self._handle_h}+{x}+{y}")
        else:
            self.window.geometry(
                f"{self._handle_w}x{self._handle_h}+"
                f"{self._current_x}+{self._target_y}")
            mx, _my, mw, _mh = self._monitor_work_area()
            self._animate_x(
                max(mx, mx + mw - self._handle_w - self._s(self.ORB_MARGIN)))

    def expand(self):
        """Restore the full card and slide it back to the top-right margin."""
        if self._closed:
            return
        self._stop_tab_animation()
        if not self.collapsed:
            self._resize_to_content()
            return
        self._cancel_slide()
        self._cancel_auto_collapse()
        self.handle_frame.pack_forget()
        self.full_frame.pack(fill="both", expand=True)
        self.collapsed = False
        self._handle_hover = False
        self.window.update_idletasks()
        width = max(self._s(self.WIDTH), self.window.winfo_reqwidth())
        height = self.window.winfo_reqheight()
        if self._custom_position:
            x, y = self._clamp_to_screen(
                self._current_x, self._target_y, width, height)
            self._current_x, self._target_y = x, y
            self.window.geometry(f"{width}x{height}+{x}+{y}")
        else:
            self.window.geometry(
                f"{width}x{height}+{self._current_x}+{self._target_y}")
            mx, _my, mw, _mh = self._monitor_work_area()
            self._animate_x(
                max(mx, mx + mw - width - self._s(self.MARGIN)))
        self._start_animation()

    def _resize_to_content(self):
        if self._closed or self.collapsed:
            return
        self.window.update_idletasks()
        width = max(self._s(self.WIDTH), self.window.winfo_reqwidth())
        height = self.window.winfo_reqheight()
        if self._custom_position:
            x, y = self._clamp_to_screen(
                self._current_x, self._target_y, width, height)
            self._current_x, self._target_y = x, y
        else:
            mx, _my, mw, _mh = self._monitor_work_area()
            self._current_x = max(
                mx, mx + mw - width - self._s(self.MARGIN))
        self.window.geometry(f"{width}x{height}+{self._current_x}+{self._target_y}")

    def _animate_x(self, target_x):
        self._cancel_slide()

        def step():
            self._slide_job = None
            if self._closed:
                return
            distance = target_x - self._current_x
            if abs(distance) <= 28:
                self._current_x = target_x
                self.window.geometry(f"+{target_x}+{self._target_y}")
                return
            self._current_x += 28 if distance > 0 else -28
            self.window.geometry(f"+{self._current_x}+{self._target_y}")
            self._slide_job = self.window.after(16, step)

        step()

    def _cancel_slide(self):
        if self._slide_job is not None:
            try:
                self.window.after_cancel(self._slide_job)
            except Exception:
                pass
            self._slide_job = None

    def _update_countdown(self):
        if self.collapsed:
            self._render_handle()

    @staticmethod
    def _rgb(color):
        return tuple(int(color[index:index + 2], 16) for index in (1, 3, 5))

    def _build_orb_photo(self):
        from PIL import Image, ImageTk

        size = self._handle_w
        radius = self._s(self.ORB_RADIUS)
        center = size / 2
        corner = self.ORB_KEY if self._orb_transparent else self.card
        image = Image.new("RGB", (size, size), corner)
        pixels = image.load()
        accent = self._rgb(self.accent)
        for y in range(size):
            for x in range(size):
                dx = (x + 0.5 - center) / radius
                dy = (y + 0.5 - center) / radius
                distance = dx * dx + dy * dy
                if distance > 1.0:
                    continue
                nz = (1.0 - distance) ** 0.5
                light = max(0.0, -dx * 0.45 - dy * 0.55 + nz * 0.75)
                shade = 0.55 + 0.45 * light
                spec = max(0.0, light - 0.82) ** 2 * 150
                rgb = [min(255, int(channel * shade + spec))
                       for channel in accent]
                rim = max(0.0, 1.0 - nz) * 0.28
                rgb = [max(0, int(channel * (1.0 - rim))) for channel in rgb]
                pixels[x, y] = tuple(rgb)
        self._orb_photo = ImageTk.PhotoImage(image, master=self.window)

    def _render_handle(self):
        canvas = self.handle_frame
        canvas.delete("overlay")
        center = self._handle_w / 2
        base = self.accent if self._handle_hover else blend_color(
            self.accent, self.card, 0.55)
        for index in range(3):
            phase = (self._tab_phase + index / 3.0) % 1.0
            radius = self._s(34 + phase * 15)
            color = blend_color(base, self.card, 0.12 + phase * 0.55)
            canvas.create_oval(
                center - radius, center - radius, center + radius,
                center + radius, outline=color,
                width=2 if phase < 0.45 else 1, tags="overlay")
        if self.target is None:
            text = "休息"
            font = ("Segoe UI", 12, "bold")
        else:
            remaining = max(0, int((self.target - datetime.now()).total_seconds()))
            text = format_seconds(remaining)
            size = 16
            if len(text) > 4:
                size = 14
            if len(text) > 5:
                size = 12
            if len(text) > 6:
                size = 10
            if len(text) > 7:
                size = 8
            font = ("Consolas", size, "bold")
        canvas.create_text(center, center, text=text, fill=self.bg,
                           font=font, tags="overlay")

    def _handle_enter(self, _event=None):
        self._handle_hover = True
        if self.collapsed:
            self._render_handle()

    def _handle_leave(self, _event=None):
        self._handle_hover = False
        if self.collapsed:
            self._render_handle()

    def _start_countdown(self):
        if self._count_job is None and not self._closed:
            self._count_job = self.window.after(1000, self._tick_countdown)

    def _cancel_countdown(self):
        if self._count_job is not None:
            try:
                self.window.after_cancel(self._count_job)
            except Exception:
                pass
            self._count_job = None

    def _tick_countdown(self):
        self._count_job = None
        if self._closed:
            return
        self._update_countdown()
        self._count_job = self.window.after(1000, self._tick_countdown)

    def _cancel_sound(self):
        if self._sound_job is not None:
            try:
                self.window.after_cancel(self._sound_job)
            except Exception:
                pass
            self._sound_job = None

    def _ring(self):
        if self._closed or self.stage != "alarm":
            return
        try:
            self.window.bell()
        except Exception:
            pass
        self._sound_job = self.window.after(2200, self._ring)

    def _handle_click(self, _event=None):
        if self.stage == "idle" and self.on_show_clipboard is not None:
            try:
                self.on_show_clipboard()
            except Exception:
                pass
            return "break"
        self.expand()
        return "break"

    def _handle_press(self, event):
        self._cancel_slide()
        self._handle_drag_origin = (event.x_root - self._current_x,
                                    event.y_root - self._target_y)
        self._handle_dragged = False
        return "break"

    def _handle_drag(self, event):
        if self._handle_drag_origin is None:
            return "break"
        dx, dy = self._handle_drag_origin
        x = event.x_root - dx
        y = event.y_root - dy
        if (abs(x - self._current_x) > 2
                or abs(y - self._target_y) > 2):
            self._handle_dragged = True
            self._custom_position = True
        if not self._handle_dragged:
            return "break"
        x, y = self._clamp_to_screen(
            x, y, self._handle_w, self._handle_h)
        self._current_x, self._target_y = x, y
        self.window.geometry(
            f"{self._handle_w}x{self._handle_h}+{x}+{y}")
        return "break"

    def _handle_release(self, _event=None):
        moved = self._handle_dragged
        self._handle_drag_origin = None
        self._handle_dragged = False
        if not moved:
            if self.stage == "idle" and self.on_show_clipboard is not None:
                try:
                    self.on_show_clipboard()
                except Exception:
                    pass
            else:
                self.expand()
        return "break"

    def _clamp_to_screen(self, x, y, width, height):
        mx, my, mw, mh = self._monitor_work_area()
        return (min(max(mx, int(x)), max(mx, mx + mw - width)),
                min(max(my, int(y)), max(my, my + mh - height)))

    def _enable_drag(self):
        self.window.bind("<Button-1>", self._drag_start)
        self.window.bind("<B1-Motion>", self._drag_move)
        self.window.bind("<ButtonRelease-1>", self._drag_end)

    def _drag_start(self, event):
        if self.collapsed:
            return
        self._cancel_slide()
        self._cancel_auto_collapse()
        self._drag_origin = (event.x_root - self._current_x,
                             event.y_root - self._target_y)
        self._drag_moved = False

    def _drag_move(self, event):
        if self._drag_origin is None:
            return
        x = event.x_root - self._drag_origin[0]
        y = event.y_root - self._drag_origin[1]
        if (abs(x - self._current_x) > 2
                or abs(y - self._target_y) > 2):
            self._drag_moved = True
            self._custom_position = True
        if not self._drag_moved:
            return
        x, y = self._clamp_to_screen(
            x, y, self.window.winfo_width(), self.window.winfo_height())
        self._current_x, self._target_y = x, y
        self.window.geometry(f"+{x}+{y}")

    def _drag_end(self, _event=None):
        self._drag_origin = None
        self._drag_moved = False
        self._schedule_auto_collapse()

    def _open_settings(self, _event=None):
        if self.on_open_settings is None:
            return "break"
        try:
            self.on_open_settings()
        except Exception:
            pass
        if self.persistent and not self.collapsed:
            self.collapse()
        return "break"

    def snooze_minutes(self):
        """稍后提醒的分钟数：读配置并夹到 1–60。"""
        try:
            value = int(self.config.get("alarm_snooze_minutes",
                                         self.SNOOZE_DEFAULT))
        except (TypeError, ValueError):
            value = self.SNOOZE_DEFAULT
        return max(1, min(60, value))

    def _fit_caption(self, text):
        """标题栏闹钟名最长 CAPTION_MAX 个字符，超出加省略号。"""
        text = str(text or "")
        if len(text) <= self.CAPTION_MAX:
            return text
        return text[: self.CAPTION_MAX - 1] + "…"

    def _dismiss_view(self, _event=None):
        """``×`` 与 Esc：收起弹框，而不是把闹钟永久确认掉。

        ``alarm`` 阶段按「稍后提醒」处理——原来这里直接走 ``_ack``，一次误点
        这次闹钟就再也不会响了。其余阶段收起即可，与原先行为一致。
        """
        if self._closed:
            return "break"
        if not self.persistent:
            self._ack()
            return "break"
        if self.stage == "alarm":
            self._snooze(self.snooze_minutes())
            return "break"
        self.collapse()
        return "break"

    def _invoke_primary(self):
        try:
            self.primary.invoke()
        except Exception:
            pass
        return "break"

    def _snooze_from_key(self):
        if self.stage in ("alarm", "early"):
            self._snooze(self.snooze_minutes())
        return "break"

    def _menu_theme(self):
        """FlatMenu 的配色：与设置页共用 menu_theme。"""
        return menu_theme((self.bg, self.card, self.accent, self.fg))

    def _snooze_menu(self, event):
        """右键稍后提醒按钮：直接选 1/3/5/10/15/30 分钟。"""
        if self._closed:
            return "break"
        try:
            from .panel_menu import FlatMenu
            items = [(f"{minutes} 分钟后提醒",
                      (lambda value=minutes: self._snooze(value)))
                     for minutes in SNOOZE_MINUTES]
            menu = FlatMenu(self.tk, self.window, items,
                            theme=self._menu_theme())
            menu.popup(event.x_root, event.y_root)
        except Exception:
            traceback.print_exc()
        return "break"

    def _ack(self):
        if self.collapsed:
            self.expand()
            return
        if not self.persistent:
            stage = self.stage
            self.destroy_without_callback()
            self.on_acknowledge(stage)
            return
        if self.stage == "alarm":
            self.on_acknowledge("alarm")
            return
        self.collapse()

    def _snooze(self, minutes):
        stage = self.stage
        if not self.persistent:
            self.destroy_without_callback()
        self.on_snooze(stage, minutes)

    def alive(self):
        if self._closed:
            return False
        try:
            return bool(self.window.winfo_exists())
        except Exception:
            return False

    def destroy_without_callback(self):
        if self._closed:
            return
        self._closed = True
        for job in (self._sound_job, self._slide_job, self._count_job,
                    self._animation_job, self._auto_collapse_job,
                    self._tab_job, self._alert_job):
            if job is not None:
                try:
                    self.window.after_cancel(job)
                except Exception:
                    pass
        try:
            self.window.destroy()
        except Exception:
            pass


class DateTimePicker:
    """闹钟日期时间控件：只读展示框 + 日历弹层，覆盖年月日时分秒。"""

    def __init__(self, parent, date_var, time_var, palette):
        import tkinter as tk

        _bg, card, accent, fg = palette
        self.tk = tk
        self.palette = palette
        self.date_var = date_var
        self.time_var = time_var
        self._popup = None
        self.frame = tk.Frame(parent, bg=card)
        self.display_var = tk.StringVar(master=parent, value="")
        self.entry = tk.Entry(
            self.frame, textvariable=self.display_var, state="readonly",
            relief="flat", bd=0, fg=fg, readonlybackground="#171625",
            disabledbackground="#201d30", disabledforeground="#8a869c",
            insertbackground=fg, highlightthickness=1,
            highlightbackground="#45405c", highlightcolor=accent,
            font=("Segoe UI", 10))
        self.entry.pack(side="left", fill="x", expand=True, ipady=3)
        self.entry.configure(cursor="hand2")
        self.entry.bind("<Button-1>", self._open_from_entry)
        self.entry.bind("<Return>", self._open_from_entry)
        self.button = tk.Button(
            self.frame, text="📅 选择", command=self.open_picker, relief="flat",
            bd=0, bg="#37334d", fg=fg, padx=8, pady=1, cursor="hand2",
            activebackground="#47415f", activeforeground=fg,
            disabledforeground="#77738c", font=("Segoe UI", 9))
        self.button.pack(side="left", padx=(6, 0))
        date_var.trace_add("write", self._refresh)
        time_var.trace_add("write", self._refresh)
        self._refresh()

    def value(self):
        """Return the selected datetime; raise ValueError when the field is invalid."""
        day = datetime.strptime(str(self.date_var.get()).strip(), DATE_FORMAT).date()
        return datetime.combine(day, parse_alarm_time(self.time_var.get()))

    def set_value(self, moment):
        """Write ``moment`` back into the shared date/time variables."""
        self.date_var.set(moment.strftime(DATE_FORMAT))
        self.time_var.set(moment.strftime("%H:%M:%S"))

    def _refresh(self, *_args):
        try:
            text = self.value().strftime(DATETIME_FORMAT)
        except (TypeError, ValueError):
            text = f"{self.date_var.get()} {self.time_var.get()}".strip()
        self.display_var.set(text)

    def _open_from_entry(self, _event=None):
        if str(self.entry.cget("state")) == "disabled":
            return None
        self.open_picker()
        return "break"

    def open_picker(self):
        if str(self.entry.cget("state")) == "disabled":
            return
        popup = self._popup
        if popup is not None:
            try:
                if popup.window.winfo_exists():
                    popup.window.lift()
                    popup.window.focus_force()
                    return
            except Exception:
                self._popup = None
        try:
            initial = self.value()
        except (TypeError, ValueError):
            initial = (datetime.now() + timedelta(minutes=5)).replace(microsecond=0)
        self._popup = CalendarTimePopup(
            self.frame.winfo_toplevel(), initial, self.palette,
            self.set_value, anchor=self.entry)


class CalendarTimePopup:
    """日历弹层：年月日 + 时分秒联动，确认后回写到设置页。"""

    WEEKDAYS = ("一", "二", "三", "四", "五", "六", "日")

    def __init__(self, parent, initial, palette, on_confirm, anchor=None):
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.parent = parent
        self.on_confirm = on_confirm
        bg, card, accent, fg = palette
        self.bg, self.card, self.accent, self.fg = bg, card, accent, fg
        self._closed = False
        self.selected_date = initial.date()
        self.window = tk.Toplevel(parent)
        self.window.title("选择日期与时间")
        self.window.configure(bg=bg)
        self.window.resizable(False, False)
        self.window.transient(parent)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.window.bind("<Escape>", lambda _event: self.close())
        shell = tk.Frame(self.window, bg=card, highlightthickness=1,
                         highlightbackground="#45405c")
        shell.pack(fill="both", expand=True, padx=1, pady=1)
        body = tk.Frame(shell, bg=card)
        body.pack(fill="both", expand=True, padx=14, pady=10)
        self.year_var = tk.StringVar(master=self.window, value=str(initial.year))
        self.month_var = tk.StringVar(master=self.window, value=str(initial.month))
        self.hour_var = tk.StringVar(master=self.window, value=f"{initial.hour:02d}")
        self.minute_var = tk.StringVar(master=self.window, value=f"{initial.minute:02d}")
        self.second_var = tk.StringVar(master=self.window, value=f"{initial.second:02d}")
        header = tk.Frame(body, bg=card)
        header.pack(fill="x")
        header.columnconfigure(0, weight=1)
        header.columnconfigure(2, weight=1)
        tk.Button(
            header, text="‹", command=lambda: self._shift(-1), relief="flat",
            bd=0, bg=card, fg=fg, padx=6, pady=0, cursor="hand2",
            activebackground="#39344f", activeforeground=accent,
            font=("Segoe UI", 12)).grid(row=0, column=0, sticky="w")
        tk.Button(
            header, text="›", command=lambda: self._shift(1), relief="flat",
            bd=0, bg=card, fg=fg, padx=6, pady=0, cursor="hand2",
            activebackground="#39344f", activeforeground=accent,
            font=("Segoe UI", 12)).grid(row=0, column=2, sticky="e")
        title = tk.Frame(header, bg=card)
        title.grid(row=0, column=1)
        self.year_box = tk.Spinbox(
            title, from_=1970, to=2100, textvariable=self.year_var, width=5,
            command=self._on_year, relief="flat", bd=0, bg="#171625", fg=fg,
            insertbackground=fg, buttonbackground="#262238", justify="center",
            highlightthickness=1, highlightbackground="#45405c",
            highlightcolor=accent, font=("Segoe UI", 10))
        self.year_box.pack(side="left")
        self.year_box.bind("<Return>", self._on_year)
        tk.Label(title, text="年", bg=card, fg="#c5c1d4",
                 font=("Segoe UI", 10)).pack(side="left", padx=(2, 8))
        self.month_box = ttk.Combobox(
            title, textvariable=self.month_var, width=3, state="readonly",
            values=tuple(str(month) for month in range(1, 13)))
        self.month_box.pack(side="left")
        self.month_box.bind("<<ComboboxSelected>>", self._on_month)
        tk.Label(title, text="月", bg=card, fg="#c5c1d4",
                 font=("Segoe UI", 10)).pack(side="left", padx=(2, 0))
        week = tk.Frame(body, bg=card)
        week.pack(fill="x", pady=(8, 2))
        for column, caption in enumerate(self.WEEKDAYS):
            tk.Label(week, text=caption, width=3, bg=card, fg="#8d88a3",
                     font=("Segoe UI", 9)).grid(row=0, column=column, padx=1)
        self.grid = tk.Frame(body, bg=card)
        self.grid.pack(fill="x")
        time_line = tk.Frame(body, bg=card)
        time_line.pack(fill="x", pady=(8, 4))
        tk.Label(time_line, text="时间", bg=card, fg="#c5c1d4",
                 font=("Segoe UI", 9)).pack(side="left")
        for var, unit, values in (
                (self.hour_var, "时", tuple(f"{hour:02d}" for hour in range(24))),
                (self.minute_var, "分", tuple(f"{minute:02d}" for minute in range(60))),
                (self.second_var, "秒", tuple(f"{second:02d}" for second in range(60)))):
            box = ttk.Combobox(time_line, textvariable=var, values=values,
                               width=3, state="readonly")
            box.pack(side="left", padx=(6, 0))
            tk.Label(time_line, text=unit, bg=card, fg="#c5c1d4",
                     font=("Segoe UI", 9)).pack(side="left", padx=(1, 0))
        tk.Button(
            time_line, text="此刻", command=self._set_now, relief="flat", bd=0,
            bg="#37334d", fg=fg, padx=8, pady=0, cursor="hand2",
            activebackground="#47415f", activeforeground=fg,
            font=("Segoe UI", 9)).pack(side="right")
        footer = tk.Frame(body, bg=card)
        footer.pack(fill="x", pady=(10, 0))
        tk.Button(
            footer, text="确定", command=self._confirm, relief="flat", bd=0,
            bg=accent, fg="#171521", padx=18, pady=6, cursor="hand2",
            activebackground=accent, activeforeground="#171521",
            font=("Segoe UI", 9, "bold")).pack(side="right")
        tk.Button(
            footer, text="取消", command=self.close, relief="flat", bd=0,
            bg=card, fg=fg, padx=14, pady=6, cursor="hand2",
            activebackground="#39344f", activeforeground=fg,
            font=("Segoe UI", 9)).pack(side="right", padx=(0, 6))
        self._render()
        self._place(anchor)
        try:
            self.window.grab_set()
        except Exception:
            pass
        self.window.focus_force()

    def _view(self):
        try:
            year = int(str(self.year_var.get()).strip())
        except (TypeError, ValueError):
            year = self.selected_date.year
        try:
            month = int(str(self.month_var.get()).strip())
        except (TypeError, ValueError):
            month = self.selected_date.month
        return min(2100, max(1970, year)), min(12, max(1, month))

    def _render(self):
        tk = self.tk
        year, month = self._view()
        self.year_var.set(str(year))
        self.month_var.set(str(month))
        if (self.selected_date.year, self.selected_date.month) != (year, month):
            self.selected_date = date(
                year, month, min(self.selected_date.day,
                                 calendar.monthrange(year, month)[1]))
        for child in self.grid.winfo_children():
            child.destroy()
        today = date.today()
        for index, cell in enumerate(month_grid(year, month)):
            row, column = divmod(index, 7)
            if cell.month != month:
                tk.Label(self.grid, text=str(cell.day), width=3, bg=self.card,
                         fg="#5d5873", font=("Segoe UI", 9)).grid(
                             row=row, column=column, padx=1, pady=1)
                continue
            selected = cell == self.selected_date
            background = self.accent if selected else self.card
            foreground = "#171521" if selected else (
                "#ffffff" if cell == today else self.fg)
            button = tk.Button(
                self.grid, text=str(cell.day), width=3, relief="flat", bd=0,
                bg=background, fg=foreground, cursor="hand2",
                activebackground="#47415f", activeforeground=self.fg,
                highlightthickness=0,
                font=("Segoe UI", 9,
                      "bold" if selected or cell == today else "normal"),
                command=lambda chosen=cell: self._choose_day(chosen))
            button.grid(row=row, column=column, padx=1, pady=1)
            button.bind("<Double-Button-1>", lambda _event: self._confirm())
            if not selected:
                button.bind(
                    "<Enter>",
                    lambda _event, widget=button: widget.configure(bg="#39344f"))
                button.bind(
                    "<Leave>",
                    lambda _event, widget=button: widget.configure(bg=self.card))

    def _shift(self, delta):
        year, month = self._view()
        month += delta
        if month < 1:
            month, year = 12, max(1970, year - 1)
        elif month > 12:
            month, year = 1, min(2100, year + 1)
        self.year_var.set(str(year))
        self.month_var.set(str(month))
        self._render()

    def _on_year(self, _event=None):
        self._render()

    def _on_month(self, _event=None):
        self._render()

    def _choose_day(self, chosen):
        self.selected_date = chosen
        self._render()

    def _set_now(self):
        moment = datetime.now().replace(microsecond=0)
        self.selected_date = moment.date()
        self.year_var.set(str(moment.year))
        self.month_var.set(str(moment.month))
        self.hour_var.set(f"{moment.hour:02d}")
        self.minute_var.set(f"{moment.minute:02d}")
        self.second_var.set(f"{moment.second:02d}")
        self._render()

    def _confirm(self):
        year, month = self._view()
        day = min(self.selected_date.day, calendar.monthrange(year, month)[1])
        try:
            moment = datetime(year, month, day, int(self.hour_var.get()),
                              int(self.minute_var.get()), int(self.second_var.get()))
        except (TypeError, ValueError):
            moment = datetime(year, month, day)
        self.on_confirm(moment)
        self.close()

    def _place(self, anchor):
        self.window.update_idletasks()
        width = self.window.winfo_reqwidth()
        height = self.window.winfo_reqheight()
        if anchor is not None:
            x = anchor.winfo_rootx()
            y = anchor.winfo_rooty() + anchor.winfo_height() + 6
        else:
            x = self.parent.winfo_rootx() + 40
            y = self.parent.winfo_rooty() + 120
        x = min(max(0, x), max(0, self.window.winfo_screenwidth() - width))
        y = min(max(0, y), max(0, self.window.winfo_screenheight() - height))
        self.window.geometry(f"+{x}+{y}")

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.window.grab_release()
        except Exception:
            pass
        try:
            self.window.destroy()
        except Exception:
            pass


class AlarmSettingsWindow:
    """独立闹钟偏好窗口；实例只持有 Tk 窗口，不耦合历史列表逻辑。"""

    def __init__(self, parent, config, on_save):
        import tkinter as tk

        self.tk = tk
        self.parent = parent
        self.config = config
        self.on_save = on_save
        self._closed = False
        self.window = tk.Toplevel(parent)
        self.window.title("Lumina · 闹钟设置")
        self._dpi = detect_dpi(self.window)
        self.window.geometry(f"{int(round(520 * self._dpi))}x"
                             f"{int(round(500 * self._dpi))}")
        self.window.resizable(False, False)
        try:
            if parent.winfo_viewable():
                self.window.transient(parent)
            else:
                self.window.attributes("-topmost", True)
        except Exception:
            pass
        bg, card, accent, fg = PALETTES.get(
            config.get("alarm_style", "neon"), PALETTES["neon"])
        self.bg, self.card, self.accent, self.fg = bg, card, accent, fg
        self.window.configure(bg=bg)
        # 颜色一律从 palette 派生，切 neon / soft / minimal 都不跑偏。
        self.muted = blend_color(fg, bg, 0.45)
        self.field_bg = blend_color(card, "#000000", 0.45)
        self.hairline = blend_color(card, accent, 0.30)
        self.warn = AlarmAlertWindow.ALERT_ACCENT
        self._dirty = False
        self._preview = None
        outer = tk.Frame(self.window, bg=bg)
        outer.pack(fill="both", expand=True, padx=self._s(20), pady=self._s(18))
        tk.Label(outer, text="⏰  闹钟", bg=bg, fg=fg,
                 font=("Microsoft YaHei UI", 20, "bold")).pack(anchor="w")
        tk.Label(outer, text="提前准备，准时提醒。设置将保存在本机。",
                 bg=bg, fg=self.muted,
                 font=("Microsoft YaHei UI", 10)).pack(
                     anchor="w", pady=(self._s(4), self._s(15)))
        card_frame = tk.Frame(outer, bg=card, highlightthickness=1,
                              highlightbackground=self.hairline)
        card_frame.pack(fill="x")
        self.enabled_var = tk.BooleanVar(
            master=parent, value=bool(config.get("alarm_enabled", False)))
        self.label_var = tk.StringVar(
            master=parent, value=str(config.get("alarm_label", "闹钟")))
        self.date_var = tk.StringVar(
            master=parent, value=str(config.get(
                "alarm_date", datetime.now().strftime("%Y-%m-%d"))))
        self.time_var = tk.StringVar(
            master=parent, value=str(config.get("alarm_time", "09:00")))
        self.lead_var = tk.StringVar(
            master=parent, value=str(config.get("alarm_lead_minutes", 5)))
        self.repeat_var = tk.StringVar(
            master=parent, value=REPEAT_LABELS.get(
                config.get("alarm_repeat", "once"), "单次"))
        self.style_var = tk.StringVar(
            master=parent, value=STYLE_LABELS.get(
                config.get("alarm_style", "neon"), "霓虹光环"))
        self.sound_var = tk.BooleanVar(
            master=parent, value=bool(config.get("alarm_sound", True)))
        self.snooze_var = tk.StringVar(
            master=parent, value=str(config.get(
                "alarm_snooze_minutes", AlarmAlertWindow.SNOOZE_DEFAULT)))

        def row(label, widget_factory):
            line = tk.Frame(card_frame, bg=card)
            line.pack(fill="x", padx=self._s(16), pady=self._s(8))
            tk.Label(line, text=label, width=13, anchor="w", bg=card,
                     fg=blend_color(fg, card, 0.30),
                     font=("Microsoft YaHei UI", 10)).pack(side="left")
            widget = widget_factory(line)
            widget.pack(side="left", fill="x", expand=True)
            return widget

        def make_entry(var):
            return lambda _line: tk.Entry(
                _line, textvariable=var, relief="flat", bd=0,
                bg=self.field_bg, fg=fg, insertbackground=fg,
                highlightthickness=1,
                highlightbackground=blend_color(card, accent, 0.35),
                highlightcolor=accent, font=("Microsoft YaHei UI", 10))

        self.label_entry = row("闹钟名称", make_entry(self.label_var))
        self.datetime_picker = None

        def make_picker(line):
            self.datetime_picker = DateTimePicker(
                line, self.date_var, self.time_var, (bg, card, accent, fg))
            return self.datetime_picker.frame

        row("日期与时间", make_picker)
        self.date_entry = self.datetime_picker.entry
        palette = (bg, card, accent, fg)
        row("重复", lambda line: PickField(
            line, self.repeat_var, tuple(REPEAT_VALUES), palette,
            width=14, dpi=self._dpi))
        row("提前提醒（分钟）", lambda line: PickField(
            line, self.lead_var,
            tuple(str(v) for v in LEAD_MINUTES), palette, width=10, dpi=self._dpi))
        row("稍后提醒（分钟）", lambda line: PickField(
            line, self.snooze_var,
            tuple(str(v) for v in SNOOZE_MINUTES), palette, width=10, dpi=self._dpi))
        row("桌面效果", lambda line: PickField(
            line, self.style_var, tuple(STYLE_VALUES), palette, width=14, dpi=self._dpi))
        self.repeat_var.trace_add("write", self._repeat_changed)
        self._repeat_changed()
        sound_line = tk.Frame(outer, bg=bg)
        sound_line.pack(fill="x", pady=(self._s(12), self._s(4)))
        check_style = {"bg": bg, "fg": fg, "activebackground": bg,
                       "activeforeground": fg, "selectcolor": card,
                       "highlightthickness": 0,
                       "font": ("Microsoft YaHei UI", 10)}
        tk.Checkbutton(sound_line, text="到点播放提示音",
                       variable=self.sound_var, **check_style).pack(side="left")
        tk.Checkbutton(sound_line, text="启用闹钟",
                       variable=self.enabled_var, **check_style).pack(side="right")
        self.status = tk.Label(outer, text="", bg=bg, fg=self.muted,
                               anchor="w", font=("Microsoft YaHei UI", 9))
        self.status.pack(fill="x", pady=(self._s(3), self._s(10)))
        buttons = tk.Frame(outer, bg=bg)
        buttons.pack(fill="x", side="bottom")
        self.preview_button = PillButton(
            buttons, text="预览提醒", command=self.preview, bg=card, fg=fg,
            hover=blend_color(card, accent, 0.25),
            pressed=blend_color(card, accent, 0.35), padx=16, pady=8,
            dpi=self._dpi, font=("Microsoft YaHei UI", 10))
        self.preview_button.pack(side="left")
        self.save_button = PillButton(
            buttons, text="保存设置", command=self.save, bg=accent,
            fg=blend_color(card, "#000000", 0.82),
            hover=blend_color(accent, "#ffffff", 0.18),
            pressed=blend_color(accent, "#000000", 0.10), padx=20, pady=8,
            dpi=self._dpi, font=("Microsoft YaHei UI", 10, "bold"))
        self.save_button.pack(side="right")
        for var in (self.label_var, self.date_var, self.time_var,
                    self.lead_var, self.repeat_var, self.style_var,
                    self.sound_var, self.snooze_var, self.enabled_var):
            var.trace_add("write", self._on_form_changed)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.window.bind("<Escape>", lambda _event: self.close())
        self.window.bind("<Return>", lambda _event: self.save())
        self.window.update_idletasks()
        mx, my, mw, mh = monitor_work_area(self.window)
        width = min(max(int(round(520 * self._dpi)),
                        self.window.winfo_reqwidth()), mw)
        height = min(max(int(round(500 * self._dpi)),
                         self.window.winfo_reqheight()), mh)
        if parent.winfo_viewable():
            x = parent.winfo_rootx() + (parent.winfo_width() - width) // 2
            y = parent.winfo_rooty() + (parent.winfo_height() - height) // 2
        else:
            x = mx + (mw - width) // 2
            y = my + (mh - height) // 2
        x = min(max(mx, x), mx + mw - width)
        y = min(max(my, y), my + mh - height)
        self._target_geometry = (x, y, width, height)
        self.window.geometry(f"{width}x{height}+{x}+{y}")
        self.window.lift()
        self.window.focus_force()
        # 打开即把光标放进第一个输入框，改名字不用先点一下。
        self.label_entry.focus_set()
        self._update_status()

    def _s(self, value):
        """逻辑像素 → 当前 DPI 下的物理像素。"""
        return int(round(value * self._dpi))

    def _repeat_changed(self, *_args):
        repeat = REPEAT_VALUES.get(self.repeat_var.get(), "once")
        single = repeat == "once"
        try:
            self.date_entry.configure(state="readonly" if single else "disabled")
            self.datetime_picker.button.configure(
                state="normal" if single else "disabled")
        except Exception:
            pass

    def _snooze_value(self):
        try:
            value = int(self.snooze_var.get())
        except (TypeError, ValueError):
            return AlarmAlertWindow.SNOOZE_DEFAULT
        if value not in SNOOZE_MINUTES:
            return AlarmAlertWindow.SNOOZE_DEFAULT
        return value

    def _values(self):
        """表单 → config 字典；非法值抛带中文原因的 ValueError。"""
        label = self.label_var.get().strip() or "闹钟"
        try:
            moment = self.datetime_picker.value()
        except (TypeError, ValueError) as exc:
            raise ValueError("请选择有效的日期与时间") from exc
        try:
            lead = int(self.lead_var.get())
        except (TypeError, ValueError) as exc:
            raise ValueError("提前提醒必须是一个整数分钟") from exc
        if lead not in LEAD_MINUTES:
            raise ValueError("提前提醒时间不在可选范围内")
        repeat = REPEAT_VALUES.get(self.repeat_var.get())
        style = STYLE_VALUES.get(self.style_var.get())
        if repeat is None or style is None:
            raise ValueError("请选择有效的重复方式和桌面效果")
        values = {
            "alarm_enabled": bool(self.enabled_var.get()),
            "alarm_label": label,
            "alarm_date": moment.strftime(DATE_FORMAT),
            "alarm_time": moment.strftime("%H:%M:%S"),
            "alarm_lead_minutes": lead,
            "alarm_repeat": repeat,
            "alarm_style": style,
            "alarm_sound": bool(self.sound_var.get()),
            "alarm_snooze_minutes": self._snooze_value(),
        }
        if values["alarm_enabled"] and next_alarm_occurrence(values) is None:
            raise ValueError("闹钟时间已过去，请选择未来时间")
        return values

    def _on_form_changed(self, *_args):
        self._dirty = True
        self._update_status()

    def _update_status(self):
        """状态行跟着**表单当前值**走（原来读的是已保存的 config）。"""
        suffix = " · 未保存" if self._dirty else ""
        try:
            values = self._values()
        except (ValueError, TypeError, OSError) as exc:
            self.status.configure(text=f"设置无效：{exc}", fg=self.warn)
            return
        if not values["alarm_enabled"]:
            text = "闹钟已关闭"
        else:
            target = next_alarm_occurrence(values)
            if target is None:
                self.status.configure(text="时间无效或已过期，请调整后保存",
                                      fg=self.warn)
                return
            text = f"下次提醒 {target:%Y-%m-%d %H:%M:%S}"
        self.status.configure(text=text + suffix, fg=self.muted)

    def save(self):
        try:
            values = self._values()
        except (ValueError, TypeError, OSError) as exc:
            self.status.configure(text=f"设置无效：{exc}", fg=self.warn)
            return False
        try:
            self.on_save(values)
        except Exception as exc:  # 回调失败也要说人话，不弹英文堆栈
            self.status.configure(text=f"保存失败：{exc}", fg=self.warn)
            return False
        self.config.update(values)
        self._dirty = False
        self._update_status()
        return True

    def _close_preview(self):
        preview, self._preview = self._preview, None
        if preview is not None:
            try:
                preview.destroy_without_callback()
            except Exception:
                pass

    def preview(self):
        """预览卡片同一时间只留一张（原来连点会叠出好几张）。"""
        self._close_preview()
        preview_config = {
            "alarm_label": self.label_var.get().strip() or "闹钟预览",
            "alarm_style": STYLE_VALUES.get(self.style_var.get(), "neon"),
            "alarm_sound": False,
        }
        if not self.enabled_var.get():
            self._preview = AlarmAlertWindow(
                self.parent, preview_config, "idle", None,
                on_acknowledge=lambda _stage: None,
                on_snooze=lambda _stage, _minutes: None,
                on_open_settings=self.summon)
            return
        try:
            moment = self.datetime_picker.value()
        except (TypeError, ValueError):
            moment = (datetime.now() + timedelta(minutes=5)).replace(
                microsecond=0)
        target = next_alarm_occurrence({
            "alarm_enabled": True,
            "alarm_time": moment.strftime("%H:%M:%S"),
            "alarm_date": moment.strftime(DATE_FORMAT),
            "alarm_repeat": REPEAT_VALUES.get(self.repeat_var.get(), "once"),
        }) or moment
        self._preview = AlarmAlertWindow(
            self.parent, preview_config, "early", target,
            on_acknowledge=lambda _stage: None,
            on_snooze=lambda _stage, _minutes: None,
            on_open_settings=self.summon)

    def summon(self):
        if self._closed:
            return
        try:
            self.window.deiconify()
            self.window.lift()
            self.window.focus_force()
        except Exception:
            pass

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._close_preview()
        try:
            self.window.destroy()
        except Exception:
            pass
