"""统一 UI 服务：单线程单 Tk root，同时管理 toast 提示与常驻历史面板。

tkinter 要求所有窗口操作在创建它的同一线程内进行，且一个进程最好只有一个
Tk 解释器。因此 toast 与面板共用这里的一个 UI 线程，通过队列接收外部指令。
"""
import io
import gc
import os
import queue
import threading
import time
import traceback

from . import clipboard_api
from .region_selector import RegionSelector
from .history_panel import HistoryPanel
from .settings import save_config

class UiServer:
    BG = "#1e1e2e"
    BORDER = "#45475a"
    TITLE_FG = "#a6e3a1"
    TEXT_FG = "#cdd6f4"
    LABELS = {"text": "文本", "image": "图片", "screenshot": "截图",
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
        self._pins = []
        self._prev_hwnd = None
        self.panel_visible = False  # 跨线程可见性提示（仅 UI 线程写入）
        self._floating_ball = None
        self._panel_locked = False
        self._floating_hide_job = None
        self._floating_hover_job = None
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
            self._build_floating_ball()
            self.started = True
            root.after(80, self._poll)
            root.mainloop()
        finally:
            self.db.close()
            if self._floating_ball:
                self._floating_ball.destroy()
                self._floating_ball = None
            try:
                root.destroy()
            except Exception:
                pass
            self._toast_stack.clear()
            self._panel = None
            self._region = None
            self._pins.clear()
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
                        if self._panel and not self._panel.is_visible():
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

    def _build_floating_ball(self):
        if not (self.panel_enabled and self.config.get("floating_enabled", True)):
            return
        try:
            self._floating_ball = FloatingBall(self)
            self._bind_panel_hover()
        except Exception:
            traceback.print_exc()
            self._floating_ball = None

    def _bind_panel_hover(self):
        if not self._panel:
            return
        self._panel.win.bind("<Enter>", self._panel_enter, add="+")
        self._panel.win.bind("<Leave>", self._panel_leave, add="+")
        self._panel.win.bind("<FocusOut>", self._panel_focus_out, add="+")

    def _panel_enter(self, _event=None):
        self._cancel_floating_hide()

    def _panel_leave(self, _event=None):
        self._schedule_floating_hide()

    def _panel_focus_out(self, _event=None):
        """Stop covering other apps as soon as the user activates one."""
        if not self._panel:
            return
        if getattr(self._panel, "_popup_open", False):
            return
        popup = getattr(self._panel, "_popup_menu", None)
        focus = self._root.focus_get() if self._root else None
        if popup is not None and focus is not None:
            try:
                if focus.winfo_toplevel() is popup:
                    return
            except Exception:
                pass
        try:
            self._panel.win.attributes("-topmost", False)
        except Exception:
            pass
        if not self._panel_locked:
            self._schedule_floating_hide()

    def _cancel_floating_hide(self):
        if self._floating_hide_job is not None:
            try:
                self._root.after_cancel(self._floating_hide_job)
            except Exception:
                pass
            self._floating_hide_job = None

    def _cancel_floating_hover(self):
        if self._floating_hover_job is not None:
            try:
                self._root.after_cancel(self._floating_hover_job)
            except Exception:
                pass
            self._floating_hover_job = None

    def _schedule_floating_hide(self):
        self._cancel_floating_hide()
        if self._panel_locked or not self._panel:
            return
        delay = max(100, int(self.config.get("floating_hide_delay_ms", 400)))
        self._floating_hide_job = self._root.after(delay, self._hide_hover_panel)

    def _hide_hover_panel(self):
        self._floating_hide_job = None
        if not self._panel_locked and self._panel and self._panel.is_visible():
            self._panel.hide()

    def _show_panel_from_floating(self, locked=False):
        if not self._panel:
            return
        self._cancel_floating_hide()
        self._panel_locked = bool(locked)
        position = self._floating_ball.panel_position() if self._floating_ball else None
        self._prev_hwnd = clipboard_api.get_foreground_hwnd()
        self._panel.show(self._prev_hwnd, position=position, topmost=True)

    def _floating_enter(self):
        if self._panel_locked:
            return
        delay = max(0, int(self.config.get("floating_hover_delay_ms", 200)))
        self._cancel_floating_hide()
        self._cancel_floating_hover()
        if delay:
            self._floating_hover_job = self._root.after(
                delay, self._show_hover_panel)
        else:
            self._show_hover_panel()

    def _show_hover_panel(self):
        self._floating_hover_job = None
        self._show_panel_from_floating()

    def _floating_leave(self):
        self._cancel_floating_hover()
        self._schedule_floating_hide()

    def _floating_click(self):
        if self._panel_locked:
            self._panel_locked = False
            if self._panel:
                self._panel.hide()
        elif self._panel and self._panel.is_visible():
            self._panel_locked = True
        else:
            self._show_panel_from_floating(locked=True)

    def _floating_position_changed(self, x, y):
        self.config["floating_x"] = int(x)
        self.config["floating_y"] = int(y)
        config_path = getattr(self.actions, "config_path", None)
        if config_path:
            try:
                save_config(self.config, config_path)
            except OSError:
                traceback.print_exc()

    def _toggle_panel(self):
        if not self._panel:
            return
        if self._floating_ball:
            self._floating_click()
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

    # ---------- 区域截图 ----------
    def _open_region(self, png):
        if self._region is not None:
            return
        try:
            self._region = RegionSelector(self, png, self._region_done)
        except Exception:
            traceback.print_exc()
            self._region = None
            self.actions.finish_region(None)

    def _region_done(self, png, pin=False, pos=None, source="region"):
        self._region = None
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


class FloatingBall:
    """桌面悬浮球，只控制 UiServer 当前的历史面板。"""

    def __init__(self, ui):
        self.ui = ui
        tk = ui._tk
        self.size = max(36, int(ui.config.get("floating_size", 52)))
        from PIL import Image, ImageDraw, ImageTk
        self._ImageTk = ImageTk
        self.win = tk.Toplevel(ui._root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg="#010203")
        try:
            self.win.attributes("-transparentcolor", "#010203")
        except tk.TclError:
            pass

        self.canvas = tk.Canvas(
            self.win, width=self.size, height=self.size,
            bg="#010203", highlightthickness=0, bd=0, cursor="hand2")
        self.canvas.pack()
        self._normal_photo, self._hover_photo = self._make_icons(
            Image, ImageDraw, ImageTk)
        self._icon_id = self.canvas.create_image(
            self.size // 2, self.size // 2, image=self._normal_photo)

        self._drag = None
        self._dragged = False
        self._place_initial()
        self.canvas.bind("<Enter>", self._enter)
        self.canvas.bind("<Leave>", self._leave)
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag_move)
        self.canvas.bind("<ButtonRelease-1>", self._release)

    def _make_icons(self, Image, ImageDraw, ImageTk):
        """Create the Aurora Drop capsule in normal and hover states."""
        scale = 4
        size = self.size * scale
        capsule_w = int(size * .78)
        left = (size - capsule_w) // 2
        right = left + capsule_w

        def make(top_color, bottom_color, outline, hover):
            image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            gradient = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            gradient_draw = ImageDraw.Draw(gradient)
            top = 2 * scale
            bottom = size - 2 * scale
            radius = capsule_w // 2
            top_rgb = top_color[:3]
            bottom_rgb = bottom_color[:3]
            for y in range(top, bottom + 1):
                ratio = (y - top) / max(1, bottom - top)
                color = tuple(round(top_rgb[i] * (1 - ratio) + bottom_rgb[i] * ratio)
                              for i in range(3)) + (top_color[3],)
                gradient_draw.line((left, y, right, y), fill=color, width=1)
            mask = Image.new("L", (size, size), 0)
            ImageDraw.Draw(mask).rounded_rectangle(
                (left, top, right, bottom), radius=radius, fill=255)
            image.paste(gradient, (0, 0), mask)
            draw = ImageDraw.Draw(image)

            # Abstract flowing paper mark: lines only, without a second box
            # inside the capsule.
            flow = (210, 255, 245, 235) if hover else (246, 249, 255, 235)
            for offset in (0, 1, 2):
                y = size * (.45 + offset * .105)
                start = size * (.41 + offset * .015)
                end = size * (.61 - offset * .015)
                draw.line((start, y, end, y), fill=flow,
                          width=max(scale, size // 30))
            # Small status bead, kept inside the capsule.
            bead = max(scale * 2, size // 13)
            cx, cy = size // 2, size * .82
            bead_color = (116, 255, 205, 240) if hover else (120, 211, 169, 210)
            draw.ellipse((cx - bead, cy - bead, cx + bead, cy + bead), fill=bead_color)

            image = image.resize((self.size, self.size), Image.Resampling.LANCZOS)
            alpha = image.getchannel("A").point(lambda value: 0 if value < 100 else value)
            image.putalpha(alpha)
            return ImageTk.PhotoImage(image)

        return (
            make((25, 29, 54, 238), (46, 53, 92, 238), (123, 145, 218, 215), False),
            make((31, 46, 82, 248), (43, 125, 151, 248), (150, 237, 255, 245), True),
        )

    def _enter(self, _event=None):
        self.canvas.itemconfigure(self._icon_id, image=self._hover_photo)
        self.ui._floating_enter()

    def _leave(self, _event=None):
        self.canvas.itemconfigure(self._icon_id, image=self._normal_photo)
        self.ui._floating_leave()

    def _place_initial(self):
        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        x = self.ui.config.get("floating_x")
        y = self.ui.config.get("floating_y")
        try:
            x, y = int(x), int(y)
        except (TypeError, ValueError):
            x = sw - self.size - 24
            y = max(0, (sh - self.size) // 2)
        x = max(0, min(x, sw - self.size))
        y = max(0, min(y, sh - self.size))
        self.win.geometry(f"{self.size}x{self.size}+{x}+{y}")

    def _press(self, event):
        self._drag = (event.x_root - self.win.winfo_x(),
                      event.y_root - self.win.winfo_y())
        self._dragged = False

    def _drag_move(self, event):
        if not self._drag:
            return
        dx, dy = self._drag
        x = event.x_root - dx
        y = event.y_root - dy
        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        x = max(0, min(x, sw - self.size))
        y = max(0, min(y, sh - self.size))
        self.win.geometry(f"+{x}+{y}")
        self._dragged = True

    def _release(self, _event):
        if self._dragged:
            self.ui._floating_position_changed(
                self.win.winfo_x(), self.win.winfo_y())
        else:
            self.ui._floating_click()
        self._drag = None

    def panel_position(self):
        if not self.ui._panel:
            return None
        self.ui._panel.win.update_idletasks()
        pw = self.ui._panel.win.winfo_width()
        ph = self.ui._panel.win.winfo_height()
        bx = self.win.winfo_x()
        by = self.win.winfo_y()
        bw = self.win.winfo_width()
        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        if bx - pw - 12 >= 0:
            x = bx - pw - 12
        else:
            x = bx + bw + 12
        y = by + (self.size - ph) // 2
        return max(0, min(x, sw - pw)), max(0, min(y, sh - ph))

    def destroy(self):
        try:
            self.win.destroy()
        except Exception:
            pass



class PinWindow:
    """钉在屏幕上的截图（类似 Snipaste 贴图）：无边框、置顶。

    - 左键拖动移动位置
    - 滚轮缩放（0.1x - 5x）
    - 双击 / ESC 关闭
    """

    MIN_SCALE = 0.1
    MAX_SCALE = 5.0

    def __init__(self, ui, png, pos=None):
        from PIL import Image, ImageTk
        self._Image = Image
        self._ImageTk = ImageTk
        self.ui = ui
        self._img = Image.open(io.BytesIO(png)).convert("RGB")
        self._scale = 1.0
        self._drag_offset = None

        tk = ui._tk
        self.win = tk.Toplevel(ui._root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)

        self._label = tk.Label(self.win, bd=1, relief="solid",
                               highlightthickness=0, cursor="fleur",
                               bg="#000000")
        self._label.pack()
        self._apply_scale()

        if pos:
            x, y = int(pos[0]), int(pos[1])
        else:
            x = (self.win.winfo_screenwidth() - self._img.width) // 2
            y = (self.win.winfo_screenheight() - self._img.height) // 2
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

    def close(self):
        try:
            if self in self.ui._pins:
                self.ui._pins.remove(self)
        except Exception:
            pass
        try:
            self.win.destroy()
        except Exception:
            pass

