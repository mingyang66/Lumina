"""隐域的临时入口认证与 Windows 会话锁屏通知。"""
import ctypes
import logging
import os
import queue

from . import taskbar

_LOG = logging.getLogger(__name__)
WM_WTSSESSION_CHANGE = 0x02B1
WTS_SESSION_LOCK = 7
NOTIFY_FOR_THIS_SESSION = 0
WM_NCDESTROY = 0x0082
# 原生子类仍存在时不能释放 ctypes 回调，包括注销失败的情形。
_LIVE_MONITORS = {}


def authenticate_vault(panel, parent=None):
    """模态等待认证，仅返回结果，不在面板上保存账号或密码。"""
    if getattr(panel, "_vault_auth_pending", False):
        return False
    panel._vault_auth_pending = True
    dialog = None
    password = None
    accepted = False
    try:
        tk = panel.tk
        owner = parent if parent is not None else panel.win
        dialog = tk.Toplevel(owner)
        dialog.withdraw()
        dialog.title("隐域认证")
        dialog.resizable(False, False)
        dialog.transient(owner)
        # 延迟导入避免入口模块循环引用，直接复用密匣浅色调色板。
        from .vault_window import VaultWindow
        p = VaultWindow.LIGHT_PALETTE
        bg, card, fg, muted = (p[key] for key in ("surface", "card", "title", "muted"))
        dialog.configure(bg=bg)
        body = tk.Frame(dialog, padx=24, pady=20, bg=bg)
        body.pack(fill="both", expand=True)
        tk.Label(body, text="隐域 · 身份验证", bg=bg, fg=fg,
                 font=("Microsoft YaHei UI", 12, "bold")).grid(
            row=0, column=0, columnspan=2, pady=(0, 12))
        tk.Label(body, text="账号", bg=bg, fg=muted).grid(row=1, column=0, padx=(0, 12))
        username = tk.Entry(body, width=28, bg=card, fg=fg, insertbackground=fg,
                            relief="flat", highlightthickness=1,
                            highlightbackground=p["border"], highlightcolor=p["link"])
        username.grid(row=1, column=1, pady=6)
        tk.Label(body, text="密码", bg=bg, fg=muted).grid(row=2, column=0, padx=(0, 12))
        password_box = tk.Frame(body, bg=card, highlightthickness=1,
                                highlightbackground=p["border"])
        password_box.grid(row=2, column=1, sticky="ew", pady=6)
        password = tk.Entry(password_box, width=24, show="*", bg=card, fg=fg,
                            insertbackground=fg, relief="flat", highlightthickness=0)
        password.pack(side="left", fill="x", expand=True, padx=(6, 0), pady=4)
        eye = tk.Canvas(password_box, width=28, height=24, bg=card,
                        highlightthickness=0, cursor="hand2", takefocus=1)
        eye.pack(side="right", padx=4)
        revealed = False

        def paint_eye():
            eye.delete("all")
            eye.create_oval(4, 6, 24, 18, outline=p["link"], width=1.5)
            eye.create_oval(11, 9, 17, 15, fill=p["link"], outline=p["link"])
            if not revealed:
                eye.create_line(4, 21, 24, 3, fill=p["link"], width=1.5)

        def toggle_password(_event=None):
            nonlocal revealed
            revealed = not revealed
            password.configure(show="" if revealed else "*")
            paint_eye()
            return "break"

        for event in ("<Button-1>", "<Return>", "<space>"):
            eye.bind(event, toggle_password)
        password.bind("<FocusIn>", lambda _e: password_box.configure(highlightbackground=p["link"]))
        password.bind("<FocusOut>", lambda _e: password_box.configure(highlightbackground=p["border"]))
        paint_eye()
        error = tk.Label(body, text="", bg=bg, fg=p["matured_fg"])
        error.grid(row=3, column=0, columnspan=2, pady=6)

        def cancel(_event=None):
            password.delete(0, "end")
            dialog.destroy()
            return "break"

        def submit(_event=None):
            nonlocal accepted, revealed
            valid = username.get() == "admin" and password.get() == "admin"
            password.delete(0, "end")
            revealed = False
            password.configure(show="*")
            paint_eye()
            if valid:
                accepted = True
                dialog.destroy()
            else:
                error.configure(text="账号或密码错误")
                password.focus_set()
            return "break"

        buttons = tk.Frame(body, bg=bg)
        buttons.grid(row=4, column=0, columnspan=2, pady=(8, 0))
        tk.Button(buttons, text="取消", width=10, command=cancel,
                  bg=card, fg=p["link"], activebackground=p["head_bg"],
                  activeforeground=fg, relief="flat", bd=0, pady=5).pack(
            side="left", padx=6)
        tk.Button(buttons, text="登录", width=10, command=submit,
                  bg=p["button"], fg=p["button_fg"], activebackground=p["button_hot"],
                  activeforeground=p["button_fg"], relief="flat", bd=0, pady=5).pack(
            side="left", padx=6)
        dialog.protocol("WM_DELETE_WINDOW", cancel)
        dialog.bind("<Return>", submit)
        dialog.bind("<Escape>", cancel)
        dialog.update_idletasks()
        x = owner.winfo_rootx() + max(0, (owner.winfo_width() - dialog.winfo_reqwidth()) // 2)
        y = owner.winfo_rooty() + max(0, (owner.winfo_height() - dialog.winfo_reqheight()) // 2)
        dialog.geometry(f"+{x}+{y}")
        dialog.deiconify()
        dialog.wait_visibility()
        dialog.grab_set()
        username.focus_force()
        dialog.wait_window()
        return accepted
    except Exception:
        _LOG.exception("隐域认证对话框失败")
        return False
    finally:
        if dialog is not None:
            try:
                if dialog.winfo_exists():
                    if password is not None:
                        password.delete(0, "end")
                    dialog.destroy()
            except Exception:
                _LOG.debug("认证对话框清理失败", exc_info=True)
        panel._vault_auth_pending = False


class SessionLockMonitor:
    """将 WTS 锁屏消息排队，由 Tk 线程轮询并执行回调。"""

    def __init__(self, window, callback):
        self.window = window
        self.callback = callback
        self._closed = False
        self._registered = False
        self._subclassed = False
        self._job = None
        self._hwnd = None
        self._proc = None
        self._events = queue.SimpleQueue()
        self._subclass_id = id(self)
        if os.name != "nt":
            return
        try:
            window.update_idletasks()
            self._hwnd = taskbar.top_level_hwnd(window)
            if not self._hwnd:
                raise OSError("无法获取隐域窗口句柄")
            self._wts = ctypes.WinDLL("wtsapi32", use_last_error=True)
            self._comctl = ctypes.WinDLL("comctl32", use_last_error=True)
            proc_type = ctypes.WINFUNCTYPE(
                ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint,
                ctypes.c_size_t, ctypes.c_ssize_t, ctypes.c_size_t, ctypes.c_size_t)
            self._proc = proc_type(self._wndproc)
            register = self._wts.WTSRegisterSessionNotification
            register.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            register.restype = ctypes.c_int
            unregister = self._wts.WTSUnRegisterSessionNotification
            unregister.argtypes = [ctypes.c_void_p]
            unregister.restype = ctypes.c_int
            install = self._comctl.SetWindowSubclass
            install.argtypes = [ctypes.c_void_p, proc_type, ctypes.c_size_t, ctypes.c_size_t]
            install.restype = ctypes.c_int
            remove = self._comctl.RemoveWindowSubclass
            remove.argtypes = [ctypes.c_void_p, proc_type, ctypes.c_size_t]
            remove.restype = ctypes.c_int
            # Tk 事件循环从 ENTER_TCL 状态进入此回调；转发时必须释放 GIL，
            # 让 Tk 的 ENTER_PYTHON 正常恢复线程状态。PyDLL 在此处会导致
            # _PyThreadState_Attach: non-NULL old thread state。
            default = self._comctl.DefSubclassProc
            default.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
            default.restype = ctypes.c_ssize_t
            if not install(self._hwnd, self._proc, self._subclass_id, 0):
                raise ctypes.WinError(ctypes.get_last_error())
            self._subclassed = True
            _LIVE_MONITORS[self._subclass_id] = self
            if not register(self._hwnd, NOTIFY_FOR_THIS_SESSION):
                raise ctypes.WinError(ctypes.get_last_error())
            self._registered = True
            # 构造和轮询均由 Tk 线程调用；原生过程只写线程安全队列。
            self._job = window.after(50, self._poll)
        except Exception:
            _LOG.exception("锁屏通知注册失败")
            self.close()

    def _wndproc(self, hwnd, message, wparam, lparam, subclass_id, ref_data):
        try:
            if not self._closed and message == WM_WTSSESSION_CHANGE and wparam == WTS_SESSION_LOCK:
                self._events.put(None)
            if message == WM_NCDESTROY:
                self.close()
                # 窗口销毁会自动移除剩余子类。
                self._subclassed = False
                _LIVE_MONITORS.pop(self._subclass_id, None)
        except Exception:
            _LOG.exception("锁屏通知处理失败")
        try:
            return self._comctl.DefSubclassProc(hwnd, message, wparam, lparam)
        except Exception:
            _LOG.exception("窗口子类消息转发失败")
            return 0

    def _poll(self):
        self._job = None
        if self._closed:
            return
        try:
            while not self._closed:
                try:
                    self._events.get_nowait()
                except queue.Empty:
                    break
                try:
                    self.callback()
                except Exception:
                    _LOG.exception("锁屏回调失败")
            if not self._closed:
                self._job = self.window.after(50, self._poll)
        except Exception:
            _LOG.exception("锁屏通知轮询失败")
            self.close()

    def close(self):
        """停止回调并释放通知；失败记录日志，保留仍被原生引用的回调。"""
        self._closed = True
        if self._job is not None:
            try:
                self.window.after_cancel(self._job)
            except Exception:
                _LOG.debug("锁屏轮询取消失败", exc_info=True)
            self._job = None
        if self._registered:
            try:
                if not self._wts.WTSUnRegisterSessionNotification(self._hwnd):
                    raise ctypes.WinError(ctypes.get_last_error())
                self._registered = False
            except Exception:
                _LOG.exception("锁屏通知注销失败")
        if self._subclassed:
            try:
                if not self._comctl.RemoveWindowSubclass(self._hwnd, self._proc, self._subclass_id):
                    raise ctypes.WinError(ctypes.get_last_error())
                self._subclassed = False
            except Exception:
                _LOG.exception("窗口子类注销失败")
        if not self._subclassed:
            _LIVE_MONITORS.pop(self._subclass_id, None)
