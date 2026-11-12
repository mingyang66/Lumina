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
    """通过金额保护口令初始化或解锁密匣与记账的金额密钥。"""
    if getattr(panel, "_vault_auth_pending", False):
        return False
    panel._vault_auth_pending = True
    dialog = None
    password = None
    confirmation = None
    confirmation_eye = None
    accepted = False
    try:
        ui = panel.ui
        config = ui.config
        metadata = config.get("money_crypto")
        first_setup = not bool(metadata)
        tk = panel.tk
        owner = parent if parent is not None else panel.win
        dialog = tk.Toplevel(owner)
        dialog.withdraw()
        dialog.title("保护口令")
        dialog.resizable(False, False)
        dialog.transient(owner)
        from .vault_window import VaultWindow
        p = VaultWindow.LIGHT_PALETTE
        bg, card, fg, muted = (p[key] for key in ("surface", "card", "title", "muted"))
        dialog.configure(bg=bg)
        body = tk.Frame(dialog, padx=24, pady=20, bg=bg)
        body.pack(fill="both", expand=True)
        heading = "首次设置保护口令" if first_setup else "解锁数据"
        tk.Label(body, text=heading, bg=bg, fg=fg,
                 font=("Microsoft YaHei UI", 12, "bold")).grid(
            row=0, column=0, columnspan=2, pady=(0, 12))
        tk.Label(body, text="保护口令", bg=bg, fg=muted).grid(
            row=1, column=0, padx=(0, 12))
        password_box = tk.Frame(body, bg=card, highlightthickness=1,
                                highlightbackground=p["border"])
        password_box.grid(row=1, column=1, sticky="ew", pady=6)
        password = tk.Entry(password_box, width=24, show="*", bg=card, fg=fg,
                            insertbackground=fg, relief="flat", highlightthickness=0)
        password.pack(side="left", fill="x", expand=True, padx=(6, 0), pady=4)
        eye = tk.Canvas(password_box, width=28, height=24, bg=card,
                        highlightthickness=0, cursor="hand2", takefocus=1)
        eye.pack(side="right", padx=4)
        revealed = False
        confirmation_revealed = False

        if first_setup:
            tk.Label(body, text="再次输入", bg=bg, fg=muted).grid(
                row=2, column=0, padx=(0, 12))
            confirmation_box = tk.Frame(body, bg=card, highlightthickness=1,
                                        highlightbackground=p["border"])
            confirmation_box.grid(row=2, column=1, sticky="ew", pady=6)
            confirmation = tk.Entry(confirmation_box, width=24, show="*", bg=card, fg=fg,
                                    insertbackground=fg, relief="flat", highlightthickness=0)
            confirmation.pack(side="left", fill="x", expand=True,
                              padx=(6, 0), pady=4)
            confirmation_eye = tk.Canvas(
                confirmation_box, width=28, height=24, bg=card,
                highlightthickness=0, cursor="hand2", takefocus=1)
            confirmation_eye.pack(side="right", padx=4)
        note_row = 3 if first_setup else 2
        note_text = ("口令至少 10 个字符；遗失后无法恢复。"
                     if first_setup else "遗失口令后无法恢复。")
        tk.Label(body, text=note_text, bg=bg, fg=muted, wraplength=340,
                 justify="left").grid(row=note_row, column=0, columnspan=2,
                                      sticky="w", pady=(8, 2))
        error_row = note_row + 1
        button_row = error_row + 1
        error = tk.Label(body, text="", bg=bg, fg=p["matured_fg"])
        error.grid(row=error_row, column=0, columnspan=2, pady=6)

        def paint_eye():
            eye.delete("all")
            eye.create_oval(4, 6, 24, 18, outline=p["link"], width=1.5)
            eye.create_oval(11, 9, 17, 15, fill=p["link"], outline=p["link"])
            if not revealed:
                eye.create_line(4, 21, 24, 3, fill=p["link"], width=1.5)

        def paint_confirmation_eye():
            if confirmation_eye is None:
                return
            confirmation_eye.delete("all")
            confirmation_eye.create_oval(4, 6, 24, 18, outline=p["link"], width=1.5)
            confirmation_eye.create_oval(
                11, 9, 17, 15, fill=p["link"], outline=p["link"])
            if not confirmation_revealed:
                confirmation_eye.create_line(
                    4, 21, 24, 3, fill=p["link"], width=1.5)

        def toggle_password(_event=None):
            nonlocal revealed
            revealed = not revealed
            password.configure(show="" if revealed else "*")
            paint_eye()
            return "break"

        def toggle_confirmation(_event=None):
            nonlocal confirmation_revealed
            confirmation_revealed = not confirmation_revealed
            confirmation.configure(show="" if confirmation_revealed else "*")
            paint_confirmation_eye()
            return "break"

        for event in ("<Button-1>", "<Return>", "<space>"):
            eye.bind(event, toggle_password)
            if confirmation_eye is not None:
                confirmation_eye.bind(event, toggle_confirmation)
        password.bind("<FocusIn>", lambda _e: password_box.configure(highlightbackground=p["link"]))
        password.bind("<FocusOut>", lambda _e: password_box.configure(highlightbackground=p["border"]))
        paint_eye()
        paint_confirmation_eye()

        def clear_entries():
            nonlocal revealed, confirmation_revealed
            password.delete(0, "end")
            revealed = False
            password.configure(show="*")
            paint_eye()
            if confirmation is not None:
                confirmation.delete(0, "end")
                confirmation_revealed = False
                confirmation.configure(show="*")
                paint_confirmation_eye()

        def cancel(_event=None):
            clear_entries()
            dialog.destroy()
            return "break"

        def submit(_event=None):
            nonlocal accepted, metadata
            secret = password.get()
            repeated = confirmation.get() if confirmation is not None else None
            clear_entries()
            try:
                from .money_crypto import create_key_envelope, unlock_key
                if first_setup:
                    if secret != repeated:
                        raise ValueError("两次输入的口令不一致")
                    metadata, data_key = create_key_envelope(secret)
                    from .settings import save_config
                    config["money_crypto"] = metadata
                    actions = getattr(ui, "actions", None)
                    save_config(config, getattr(actions, "config_path", None))
                else:
                    data_key = unlock_key(metadata, secret)
                ui.db.unlock_financial_data(data_key)
                accepted = True
                dialog.destroy()
            except Exception as exc:
                error.configure(text=str(exc) or "口令错误或加密配置异常")
                password.focus_set()
            return "break"

        buttons = tk.Frame(body, bg=bg)
        buttons.grid(row=button_row, column=0, columnspan=2, pady=(8, 0))
        tk.Button(buttons, text="取消", width=10, command=cancel,
                  bg=card, fg=p["link"], activebackground=p["head_bg"],
                  activeforeground=fg, relief="flat", bd=0, pady=5).pack(
            side="left", padx=6)
        tk.Button(buttons, text="解锁" if not first_setup else "设置", width=10,
                  command=submit, bg=p["button"], fg=p["button_fg"],
                  activebackground=p["button_hot"], activeforeground=p["button_fg"],
                  relief="flat", bd=0, pady=5).pack(side="left", padx=6)
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
        password.focus_force()
        dialog.wait_window()
        return accepted
    except Exception:
        _LOG.exception("金额保护口令对话框失败")
        return False
    finally:
        if dialog is not None:
            try:
                if dialog.winfo_exists():
                    if password is not None:
                        password.delete(0, "end")
                    if confirmation is not None:
                        confirmation.delete(0, "end")
                    dialog.destroy()
            except Exception:
                _LOG.debug("认证对话框清理失败", exc_info=True)
        panel._vault_auth_pending = False


def change_vault_passphrase(panel, parent=None):
    """Change the vault passphrase without changing encrypted finance rows."""
    if getattr(panel, "_vault_auth_pending", False):
        return False
    ui = panel.ui
    config = ui.config
    if not config.get("money_crypto"):
        return authenticate_vault(panel, parent=parent)
    panel._vault_auth_pending = True
    dialog = None
    entries = []
    eye_resetters = []
    accepted = False
    try:
        metadata = config.get("money_crypto")
        tk = panel.tk
        owner = parent if parent is not None else panel.win
        dialog = tk.Toplevel(owner)
        dialog.withdraw()
        dialog.title("修改保护口令")
        dialog.resizable(False, False)
        dialog.transient(owner)
        from .vault_window import VaultWindow
        p = VaultWindow.LIGHT_PALETTE
        bg, card, fg, muted = (p[key] for key in ("surface", "card", "title", "muted"))
        dialog.configure(bg=bg)
        body = tk.Frame(dialog, padx=24, pady=20, bg=bg)
        body.pack(fill="both", expand=True)
        tk.Label(body, text="修改保护口令", bg=bg, fg=fg,
                 font=("Microsoft YaHei UI", 12, "bold")).grid(
            row=0, column=0, columnspan=2, pady=(0, 12))

        def add_eye(entry, box):
            eye = tk.Canvas(box, width=28, height=24, bg=card,
                            highlightthickness=0, cursor="hand2", takefocus=1)
            eye.pack(side="right", padx=4)
            state = {"revealed": False}

            def paint():
                eye.delete("all")
                eye.create_oval(4, 6, 24, 18, outline=p["link"], width=1.5)
                eye.create_oval(11, 9, 17, 15, fill=p["link"], outline=p["link"])
                if not state["revealed"]:
                    eye.create_line(4, 21, 24, 3, fill=p["link"], width=1.5)

            def toggle(_event=None):
                state["revealed"] = not state["revealed"]
                entry.configure(show="" if state["revealed"] else "*")
                paint()
                return "break"

            def hide():
                state["revealed"] = False
                entry.configure(show="*")
                paint()

            for event in ("<Button-1>", "<space>", "<Return>"):
                eye.bind(event, toggle)
            paint()
            eye_resetters.append(hide)

        labels = ("当前口令", "新口令", "再次输入新口令")
        for row, label_text in enumerate(labels, start=1):
            tk.Label(body, text=label_text, bg=bg, fg=muted).grid(
                row=row, column=0, padx=(0, 12), pady=5, sticky="e")
            box = tk.Frame(body, bg=card, highlightthickness=1,
                           highlightbackground=p["border"])
            box.grid(row=row, column=1, sticky="ew", pady=5)
            entry = tk.Entry(box, width=28, show="*", bg=card, fg=fg,
                             insertbackground=fg, relief="flat", highlightthickness=0)
            entry.pack(side="left", fill="x", expand=True, padx=(6, 0), pady=4)
            add_eye(entry, box)
            entries.append(entry)

        tk.Label(body, text="至少 10 个字符。更换口令不会重新加密已有数据。",
                 bg=bg, fg=muted, wraplength=340, justify="left").grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(8, 2))
        error = tk.Label(body, text="", bg=bg, fg=p["matured_fg"])
        error.grid(row=5, column=0, columnspan=2, pady=6)

        def clear_entries():
            for entry in entries:
                entry.delete(0, "end")
            for reset_eye in eye_resetters:
                reset_eye()

        def cancel(_event=None):
            clear_entries()
            dialog.destroy()
            return "break"

        def submit(_event=None):
            nonlocal accepted
            current = entries[0].get()
            new = entries[1].get()
            repeated = entries[2].get()
            clear_entries()
            try:
                if new != repeated:
                    raise ValueError("两次输入的新口令不一致")
                from .money_crypto import rewrap_key_envelope
                new_metadata = rewrap_key_envelope(metadata, current, new)
                candidate = dict(config)
                candidate["money_crypto"] = new_metadata
                from .settings import save_config
                actions = getattr(ui, "actions", None)
                save_config(candidate, getattr(actions, "config_path", None))
                config["money_crypto"] = new_metadata
                accepted = True
                dialog.destroy()
            except Exception as exc:
                error.configure(text=str(exc) or "修改口令失败")
                entries[0].focus_set()
            finally:
                current = new = repeated = None
            return "break"

        buttons = tk.Frame(body, bg=bg)
        buttons.grid(row=6, column=0, columnspan=2, pady=(8, 0))
        tk.Button(buttons, text="取消", width=10, command=cancel,
                  bg=card, fg=p["link"], activebackground=p["head_bg"],
                  activeforeground=fg, relief="flat", bd=0, pady=5).pack(
            side="left", padx=6)
        tk.Button(buttons, text="确认修改", width=10, command=submit,
                  bg=p["button"], fg=p["button_fg"],
                  activebackground=p["button_hot"],
                  activeforeground=p["button_fg"], relief="flat", bd=0,
                  pady=5).pack(side="left", padx=6)
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
        entries[0].focus_force()
        dialog.wait_window()
        return accepted
    except Exception:
        _LOG.exception("修改金额保护口令对话框失败")
        return False
    finally:
        if dialog is not None:
            try:
                if dialog.winfo_exists():
                    for entry in entries:
                        entry.delete(0, "end")
                    for reset_eye in eye_resetters:
                        reset_eye()
                    dialog.destroy()
            except Exception:
                _LOG.debug("修改口令对话框清理失败", exc_info=True)
        panel._vault_auth_pending = False


def reset_encrypted_financial_data(panel, revoke_callback=None):
    """Discard encrypted finance rows, then remove the old key envelope.

    This is intentionally independent of the old passphrase so it remains usable
    after the vault has been locked. The database is cleared *before* the config
    envelope is removed: if either step fails, the existing ciphertext is never
    left behind with a missing key configuration.
    """
    if getattr(panel, "_vault_auth_pending", False):
        return False
    ui = panel.ui
    config = ui.config
    if not config.get("money_crypto"):
        return False

    panel._vault_auth_pending = True
    try:
        db = ui.db
        panel._vault_authenticated = False
        panel._vault_write_generation = getattr(
            panel, "_vault_write_generation", 0) + 1
        db.lock_financial_data()
        if revoke_callback is not None:
            revoke_callback()

        # This drops/recreates only finance tables; clipboard schema and rows stay.
        db.init_schema(encrypted_finances=True, reset_financial_data=True)

        candidate = dict(config)
        candidate.pop("money_crypto", None)
        from .settings import save_config
        actions = getattr(ui, "actions", None)
        save_config(candidate, getattr(actions, "config_path", None))
        config.pop("money_crypto", None)
        return True
    finally:
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
