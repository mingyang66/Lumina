"""自绘弹出菜单（从 history_panel 拆出）。

Tk 原生菜单挂在 overrideredirect 窗口上时，Windows 的菜单跟踪循环会因
激活/捕获状态异常立即取消（菜单未点击就消失），因此全项目统一用这里这个
自绘弹窗。
"""
import traceback

from . import clipboard_api


class FlatMenu:
    """轻量自绘弹出菜单。

    Tk 原生菜单挂在 overrideredirect 窗口上时，Windows 菜单跟踪循环会因
    激活/捕获状态异常立即取消（菜单未点击即消失），故统一用自绘弹窗：
    白底圆角边框 + flat 按钮；点击项/Esc/失焦关闭。
    """

    def __init__(self, tk, parent_win, items, steal_focus=False, theme=None):
        self.tk = tk
        self._closed = False
        self._on_close_cb = None
        self._focus_check = None
        self._outside_check = None
        self._steal_focus = steal_focus
        T = theme or {"hairline": "#c9cdd6", "card_bg": "#ffffff",
                      "separator": "#e3e5e8", "label": "#202124",
                      "accent": "#007AFF", "accent_soft": "#E5F0FF",
                      "fill_hover": "#DCDCE1"}
        self.win = tk.Toplevel(parent_win)
        self.win.overrideredirect(True)
        self.win.configure(bg=T["hairline"])
        frame = tk.Frame(self.win, bg=T["card_bg"])
        frame.pack(fill="both", expand=True, padx=1, pady=1)
        f = ("Microsoft YaHei UI", 10)
        for item in items:
            if item is None:
                tk.Frame(frame, bg=T["separator"], height=1).pack(
                    fill="x", padx=6, pady=3)
                continue
            label, cmd = item
            b = tk.Button(frame, text=label, anchor="w", relief="flat", bd=0,
                          font=f, bg=T["card_bg"], fg=T["label"],
                          activebackground=T["accent_soft"],
                          activeforeground=T["accent"], padx=18, pady=6,
                          cursor="hand2", highlightthickness=0,
                          command=lambda c=cmd: self._invoke(c))
            b.pack(fill="x", padx=2, pady=1)
        self.win.bind("<Escape>", lambda e: self.close())
        self.win.bind("<FocusOut>", self._on_focus_out)

    def popup(self, x, y, on_close=None):
        self._on_close_cb = on_close
        self.win.update_idletasks()
        w, h = self.win.winfo_reqwidth(), self.win.winfo_reqheight()
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        x = max(0, min(x, sw - w))
        y = max(0, min(y, sh - h))
        self.win.geometry(f"+{x}+{y}")
        self.win.deiconify()
        self.win.lift()
        if self._steal_focus:
            self.win.focus_force()
        else:
            # 保持文本浮窗的焦点与选区高亮：菜单靠轮询检测外部点击来关闭
            self._outside_check = self.win.after(120, self._check_outside_click)

    def contains_point(self, x, y):
        try:
            x0 = self.win.winfo_rootx()
            y0 = self.win.winfo_rooty()
            return (x0 <= x <= x0 + self.win.winfo_width()
                    and y0 <= y <= y0 + self.win.winfo_height())
        except Exception:
            return False

    def _check_outside_click(self):
        """不抢焦点模式下：轮询鼠标按键，点击菜单外即关闭。"""
        self._outside_check = None
        if self._closed:
            return
        try:
            if clipboard_api.mouse_button_down():
                px, py = self.win.winfo_pointerxy()
                if not self.contains_point(px, py):
                    self.close()
                    return
        except Exception:
            pass
        if not self._closed:
            try:
                self._outside_check = self.win.after(
                    100, self._check_outside_click)
            except Exception:
                pass

    def _invoke(self, cmd):
        self._destroy()          # 先收起菜单界面
        try:
            cmd()                # 动作期间保持 on_close 未触发（如文件对话框）
        except Exception:
            traceback.print_exc()
        finally:
            self._fire_close()

    def _on_focus_out(self, _event):
        if self._closed or self._focus_check is not None:
            return
        try:
            self._focus_check = self.win.after(150, self._check_focus)
        except Exception:
            pass

    def _check_focus(self):
        self._focus_check = None
        if self._closed:
            return
        try:
            focused = self.win.focus_get()
        except Exception:
            focused = None
        top = None
        if focused is not None:
            try:
                top = focused.winfo_toplevel()
            except Exception:
                top = None
        if top is not self.win:
            self.close()

    def _destroy(self):
        if self._closed:
            return
        self._closed = True
        if self._outside_check is not None:
            try:
                self.win.after_cancel(self._outside_check)
            except Exception:
                pass
            self._outside_check = None
        try:
            self.win.destroy()
        except Exception:
            pass

    def _fire_close(self):
        cb, self._on_close_cb = self._on_close_cb, None
        if cb:
            try:
                cb()
            except Exception:
                traceback.print_exc()

    def close(self):
        self._destroy()
        self._fire_close()
