"""统一 UI 服务：单线程单 Tk root，同时管理 toast 提示与常驻历史面板。

tkinter 要求所有窗口操作在创建它的同一线程内进行，且一个进程最好只有一个
Tk 解释器。因此 toast 与面板共用这里的一个 UI 线程，通过队列接收外部指令。
"""
import gc
import io
import queue
import threading
import time
import traceback
from typing import ClassVar

from . import clipboard_api
from .diff_window import DiffWindow
from .history_panel import HistoryPanel
from .region_selector import RegionSelector


class UiServer:
    BG = "#1e1e2e"
    BORDER = "#45475a"
    TITLE_FG = "#a6e3a1"
    TEXT_FG = "#cdd6f4"
    LABELS: ClassVar[dict] = {"text": "文本", "image": "图片", "screenshot": "截图",
              "region": "选区截图", "download": "下载", "code": "代码",
              "link": "链接", "file": "文件"}

    def __init__(self, db, config, actions):
        """db: Database; actions: 提供 copy_back(row)/paste_back(row) 的对象。"""
        self.db = db
        self.config = config
        self.actions = actions
        self.toast_enabled = bool(config.get("popup_enabled", True))
        self.toast_seconds = float(config.get("popup_seconds", 3))
        self.panel_enabled = bool(config.get("panel_enabled", True))
        self.region_enabled = bool(config.get("hotkey_region"))
        self._q = queue.Queue(maxsize=32)
        self._thread = None
        self._stop_lock = threading.Lock()
        self._last_panel_toggle = 0.0
        self._root = None
        self._tk = None
        self._toast_stack = []
        self._panel = None
        self._region = None
        self._region_suspended_grab = None
        self._pins = []
        self._image_preview = None
        self._image_preview_key = None
        self._diff_window = None
        self._diff_session = None
        self._prev_hwnd = None
        self.panel_visible = False  # 跨线程可见性提示（仅 UI 线程写入）
        self.started = False

    # ---------- 对外线程安全接口 ----------
    def start(self):
        if not (self.toast_enabled or self.panel_enabled or self.region_enabled):
            return
        self._thread = threading.Thread(target=self._run, name="lumina-ui",
                                        daemon=True)
        self._thread.start()
        deadline = time.time() + 3
        while not self.started and time.time() < deadline:
            time.sleep(0.02)

    def stop(self):
        with self._stop_lock:
            thread = self._thread
            if not thread:
                return
            self._put(("quit",), replace_oldest=True)
            thread.join(timeout=3)
            if thread.is_alive():
                print("[lumina] UI thread did not stop in time", flush=True)
            else:
                self._thread = None

    def notify(self, kind, clip_id, preview="", png=None, source=""):
        if self.toast_enabled and self.started:
            if self._q.full():
                return
            self._put(("toast", (kind, clip_id, preview,
                                  self._thumbnail(png), source)))

    def toggle_panel(self):
        if self.panel_enabled and self.started:
            self._put(("toggle_panel",))

    def show_panel(self):
        """显示面板并刷新（供 CLI 面板命令等外部调用）。"""
        if self.panel_enabled and self.started:
            self._put(("show_panel",))
            self._put(("refresh_panel", ""))

    def refresh_panel(self):
        if self.panel_enabled and self.started:
            self._put(("refresh_panel",))

    def request_hide_panel(self):
        """请求 UI 线程隐藏面板（热键截图前使用，线程安全）。"""
        if self.started:
            self._put(("hide_panel",), replace_oldest=True)

    def request_region_capture(self, frozen_png):
        """frozen_png: 已冻结的全屏截图（在遮罩显示前抓取，避免拍到遮罩）。"""
        if self.started:
            return self._put(("region", frozen_png))
        return False

    def _put(self, item, replace_oldest=False):
        try:
            self._q.put_nowait(item)
            return True
        except queue.Full:
            if not replace_oldest:
                return False
            try:
                self._q.get_nowait()
                self._q.put_nowait(item)
                return True
            except (queue.Empty, queue.Full):
                return False

    @staticmethod
    def _thumbnail(png):
        if not png:
            return None
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(png))
            img.thumbnail((96, 96))
            buf = io.BytesIO()
            img.save(buf, "PNG")
            return buf.getvalue()
        except Exception:
            return None

    # ---------- UI 线程内部 ----------
    def _run(self):
        try:
            import tkinter as tk
        except ImportError:
            print("[lumina] UI disabled: tkinter unavailable", flush=True)
            self.toast_enabled = self.panel_enabled = False
            return
        self._tk = tk
        try:
            root = tk.Tk()
        except Exception as e:
            print(f"[lumina] UI disabled: {e}", flush=True)
            self.toast_enabled = self.panel_enabled = False
            return
        try:
            root.withdraw()
            self._root = root
            self._build_panel()
            self._show_panel_on_startup()
            self.started = True
            root.after(80, self._poll)
            root.mainloop()
        finally:
            self.clear_text_diff()
            self.db.close()
            try:
                root.destroy()
            except Exception:
                pass
            self._toast_stack.clear()
            self._panel = None
            self._region = None
            self._pins.clear()
            self._image_preview = None
            self._image_preview_key = None
            self._root = None
            self.started = False
            gc.collect()

    def _poll(self):
        quitting = False
        try:
            for _ in range(20):
                kind, *rest = self._q.get_nowait()
                try:
                    if kind == "quit":
                        quitting = True
                        self._root.quit()
                        break
                    elif kind == "toast":
                        self._show_toast(*rest[0])
                    elif kind == "toggle_panel":
                        self._toggle_panel()
                    elif kind == "show_panel":
                        if self._panel:
                            if self._panel.is_visible():
                                self._panel.win.lift()
                                self._panel.win.focus_force()
                            else:
                                self._prev_hwnd = clipboard_api.get_foreground_hwnd()
                                self._panel.show(self._prev_hwnd)
                    elif kind == "hide_panel":
                        if self._panel and self._panel.is_visible():
                            self._panel.hide()
                    elif kind == "refresh_panel":
                        if self._panel:
                            if rest:
                                self._panel.search_var.set(rest[0])
                            self._panel.refresh()
                    elif kind == "region":
                        self._open_region(rest[0])
                    elif kind == "call":
                        fn, box, ev = rest[0]
                        try:
                            box["v"] = fn(self)
                        except Exception as e:
                            box["e"] = e
                        finally:
                            ev.set()
                except Exception:
                    traceback.print_exc()
        except queue.Empty:
            pass
        if not quitting and self._root is not None:
            self._root.after(80, self._poll)

    def call_ui(self, fn, timeout=5):
        """在 UI 线程执行 fn(ui_server) 并返回结果（线程安全）。"""
        box = {}
        ev = threading.Event()
        if not self._put(("call", (fn, box, ev))):
            raise RuntimeError("ui command queue is full")
        if not ev.wait(timeout):
            raise TimeoutError("ui thread not responding")
        if "e" in box:
            raise box["e"]
        return box.get("v")

    # ---------- toast ----------
    def _show_toast(self, kind, clip_id, preview, png, source):
        tk = self._tk
        while len(self._toast_stack) >= 5:
            self._close_toast(self._toast_stack[0])
        win = tk.Toplevel(self._root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        frame = tk.Frame(win, bg=self.BG, highlightbackground=self.BORDER,
                         highlightthickness=1)
        frame.pack()

        title = f"已保存 #{clip_id} · {self.LABELS.get(kind, kind)}"
        if source and source != "screenshot":
            title += f"  ({source})"
        tk.Label(frame, text=title, fg=self.TITLE_FG, bg=self.BG,
                 font=("Microsoft YaHei UI", 10, "bold"),
                 anchor="w", padx=10, pady=4).pack(fill="x")

        body_row = tk.Frame(frame, bg=self.BG)
        body_row.pack(fill="both", padx=10, pady=(0, 8))
        if png:
            try:
                from PIL import Image, ImageTk
                img = Image.open(io.BytesIO(png))
                img.thumbnail((96, 96))
                photo = ImageTk.PhotoImage(img)
                lbl = tk.Label(body_row, image=photo, bg=self.BG)
                lbl.image = photo
                lbl.pack(side="left", padx=(0, 8))
            except Exception:
                pass
            detail = f"{len(png) // 1024} KB"
        else:
            detail = preview if len(preview) <= 100 else preview[:100] + "..."
        if detail:
            tk.Label(body_row, text=detail, fg=self.TEXT_FG, bg=self.BG,
                     font=("Microsoft YaHei UI", 9), anchor="nw",
                     justify="left", wraplength=320).pack(side="left", fill="both",
                                                           expand=True)

        win.bind("<Button-1>", lambda e: self._close_toast(win))
        win.update_idletasks()
        w, h = win.winfo_width(), win.winfo_height()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        slot = len(self._toast_stack)
        win.geometry(f"+{(sw - w) // 2}+{sh - 48 - h - slot * (h + 8)}")
        self._toast_stack.append(win)
        win.after(int(self.toast_seconds * 1000), lambda: self._close_toast(win))

    def _close_toast(self, win):
        if win in self._toast_stack:
            self._toast_stack.remove(win)
        try:
            win.destroy()
        except Exception:
            pass

    # ---------- 历史面板 ----------
    def _build_panel(self):
        if not self.panel_enabled:
            return
        try:
            self._panel = HistoryPanel(self._root, self._tk, self)
        except Exception:
            traceback.print_exc()
            self._panel = None

    def _show_panel_on_startup(self):
        """启动后立即显示历史面板，不再依赖热键。"""
        if not self._panel:
            return
        try:
            self._prev_hwnd = clipboard_api.get_foreground_hwnd()
            self._panel.show(self._prev_hwnd)
        except Exception:
            traceback.print_exc()

    def _toggle_panel(self):
        if not self._panel:
            return
        now = time.monotonic()
        if now - self._last_panel_toggle < 0.35:
            return
        self._last_panel_toggle = now
        if self._panel.is_visible():
            self._panel.hide()
        else:
            self._prev_hwnd = clipboard_api.get_foreground_hwnd()
            self._panel.show(self._prev_hwnd)

    def show_image_preview(self, data, key):
        """UI线程调用：所有历史图片共用一个普通层级预览窗口。"""
        preview = self._image_preview
        if preview is not None and not preview.win.winfo_exists():
            self._image_preview = preview = None
            self._image_preview_key = None
        if preview is None:
            preview = PinWindow(self, data, preview=True)
            self._image_preview = preview
        elif key != self._image_preview_key:
            preview.replace_image(data)
        self._image_preview_key = key
        preview.win.deiconify()
        preview.win.lift()
        preview.win.focus_force()

    def _save_diff_session(self, window):
        """仅接受当前窗口在 UI 线程导出的快照，旧窗口不能回写。"""
        if getattr(self, '_diff_window', None) is window:
            self._diff_session = window.export_session()

    def clear_text_diff(self):
        """取消或退出：先关闭，再清空，避免关闭时回写会话。"""
        window = getattr(self, '_diff_window', None)
        if window is not None:
            window.close()
        self._diff_window = None
        self._diff_session = None

    def show_text_diff(self, left_row, right_row):
        """UI 线程提交新配对，替换旧会话及其配置。"""
        window = DiffWindow(self, left_row, right_row)
        self.clear_text_diff()
        self._diff_session = {
            'rows': (dict(left_row), dict(right_row)), 'roles': ('A', 'B'), 'mode': 'text',
            'options': dict.fromkeys(('ignore_trailing_space', 'ignore_line_endings'), False)}
        self._diff_window = window
        window.win.deiconify()
        window.win.lift()
        window.win.focus_force()

    def view_text_diff(self):
        """查看存活窗口仅激活；关闭后从内存原文快照重新比较。"""
        session = getattr(self, '_diff_session', None)
        if session is None:
            return
        window = getattr(self, '_diff_window', None)
        if window is not None and not window.is_alive():
            self._diff_window = window = None
        if window is None:
            window = DiffWindow(self, *session['rows'], session=session)
            self._diff_window = window
        window.win.deiconify()
        window.win.lift()
        window.win.focus_force()

    # ---------- 区域截图 ----------
    def _suspend_region_grab(self):
        """释放本程序模态窗的 Tk grab，让全屏选区窗能接收鼠标事件。"""
        try:
            grabbed = self._root.grab_current()
            if grabbed is None:
                return None
            status = grabbed.grab_status()
            if status not in ("local", "global"):
                return None
            grabbed.grab_release()
            return grabbed, status
        except Exception:
            traceback.print_exc()
            return None

    def _restore_region_grab(self):
        suspended = self._region_suspended_grab
        self._region_suspended_grab = None
        if suspended is None:
            return
        grabbed, status = suspended
        try:
            if not grabbed.winfo_exists():
                return
            current = self._root.grab_current()
            # 不覆盖截图过程中由用户或其他弹框新建的 grab。
            if current is not None and current != grabbed:
                return
            if status == "global":
                grabbed.grab_set_global()
            else:
                grabbed.grab_set()
            grabbed.focus_force()
        except Exception:
            traceback.print_exc()

    def _open_region(self, png):
        if self._region is not None:
            return
        # 新增银行/存单等窗口使用 grab_set() 保证模态；若不暂时释放，
        # 区域截图遮罩虽会出现，但鼠标事件仍被旧弹框截走，无法框选。
        self._region_suspended_grab = self._suspend_region_grab()
        try:
            self._region = RegionSelector(self, png, self._region_done)
        except Exception:
            traceback.print_exc()
            self._region = None
            self._restore_region_grab()
            self.actions.finish_region(None)

    def _region_done(self, png, pin=False, pos=None, source="region"):
        self._region = None
        self._restore_region_grab()
        if png is not None and pin:
            try:
                self._pins.append(PinWindow(self, png, pos))
            except Exception:
                traceback.print_exc()
        threading.Thread(target=self._finish_region, args=(png, source),
                         name="lumina-region-save", daemon=True).start()

    def _finish_region(self, png, source="region"):
        try:
            self.actions.finish_region(png, source)
        except Exception:
            traceback.print_exc()

    def get_prev_hwnd(self):
        return self._prev_hwnd


class PinWindow:
    """钉在屏幕上的截图（类似 Snipaste 贴图）：无边框、置顶。

    - 左键拖动移动位置
    - 滚轮缩放（0.1x - 5x）
    - 双击 / ESC 关闭
    """

    MIN_SCALE = 0.1
    MAX_SCALE = 5.0

    def __init__(self, ui, png, pos=None, preview=False):
        from PIL import Image, ImageTk
        self._Image = Image
        self._ImageTk = ImageTk
        self.ui = ui
        self._img = Image.open(io.BytesIO(png)).convert("RGB")
        self._scale = 1.0
        self._drag_offset = None

        tk = ui._tk
        self.win = tk.Toplevel(ui._root)
        self._preview = preview
        self.win.overrideredirect(not preview)
        self.win.attributes("-topmost", not preview)
        if preview:
            self.win.title("Lumina 图片预览 · 滚轮缩放")
            self.win.protocol("WM_DELETE_WINDOW", self.close)
            self._fit_preview()

        self._label = tk.Label(self.win, bd=1, relief="solid",
                               highlightthickness=0, cursor="fleur",
                               bg="#000000")
        self._label.pack()
        self._apply_scale()

        if pos:
            x, y = int(pos[0]), int(pos[1])
        else:
            x = max(0, (self.win.winfo_screenwidth() - int(self._img.width * self._scale)) // 2)
            y = max(0, (self.win.winfo_screenheight() - int(self._img.height * self._scale)) // 2)
        self.win.geometry(f"+{x}+{y}")

        self._label.bind("<ButtonPress-1>", self._on_press)
        self._label.bind("<B1-Motion>", self._on_drag)
        self._label.bind("<Double-Button-1>", lambda e: self.close())
        # 右键不关闭钉图，避免误触导致贴图消失。
        self.win.bind("<MouseWheel>", self._on_wheel)
        self._label.bind("<MouseWheel>", self._on_wheel)
        # ESC 关闭钉图：无边框窗口默认拿不到键盘焦点，
        # 创建和点击时 focus_force，滚轮缩放也因此更可靠
        self.win.bind("<Escape>", lambda e: self.close())
        self._label.bind("<Escape>", lambda e: self.close())
        try:
            self.win.focus_force()
        except Exception:
            pass

    def _fit_preview(self):
        self._scale = min(
            1.0, self.win.winfo_screenwidth() * 0.8 / self._img.width,
            self.win.winfo_screenheight() * 0.8 / self._img.height)

    def replace_image(self, png):
        image = self._Image.open(io.BytesIO(png)).convert("RGB")
        self._img = image
        self._drag_offset = None
        self._scale = 1.0
        if self._preview:
            self._fit_preview()
        self._apply_scale()

    def _apply_scale(self):
        w = max(1, int(self._img.width * self._scale))
        h = max(1, int(self._img.height * self._scale))
        # 1:1 钉图直接使用原图，避免首次显示时发生二次重采样。
        img = self._img if self._scale == 1.0 else self._img.resize(
            (w, h), self._Image.Resampling.LANCZOS)
        self._photo = self._ImageTk.PhotoImage(img)
        self._label.configure(image=self._photo)

    def _on_press(self, e):
        self._drag_offset = (e.x_root - self.win.winfo_x(),
                             e.y_root - self.win.winfo_y())
        try:
            self.win.focus_force()  # 点击后 ESC / 滚轮作用于本钉图
        except Exception:
            pass

    def _on_drag(self, e):
        if self._drag_offset is None:
            return
        dx, dy = self._drag_offset
        self.win.geometry(f"+{e.x_root - dx}+{e.y_root - dy}")

    def _on_wheel(self, e):
        factor = 1.1 if getattr(e, "delta", 0) > 0 else 1 / 1.1
        new_scale = min(self.MAX_SCALE, max(self.MIN_SCALE, self._scale * factor))
        if new_scale != self._scale:
            self._scale = new_scale
            self._apply_scale()
        # Label 的 bindtags 还包含 Toplevel，阻止同一滚轮事件再次缩放。
        return "break"

    def close(self):
        if getattr(self.ui, "_image_preview", None) is self:
            self.ui._image_preview = None
            self.ui._image_preview_key = None
        try:
            if self in self.ui._pins:
                self.ui._pins.remove(self)
        except Exception:
            pass
        try:
            self.win.destroy()
        except Exception:
            pass

