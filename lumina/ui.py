"""统一 UI 服务：单线程单 Tk root，同时管理 toast 提示与常驻历史面板。

tkinter 要求所有窗口操作在创建它的同一线程内进行，且一个进程最好只有一个
Tk 解释器。因此 toast 与面板共用这里的一个 UI 线程，通过队列接收外部指令。
"""
import io
import gc
import ctypes
import json
import os
import queue
import re
import shutil
import threading
import time
import traceback
from datetime import datetime, timedelta

from . import clipboard_api


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
            self.started = True
            root.after(80, self._poll)
            root.mainloop()
        finally:
            self.db.close()
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


class RegionSelector:
    """全屏遮罩：显示冻结画面并压暗，鼠标拖拽框选区域，松手完成截图。

    坐标系换算：屏幕可能存在 DPI 缩放，tkinter 的 winfo 尺寸是逻辑像素，
    而 mss 抓的冻结图是物理像素，因此按 sx/sy 比例换算后再裁剪原图，
    保证输出的是全分辨率区域。
    """

    HINT = "拖拽框选区域 · 松手后 ✓保存(Enter) / 📌钉图 / ✕取消(Esc)"

    def __init__(self, ui, frozen_png, on_done):
        from PIL import Image, ImageEnhance, ImageTk
        self._ImageTk = ImageTk
        self.ui = ui
        self.on_done = on_done
        self.done = False
        self.src = Image.open(io.BytesIO(frozen_png)).convert("RGB")

        tk = ui._tk
        self.win = tk.Toplevel(ui._root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)

        self.sw = self.win.winfo_screenwidth()
        self.sh = self.win.winfo_screenheight()
        self.win.geometry(f"{self.sw}x{self.sh}+0+0")
        self.win.configure(bg="#000000")
        self.sx = self.src.width / self.sw
        self.sy = self.src.height / self.sh

        self.disp = self.src.resize((self.sw, self.sh)) \
            if (self.src.width, self.src.height) != (self.sw, self.sh) else self.src
        dark = ImageEnhance.Brightness(self.disp).enhance(0.4)
        self._dark_photo = ImageTk.PhotoImage(dark)

        self.canvas = tk.Canvas(self.win, width=self.sw, height=self.sh,
                                highlightthickness=0, bd=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_image(0, 0, image=self._dark_photo, anchor="nw")
        self._hint_id = self.canvas.create_text(
            self.sw // 2, 40, text=self.HINT, fill="#ffffff",
            font=("Microsoft YaHei UI", 13, "bold"))
        self._coord_id = None

        self._start = None
        self._rect_id = None
        self._shot_id = None
        self._size_id = None
        self._shot_photo = None
        self._last_draw = 0.0
        self._sel_box = None      # 松手后的选区（逻辑坐标）
        self._tb_buttons = []     # 工具栏按钮
        self._tb_button_map = {}
        self._tb_item_ids = []    # 工具栏 canvas 项（圆角背景图 + 按钮窗口）
        self._tb_bg_photo = None  # 圆角背景 PhotoImage（防 GC）
        self._tb_icons = {}       # 图标 PhotoImage 引用（防 GC）
        self._tool = None
        self._draw_start = None
        self._draw_preview = None
        self._draw_preview_items = []
        self._draw_points = []
        self._annotations = []
        self._redo = []
        self._edit_items = []
        self._text_editor = None
        self._text_commit = None
        self._text_live_item = None
        self._text_caret_job = None
        self._text_input_proxy = None
        self._text_click_binding = None
        self._text_style_popup = None
        self._text_tool_anchor = None
        self._text_font_size = 18
        self._text_color = "#ff3b30"
        self._ocr_panel = None
        self._ocr_panel_item = None
        self._ocr_photo = None
        self._ocr_bg_photo = None
        self._ocr_popup = None

        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Button-3>", lambda e: self.cancel())
        self.win.bind("<Escape>", lambda e: self.cancel())
        self.win.bind("<Return>", lambda e: self._confirm())
        self.win.bind_all("<Control-z>", lambda e: (self._undo(), "break")[1])
        self.win.bind_all("<Control-y>", lambda e: (self._redo_action(), "break")[1])
        self.win.focus_force()
        self.win.after_idle(self._show_initial_coordinates)

    def _coords(self, e):
        x0, y0 = self._start
        x1, y1 = max(0, min(e.x, self.sw)), max(0, min(e.y, self.sh))
        return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)

    def _on_motion(self, e):
        if self._start is not None or self.done:
            return
        self._show_coordinates(e.x, e.y)

    def _show_initial_coordinates(self):
        if self.done or self._start is not None:
            return
        try:
            px, py = self.win.winfo_pointerxy()
            x = px - self.win.winfo_rootx()
            y = py - self.win.winfo_rooty()
        except Exception:
            x, y = self.sw // 2, self.sh // 2
        self._show_coordinates(x, y)

    def _show_coordinates(self, x, y):
        x = max(0, min(int(x), self.sw))
        y = max(0, min(int(y), self.sh))
        label = f"坐标：({int(x * self.sx)}, {int(y * self.sy)})"
        if self._coord_id is None:
            self._coord_id = self.canvas.create_text(
                0, 0, text=label, anchor="nw", fill="#ffffff",
                font=("Microsoft YaHei UI", 12, "bold"),
                activefill="#ffffff")
        self.canvas.itemconfigure(self._coord_id, state="normal")
        self.canvas.itemconfigure(self._coord_id, text=label)
        self.canvas.tag_raise(self._coord_id)
        bbox = self.canvas.bbox(self._coord_id)
        if not bbox:
            return
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        tx = x + 16
        ty = y + 16
        if tx + tw > self.sw:
            tx = x - tw - 16
        if ty + th > self.sh:
            ty = y - th - 16
        self.canvas.coords(self._coord_id, tx, ty)

    def _on_press(self, e):
        if self.done:
            return
        # 先结束当前文字编辑，点击选区外或工具栏时也不能留下空输入框。
        if self._text_commit is not None:
            self._text_commit()
        if self._sel_box and self._tool:
            x0, y0, x1, y1 = self._sel_box
            if not (x0 <= e.x <= x1 and y0 <= e.y <= y1):
                return
        self.canvas.focus_set()
        if self._sel_box and self._tool:
            self._begin_annotation(e)
            return
        self._tool = None
        self._start = (max(0, min(e.x, self.sw)), max(0, min(e.y, self.sh)))
        self._sel_box = None
        self._clear_draw_preview()
        self._clear_ocr_panel()
        if self._coord_id is not None:
            self.canvas.itemconfigure(self._coord_id, state="hidden")
        self._clear_toolbar()
        if self._hint_id is not None:
            self.canvas.delete(self._hint_id)
            self._hint_id = None
        for item in (self._rect_id, self._shot_id, self._size_id):
            if item is not None:
                self.canvas.delete(item)
        self._rect_id = self._shot_id = self._size_id = None
        self._shot_photo = None

    def _on_drag(self, e):
        if self._draw_start is not None:
            self._update_annotation(self._clamp_to_selection(e))
            return
        if self._start is None:
            return
        now = time.perf_counter()
        if now - self._last_draw < 0.033:
            return
        self._last_draw = now
        x0, y0, x1, y1 = self._coords(e)
        w, h = x1 - x0, y1 - y0
        if w <= 0 or h <= 0:
            return
        crop = self.disp.crop((x0, y0, x1, y1))
        self._shot_photo = self._ImageTk.PhotoImage(crop)
        if self._shot_id is None:
            self._shot_id = self.canvas.create_image(x0, y0, anchor="nw",
                                                     image=self._shot_photo)
            self._rect_id = self.canvas.create_rectangle(
                x0, y0, x1, y1, outline=UiServer.TITLE_FG, width=2)
            self._size_id = self.canvas.create_text(
                x0, y0 - 12, text="", anchor="sw", fill=UiServer.TITLE_FG,
                font=("Consolas", 11, "bold"))
        else:
            self.canvas.itemconfigure(self._shot_id, image=self._shot_photo)
            self.canvas.coords(self._shot_id, x0, y0)
            self.canvas.coords(self._rect_id, x0, y0, x1, y1)
            ly = y0 - 12 if y0 > 26 else y1 + 12
            self.canvas.coords(self._size_id, x0, ly)
            self.canvas.itemconfigure(self._size_id, anchor="sw" if y0 > 26 else "nw")
        self.canvas.itemconfigure(
            self._size_id, text=f"{int(w * self.sx)} x {int(h * self.sy)}")
        self.canvas.tag_raise(self._rect_id)
        self.canvas.tag_raise(self._size_id)

    def _on_release(self, e):
        if self._draw_start is not None:
            self._finish_annotation(self._clamp_to_selection(e))
            return
        if self._start is None:
            return
        x0, y0, x1, y1 = self._coords(e)
        self._start = None
        if x1 - x0 < 4 or y1 - y0 < 4:
            self.cancel()
            return
        # 不直接保存：保留选区，浮出工具栏等待用户确认/钉图/取消
        self._sel_box = (x0, y0, x1, y1)
        self._show_toolbar()

    def _clamp_to_selection(self, event):
        """将标注坐标限制在当前选区内，防止工具绘制到截图区域外。"""
        if not self._sel_box:
            return event
        x0, y0, x1, y1 = self._sel_box
        event.x = max(x0, min(x1, event.x))
        event.y = max(y0, min(y1, event.y))
        return event

    def _crop_box(self, include_annotations=True):
        """按当前选区从原始冻结图裁剪（DPI 换算到物理像素）。"""
        if not self._sel_box:
            return None
        x0, y0, x1, y1 = self._sel_box
        box = (int(x0 * self.sx), int(y0 * self.sy),
               int(round(x1 * self.sx)), int(round(y1 * self.sy)))
        box = (max(0, box[0]), max(0, box[1]),
               min(self.src.width, box[2]), min(self.src.height, box[3]))
        if box[2] - box[0] < 1 or box[3] - box[1] < 1:
            return None
        from PIL import ImageDraw, ImageFont, ImageStat
        result = self.src.crop(box).convert("RGBA")
        if not include_annotations:
            buf = io.BytesIO()
            result.convert("RGB").save(buf, "PNG")
            return buf.getvalue()
        scale_x = result.width / max(1, x1 - x0)
        scale_y = result.height / max(1, y1 - y0)
        draw = ImageDraw.Draw(result)
        for item in self._annotations:
            kind = item[0]
            if kind in ("rect", "oval", "arrow"):
                _, a, b = item
                coords = tuple(int(v) for p in (a, b)
                               for v in ((p[0] - x0) * scale_x, (p[1] - y0) * scale_y))
                width = max(2, int(3 * min(scale_x, scale_y)))
                if kind == "rect":
                    draw.rectangle(coords, outline="#ff3b30", width=width)
                elif kind == "oval":
                    draw.ellipse(coords, outline="#ff3b30", width=width)
                else:
                    draw.line(coords, fill="#ff3b30", width=width)
                    import math
                    ax, ay, bx, by = coords
                    angle = math.atan2(by - ay, bx - ax)
                    length = max(12, int(16 * min(scale_x, scale_y)))
                    points = [(bx, by),
                              (bx - length * math.cos(angle - .5), by - length * math.sin(angle - .5)),
                              (bx - length * math.cos(angle + .5), by - length * math.sin(angle + .5))]
                    draw.polygon(points, fill="#ff3b30")
            elif kind in ("brush", "mosaic"):
                _, points = item
                pts = [(int((px - x0) * scale_x), int((py - y0) * scale_y)) for px, py in points]
                if len(pts) > 1 and kind == "brush":
                    draw.line(pts, fill="#ff3b30", width=max(3, int(5 * min(scale_x, scale_y))), joint="curve")
                elif kind == "mosaic":
                    radius = max(4, int(7 * min(scale_x, scale_y)))
                    for (px, py), (src_x, src_y) in zip(pts, points):
                        sample_x = int((src_x - x0) * scale_x)
                        sample_y = int((src_y - y0) * scale_y)
                        sample_box = (
                            max(0, sample_x - radius), max(0, sample_y - radius),
                            min(result.width, sample_x + radius + 1),
                            min(result.height, sample_y + radius + 1))
                        color = tuple(int(v) for v in ImageStat.Stat(
                            result.convert("RGB").crop(sample_box)).mean)
                        draw.rectangle((px - radius, py - radius, px + radius, py + radius),
                                       fill=color)
            elif kind == "text":
                pos, text = item[1], item[2]
                try:
                    font_size = item[3] if len(item) > 3 else 20
                    font = ImageFont.truetype("msyh.ttc", max(14, int(font_size * min(scale_x, scale_y))))
                except Exception:
                    font = ImageFont.load_default()
                color = item[4] if len(item) > 4 else "#ff3b30"
                draw.text((int((pos[0] - x0) * scale_x), int((pos[1] - y0) * scale_y)), text,
                          fill=color, font=font)
        buf = io.BytesIO()
        result.convert("RGB").save(buf, "PNG")
        return buf.getvalue()

    # ---------- 选区工具栏 ----------
    def _toolbar_icons(self):
        if self._tb_icons:
            return
        from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageTk
        try:
            dpi = self.win.winfo_fpixels("1i") / 96.0
        except Exception:
            dpi = 1.0
        # Tk9 的 image 按钮会忽略 padx/pady 与 ipadx/ipady，
        # 因此把内边距直接做进图标画布：字形 ~20px，画布 ~34px。
        size = max(28, int(round(34 * max(0.8, min(dpi, 3.0)))))
        ss = 3  # 超采样，图钉字形边缘更平滑

        def finalize(img_big):
            return ImageTk.PhotoImage(img_big.resize((size, size), Image.LANCZOS))

        def icon_x(color):
            s = size * ss
            im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
            dr = ImageDraw.Draw(im)
            w = max(2, s // 11)
            m = s * 0.27
            dr.line([(m, m), (s - m, s - m)], fill=color, width=w)
            dr.line([(s - m, m), (m, s - m)], fill=color, width=w)
            return finalize(im)

        def icon_check(color):
            s = size * ss
            im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
            dr = ImageDraw.Draw(im)
            dr.line([(s * 0.24, s * 0.53), (s * 0.42, s * 0.72), (s * 0.78, s * 0.30)],
                    fill=color, width=max(2, s // 11), joint="curve")
            return finalize(im)

        def icon_download(color):
            # 下载：向下箭头 + 底部托盘
            s = size * ss
            im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
            dr = ImageDraw.Draw(im)
            dr.rectangle([s * 0.44, s * 0.18, s * 0.56, s * 0.48], fill=color)
            dr.polygon([(s * 0.32, s * 0.46), (s * 0.68, s * 0.46),
                        (s * 0.50, s * 0.63)], fill=color)
            dr.line([(s * 0.29, s * 0.66), (s * 0.29, s * 0.80),
                     (s * 0.71, s * 0.80), (s * 0.71, s * 0.66)],
                    fill=color, width=max(2, int(s * 0.06)), joint="curve")
            return finalize(im)

        def icon_pin():
            # 微信同款图钉形状（U+1F4CC，钉帽右上、针尖左下），
            # 线条图标风格：黑色描边 + 白色填充。
            # 做法：字形 alpha 蒙版 → MinFilter 腐蚀得内部(白)，
            # 蒙版减腐蚀得边缘环(黑)。
            s = size * ss
            im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
            dr = ImageDraw.Draw(im)
            ch = "\U0001F4CC"
            sym = os.path.join(os.environ.get("WINDIR", r"C:\Windows"),
                               "Fonts", "seguisym.ttf")
            mask = None
            if os.path.exists(sym):
                try:
                    fnt = ImageFont.truetype(sym, int(s * 0.78))
                    bb = dr.textbbox((0, 0), ch, font=fnt)
                    dr.text(((s - (bb[2] - bb[0])) / 2 - bb[0],
                             (s - (bb[3] - bb[1])) / 2 - bb[1]),
                            ch, font=fnt, fill=(255, 255, 255, 255))
                    m = im.split()[3]
                    if m.getextrema()[1] > 0:
                        mask = m
                except Exception:
                    mask = None
            if mask is None:
                # 回退：手绘倾斜图钉（灰色实心）
                base = Image.new("RGBA", (s, s), (0, 0, 0, 0))
                bd = ImageDraw.Draw(base)
                cx = s / 2
                col = (95, 99, 104, 255)
                plate_h = s * 0.12
                bd.rounded_rectangle([cx - s * 0.22, s * 0.14, cx + s * 0.22,
                                      s * 0.14 + plate_h], radius=plate_h / 2, fill=col)
                bd.rectangle([cx - s * 0.075, s * 0.14 + plate_h, cx + s * 0.075,
                              s * 0.52], fill=col)
                bd.rounded_rectangle([cx - s * 0.14, s * 0.50, cx + s * 0.14, s * 0.60],
                                     radius=s * 0.04, fill=col)
                bd.polygon([(cx - s * 0.03, s * 0.60), (cx + s * 0.03, s * 0.60),
                            (cx, s * 0.84)], fill=col)
                rot = base.rotate(-45, expand=True, resample=Image.BICUBIC)
                out = Image.new("RGBA", (s, s), (0, 0, 0, 0))
                out.paste(rot, ((s - rot.width) // 2, (s - rot.height) // 2), rot)
                return finalize(out)
            eroded = mask.filter(ImageFilter.MinFilter(9))
            border = ImageChops.subtract(mask, eroded)
            white = Image.new("RGBA", (s, s), (255, 255, 255, 255))
            gray = Image.new("RGBA", (s, s), (95, 99, 104, 255))
            out = Image.new("RGBA", (s, s), (0, 0, 0, 0))
            out = Image.composite(white, out, eroded)
            out = Image.composite(gray, out, border)
            return finalize(out)

        def icon_tool(kind, color="#5f6368"):
            """绘制编辑工具图标，避免用文字占用工具栏按钮。"""
            s = size * ss
            im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
            dr = ImageDraw.Draw(im)
            w = max(2, s // 11)
            m = s * .22
            if kind == "rect":
                dr.rectangle((m, m, s - m, s - m), outline=color, width=w)
            elif kind == "oval":
                dr.ellipse((m, m, s - m, s - m), outline=color, width=w)
            elif kind == "arrow":
                dr.line((s * .22, s * .75, s * .75, s * .25), fill=color, width=w)
                dr.polygon(((s * .58, s * .22), (s * .80, s * .20), (s * .78, s * .42)), fill=color)
            elif kind == "brush":
                # 一根横线代表纸张，细长钢笔斜放其上并突出尖锐笔尖。
                line_color = "#a8b1ba"
                dr.line((s * .18, s * .77, s * .78, s * .77),
                        fill=line_color, width=max(2, s // 18))
                pen_outline = "#263746"
                dr.polygon(((s * .29, s * .65), (s * .71, s * .19),
                            (s * .78, s * .27), (s * .37, s * .70)),
                           fill="#344f63", outline=pen_outline, width=max(2, s // 19))
                dr.line((s * .38, s * .68, s * .73, s * .24),
                        fill="#6f91a5", width=max(2, s // 20))
                dr.line((s * .65, s * .25, s * .76, s * .36),
                        fill="#d4dbe0", width=max(2, s // 18))
                # 单独拉出笔尖，使用高对比色增强识别度。
                dr.polygon(((s * .29, s * .65), (s * .18, s * .83),
                            (s * .37, s * .70)), fill="#e5edf1",
                           outline=pen_outline, width=max(2, s // 21))
                dr.line((s * .19, s * .82, s * .33, s * .69),
                        fill="#263746", width=max(2, s // 25))
                dr.ellipse((s * .22, s * .76, s * .27, s * .81), fill="#263746")
            elif kind == "mosaic":
                # 四格马赛克：两条对角线分别使用黑色和白色。
                left, top = s * .22, s * .22
                block = s * .27
                gap = max(2, int(s * .035))
                shades = (("#20252a", "#ffffff"), ("#ffffff", "#20252a"))
                for row in range(2):
                    for col in range(2):
                        x = left + col * (block + gap)
                        y = top + row * (block + gap)
                        dr.rectangle((x, y, x + block, y + block),
                                     fill=shades[row][col])
                dr.rectangle((left, top, left + block * 2 + gap, top + block * 2 + gap),
                             outline="#4d5962", width=max(2, s // 24))
            elif kind == "text":
                # 使用几何线条绘制 T，避免小尺寸字体渲染发虚或缺笔画。
                text_color = "#6f808c"
                stroke = max(3, s // 9)
                dr.line((s * .23, s * .25, s * .77, s * .25),
                        fill=text_color, width=stroke)
                dr.line((s * .50, s * .25, s * .50, s * .78),
                        fill=text_color, width=stroke)
                # 端点补圆，使线条在缩放后保持平滑。
                radius = stroke / 2
                for cx, cy in ((s * .23, s * .25), (s * .77, s * .25),
                               (s * .50, s * .78)):
                    dr.ellipse((cx - radius, cy - radius, cx + radius, cy + radius),
                               fill=text_color)
            elif kind == "ocr":
                # OCR：四角扫描框 + 中央“文”，直接表达提取文字功能。
                scan_color = "#6f808c"
                corner = s * .23
                edge = s * .78
                arm = s * .14
                scan_w = max(2, s // 12)
                for x, y, dx, dy in ((corner, corner, 1, 1),
                                     (edge, corner, -1, 1),
                                     (corner, edge, 1, -1),
                                     (edge, edge, -1, -1)):
                    dr.line((x, y, x + dx * arm, y), fill=scan_color, width=scan_w)
                    dr.line((x, y, x, y + dy * arm), fill=scan_color, width=scan_w)
                try:
                    ocr_font = ImageFont.truetype("msyh.ttc", int(s * .30))
                except Exception:
                    ocr_font = ImageFont.load_default()
                label = "文"
                bbox = dr.textbbox((0, 0), label, font=ocr_font)
                text_w = bbox[2] - bbox[0]
                text_h = bbox[3] - bbox[1]
                dr.text(((s - text_w) / 2 - bbox[0], (s - text_h) / 2 - bbox[1]),
                        label, font=ocr_font, fill=scan_color)
            elif kind in ("undo", "redo"):
                # 微信风格回转箭头整体旋转：撤销顺时针 90 度，恢复逆时针 90 度。
                arrow_color = "#647581"
                curve = [(s * .30, s * .36), (s * .38, s * .27),
                         (s * .51, s * .24), (s * .65, s * .29),
                         (s * .74, s * .40), (s * .76, s * .55),
                         (s * .72, s * .68)]
                center = s / 2

                def rotate(point, clockwise):
                    px, py = point[0] - center, point[1] - center
                    if clockwise:
                        return (center - py, center + px)
                    return (center + py, center - px)

                if kind == "redo":
                    curve = [(s - x, y) for x, y in curve]
                    clockwise = False
                else:
                    clockwise = True
                curve = [rotate(point, clockwise) for point in curve]
                dr.line(curve, fill=arrow_color, width=w, joint="curve")
                # 在最终旋转后的方向上延长箭头，避免旋转前调整导致方向不明显。
                tip_x, tip_y = curve[0]
                head_length = s * .26
                head_width = s * .13
                if kind == "undo":
                    head = ((tip_x - head_length, tip_y),
                            (tip_x, tip_y - head_width),
                            (tip_x, tip_y + head_width))
                else:
                    head = ((tip_x + head_length, tip_y),
                            (tip_x, tip_y - head_width),
                            (tip_x, tip_y + head_width))
                dr.polygon(head, fill=arrow_color)
            return finalize(im)

        self._tb_icons = {
            "rect": (icon_tool("rect"), size),
            "oval": (icon_tool("oval"), size),
            "arrow": (icon_tool("arrow"), size),
            "brush": (icon_tool("brush"), size),
            "mosaic": (icon_tool("mosaic"), size),
            "text": (icon_tool("text"), size),
            "ocr": (icon_tool("ocr"), size),
            "undo": (icon_tool("undo"), size),
            "redo": (icon_tool("redo"), size),
            "x": (icon_x("#e5484d"), size),
            "check": (icon_check("#2f9e44"), size),
            "pin": (icon_pin(), size),
            "download": (icon_download("#5f6368"), size),
        }

    def _select_tool(self, tool):
        self._tool = tool
        if tool == "text":
            if self._text_tool_anchor is not None:
                self._show_text_style_popup(*self._text_tool_anchor, offset=8)
            else:
                self._hide_text_style_popup()
        else:
            self._hide_text_style_popup()
        self.canvas.configure(cursor="crosshair" if tool else "arrow")
        self._update_tool_button_state()

    def _update_tool_button_state(self):
        """用浅蓝选中态区分当前编辑工具，避免工具栏看起来像一排相同按钮。"""
        for key, button in self._tb_button_map.items():
            selected = key == self._tool
            try:
                button.configure(
                    bg="#e8f1ff" if selected else "#ffffff",
                    activebackground="#dbeaff" if selected else "#f1f4f8",
                    relief="flat", bd=0)
            except Exception:
                pass

    def _show_text_style_popup(self, x=None, y=None, offset=42):
        self._hide_text_style_popup()
        tk = self.ui._tk
        from PIL import Image, ImageDraw, ImageTk
        popup = tk.Toplevel(self.win)
        popup.overrideredirect(True)
        popup.attributes("-topmost", True)
        transparent = "#010203"
        popup.configure(bg=transparent)
        try:
            popup.attributes("-transparentcolor", transparent)
        except tk.TclError:
            pass
        frame = tk.Frame(popup, bg="#ffffff", bd=0,
                         highlightthickness=0)
        frame.pack(padx=8, pady=(10, 8))
        block_size = 28
        for label, size in (("小", 14), ("中", 18), ("大", 26)):
            selected = size == self._text_font_size
            holder = tk.Frame(frame, bg="#3478f6" if selected else "#ffffff",
                              width=block_size, height=block_size)
            holder.pack(side="left", padx=2)
            holder.pack_propagate(False)
            button = tk.Button(
                holder, text=label,
                command=lambda value=size: self._set_text_size(value),
                bg="#e8f1ff" if selected else "#ffffff",
                activebackground="#dbeaff", fg="#263238",
                bd=0, relief="flat",
                highlightthickness=0, width=1, height=1,
                padx=0, pady=0, font=("Microsoft YaHei UI", 9, "bold"),
                cursor="hand2")
            button.pack(fill="both", expand=True, padx=2 if selected else 0,
                        pady=2 if selected else 0)
        tk.Frame(frame, bg="#e1e5ea", width=1, height=block_size).pack(side="left", padx=6)
        for color in ("#ff3b30", "#111111", "#1677ff", "#07c160", "#ff9500"):
            selected = color.lower() == self._text_color.lower()
            holder = tk.Frame(frame, bg="#3478f6" if selected else "#ffffff",
                              width=block_size, height=block_size)
            holder.pack(side="left", padx=2)
            holder.pack_propagate(False)
            color_button = tk.Button(holder, bg=color, activebackground=color,
                      bd=0, relief="flat",
                      width=1, height=1,
                      padx=0, pady=0, cursor="hand2",
                      text="✓" if selected else "",
                      fg="#ffffff" if selected else color,
                      font=("Microsoft YaHei UI", 10, "bold"),
                      command=lambda value=color: self._set_text_color(value))
            color_button.pack(fill="both", expand=True, padx=3 if selected else 1,
                              pady=3 if selected else 1)
        self._text_style_popup = popup
        popup.update_idletasks()
        popup_w = max(1, popup.winfo_reqwidth())
        popup_h = max(1, popup.winfo_reqheight())
        chrome = tk.Canvas(popup, width=popup_w, height=popup_h,
                           bg=transparent, bd=0, highlightthickness=0)
        chrome.place(x=0, y=0, width=popup_w, height=popup_h)
        bg = Image.new("RGBA", (popup_w * 2, popup_h * 2), (0, 0, 0, 0))
        ImageDraw.Draw(bg).rounded_rectangle(
            (1, 1, popup_w * 2 - 2, popup_h * 2 - 2), radius=10 * 2,
            fill="#ffffff", outline="#d6dce4", width=2)
        popup._text_style_bg = ImageTk.PhotoImage(
            bg.resize((popup_w, popup_h), Image.Resampling.LANCZOS))
        bg_item = chrome.create_image(0, 0, image=popup._text_style_bg, anchor="nw")
        chrome.create_polygon(popup_w // 2, 0, popup_w // 2 - 6, 9,
                              popup_w // 2 + 6, 9, fill="#ffffff", outline="")
        # lower() 是窗口控件方法；这里需要降低 Canvas 内部图元，不能降低 Canvas 控件本身。
        chrome.tag_lower(bg_item)
        frame.lift()
        if x is None or y is None:
            x = self.win.winfo_rootx() + 12
            y = self.win.winfo_rooty() + 12
        else:
            x = self.win.winfo_rootx() + int(x)
            y = self.win.winfo_rooty() + int(y) + offset
        sw = popup.winfo_screenwidth()
        sh = popup.winfo_screenheight()
        x -= popup.winfo_width() // 2
        x = max(4, min(x, sw - popup.winfo_width() - 4))
        y = max(4, min(y, sh - popup.winfo_height() - 4))
        popup.geometry(f"+{x}+{y}")
        # 箭头固定在属性栏顶部中央，位置只由文字工具按钮决定，不随鼠标移动。
        popup.deiconify()
        popup.update_idletasks()
        popup.lift()
        popup.after(30, popup.lift)

    def _hide_text_style_popup(self):
        if self._text_style_popup is not None:
            try:
                self._text_style_popup.destroy()
            except Exception:
                pass
            self._text_style_popup = None

    def _set_text_size(self, size):
        self._text_font_size = size
        if self._text_live_item is not None:
            self.canvas.itemconfigure(self._text_live_item,
                                      font=("Microsoft YaHei UI", size, "bold"))
        if self._tool == "text" and self._text_tool_anchor is not None:
            self._show_text_style_popup(*self._text_tool_anchor, offset=8)

    def _set_text_color(self, color):
        self._text_color = color
        if self._text_live_item is not None:
            self.canvas.itemconfigure(self._text_live_item, fill=color)
        if self._tool == "text" and self._text_tool_anchor is not None:
            self._show_text_style_popup(*self._text_tool_anchor, offset=8)

    def _begin_annotation(self, e):
        if self._tool == "text":
            if self._text_commit is not None:
                self._text_commit()
            existing = None
            for item_id in reversed(self.canvas.find_overlapping(e.x, e.y, e.x, e.y)):
                try:
                    if self.canvas.type(item_id) != "text":
                        continue
                    tags = self.canvas.gettags(item_id)
                    index_tag = next((tag for tag in tags
                                      if tag.startswith("annotation-text:")), None)
                    if index_tag is not None:
                        index = int(index_tag.split(":", 1)[1])
                        item = self._annotations[index]
                        if item[0] == "text":
                            existing = (index, item)
                            break
                except Exception:
                    continue
                if existing:
                    break
            if existing:
                index, item = existing
                self._annotations.pop(index)
                self._redraw_annotations()
                self._text_font_size = item[3] if len(item) > 3 else 18
                self._text_color = item[4] if len(item) > 4 else "#ff3b30"
                self._open_text_editor(item[1][0], item[1][1], item[2])
            else:
                self._open_text_editor(e.x, e.y)
            return
        self._draw_start = (e.x, e.y)
        self._draw_points = [(e.x, e.y)]
        self._draw_preview = None

    def _update_annotation(self, e):
        x, y = e.x, e.y
        if self._tool in ("brush", "mosaic"):
            self._draw_points.append((x, y))
            if len(self._draw_points) > 1:
                if self._tool == "mosaic":
                    radius = 6
                    item_id = self.canvas.create_rectangle(
                        x - radius, y - radius, x + radius, y + radius,
                        fill=self._mosaic_color(x, y, radius),
                        outline="")
                else:
                    item_id = self.canvas.create_line(
                        self._draw_points[-2], self._draw_points[-1], fill="#ff3b30",
                        width=5, capstyle="round")
                self._draw_preview_items.append(item_id)
                self._draw_preview = item_id
        else:
            if self._draw_preview is not None:
                self.canvas.delete(self._draw_preview)
            x0, y0 = self._draw_start
            if self._tool == "rect":
                self._draw_preview = self.canvas.create_rectangle(x0, y0, x, y, outline="#ff3b30", width=3)
            elif self._tool == "oval":
                self._draw_preview = self.canvas.create_oval(x0, y0, x, y, outline="#ff3b30", width=3)
            else:
                self._draw_preview = self.canvas.create_line(x0, y0, x, y, fill="#ff3b30", width=3, arrow="last")

    def _finish_annotation(self, e):
        start = self._draw_start
        self._draw_start = None
        self._clear_draw_preview()
        if self._tool in ("brush", "mosaic"):
            item = (self._tool, list(self._draw_points))
            self._draw_points = []
        else:
            item = (self._tool, start, (e.x, e.y))
        if self._tool in ("rect", "oval", "arrow", "brush", "mosaic"):
            self._record_annotation(item)

    def _record_annotation(self, item):
        """提交一笔完整标注，所有工具共用同一撤销/恢复栈。"""
        self._annotations.append(item)
        self._redo.clear()
        self._redraw_annotations()

    def _clear_draw_preview(self):
        """删除当前笔划生成的全部临时线段，避免残留在正式标注层。"""
        item_ids = list(self._draw_preview_items)
        if self._draw_preview is not None and self._draw_preview not in item_ids:
            item_ids.append(self._draw_preview)
        for item_id in item_ids:
            try:
                self.canvas.delete(item_id)
            except Exception:
                pass
        self._draw_preview_items = []
        self._draw_preview = None

    def _redraw_annotations(self):
        for item_id in self._edit_items:
            self.canvas.delete(item_id)
        self._edit_items = []
        for annotation_index, item in enumerate(self._annotations):
            kind = item[0]
            if kind in ("rect", "oval", "arrow"):
                _, a, b = item
                fn = self.canvas.create_rectangle if kind == "rect" else self.canvas.create_oval
                if kind == "arrow":
                    item_id = self.canvas.create_line(*a, *b, fill="#ff3b30", width=3, arrow="last")
                    self._edit_items.append(item_id)
                    continue
                self._edit_items.append(fn(*a, *b, outline="#ff3b30", width=3))
            elif kind in ("brush", "mosaic"):
                _, points = item
                if kind == "mosaic":
                    radius = 6
                    for px, py in points:
                        self._edit_items.append(self.canvas.create_rectangle(
                            px - radius, py - radius, px + radius, py + radius,
                            fill=self._mosaic_color(px, py, radius),
                            outline=""))
                elif len(points) > 1:
                    self._edit_items.append(self.canvas.create_line(
                        *[p for point in points for p in point], fill="#ff3b30",
                        width=5, capstyle="round", smooth=True))
            elif kind == "text":
                pos, text = item[1], item[2]
                self._edit_items.append(self.canvas.create_text(*pos, text=text, anchor="nw",
                                                               fill=item[4] if len(item) > 4 else "#ff3b30",
                                                               font=("Microsoft YaHei UI", item[3] if len(item) > 3 else 18, "bold"),
                                                               tags=("annotation-text", f"annotation-text:{annotation_index}")))
        for item_id in self._edit_items:
            self.canvas.tag_raise(item_id)

    def _mosaic_color(self, x, y, radius=6):
        """取显示截图对应区域的平均色，生成彩色方块马赛克。"""
        try:
            box = (max(0, int(x - radius)), max(0, int(y - radius)),
                   min(self.disp.width, int(x + radius + 1)),
                   min(self.disp.height, int(y + radius + 1)))
            from PIL import ImageStat
            mean = ImageStat.Stat(self.disp.crop(box).convert("RGB")).mean
            return "#%02x%02x%02x" % tuple(max(0, min(255, int(v))) for v in mean)
        except Exception:
            return "#808080"

    def _open_text_editor(self, x, y, initial_text=""):
        if self._text_editor is not None:
            return
        text_x, text_y = x + 6, y + 5
        # 直接让 Canvas 接收键盘，避免任何 Entry 背景遮挡截图内容。
        input_box = self.canvas.create_rectangle(
            x, y, x + 132, y + 40,
            outline="#ff3b30", width=2, fill="")
        live_item = self.canvas.create_text(
            text_x, text_y, text=initial_text, anchor="nw", fill=self._text_color,
            font=("Microsoft YaHei UI", self._text_font_size, "bold"))
        self._text_editor = live_item
        self._text_live_item = live_item
        # 属性栏固定在工具栏的“文字”按钮下方，不跟随截图中的编辑光标移动。
        if self._text_tool_anchor is not None:
            self._show_text_style_popup(*self._text_tool_anchor, offset=8)
        caret = self.canvas.create_line(text_x, text_y + 1, text_x, text_y + self._text_font_size + 9,
                                        fill=self._text_color, width=2)
        tk = self.ui._tk
        input_var = tk.StringVar(self.canvas, value=initial_text)
        input_proxy = tk.Entry(self.canvas, textvariable=input_var, width=1,
                               bd=0, relief="flat", highlightthickness=0,
                               bg="#1e1e2e", fg="#1e1e2e",
                               insertbackground="#1e1e2e")
        input_proxy.place(x=text_x, y=text_y + 8, width=2, height=2)
        self._text_input_proxy = input_proxy
        if initial_text:
            bbox = self.canvas.bbox(live_item)
            if bbox:
                _, top, right, bottom = bbox
                self.canvas.coords(input_box, x, y,
                                  max(x + 132, right + 6), max(y + 40, bottom + 6))
                self.canvas.coords(caret, right + 2, top, right + 2, bottom)

        def blink_caret(visible=True):
            if self._text_editor != live_item:
                return
            try:
                self.canvas.itemconfigure(caret, state="normal" if visible else "hidden")
                self._text_caret_job = self.win.after(
                    520, lambda: blink_caret(not visible))
            except Exception:
                self._text_caret_job = None

        blink_caret()

        state = {"text": initial_text}

        def finish(commit):
            if self._text_editor != live_item:
                return "break"
            text = state["text"].strip()
            self.win.unbind_all("<KeyPress>")
            if self._text_click_binding is not None:
                try:
                    self.win.unbind_all("<ButtonPress-1>")
                except Exception:
                    pass
                self._text_click_binding = None
            if self._text_caret_job is not None:
                try:
                    self.win.after_cancel(self._text_caret_job)
                except Exception:
                    pass
                self._text_caret_job = None
            try:
                if self.canvas.winfo_exists():
                    self.canvas.delete(input_box)
                    self.canvas.delete(live_item)
                    self.canvas.delete(caret)
            except Exception:
                pass
            try:
                input_proxy.destroy()
            except Exception:
                pass
            self._text_input_proxy = None
            self._text_editor = None
            self._text_commit = None
            self._text_live_item = None
            if commit and text:
                self._record_annotation(("text", (x, y), text,
                                          self._text_font_size, self._text_color))
            return "break"

        def commit(_event=None):
            return finish(True)

        def cancel_edit(_event=None):
            return finish(False)

        def update_from_input(*_args):
            if self._text_editor != live_item:
                return
            state["text"] = input_var.get()
            self.canvas.itemconfigure(live_item, text=state["text"])
            bbox = self.canvas.bbox(live_item)
            if bbox:
                left, top, right, bottom = bbox
                self.canvas.coords(input_box, x, y,
                                  max(x + 132, right + 6), max(y + 40, bottom + 6))
                caret_x = right + 2
                caret_top = top
                caret_bottom = bottom
            else:
                self.canvas.coords(input_box, x, y, x + 132, y + 40)
                caret_x = text_x
                caret_top = y + 7
                caret_bottom = y + 33
            self.canvas.coords(caret, caret_x, caret_top, caret_x, caret_bottom)
            self.canvas.itemconfigure(caret, state="normal")
            input_proxy.place(x=caret_x, y=text_y + 8, width=2, height=2)

        def edit_key(event):
            if event.keysym == "Return":
                return commit(event)
            if event.keysym == "Escape":
                return cancel_edit(event)
            if event.keysym == "BackSpace":
                state["text"] = state["text"][:-1]
            elif event.keysym == "Delete":
                state["text"] = ""
            elif event.state & 0x0004 and event.keysym.lower() == "a":
                state["text"] = state["text"]
            elif event.char and event.char.isprintable():
                state["text"] += event.char
            else:
                return "break"
            update_from_input()
            return "break"

        self._text_commit = commit
        # 文字编辑期间从整个截图窗口接收按键，避免工具栏控件抢走 Canvas 焦点。
        input_var.trace_add("write", update_from_input)
        input_proxy.bind("<Return>", commit)
        input_proxy.bind("<Escape>", cancel_edit)
        input_proxy.focus_force()
        # 监听截图窗口内所有点击，包括选区外和工具栏控件的点击。
        self._text_click_binding = self.win.bind_all(
            "<ButtonPress-1>", lambda _event: finish(bool(state["text"].strip())), add="+")

    def _undo(self):
        if self._text_commit is not None:
            self._text_commit()
        if self._draw_start is not None:
            self._clear_draw_preview()
            self._draw_start = None
        if self._annotations:
            self._redo.append(self._annotations.pop())
            self._redraw_annotations()
        else:
            self._redraw_annotations()
        try:
            if self.canvas.winfo_exists() and not self.done:
                self.canvas.focus_set()
                self.canvas.update_idletasks()
        except Exception:
            pass
        return "break"

    def _redo_action(self):
        if self._redo:
            self._annotations.append(self._redo.pop())
            self._redraw_annotations()
        else:
            self._redraw_annotations()
        try:
            if self.canvas.winfo_exists() and not self.done:
                self.canvas.focus_set()
                self.canvas.update_idletasks()
        except Exception:
            pass
        return "break"

    def _extract_text(self):
        from tkinter import messagebox
        try:
            import pytesseract
            from PIL import Image
            # Windows 安装程序不一定会自动把 Tesseract 加入 PATH。
            candidates = (
                r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
            )
            # 优先使用已确认的 Windows 安装路径，避免 pythonw 进程未继承最新 PATH。
            tesseract = next((p for p in candidates if os.path.isfile(p)), None)
            if not tesseract:
                tesseract = shutil.which("tesseract")
            if not tesseract:
                raise pytesseract.pytesseract.TesseractNotFoundError(
                    "Tesseract OCR executable was not found")
            pytesseract.pytesseract.tesseract_cmd = tesseract

            png = self._crop_box()
            if not png:
                messagebox.showwarning("提取文字", "当前没有有效的截图选区。请重新框选文字区域。", parent=self.win)
                return
            image = Image.open(io.BytesIO(png)).convert("RGB")
            ocr_png = self._crop_box(include_annotations=False)
            if ocr_png:
                image = Image.open(io.BytesIO(ocr_png)).convert("RGB")
            from PIL import ImageEnhance, ImageFilter, ImageOps
            image = ImageOps.autocontrast(image.convert("L"))
            image = image.resize((image.width * 2, image.height * 2), Image.Resampling.LANCZOS)
            image = ImageEnhance.Contrast(image).enhance(1.35)
            image = image.filter(ImageFilter.SHARPEN)
            languages = set(pytesseract.get_languages(config=""))
            lang = "chi_sim+eng" if "chi_sim" in languages else "eng"
            if lang == "eng":
                messagebox.showwarning(
                    "提取文字",
                    "未找到中文识别模型 chi_sim，请先安装中文语言包后重试。",
                    parent=self.win)
                return
            text = pytesseract.image_to_string(image, lang=lang, config="--psm 6").strip()
            if text:
                try:
                    self._show_ocr_panel(text, png)
                except Exception as exc:
                    traceback.print_exc()
                    messagebox.showerror("OCR 结果面板", f"识别成功，但结果面板显示失败：{exc}", parent=self.win)
            else:
                messagebox.showinfo("提取文字", "未识别到文字。", parent=self.win)
        except ImportError:
            messagebox.showwarning("提取文字", "请安装 pytesseract 和 Tesseract OCR 后重试。", parent=self.win)
        except Exception as exc:
            if exc.__class__.__name__ == "TesseractNotFoundError":
                messagebox.showwarning(
                    "提取文字",
                    "未找到 Tesseract OCR。请安装 Tesseract，并将其加入 PATH，\n"
                    "或安装到 C:\\Program Files\\Tesseract-OCR。",
                    parent=self.win)
                return
            traceback.print_exc()
            messagebox.showerror(
                "提取文字失败",
                f"OCR 识别失败：{exc}",
                parent=self.win)

    def _show_ocr_panel(self, text, png):
        """显示微信风格的截图预览与 OCR 文字并排结果卡片。"""
        self._clear_ocr_panel()
        from PIL import Image, ImageDraw, ImageTk
        tk = self.ui._tk
        preview = Image.open(io.BytesIO(png)).convert("RGB")
        image_ratio = preview.width / max(1, preview.height)
        # 预览区按原图比例计算，避免横图被压得过高或竖图占满整个面板。
        preview_max_w = min(420, max(250, int(self.sw * .32)))
        preview_max_h = min(380, max(220, int(self.sh * .40)))
        if image_ratio >= 1:
            preview_w = preview_max_w
            preview_h = max(100, int(preview_w / image_ratio))
            if preview_h > preview_max_h:
                preview_h = preview_max_h
                preview_w = max(150, int(preview_h * image_ratio))
        else:
            preview_h = preview_max_h
            preview_w = max(150, int(preview_h * image_ratio))
        # 横向截图的原图高度较小，不能让它把文字栏也压扁；为文字阅读保留固定基准高度。
        content_h = max(preview_h, min(440, max(300, int(self.sh * .48))))
        panel_h = min(max(360, content_h + 96), int(self.sh * .88))
        text_w = min(420, max(300, int(self.sw * .26)))
        panel_w = min(max(580, preview_w + text_w + 48), max(620, int(self.sw * .88)))
        x0, y0, x1, y1 = self._sel_box
        px = x1 + 18
        if px + panel_w > self.sw:
            px = max(12, x0 - panel_w - 18)
        if px + panel_w > self.sw:
            px = max(12, (self.sw - panel_w) // 2)
        py = max(12, min(y0, self.sh - panel_h - 12))

        # OCR 结果卡片独立展示，移除底层原选区和工具栏，避免重复框叠在结果上。
        self._clear_toolbar()
        for item_id in (self._rect_id, self._shot_id, self._size_id):
            if item_id is not None:
                self.canvas.delete(item_id)
        self._rect_id = self._shot_id = self._size_id = None
        self._shot_photo = None
        for item_id in self._edit_items:
            self.canvas.delete(item_id)
        self._edit_items = []
        self._sel_box = None

        # 使用独立透明窗口，避免全屏遮罩 Canvas 的矩形背景露在圆角卡片外。
        popup = tk.Toplevel(self.ui._root)
        popup.overrideredirect(True)
        popup.attributes("-topmost", True)
        transparent = "#010203"
        popup.configure(bg=transparent)
        popup.geometry(f"{panel_w}x{panel_h}+{px}+{py}")
        try:
            popup.attributes("-transparentcolor", transparent)
        except tk.TclError:
            pass
        panel = tk.Canvas(popup, width=panel_w, height=panel_h,
                          bg=transparent, bd=0, highlightthickness=0)
        panel.pack(fill="both", expand=True)
        bg = Image.new("RGBA", (panel_w * 2, panel_h * 2), (0, 0, 0, 0))
        ImageDraw.Draw(bg).rounded_rectangle(
            (1, 1, panel_w * 2 - 2, panel_h * 2 - 2), radius=12 * 2,
            fill="#ffffff", outline="#d5dbe2", width=2)
        self._ocr_bg_photo = ImageTk.PhotoImage(
            bg.resize((panel_w, panel_h), Image.Resampling.LANCZOS))
        panel.create_image(0, 0, image=self._ocr_bg_photo, anchor="nw")
        surface = tk.Frame(panel, bg="#ffffff", bd=0, highlightthickness=0)
        # 内容层避开圆角区域，避免白色矩形在四角露出直角。
        inset = 11
        panel.create_window(inset, inset, window=surface, anchor="nw",
                           width=panel_w - inset * 2, height=panel_h - inset * 2)
        header = tk.Frame(surface, bg="#ffffff", height=20)
        header.pack(fill="x")
        header.pack_propagate(False)
        drag = {}

        def begin_drag(event):
            drag["x"] = event.x_root - popup.winfo_x()
            drag["y"] = event.y_root - popup.winfo_y()

        def move_drag(event):
            popup.geometry(f"+{event.x_root - drag['x']}+{event.y_root - drag['y']}")

        header.configure(cursor="fleur")
        header.bind("<ButtonPress-1>", begin_drag)
        header.bind("<B1-Motion>", move_drag)
        tk.Frame(surface, bg="#edf0f2", height=1).pack(fill="x")
        body = tk.Frame(surface, bg="#f7f8fa")
        body.pack(fill="both", expand=True, padx=8, pady=8)

        preview_w = max(180, min(preview_w + 30, int(panel_w * .50)))
        preview_frame = tk.Frame(body, bg="#f1f3f5", bd=0,
                                 highlightthickness=0)
        preview_frame.pack(side="left", fill="both", padx=(0, 10))
        preview_frame.configure(width=preview_w)
        preview_frame.pack_propagate(False)
        preview_box = tk.Frame(preview_frame, bg="#f0f2f4")
        preview_box.pack(fill="both", expand=True, padx=9, pady=9)
        preview.thumbnail((preview_w - 28, panel_h - 88), Image.LANCZOS)
        self._ocr_photo = ImageTk.PhotoImage(preview)
        tk.Label(preview_box, image=self._ocr_photo, bg="#f0f2f4").pack(expand=True, padx=10, pady=10)

        text_frame = tk.Frame(body, bg="#ffffff", bd=0, highlightthickness=0)
        text_frame.pack(side="left", fill="both", expand=True)
        scrollbar = tk.Scrollbar(text_frame, relief="flat", bd=0,
                                 bg="#d5dadd", troughcolor="#ffffff",
                                 activebackground="#aeb5bb", width=7)
        scrollbar.pack(side="right", fill="y", padx=(0, 4), pady=5)
        text_box = tk.Text(text_frame, wrap="word", undo=False, bd=0,
                           bg="#ffffff", fg="#263238",
                           insertbackground="#07c160", selectbackground="#ccebd9",
                           padx=13, pady=11, spacing1=2, spacing3=4,
                           font=("Microsoft YaHei UI", 11),
                           yscrollcommand=scrollbar.set)
        text_box.pack(side="left", fill="both", expand=True, padx=(4, 0), pady=3)
        scrollbar.config(command=text_box.yview)
        text_box.insert("1.0", text)

        def copy_selected(_event=None):
            try:
                selected = text_box.get("sel.first", "sel.last")
            except tk.TclError:
                selected = ""
            if selected:
                self._copy_ocr_text(selected)
            return "break"

        def select_all(_event=None):
            text_box.tag_add("sel", "1.0", "end-1c")
            text_box.mark_set("insert", "end-1c")
            return "break"

        def block_edit(_event):
            return "break"

        # 保持只读，但允许 Tk 原生选择、Ctrl+C 和 Ctrl+A。
        text_box.configure(state="normal")
        text_box.configure(exportselection=False)
        text_box.bind("<Control-c>", copy_selected, add="+")
        text_box.bind("<Control-C>", copy_selected, add="+")
        text_box.bind("<Control-a>", select_all, add="+")
        text_box.bind("<Control-A>", select_all, add="+")
        text_box.bind("<<Copy>>", copy_selected, add="+")
        for sequence in ("<BackSpace>", "<Delete>", "<Return>", "<Tab>"):
            text_box.bind(sequence, block_edit, add="+")
        footer = tk.Frame(surface, bg="#ffffff", height=50)
        footer.pack(fill="x", padx=14, pady=(0, 9))
        tk.Label(footer, text="可滚动查看 · 内容不会自动复制", bg="#ffffff", fg="#a0a8b0",
                 font=("Microsoft YaHei UI", 9)).pack(side="left", padx=(2, 0), pady=8)
        tk.Button(footer, text="复制文字", command=lambda: self._copy_ocr_text(text),
                  bd=0, bg="#07c160", activebackground="#06ad56", fg="#ffffff",
                  font=("Microsoft YaHei UI", 10, "bold"), relief="flat",
                  cursor="hand2", padx=14, pady=6,
                  highlightthickness=0).pack(side="right", pady=4)
        # 关闭按钮使用稳定的 Tk 控件，避免透明 Canvas 在 Windows 颜色键窗口上报错。
        close_button = tk.Button(
            popup, text="×", command=self.cancel,
            bd=0, relief="flat", highlightthickness=0,
            bg="#ffffff", activebackground="#f1f3f5",
            fg="#7e8994", activeforeground="#d14343",
            font=("Microsoft YaHei UI", 18, "bold"), cursor="hand2",
            padx=0, pady=0)
        close_button.place(x=panel_w - inset - 30, y=max(3, inset - 9),
                           width=30, height=30)
        close_button.lift()

        self._ocr_popup = popup
        self._ocr_panel = panel
        # 根窗口常驻隐藏，结果窗口不能设置 transient，否则会被隐藏根窗口连带隐藏。
        popup.deiconify()
        popup.update_idletasks()
        popup.attributes("-topmost", True)
        popup.lift()
        # 不抢焦点，避免鼠标释放事件回到全屏选区窗口后触发清理。
        popup.after(20, popup.lift)
        # OCR 结果已经独立显示，隐藏全屏截图遮罩，让屏幕恢复正常亮度。
        self.win.withdraw()

    def _copy_ocr_text(self, text):
        self.win.clipboard_clear()
        self.win.clipboard_append(text)
        self.win.update()

    def _clear_ocr_panel(self):
        # OCR 面板关闭后退出截图选择状态，恢复普通鼠标光标。
        try:
            self.canvas.configure(cursor="arrow")
            self.win.configure(cursor="arrow")
        except Exception:
            pass
        if self._ocr_panel_item is not None:
            try:
                self.canvas.delete(self._ocr_panel_item)
            except Exception:
                pass
            self._ocr_panel_item = None
        if self._ocr_popup is not None:
            try:
                self._ocr_popup.destroy()
            except Exception:
                pass
            self._ocr_popup = None
        if self._ocr_panel is not None:
            try:
                self._ocr_panel.destroy()
            except Exception:
                pass
            self._ocr_panel = None
        self._ocr_photo = None
        self._ocr_bg_photo = None

    def _show_toolbar(self):
        """圆角工具栏：PIL 画圆角矩形背景图放到 canvas，按钮直接叠在上面。

        tk.Frame 无法做圆角，而 canvas 图片项支持透明像素，
        四角透出暗色遮罩即呈现圆角效果。
        """
        self._clear_toolbar()
        try:
            self._toolbar_icons()
        except Exception:
            traceback.print_exc()
            return
        from PIL import Image, ImageDraw, ImageTk
        tk = self.ui._tk
        try:
            dpi = max(0.8, min(self.win.winfo_fpixels("1i") / 96.0, 3.0))
        except Exception:
            dpi = 1.0
        size = self._tb_icons["pin"][1]
        pad = max(3, int(round(4 * dpi)))
        gap = max(2, int(round(2 * dpi)))
        border_w = max(1, int(round(dpi)))
        radius = max(6, int(round(10 * dpi)))
        buttons = (("rect", lambda: self._select_tool("rect"), "矩形"),
                   ("oval", lambda: self._select_tool("oval"), "圆形"),
                   ("arrow", lambda: self._select_tool("arrow"), "箭头"),
                   ("brush", lambda: self._select_tool("brush"), "画笔"),
                   ("mosaic", lambda: self._select_tool("mosaic"), "马赛克"),
                   ("text", lambda: self._select_tool("text"), "文字"),
                   ("ocr", self._extract_text, "提取文字"),
                   ("undo", self._undo, "撤销"), ("redo", self._redo_action, "恢复"),
                   ("pin", self._pin, "钉图"), ("download", self._download, "下载"),
                   ("x", self.cancel, "取消"), ("check", self._confirm, "保存"))
        columns = len(buttons)
        rows = 1
        bw = size * columns + gap * (columns - 1) + pad * 2 + border_w * 2
        bh = size * rows + gap * (rows - 1) + pad * 2 + border_w * 2
        radius = min(radius, bh // 2)

        ss = 3
        bg = Image.new("RGBA", (bw * ss, bh * ss), (0, 0, 0, 0))
        dr = ImageDraw.Draw(bg)
        dr.rounded_rectangle([0, 0, bw * ss - 1, bh * ss - 1], radius=radius * ss,
                              fill="#fbfcfe", outline="#c3cbd5", width=border_w * ss)
        self._tb_bg_photo = ImageTk.PhotoImage(bg.resize((bw, bh), Image.LANCZOS))

        x0, y0, x1, y1 = self._sel_box
        tx = min(max(0, x1 - bw), max(0, self.sw - bw))
        ty = y1 + 8
        if ty + bh > self.sh:
            ty = max(0, y0 - bh - 8)

        self._tb_item_ids = [self.canvas.create_image(
            tx, ty, image=self._tb_bg_photo, anchor="nw")]
        inner_x = tx + border_w + pad
        inner_y = ty + border_w + pad
        text_index = next((i for i, item in enumerate(buttons) if item[0] == "text"), None)
        if text_index is not None:
            self._text_tool_anchor = (
                inner_x + text_index * (size + gap) + size // 2,
                inner_y + size)
        # 以细分隔线划分编辑、识别、撤销和输出四组操作。
        for boundary in (6, 7, 9):
            sep_x = inner_x + boundary * (size + gap) - gap // 2
            self._tb_item_ids.append(self.canvas.create_line(
                sep_x, inner_y + 4, sep_x, inner_y + size - 4,
                fill="#dfe5ec", width=1))
        self._tb_button_map = {}
        for i, (key, cmd, tip) in enumerate(buttons):
            row, col = divmod(i, columns)
            photo = self._tb_icons[key][0]
            def run_command(action=cmd, button_key=key):
                if button_key == "undo":
                    self._undo()
                elif button_key == "redo":
                    self._redo_action()
                else:
                    action()
                try:
                    if self.canvas.winfo_exists() and not self.done:
                        self.canvas.focus_set()
                except Exception:
                    pass

            b = tk.Button(self.canvas, image=photo, command=run_command, bd=0,
                           bg="#ffffff", activebackground="#e6eaf2", relief="flat",
                           cursor="hand2", highlightthickness=0, takefocus=0)
            b.image = photo
            b.bind("<Enter>", lambda _event, text=tip: self._show_tooltip(text))
            b.bind("<Leave>", lambda _event: self._hide_tooltip())
            self._tb_buttons.append(b)
            self._tb_button_map[key] = b
            self._tb_item_ids.append(self.canvas.create_window(
                inner_x + col * (size + gap), inner_y + row * (size + gap), window=b, anchor="nw",
                width=size, height=size))
        self._update_tool_button_state()

    def _show_tooltip(self, text):
        self._hide_tooltip()
        self._tooltip = self.ui._tk.Label(self.win, text=text, bg="#222831", fg="#ffffff",
                                          font=("Microsoft YaHei UI", 9), padx=5, pady=2)
        self._tooltip.place(x=self.win.winfo_pointerx() - self.win.winfo_rootx() + 8,
                            y=self.win.winfo_pointery() - self.win.winfo_rooty() + 8)

    def _hide_tooltip(self):
        tooltip = getattr(self, "_tooltip", None)
        if tooltip is not None:
            tooltip.destroy()
            self._tooltip = None

    def _clear_toolbar(self):
        for item_id in self._tb_item_ids:
            try:
                self.canvas.delete(item_id)
            except Exception:
                pass
        self._tb_item_ids = []
        for b in self._tb_buttons:
            try:
                b.destroy()
            except Exception:
                pass
        self._tb_buttons = []
        self._tb_button_map = {}
        self._tb_bg_photo = None

    def _confirm(self):
        if self.done or not self._sel_box:
            return
        if self._text_commit is not None:
            self._text_commit()
        png = self._crop_box()
        if png is None:
            self.cancel()
            return
        self._finish(png, pin=False)

    def _pin(self):
        if self.done or not self._sel_box:
            return
        if self._text_commit is not None:
            self._text_commit()
        png = self._crop_box()
        if png is None:
            self.cancel()
            return
        self._finish(png, pin=True, pos=(self._sel_box[0], self._sel_box[1]))

    def _download(self):
        """另存为对话框下载当前选区；取消则保留选区可继续操作。"""
        if self.done or not self._sel_box:
            return
        if self._text_commit is not None:
            self._text_commit()
        png = self._crop_box()
        if png is None:
            self.cancel()
            return
        from tkinter import filedialog
        default_dir = self.ui.config.get("download_dir") or os.path.join(
            os.path.expanduser("~"), "Pictures", "Lumina")
        try:
            os.makedirs(default_dir, exist_ok=True)
        except OSError:
            default_dir = os.path.expanduser("~")
        fname = time.strftime("lumina_%Y%m%d_%H%M%S.png")
        try:
            path = filedialog.asksaveasfilename(
                parent=self.win, title="保存截图",
                initialdir=default_dir, initialfile=fname,
                defaultextension=".png",
                filetypes=[("PNG 图片", "*.png"), ("所有文件", "*.*")])
        except Exception:
            traceback.print_exc()
            return
        if not path:
            return
        try:
            with open(path, "wb") as f:
                f.write(png)
        except OSError:
            traceback.print_exc()
            return
        self._finish(png, pin=False, source="download")

    def cancel(self):
        if not self.done:
            self._finish(None)

    def _finish(self, png, pin=False, pos=None, source="region"):
        if self.done:
            return
        self.done = True
        self._clear_ocr_panel()
        try:
            self.win.destroy()
        except Exception:
            pass
        self.on_done(png, pin, pos, source)


class HistoryPanel:
    COMPACT_W = 1160
    COMPACT_H = 720
    TRANS_COLOR = "#010203"
    FILTERS = (("all", "全部"), ("text", "文本"), ("code", "代码"),
               ("link", "链接"), ("image", "图片"), ("file", "文件"),
               ("pinned", "收藏"))
    CATEGORY_LABELS = {"text": "文本", "code": "代码", "link": "链接",
                        "image": "图片", "file": "文件"}
    # source 进程名（小写）→ (显示名, 系统色, 渲染风格)
    # 渲染风格：ide=代码风格 term=控制台风格 chat=聊天气泡 browser=地址栏+正文
    SOURCE_APPS = {
        # IDE / 代码编辑器
        "idea64.exe": ("IDEA", "orange", "ide"),
        "pycharm64.exe": ("PyCharm", "green", "ide"),
        "webstorm64.exe": ("WebStorm", "blue", "ide"),
        "goland64.exe": ("GoLand", "teal", "ide"),
        "clion64.exe": ("CLion", "purple", "ide"),
        "rider64.exe": ("Rider", "red", "ide"),
        "datagrip64.exe": ("DataGrip", "indigo", "ide"),
        "dataspell64.exe": ("DataSpell", "orange", "ide"),
        "studio64.exe": ("Android Studio", "green", "ide"),
        "code.exe": ("VS Code", "blue", "ide"),
        "cursor.exe": ("Cursor", "indigo", "ide"),
        "devenv.exe": ("Visual Studio", "purple", "ide"),
        "sublime_text.exe": ("Sublime", "orange", "ide"),
        "notepad++.exe": ("Notepad++", "gray", "ide"),
        # 聊天
        "wechat.exe": ("微信", "green", "chat"),
        "weixin.exe": ("微信", "green", "chat"),
        "wechatapp.exe": ("微信小程序", "green", "chat"),
        "qq.exe": ("QQ", "blue", "chat"),
        "tim.exe": ("TIM", "blue", "chat"),
        "dingtalk.exe": ("钉钉", "blue", "chat"),
        "feishu.exe": ("飞书", "teal", "chat"),
        "telegram.exe": ("Telegram", "teal", "chat"),
        # 浏览器
        "chrome.exe": ("Chrome", "red", "browser"),
        "msedge.exe": ("Edge", "blue", "browser"),
        "firefox.exe": ("Firefox", "orange", "browser"),
        "iexplore.exe": ("IE", "blue", "browser"),
        "360se.exe": ("360浏览器", "green", "browser"),
        "360chrome.exe": ("360浏览器", "green", "browser"),
        "360chromex.exe": ("360浏览器", "green", "browser"),
        # 终端
        "cmd.exe": ("命令提示符", "gray", "term"),
        "powershell.exe": ("PowerShell", "indigo", "term"),
        "pwsh.exe": ("PowerShell", "indigo", "term"),
        "windowsterminal.exe": ("终端", "gray", "term"),
        "wt.exe": ("终端", "gray", "term"),
        # Office / 文档
        "winword.exe": ("Word", "blue", "office"),
        "excel.exe": ("Excel", "green", "office"),
        "powerpnt.exe": ("PPT", "red", "office"),
        "wps.exe": ("WPS", "blue", "office"),
        "et.exe": ("WPS表格", "green", "office"),
        "wpp.exe": ("WPS演示", "red", "office"),
        # 系统 / Lumina 虚拟来源
        "explorer.exe": ("资源管理器", "teal", "app"),
        "screenshot": ("截图", "purple", "shot"),
        "region": ("截图", "purple", "shot"),
        "fullscreen": ("截图", "purple", "shot"),
        "download": ("图片下载", "purple", "shot"),
    }

    # ---------- Apple 设计令牌 ----------
    # 系统色板：(浅色, 深色)，取自 iOS/macOS system colors
    SYS_COLORS = {
        "blue":   ("#007AFF", "#0A84FF"),
        "green":  ("#34C759", "#30D158"),
        "indigo": ("#5856D6", "#5E5CE6"),
        "orange": ("#FF9500", "#FF9F0A"),
        "pink":   ("#FF2D55", "#FF375F"),
        "purple": ("#AF52DE", "#BF5AF2"),
        "red":    ("#FF3B30", "#FF453A"),
        "teal":   ("#5AC8FA", "#64D2FF"),
        "gray":   ("#8E8E93", "#98989D"),
    }
    # 语义令牌：(浅色, 深色)
    THEME = {
        "window_bg":   ("#F2F2F7", "#161618"),  # systemGroupedBackground
        "card_bg":     ("#FFFFFF", "#2C2C2E"),  # secondarySystemGroupedBackground
        "field":       ("#E7E7EA", "#2C2C2E"),  # 搜索/输入填充
        "fill":        ("#E9E9EB", "#323236"),  # 胶囊/按钮填充
        "fill_hover":  ("#DCDCE1", "#3E3E44"),
        "separator":   ("#D1D1D6", "#3A3A3C"),
        "hairline":    ("#C7C7CC", "#45454A"),
        "label":       ("#1C1C1E", "#F5F5F7"),
        "label2":      ("#6C6C70", "#9A9AA0"),
        "label3":      ("#94949A", "#6C6C72"),
        "accent":      ("#007AFF", "#0A84FF"),
        "accent_fg":   ("#FFFFFF", "#FFFFFF"),
        "accent_soft": ("#E5F0FF", "#1E3050"),  # 选中/悬停的强调淡底
        "shadow":      ("#9AA0A8", "#000000"),
    }
    # 分类 → (系统色名, 字形名)
    CAT_STYLE = {
        "text":  ("gray",   "doc"),
        "code":  ("orange", "code"),
        "link":  ("blue",   "link"),
        "image": ("purple", "photo"),
        "file":  ("green",  "folder"),
    }
    FONT_FAMILY = "Microsoft YaHei UI"

    def __init__(self, root, tk, ui):
        self.ui = ui
        self.db = ui.db
        self.tk = tk
        self._root = root
        self._dark = self._is_dark_mode()
        self._prev_hwnd = None
        self._rows = {}
        self._filter = "all"
        self._thumb_cache = {}
        self._tile_cache = {}
        self._cardbg_cache = {}
        self._marker_cache = {}
        self._preview_cache = {}
        self._drag_off = None
        self._maxed = False
        self._normal_geom = None
        self._sel_id = None
        self._hover_id = None
        self._row_rects = []
        self._row_bg_items = {}
        self._list_canvas = None
        self._card_w = 0
        self._row_h = 0
        self._rows_displayed = []
        self._canvas_w = None
        self._relayout_id = None
        self._detail = None
        self._detail_hover_id = None
        self._flat_menu = None

        self.win = tk.Toplevel(root)
        self.win.title("Lumina 历史面板")
        self.win.protocol("WM_DELETE_WINDOW", self.hide)
        self._build_widgets()
        self.win.withdraw()

    def _build_widgets(self):
        tk = self.tk
        self.search_var = tk.StringVar()
        self.status_var = tk.StringVar(value="就绪")
        self._hide_on_capture = tk.BooleanVar(
            value=bool(self.ui.config.get("hide_panel_on_capture", True)))
        self._popup_menu = None
        self._popup_open = False
        self._popup_focus_check = None
        self._popup_outside_binding = None
        self._menu_anchor = None
        self._menu_kind = None
        self._search_active = False
        self._search_focus_check = None

        self.win.bind("<F5>", lambda e: self.refresh())
        self.win.bind("<Escape>", lambda e: self.hide())
        self.win.bind("<Delete>", lambda e: (self.delete_selected(), "break")[1])
        self.win.bind("<Control-p>", lambda e: (self.toggle_pin(), "break")[1])
        self.win.bind("<Control-P>", lambda e: (self.toggle_pin(), "break")[1])
        self.win.bind("<Control-c>", lambda e: (self.copy_only(), "break")[1])
        self.win.bind("<Return>", lambda e: self.paste_back())
        self.win.bind("<Up>", lambda e: (self._move_selection(-1), "break")[1])
        self.win.bind("<Down>", lambda e: (self._move_selection(1), "break")[1])
        self.win.bind("<Home>", lambda e: (self._move_selection(-len(self._rows_displayed)), "break")[1])
        self.win.bind("<End>", lambda e: (self._move_selection(len(self._rows_displayed)), "break")[1])

        self._debounce_id = None
        self._build_compact()
        self._bind_extra_keys()
        self.apply_mode()

    def _is_dark_mode(self):
        """跟随 Windows 应用主题：AppsUseLightTheme==0 表示深色。"""
        try:
            import winreg
            with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
                val = winreg.QueryValueEx(k, "AppsUseLightTheme")[0]
                return val == 0
        except Exception:
            pass
        return False

    def _theme(self):
        """返回 (色板字典, is_dark)，按当前系统主题取值。"""
        idx = 1 if self._is_dark_mode() else 0
        return {k: v[idx] for k, v in self.THEME.items()}, bool(idx)

    def _c(self, token):
        """单个语义色，如 self._c('card_bg')。"""
        idx = 1 if self._is_dark_mode() else 0
        return self.THEME[token][idx]

    def _sys(self, name):
        """单个系统色，如 self._sys('blue')。"""
        idx = 1 if self._is_dark_mode() else 0
        return self.SYS_COLORS.get(name, self.SYS_COLORS["gray"])[idx]

    def _source_app(self, source):
        """source 字段 → (显示名, 系统色名, 渲染风格)。

        未收录的进程回退为去 .exe 的进程名 + 默认风格。
        """
        s = (source or "").strip()
        hit = self.SOURCE_APPS.get(s.lower())
        if hit:
            return hit
        name = s[:-4] if s.lower().endswith(".exe") else s
        return (name or "未知来源", "gray", "app")

    def _source_mark(self, source):
        """Return a compact source mark for the detail header."""
        key = (source or "").strip().lower()
        marks = {
            "explorer.exe": "EX",
            "code.exe": "<>" ,
            "devenv.exe": "VS",
            "idea64.exe": "IJ",
            "pycharm64.exe": "PC",
            "webstorm64.exe": "WS",
            "goland64.exe": "GO",
            "clion64.exe": "CL",
            "rider64.exe": "RD",
            "studio64.exe": "AS",
            "wechat.exe": "WX",
            "weixin.exe": "WX",
            "chrome.exe": "CH",
            "msedge.exe": "ED",
            "firefox.exe": "FF",
            "powershell.exe": "PS",
            "cmd.exe": "CMD",
        }
        if key in marks:
            return marks[key]
        name = self._source_app(source)[0]
        return (name[:2] or "?").upper()

    def _font(self, size, weight="normal"):
        return (self.FONT_FAMILY, size, weight)

    @staticmethod
    def _hex_to_rgb(h):
        h = h.lstrip("#")
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))

    def _canvas_bg(self):
        return self._c("window_bg")

    def _sync_theme(self):
        """系统明暗切换后整窗重建：颜色/图标/位图缓存都与主题绑定，
        逐个控件改色易漏，直接销毁重建最稳妥。"""
        if self._dark == self._is_dark_mode():
            return False
        root, tk, ui = self._root, self.tk, self.ui
        sel = self._sel_id
        maxed = self._maxed
        self._close_popup_menu()
        try:
            self.win.destroy()
        except Exception:
            pass
        for cache in (self._thumb_cache, self._tile_cache,
                      self._cardbg_cache, self._marker_cache,
                      self._preview_cache):
            cache.clear()
        self.__init__(root, tk, ui)
        self._sel_id = sel
        self._maxed = maxed
        return True

    # ---------- 分类彩色图标 tile（SF Symbols 风格） ----------
    def _category_tile(self, cat, size):
        """圆角彩色底 + 白色字形；按 (类别, 尺寸, 明暗) 缓存 PIL 图。"""
        dark = self._is_dark_mode()
        size = max(8, int(size))
        key = (cat, size, dark)
        hit = self._tile_cache.get(key)
        if hit is not None:
            return hit
        from PIL import Image, ImageDraw
        color_name, glyph = self.CAT_STYLE.get(cat, self.CAT_STYLE["text"])
        rgb = self._hex_to_rgb(self.SYS_COLORS[color_name][1 if dark else 0])
        ss = 2
        s = size * ss
        im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        dr = ImageDraw.Draw(im)
        dr.rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.24),
                             fill=rgb + (255,))
        self._draw_glyph(dr, glyph, s, (255, 255, 255, 255))
        out = im.resize((size, size), Image.LANCZOS)
        self._tile_cache[key] = out
        return out

    def _source_code_tile(self, source, size):
        """Source tile showing the complete application name."""
        dark = self._is_dark_mode()
        app_name, color_name, _style = self._source_app(source)
        source_key = (source or "").lower()
        colors = {
            "idea64.exe": "#FF7A00",
            "pycharm64.exe": "#21D789",
            "webstorm64.exe": "#00B8F5",
            "goland64.exe": "#20D5C2",
            "clion64.exe": "#F2C94C",
            "rider64.exe": "#9B51E0",
            "datagrip64.exe": "#FF5C7A",
            "dataspell64.exe": "#FF9F43",
            "studio64.exe": "#3DDC84",
            "code.exe": "#1683FF",
            "cursor.exe": "#6C63FF",
            "devenv.exe": "#8B5CF6",
            "sublime_text.exe": "#FF9800",
        }
        mark = app_name
        color = colors.get(source_key, self._sys(color_name))
        key = ("source", source_key, mark, size, dark)
        hit = self._tile_cache.get(key)
        if hit is not None:
            return hit
        from PIL import Image, ImageDraw
        size = max(8, int(size))
        ss = 2
        s = size * ss
        im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        dr = ImageDraw.Draw(im)
        bg = self._hex_to_rgb(color)
        dr.rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.24),
                             fill=bg + (255,))
        font_size = max(8, int(s * 0.32))
        font = self._load_font(font_size)
        while font_size > 8:
            bbox = dr.textbbox((0, 0), mark, font=font)
            if bbox[2] - bbox[0] <= s * 0.86 and bbox[3] - bbox[1] <= s * 0.72:
                break
            font_size -= 1
            font = self._load_font(font_size)
        bbox = dr.textbbox((0, 0), mark, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        fg = (255, 255, 255, 255)
        dr.text(((s - tw) / 2 - bbox[0], (s - th) / 2 - bbox[1]),
                mark, font=font, fill=fg)
        out = im.resize((size, size), Image.LANCZOS)
        self._tile_cache[key] = out
        return out

    def _file_extension_tile(self, ext, size, exists=True):
        """Create the shared file-extension tile used by list and preview."""
        from PIL import Image, ImageDraw, ImageTk

        ext = (ext or "").lower()
        label = "文件夹" if ext == "folder" else (ext.lstrip(".").upper()[:4] or "文件")
        color_name = "gray" if not exists else DetailPane.EXT_COLORS.get(
            ext, "teal")
        key = ("file-ext", ext, label, size, exists, self._is_dark_mode())
        hit = self._tile_cache.get(key)
        if hit is not None:
            return hit
        size = max(8, int(size))
        ss = 2
        s = size * ss
        color = self._hex_to_rgb(self._sys(color_name))
        image = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((0, 0, s - 1, s - 1), radius=int(s * 0.22),
                               fill=color + (255,))
        font = self._load_font(max(8, int(s * (0.25 if len(label) > 3 else 0.34))))
        bbox = draw.textbbox((0, 0), label, font=font)
        draw.text(((s - bbox[2] + bbox[0]) / 2,
                   (s - bbox[3] + bbox[1]) / 2), label,
                  font=font, fill=(255, 255, 255, 255))
        out = ImageTk.PhotoImage(image.resize((size, size), Image.LANCZOS))
        self._tile_cache[key] = out
        return out

    def _draw_glyph(self, dr, name, s, color):
        """在 s×s 画布内绘制白色 SF Symbols 风格字形。"""
        w = max(2, int(round(s * 0.075)))
        wt = max(2, w - 1)
        if name == "code":
            dr.line([(s * 0.36, s * 0.34), (s * 0.24, s * 0.50), (s * 0.36, s * 0.66)],
                    fill=color, width=w, joint="curve")
            dr.line([(s * 0.64, s * 0.34), (s * 0.76, s * 0.50), (s * 0.64, s * 0.66)],
                    fill=color, width=w, joint="curve")
            dr.line([(s * 0.56, s * 0.30), (s * 0.44, s * 0.70)], fill=color, width=w)
        elif name == "link":
            cx, cy, r = s / 2, s / 2, s * 0.26
            dr.ellipse([cx - r, cy - r, cx + r, cy + r], outline=color, width=w)
            dr.ellipse([cx - r * 0.45, cy - r, cx + r * 0.45, cy + r],
                       outline=color, width=wt)
            dr.line([(cx - r, cy), (cx + r, cy)], fill=color, width=wt)
        elif name == "photo":
            m = s * 0.25
            dr.rounded_rectangle([m, m, s - m, s - m], radius=s * 0.06,
                                 outline=color, width=w)
            dr.ellipse([s * 0.33, s * 0.33, s * 0.43, s * 0.43], fill=color)
            dr.polygon([(s * 0.31, s * 0.67), (s * 0.46, s * 0.50),
                        (s * 0.61, s * 0.67)], fill=color)
        elif name == "folder":
            dr.rounded_rectangle([s * 0.22, s * 0.40, s * 0.78, s * 0.72],
                                 radius=s * 0.05, outline=color, width=w)
            dr.line([(s * 0.24, s * 0.40), (s * 0.24, s * 0.33), (s * 0.42, s * 0.33),
                     (s * 0.47, s * 0.40)], fill=color, width=w, joint="curve")
        else:  # doc / text
            dr.rounded_rectangle([s * 0.31, s * 0.23, s * 0.69, s * 0.77],
                                 radius=s * 0.06, outline=color, width=w)
            for yy in (0.40, 0.52, 0.64):
                dr.line([(s * 0.39, s * yy), (s * 0.61, s * yy)], fill=color, width=wt)

    def _on_canvas_configure(self, event):
        canvas = event.widget
        bbox = canvas.bbox("all")
        if bbox and bbox != (0, 0, 0, 0):
            canvas.configure(scrollregion=bbox)
        w = event.width
        if w > 1 and self._canvas_w != w:
            self._canvas_w = w
            if self._relayout_id is not None:
                try:
                    canvas.after_cancel(self._relayout_id)
                except Exception:
                    pass
            self._relayout_id = canvas.after(30, self._relayout_cards)

    def _relayout_cards(self):
        """画布宽度变化（首次映射/全屏切换）后按真实宽度重渲染卡片。"""
        self._relayout_id = None
        if not self.is_visible():
            return
        canvas = self._active_canvas()
        if canvas is None or canvas.winfo_width() <= 1:
            return
        self._refresh_cards(self._rows_displayed)

    def _on_canvas_click(self, event):
        canvas = self._active_canvas()
        if canvas is None or event.widget is not canvas:
            return
        # 星标图像含透明区域，Canvas 的图元命中在不同 Tk 版本上不稳定。
        # 用每行右侧固定热区兜底，确保点击星标始终能触发收藏。
        clip_id = self._get_clip_id_from_event(canvas, event)
        try:
            canvas_x = canvas.canvasx(event.x)
            favorite_zone = self._card_w - max(44, int(round(30 * self._dpi)))
            if (clip_id is not None and
                    canvas_x >= int(round(12 * self._dpi)) + favorite_zone):
                self._favorite_click(clip_id)
                return "break"
        except (TypeError, ValueError):
            pass
        for item in reversed(canvas.find_overlapping(
                event.x, event.y, event.x, event.y)):
            tags = canvas.gettags(item)
            favorite_tag = next((tag for tag in tags
                                 if tag.startswith("favorite:")), None)
            if favorite_tag:
                self._favorite_click(int(favorite_tag.split(":", 1)[1]))
                return "break"
        if clip_id is None:
            return
        self._select(clip_id)
        # 左键只选择记录，不自动切回原窗口，避免面板被 PowerShell 等窗口盖住。
        # 回贴仍可通过 Enter、Ctrl+V 或详情区的回贴操作执行。
        return "break"

    def _on_canvas_motion(self, event):
        canvas = self._active_canvas()
        if canvas is None or event.widget is not canvas:
            return
        clip_id = self._get_clip_id_from_event(canvas, event)
        cid = str(clip_id) if clip_id is not None else None
        if cid != self._hover_id:
            old = self._hover_id
            self._hover_id = cid
            if old is not None:
                self._apply_row_bg(old)
            if cid is not None:
                self._apply_row_bg(cid)
        self._detail_follow(cid)

    def _on_canvas_leave(self, event):
        self._detail_follow(None)

    def _detail_follow(self, cid):
        """悬停跟随：指针所在行的详情防抖渲染；离开列表回到选中行。"""
        if self._detail is None or cid == self._detail_hover_id:
            return
        self._detail_hover_id = cid
        self._detail.show(cid if cid is not None else self._sel_id)

    def _on_canvas_rightclick(self, event):
        canvas = self._active_canvas()
        if canvas is None or event.widget is not canvas:
            return
        clip_id = self._get_clip_id_from_event(canvas, event)
        if clip_id is None:
            return
        self._select(clip_id)
        self._context_menu(clip_id, event.x_root, event.y_root)

    def _on_canvas_wheel(self, event):
        event.widget.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _on_list_scroll(self, first, last):
        """紧凑列表的 Apple 风格细滚动指示条（画在卡片画布右缘）。"""
        canvas = getattr(self, "cards_canvas", None)
        if canvas is None:
            return
        try:
            h = canvas.winfo_height()
            if h <= 1:
                return
            f, l = float(first), float(last)
            if l - f >= 0.999:
                if self._scroll_ind is not None:
                    canvas.delete(self._scroll_ind)
                    self._scroll_ind = None
                return
            y0 = max(2, f * h)
            y1 = min(h - 2, l * h)
            if y1 - y0 < 24:
                y1 = min(h - 2, y0 + 24)
            w = max(3, int(round(4 * self._dpi)))
            x1 = canvas.winfo_width() - 3
            x0 = x1 - w
            col = self._c("label3")
            if self._scroll_ind is None or not canvas.winfo_exists():
                self._scroll_ind = canvas.create_rectangle(
                    x0, y0, x1, y1, fill=col, outline="", width=0,
                    tags=("scrollind",))
            else:
                canvas.coords(self._scroll_ind, x0, y0, x1, y1)
                canvas.itemconfigure(self._scroll_ind, fill=col)
            canvas.tag_raise(self._scroll_ind)
        except Exception:
            pass

    def _paste_row(self, row):
        try:
            full = self._load_full(row) or row
            if not self.ui.actions.copy_to_clipboard(full):
                return
            prev = self._prev_hwnd
            if prev:
                import ctypes
                user32 = ctypes.windll.user32
                user32.SetForegroundWindow(prev)
                time.sleep(0.12)
                self._send_ctrl_v()
        except Exception:
            traceback.print_exc()

    @staticmethod
    def _send_ctrl_v():
        VK_CONTROL, VK_V = 0x11, 0x56
        KEYEVENTF_KEYUP = 0x0002
        user32 = ctypes.windll.user32
        user32.keybd_event(VK_CONTROL, 0, 0, 0)
        user32.keybd_event(VK_V, 0, 0, 0)
        time.sleep(0.03)
        user32.keybd_event(VK_V, 0, KEYEVENTF_KEYUP, 0)
        user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)

    def _get_clip_id_from_event(self, canvas, event):
        """按 y 坐标命中当前渲染的行（考虑滚动偏移）。"""
        return self._hit_row(canvas, event.y)

    def _hit_row(self, canvas, y):
        try:
            cy = canvas.canvasy(y)
        except Exception:
            cy = y
        for y0, y1, cid in self._row_rects:
            if y0 <= cy <= y1:
                return cid
        return None

    def _context_menu(self, clip_id, x, y):
        row = self._rows.get(str(clip_id))
        if not row:
            return
        cat = self._cat_of(row)
        items = [("仅复制  (Ctrl+C)", self.copy_only),
                 ("收藏/取消收藏  (Ctrl+P)", self.toggle_pin),
                 ("导出…", self._export_selected),
                 None,
                 ("删除  (Del)", self.delete_selected)]
        if cat == "file":
            items.insert(0, ("打开", self._open_file_from_row(row)))
            items.insert(1, ("在资源管理器中显示", self._reveal_file_from_row(row)))
        self._flat_menu = FlatMenu(self.tk, self.win, items, theme=self._theme()[0])
        self._flat_menu.popup(x, y)

    def _open_file_from_row(self, row):
        def _action():
            full = self.db.get(row["id"])
            path = (full["content"].splitlines()[0].strip()
                    if full and full["content"] else "")
            if path and os.path.exists(path):
                os.startfile(path)
        return _action

    def _reveal_file_from_row(self, row):
        def _action():
            full = self.db.get(row["id"])
            path = (full["content"].splitlines()[0].strip()
                    if full and full["content"] else "")
            if path:
                import subprocess
                subprocess.Popen(["explorer", "/select," + path])
        return _action

    # ---------- 可见性 ----------
    def is_visible(self):
        try:
            return bool(self.win.winfo_viewable())
        except Exception:
            return False

    def show(self, prev_hwnd=None):
        self._sync_theme()
        self._prev_hwnd = prev_hwnd
        self.apply_mode()  # 重设边框/尺寸/位置（光标附近或全屏态）并刷新
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()
        if self.search_var.get().strip():
            self._activate_search()
        self.ui.panel_visible = True

    def hide(self):
        self._close_popup_menu()
        self._deactivate_search(refocus=False)
        if self._detail is not None:
            self._detail._close_menu()
        try:
            self.win.withdraw()
        except Exception:
            pass
        self.ui.panel_visible = False

    # ---------- 顶栏下拉弹窗（截图 / 设置） ----------
    # 不用 Tk 原生菜单：Windows 下 menu.post 会进入模态跟踪循环，
    # 在其 Unmap 回调里重新 post 会让 UI 线程死锁，且无法自动化验证。
    # 自绘 overrideredirect 弹窗行为完全可控：勾选项点击不收起，Esc/失焦收起。
    def _toggle_popup_menu(self, kind, anchor):
        if self._popup_open and self._menu_kind == kind:
            self._close_popup_menu()
            return
        self._close_popup_menu()
        self._menu_kind = kind
        self._menu_anchor = anchor
        self._open_popup_menu(kind)

    def _open_popup_menu(self, kind):
        tk = self.tk
        T, dark = self._theme()
        pop = tk.Toplevel(self.win)
        pop.overrideredirect(True)
        pop.configure(bg=T["hairline"])
        frame = tk.Frame(pop, bg=T["card_bg"])
        frame.pack(fill="both", expand=True, padx=7, pady=7)
        f = self._font(11)
        btn_kw = dict(anchor="w", relief="flat", font=f, bd=0, cursor="hand2",
                      bg=T["card_bg"], fg=T["label"],
                      activebackground=T["fill_hover"], activeforeground=T["label"],
                      highlightthickness=0, padx=16, pady=7)

        def sm_icon(draw_fn, size, color_name="label", color_hex=None):
            """创建一个小尺寸图标 PhotoImage，供弹出菜单项右侧使用。"""
            from PIL import Image, ImageDraw, ImageTk
            s = size * 2  # 超采样保证平滑
            rgb = self._hex_to_rgb(color_hex or self._sys(color_name)) + (255,)
            im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
            draw_fn(ImageDraw.Draw(im), s, rgb, max(1, int(s * 0.062)))
            return ImageTk.PhotoImage(im.resize((size, size), Image.LANCZOS))

        def sel_icon(dr, s, c, w):
            """选区框：四个角括号"""
            w = int(round(w))
            m = s * 0.28
            dr.line([(m, s*0.12), (m, s*0.28), (s*0.12, s*0.28)], fill=c, width=w)
            dr.line([(s-m, s*0.12), (s-m, s*0.28), (s*0.88, s*0.28)], fill=c, width=w)
            dr.line([(m, s*0.88), (m, s*0.72), (s*0.12, s*0.72)], fill=c, width=w)
            dr.line([(s-m, s*0.88), (s-m, s*0.72), (s*0.88, s*0.72)], fill=c, width=w)

        def eye_icon(dr, s, c, w):
            """眼睛：可见/隐藏"""
            w = int(round(w))
            cx, cy = s*0.5, s*0.5
            r = s*0.22
            dr.ellipse([cx-r, cy-r, cx+r, cy+r], outline=c, width=w)
            dr.ellipse([cx-s*0.06, cy-s*0.06, cx+s*0.06, cy+s*0.06], fill=c)
            dr.arc([cx-r*0.3, cy-r*0.55, cx+r*0.3, cy+r*0.55],
                   start=-60, end=60, fill=c, width=w)

        def refresh_icon(dr, s, c, w):
            """循环刷新箭头"""
            import math
            w = int(round(w))
            cx, cy = s*0.5, s*0.5
            r = s*0.30
            dr.ellipse([cx-r, cy-r, cx+r, cy+r], outline=c, width=w)
            for a in range(0, 360, 60):
                rad = math.pi*a/180
                x1 = cx + math.cos(rad)*r*0.75
                y1 = cy + math.sin(rad)*r*0.75
                x2 = cx + math.cos(rad)*r*0.92
                y2 = cy + math.sin(rad)*r*0.92
                dr.line([(x1,y1),(x2,y2)], fill=c, width=w)
            dr.line([(cx+s*0.18, cy-s*0.18), (cx-s*0.04, cy-s*0.30), (cx-s*0.26, cy-s*0.14)],
                    fill=c, width=w)

        def trash_icon(dr, s, c, w):
            """垃圾桶"""
            w = int(round(w))
            dr.line([(s*0.24, s*0.22), (s*0.24, s*0.78), (s*0.76, s*0.78)], fill=c, width=w*2)
            dr.line([(s*0.24, s*0.78), (s*0.76, s*0.78)], fill=c, width=w)
            for yy in (s*0.34, s*0.46):
                dr.line([(s*0.34, yy), (s*0.66, yy)], fill=c, width=w)

        def fullscreen_icon(dr, s, c, w):
            """显示器加取景框：用于全屏截图。"""
            w = int(round(w))
            left, top, right, bottom = s * 0.18, s * 0.22, s * 0.82, s * 0.68
            dr.rounded_rectangle([left, top, right, bottom],
                                 radius=s * 0.05, outline=c, width=w)
            dr.line([(s * 0.50, bottom), (s * 0.50, s * 0.80)],
                    fill=c, width=w)
            dr.line([(s * 0.34, s * 0.82), (s * 0.66, s * 0.82)],
                    fill=c, width=w)
            corner = s * 0.19
            for x, y, dx, dy in (
                    (s * 0.30, s * 0.36, 1, 1),
                    (s * 0.70, s * 0.36, -1, 1),
                    (s * 0.30, s * 0.58, 1, -1),
                    (s * 0.70, s * 0.58, -1, -1)):
                dr.line([(x, y), (x + dx * corner, y)], fill=c, width=w)
                dr.line([(x, y), (x, y + dy * corner)], fill=c, width=w)

        if kind == "shot":
            cut_photo = sm_icon(fullscreen_icon, 18, color_hex=self._sys("blue"))
            cut_button = tk.Button(
                frame, text="全屏截图", image=cut_photo, compound="right",
                command=lambda: self._popup_action(self.capture_fullscreen),
                **btn_kw)
            cut_button.image = cut_photo
            cut_button.pack(fill="x", padx=4, pady=(4, 1))
            sel_photo = sm_icon(sel_icon, 18, color_hex=self._sys("purple"))
            sel_button = tk.Button(
                frame, text="区域截图", image=sel_photo, compound="right",
                command=lambda: self._popup_action(self.capture_region),
                **btn_kw)
            sel_button.image = sel_photo
            sel_button.pack(fill="x", padx=4, pady=1)
            tk.Frame(frame, bg=T["separator"], height=1).pack(
                fill="x", padx=6, pady=4)
            eye_photo = sm_icon(eye_icon, 18, color_hex=self._sys("gray"))
            eye_button = tk.Checkbutton(
                frame, text="隐藏此窗口", image=eye_photo, compound="right",
                variable=self._hide_on_capture,
                command=self._hide_on_capture_changed, anchor="w",
                font=f, bg=T["card_bg"], fg=T["label"],
                selectcolor=T["field"], activebackground=T["card_bg"],
                activeforeground=T["label"], highlightthickness=0,
                cursor="hand2")
            eye_button.image = eye_photo
            eye_button.pack(fill="x", padx=6, pady=(0, 4))
        else:
            refresh_photo = sm_icon(refresh_icon, 18, color_hex=self._sys("blue"))
            refresh_button = tk.Button(
                frame, text="刷新  (F5)", image=refresh_photo, compound="right",
                command=lambda: self._popup_action(self.refresh), **btn_kw)
            refresh_button.image = refresh_photo
            refresh_button.pack(fill="x", padx=4, pady=(4, 1))
            trash_photo = sm_icon(trash_icon, 18, color_hex=self._sys("red"))
            trash_button = tk.Button(
                frame, text="清理过期记录", image=trash_photo, compound="right",
                command=lambda: self._popup_action(self._cleanup_now), **btn_kw)
            trash_button.image = trash_photo
            trash_button.pack(fill="x", padx=4, pady=(1, 4))
            tk.Frame(frame, bg=T["separator"], height=1).pack(
                fill="x", padx=6, pady=(3, 4))
            tk.Button(frame, text="设置…",
                      command=lambda: self._popup_action(self._open_settings_dialog),
                      **btn_kw).pack(fill="x", padx=4, pady=(0, 4))
        pop.bind("<Escape>", lambda e: self._close_popup_menu())
        btn = self._menu_anchor
        btn.update_idletasks()
        pop.update_idletasks()
        x = btn.winfo_rootx()
        y = btn.winfo_rooty() + btn.winfo_height() + 4
        sw = self.win.winfo_screenwidth()
        if x + pop.winfo_reqwidth() > sw - 8:
            x = max(0, sw - 8 - pop.winfo_reqwidth())
        pop.geometry(f"+{x}+{y}")
        self._round_popup_background(pop, frame, T)
        pop.deiconify()
        pop.lift()
        self._popup_menu = pop
        self._popup_open = True
        self._popup_outside_binding = self.win.bind_all(
            "<Button-1>", self._popup_outside_click, add="+")

    def _round_popup_background(self, pop, frame, theme):
        """Paint a rounded popup shell behind the menu controls."""
        from PIL import Image, ImageDraw, ImageTk

        pop.update_idletasks()
        width = max(1, pop.winfo_reqwidth())
        height = max(1, pop.winfo_reqheight())
        scale = 2
        image = Image.new("RGBA", (width * scale, height * scale), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        fill = self._hex_to_rgb(theme["card_bg"]) + (255,)
        border = self._hex_to_rgb(theme["hairline"]) + (255,)
        draw.rounded_rectangle(
            (0, 0, width * scale - 1, height * scale - 1),
            radius=10 * scale, fill=fill, outline=border, width=scale)
        photo = ImageTk.PhotoImage(image.resize((width, height), Image.LANCZOS))
        background = self.tk.Label(pop, image=photo, bd=0, highlightthickness=0)
        background.image = photo
        background.place(x=0, y=0, relwidth=1, relheight=1)
        background.lower()
        pop.configure(bg=theme["card_bg"])

    def _open_settings_dialog(self):
        """Open the editable config.json settings dialog."""
        tk = self.tk
        T, _dark = self._theme()
        dialog = tk.Toplevel(self.win)
        dialog.title("Lumina 偏好设置")
        dialog.transient(self.win)
        dialog.configure(bg=T["window_bg"])
        dialog.resizable(False, False)
        dialog.geometry("520x540")
        shell = tk.Frame(dialog, bg=T["window_bg"])
        shell.pack(fill="both", expand=True, padx=12, pady=10)
        content = tk.Frame(shell, bg=T["window_bg"])
        content.pack(fill="both", expand=True)
        canvas = tk.Canvas(content, bg=T["window_bg"], highlightthickness=0,
                           bd=0)
        scrollbar = tk.Scrollbar(content, orient="vertical",
                                 command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y", padx=(8, 0))
        body = tk.Frame(canvas, bg=T["window_bg"])
        body_window = canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", lambda _e: canvas.configure(
            scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(
            body_window, width=e.width))
        canvas.bind("<MouseWheel>",
                    lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))
        fields = {}

        def scroll_settings(event):
            canvas.yview_scroll(int(-event.delta / 120), "units")
            return "break"

        def bind_scroll(widget):
            widget.bind("<MouseWheel>", scroll_settings, add="+")
            for child in widget.winfo_children():
                bind_scroll(child)

        def section(title):
            # 连续表单布局：模块不显示标题，仅保留少量间距。
            section_frame = tk.Frame(body, bg=T["window_bg"])
            section_frame.pack(fill="x", pady=(5, 0))
            return section_frame

        def add_row(label, key, kind="text", parent=body):
            row_bg = T["window_bg"]
            line = tk.Frame(parent, bg=row_bg)
            line.pack(fill="x", padx=14, pady=4)
            tk.Label(line, text=label, width=17, anchor="w",
                     bg=row_bg, fg=T["label"],
                     font=self._font(9)).pack(side="left")
            if kind == "bool":
                var = tk.BooleanVar(value=bool(self.ui.config.get(key, False)))
                tk.Checkbutton(line, text="启用", variable=var,
                               onvalue=True, offvalue=False, indicatoron=True,
                               bg=row_bg, activebackground=row_bg,
                               selectcolor="#DCEBFF", highlightthickness=0,
                               activeforeground=T["label"], fg=T["label"],
                               cursor="hand2", font=self._font(9)).pack(side="left")
            else:
                var = tk.StringVar(value=str(self.ui.config.get(key, "")))
                tk.Entry(line, textvariable=var, width=24, relief="flat",
                         bg=T["field"], fg=T["label"],
                         insertbackground=T["label"],
                         highlightthickness=1, highlightbackground=T["separator"],
                         highlightcolor=T["accent"],
                         font=self._font(9)).pack(side="left", ipady=2)
            fields[key] = (var, kind)

        shortcut_card = section("快捷键")
        add_row("全屏截图", "hotkey_capture", parent=shortcut_card)
        add_row("区域截图", "hotkey_region", parent=shortcut_card)
        add_row("打开面板", "hotkey_panel", parent=shortcut_card)
        capture_card = section("通知与截图")
        add_row("启用面板", "panel_enabled", "bool", capture_card)
        add_row("截图后复制到剪贴板", "copy_screenshot_to_clipboard", "bool", capture_card)
        add_row("截图显示器", "capture_monitor", parent=capture_card)
        add_row("归档复制的文件", "archive_files", "bool", capture_card)
        add_row("开机自动启动", "autostart", "bool", capture_card)
        add_row("单文件上限（MB）", "max_file_mb", parent=capture_card)
        add_row("下载目录", "download_dir", parent=capture_card)
        data_card = section("数据保留")
        add_row("保留天数", "retention_days", parent=data_card)
        add_row("最大记录数", "max_rows", parent=data_card)
        add_row("清理间隔（分钟）", "cleanup_interval_minutes", parent=data_card)
        add_row("最大文本（KB）", "max_text_kb", parent=data_card)
        add_row("最大图片（MB）", "max_image_mb", parent=data_card)
        bind_scroll(body)

        buttons = tk.Frame(shell, bg=T["window_bg"])
        buttons.pack(fill="x", pady=(7, 0))

        def save():
            numeric = {"popup_seconds": float, "capture_monitor": int,
                       "max_file_mb": int,
                       "retention_days": int, "max_rows": int,
                       "cleanup_interval_minutes": int, "max_text_kb": int,
                       "max_image_mb": int}
            values = {}
            try:
                for key, (var, kind) in fields.items():
                    value = var.get()
                    if kind == "bool":
                        values[key] = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
                    else:
                        values[key] = value
                    if key in numeric:
                        values[key] = numeric[key](value)
                        if values[key] < 0 or (key == "popup_seconds" and values[key] > 60):
                            raise ValueError
                self.ui.actions.save_settings(values)
            except (ValueError, TypeError, OSError):
                from tkinter import messagebox
                messagebox.showerror("设置无效", "请检查数值范围和配置文件权限。",
                                     parent=dialog)
                return
            self.ui.config.update(values)
            self.toast_enabled = bool(self.ui.config.get("popup_enabled", True))
            self.toast_seconds = float(self.ui.config.get("popup_seconds", 3))
            self.status_var.set("设置已保存，快捷键重启后生效")
            dialog.destroy()

        tk.Button(buttons, text="取消", command=dialog.destroy, relief="flat",
                  bd=0, bg=T["field"], fg=T["label2"], padx=18, pady=7,
                  cursor="hand2", activebackground=T["fill_hover"],
                  activeforeground=T["label"]).pack(side="right", padx=(8, 0))
        tk.Button(buttons, text="保存", command=save, relief="flat", bd=0,
                  bg=T["accent"], fg="#FFFFFF", padx=20, pady=7,
                  cursor="hand2", activebackground="#005FCC",
                  activeforeground="#FFFFFF").pack(side="right")
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.update_idletasks()
        px = self.win.winfo_rootx() + max(0, (self.win.winfo_width() - dialog.winfo_width()) // 2)
        py = self.win.winfo_rooty() + max(0, (self.win.winfo_height() - dialog.winfo_height()) // 2)
        dialog.geometry(f"+{px}+{py}")
        dialog.grab_set()
        dialog.focus_force()

    def _popup_action(self, fn):
        self._close_popup_menu()
        fn()

    def _cleanup_now(self):
        try:
            deleted = self.db.cleanup(
                self.ui.config.get("retention_days", 30),
                self.ui.config.get("max_rows", 5000))
        except Exception:
            traceback.print_exc()
            self.status_var.set("清理失败")
            return
        self.refresh()
        self.status_var.set(f"已清理 {deleted} 条过期记录（收藏保留）")

    def _close_popup_menu(self):
        self._popup_open = False
        if self._popup_outside_binding is not None:
            try:
                self.win.unbind_all("<Button-1>", self._popup_outside_binding)
            except Exception:
                pass
            self._popup_outside_binding = None
        pop = self._popup_menu
        self._popup_menu = None
        if pop is not None:
            try:
                pop.destroy()
            except Exception:
                pass

    def _popup_outside_click(self, event):
        """Close a dropdown when another part of the main panel is clicked."""
        if not self._popup_open or self._popup_menu is None:
            return
        try:
            if event.widget is self._menu_anchor:
                return
            if event.widget.winfo_toplevel() is self._popup_menu:
                return
        except Exception:
            pass
        self._close_popup_menu()

    # ---------- 截屏入口 ----------
    def _hide_on_capture_changed(self):
        fn = getattr(self.ui.actions, "set_hide_panel_on_capture", None)
        if fn is None:
            return
        try:
            fn(bool(self._hide_on_capture.get()))
        except Exception:
            traceback.print_exc()

    def capture_fullscreen(self):
        fn = getattr(self.ui.actions, "capture_screen", None)
        if fn is None:
            return
        if self._hide_on_capture.get():
            self.hide()
            delay = 0.35
        else:
            delay = 0.15
        threading.Timer(delay, self._safe_action, args=(fn,)).start()

    def capture_region(self):
        fn = getattr(self.ui.actions, "start_region_capture", None)
        if fn is None:
            return
        if self._hide_on_capture.get():
            self.hide()
            delay = 0.35
        else:
            delay = 0.15
        threading.Timer(delay, self._safe_action, args=(fn,)).start()

    @staticmethod
    def _safe_action(fn):
        try:
            fn()
        except Exception:
            traceback.print_exc()

    # ---------- 数据 ----------
    def _debounce_refresh(self):
        if self._debounce_id is not None:
            self.win.after_cancel(self._debounce_id)
        self._debounce_id = self.win.after(250, self._debounced)

    def _debounced(self):
        self._debounce_id = None
        self.refresh()

    def refresh(self):
        q = self.search_var.get().strip()
        limit = 60
        rows = self.db.search(q, limit) if q else self.db.list_recent(limit)
        rows = self._apply_filter(rows)
        self._rows = {str(r["id"]): r for r in rows}
        self._refresh_cards(rows)
        if self._filter == "pinned":
            total = self.db.count(q, pinned=True)
        elif self._filter == "all":
            total = self.db.count(q)
        else:
            total = self.db.count(q, category=self._filter)
        shown = len(rows)
        self.status_var.set(
            f"显示 {shown} / 共 {total} 条" if shown != total
            else f"共 {total} 条")
        if self._detail is not None:
            self._detail_hover_id = None
            self._detail.show(self._sel_id, immediate=True)

    def _apply_filter(self, rows):
        if self._filter == "pinned":
            return [r for r in rows if r["pinned"]]
        if self._filter in ("text", "code", "link", "image", "file"):
            return [r for r in rows if self._cat_of(r) == self._filter]
        return rows

    @staticmethod
    def _cat_of(r):
        try:
            cat = r["category"]
        except (IndexError, KeyError):
            cat = ""
        return cat or ("image" if r["kind"] == "image" else "text")

    def _active_canvas(self):
        if hasattr(self, "cards_canvas") and self.cards_canvas.winfo_exists():
            return self.cards_canvas
        return None

    def _refresh_cards(self, rows):
        canvas = self._active_canvas()
        if not canvas:
            return
        now = datetime.now()
        self._list_canvas = canvas
        self._row_bg_items = {}
        self._row_rects = []
        self._card_photos = []
        d = self._dpi
        self._render_list(canvas, rows, now, row_h=int(round(66 * d)),
                          tile_size=int(round(46 * d)), margin=int(round(12 * d)),
                          title_size=11, meta_size=9)

    def _render_list(self, canvas, rows, now, row_h, tile_size, margin,
                     title_size, meta_size):
        """Apple 分组卡片列表：分类彩色 tile + 标题/副标题 + 分组标题 + 选中态。"""
        T, dark = self._theme()
        cw = canvas.winfo_width()
        if cw <= 1:
            cw = self._left_col_width()
        card_w = max(80, cw - 2 * margin)
        gap = int(round(8 * self._dpi))
        self._card_w, self._row_h = card_w, row_h
        # 收藏按钮是 Canvas 上的真实 Tk 控件，重绘前必须销毁旧控件。
        for child in canvas.winfo_children():
            child.destroy()
        self._favorite_buttons = {}
        canvas.delete("all")
        self._scroll_ind = None

        if not rows:
            eh = max(row_h * 4, int(round(300 * self._dpi)))
            self._draw_empty(canvas, cw, eh, T)
            canvas.configure(scrollregion=(0, 0, cw, eh))
            self._rows_displayed = rows
            return

        y = int(round(4 * self._dpi))
        prev_group = None
        for r in rows:
            cid = str(r["id"])
            group = self._group_of(self._parse_ts(r["created_at"]), now)
            if group != prev_group:
                count = sum(1 for x in rows if self._group_of(
                    self._parse_ts(x["created_at"]), now) == group)
                canvas.create_text(margin + 2, y + int(round(11 * self._dpi)),
                                   anchor="w", text=f"{group} · {count} 条",
                                   fill=T["label2"], font=self._font(10, "bold"))
                y += int(round(28 * self._dpi))
                prev_group = group

            state = "selected" if cid == self._sel_id else "normal"
            bg = self._row_card_bg(card_w, row_h, state)
            self._card_photos.append(bg)
            self._row_bg_items[cid] = canvas.create_image(
                margin, y, image=bg, anchor="nw", tags=("cardbg", "card", cid))
            self._row_rects.append((y, y + row_h, int(cid)))

            cat = self._cat_of(r)
            tile = self._row_tile(r, cat, tile_size)
            if tile is not None:
                self._card_photos.append(tile)
                canvas.create_image(margin + int(round(12 * self._dpi)),
                                    y + row_h // 2, image=tile, anchor="w",
                                    tags=("card", cid))
            text_x = margin + int(round(12 * self._dpi)) + tile_size + int(round(12 * self._dpi))
            avail = card_w - (text_x - margin) - int(round(46 * self._dpi))
            main = self._fit_text(self._row_main(r, cat), self._font(title_size), avail)
            meta = self._fit_text(self._row_meta(r, group, now), self._font(meta_size), avail)
            canvas.create_text(text_x, y + row_h // 2 - int(round(10 * self._dpi)),
                               anchor="w", text=main, fill=T["label"],
                               font=self._font(title_size), tags=("card", cid))
            canvas.create_text(text_x, y + row_h // 2 + int(round(9 * self._dpi)),
                               anchor="w", text=meta, fill=T["label2"],
                               font=self._font(meta_size), tags=("card", cid))

            favorite_size = max(16, int(round(18 * self._dpi)))
            favorite_x = margin + card_w - int(round(12 * self._dpi))
            favorite = self._favorite_marker(bool(r["pinned"]), favorite_size)
            self._card_photos.append(favorite)
            favorite_button = self.tk.Button(
                canvas, image=favorite, command=lambda item_id=int(cid):
                self._favorite_click(item_id), relief="flat", bd=0,
                highlightthickness=0, padx=0, pady=0, cursor="hand2",
                bg=T["window_bg"], activebackground=T["window_bg"])
            favorite_button.image = favorite
            favorite_button._favorite_id = str(cid)
            favorite_button.bind(
                "<Enter>", lambda _e, b=favorite_button:
                self._set_favorite_button_bg(b, "hover"))
            favorite_button.bind(
                "<Leave>", lambda _e, b=favorite_button:
                self._set_favorite_button_bg(b, "normal"))
            self._favorite_buttons[str(cid)] = favorite_button
            canvas.create_window(
                favorite_x, y + row_h // 2, window=favorite_button,
                anchor="e", width=favorite_size, height=favorite_size,
                tags=("card", cid, "favorite"))
            self._set_favorite_button_bg(favorite_button)
            y += row_h + gap

        canvas.configure(scrollregion=(0, 0, cw, y + int(round(4 * self._dpi))))
        self._rows_displayed = rows
        ids = [str(r["id"]) for r in rows]
        if self._sel_id not in ids:
            self._sel_id = ids[0] if ids else None
            if self._sel_id is not None:
                self._apply_row_bg(self._sel_id)

    def _favorite_click(self, clip_id):
        """Toggle a row's favorite state without triggering paste-back."""
        self._select(clip_id)
        self.toggle_pin(clip_id)
        return "break"

    def _set_favorite_button_bg(self, button, state=None):
        if not button or not button.winfo_exists():
            return
        cid = str(getattr(button, "_favorite_id", ""))
        if state is None:
            state = ("selected" if cid == self._sel_id else
                     "hover" if cid == self._hover_id else "normal")
        T, _ = self._theme()
        color = {"selected": T["accent_soft"],
                 "hover": T["fill_hover"],
                 "normal": T["card_bg"]}.get(state, T["card_bg"])
        button.configure(bg=color, activebackground=color)

    # ---------- 行内容/图标 ----------
    def _row_main(self, r, cat):
        preview = (r["preview"] or "").strip()
        if cat == "image":
            return f"图片 · {self._fmt_size(r)}"
        if cat == "file":
            return preview or "文件"
        if cat == "code":
            return preview or "代码"
        if cat == "link":
            return preview or "链接"
        return preview or "(空)"

    def _row_meta(self, r, group, now):
        src = self._source_app(r["source"])[0][:18]
        when = self._fmt_row_time(r["created_at"], group, now)
        parts = [when, src[:18]]
        if self._cat_of(r) != "image":
            parts.append(self._fmt_size(r))
        if r["tags"]:
            parts.insert(0, "#" + r["tags"].split(",")[0][:8])
        return " · ".join(parts)

    def _row_tile(self, r, cat, size):
        """行首图标：图片类=圆角缩略图，其余=分类彩色 tile。返回 PhotoImage。"""
        from PIL import ImageTk
        dark = self._is_dark_mode()
        if cat in ("image", "code", "file", "text", "link"):
            source = r["source"] if "source" in r.keys() else ""
            source_key = (source or "").lower()
            key = f"tile:source:{source_key}:{size}:{dark}"
            hit = self._thumb_cache.get(key)
            if hit is None:
                hit = ImageTk.PhotoImage(self._source_code_tile(source, size))
                self._thumb_cache[key] = hit
            return hit
        key = f"tile:{cat}:{size}:{dark}"
        hit = self._thumb_cache.get(key)
        if hit is None:
            hit = ImageTk.PhotoImage(self._category_tile(cat, size))
            self._thumb_cache[key] = hit
        return hit

    def _image_thumb(self, clip_id, size):
        from PIL import Image, ImageDraw
        full = self.db.get(clip_id)
        if not full or not full["data"]:
            return None
        try:
            img = Image.open(io.BytesIO(full["data"])).convert("RGBA")
        except Exception:
            return None
        img.thumbnail((size - 4, size - 4), Image.LANCZOS)
        tile_bg = self._hex_to_rgb(self._c("card_bg")) + (255,)
        border = self._hex_to_rgb(self._c("hairline")) + (255,)
        out = Image.new("RGBA", (size, size), tile_bg)
        out.paste(img, ((size - img.width) // 2, (size - img.height) // 2), img)
        ss = 2
        mask = Image.new("L", (size * ss, size * ss), 0)
        mask_draw = ImageDraw.Draw(mask)
        mask_draw.rounded_rectangle(
            [0, 0, size * ss - 1, size * ss - 1],
            radius=int(size * 0.22 * ss), fill=255)
        out.putalpha(mask.resize((size, size), Image.LANCZOS))
        draw = ImageDraw.Draw(out)
        draw.rounded_rectangle(
            [0, 0, size - 1, size - 1],
            radius=int(size * 0.22), outline=border, width=max(1, size // 20))
        return out

    def _row_card_bg(self, w, h, state):
        """行卡片背景（normal/hover/selected 三态），实例级缓存 PhotoImage。"""
        dark = self._is_dark_mode()
        key = (w, h, state, dark)
        hit = self._cardbg_cache.get(key)
        if hit is not None:
            return hit
        from PIL import Image, ImageDraw, ImageTk
        T, _ = self._theme()
        ss = 2
        W, H = max(1, w) * ss, max(1, h) * ss
        radius = int(round(12 * self._dpi)) * ss
        if state == "selected":
            fill = self._hex_to_rgb(T["accent_soft"])
            border = self._hex_to_rgb(T["accent"])
            bw = 2 * ss
        elif state == "hover":
            fill = self._hex_to_rgb(T["fill_hover"])
            border = self._hex_to_rgb(T["hairline"])
            bw = ss
        else:
            fill = self._hex_to_rgb(T["card_bg"])
            border = self._hex_to_rgb(T["hairline"])
            bw = ss
        im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(im).rounded_rectangle(
            [0, 0, W - 1, H - 1], radius=radius, fill=fill + (255,),
            outline=border + (255,), width=max(1, bw))
        photo = ImageTk.PhotoImage(im.resize((max(1, w), max(1, h)), Image.LANCZOS))
        self._cardbg_cache[key] = photo
        return photo

    def _pin_marker(self):
        dark = self._is_dark_mode()
        size = max(12, int(round(16 * self._dpi)))
        key = (size, dark)
        hit = self._marker_cache.get(key)
        if hit is not None:
            return hit
        from PIL import Image, ImageDraw, ImageTk
        rgb = self._hex_to_rgb(self._sys("orange")) + (255,)
        ss = 2
        s = size * ss
        im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        dr = ImageDraw.Draw(im)
        import math
        cx, cy = s * 0.5, s * 0.5
        outer, inner = s * 0.40, s * 0.18
        points = []
        for i in range(10):
            angle = -math.pi / 2 + i * math.pi / 5
            radius = outer if i % 2 == 0 else inner
            points.append((cx + math.cos(angle) * radius,
                           cy + math.sin(angle) * radius))
        dr.polygon(points, fill=rgb)
        photo = ImageTk.PhotoImage(im.resize((size, size), Image.LANCZOS))
        self._marker_cache[key] = photo
        return photo

    def _favorite_marker(self, selected, size):
        """Return a hollow or filled orange star for the list favorite action."""
        dark = self._is_dark_mode()
        key = ("favorite", selected, size, dark)
        hit = self._marker_cache.get(key)
        if hit is not None:
            return hit
        from PIL import Image, ImageDraw, ImageTk
        import math

        color_name = "orange" if selected else "gray"
        color = self._hex_to_rgb(
            self._sys(color_name) if selected else self._c("label2")) + (255,)
        ss = 3
        s = size * ss
        image = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        cx, cy = s / 2, s / 2
        outer, inner = s * 0.42, s * 0.19
        points = []
        for i in range(10):
            angle = -math.pi / 2 + i * math.pi / 5
            radius = outer if i % 2 == 0 else inner
            points.append((cx + math.cos(angle) * radius,
                           cy + math.sin(angle) * radius))
        if selected:
            draw.polygon(points, fill=color)
        else:
            draw.line(points + [points[0]], fill=color, width=max(2, s // 14),
                      joint="curve")
        photo = ImageTk.PhotoImage(image.resize((size, size), Image.LANCZOS))
        self._marker_cache[key] = photo
        return photo

    def _draw_empty(self, canvas, cw, ch, T):
        from PIL import ImageTk
        tile = self._category_tile("text", int(round(56 * self._dpi)))
        photo = ImageTk.PhotoImage(tile)
        self._card_photos.append(photo)
        canvas.create_image(cw // 2, ch // 2 - int(round(24 * self._dpi)),
                            image=photo, anchor="center")
        canvas.create_text(cw // 2, ch // 2 + int(round(16 * self._dpi)),
                           text="暂无剪贴板内容", fill=T["label2"],
                           font=self._font(12, "bold"), anchor="n")
        canvas.create_text(cw // 2, ch // 2 + int(round(40 * self._dpi)),
                           text="复制的文本、图片、文件会自动出现在这里",
                           fill=T["label3"], font=self._font(9), anchor="n")

    # ---------- 文本度量 ----------
    def _measure(self, text, font_tuple):
        from tkinter import font as tkfont
        if not hasattr(self, "_tkfonts"):
            self._tkfonts = {}
        f = self._tkfonts.get(font_tuple)
        if f is None:
            f = tkfont.Font(family=font_tuple[0], size=font_tuple[1],
                            weight=(font_tuple[2] if len(font_tuple) > 2 else "normal"))
            self._tkfonts[font_tuple] = f
        return f.measure(text)

    def _fit_text(self, text, font_tuple, maxpx):
        if not text or maxpx <= 0:
            return text
        if self._measure(text, font_tuple) <= maxpx:
            return text
        lo = text
        while lo and self._measure(lo + "…", font_tuple) > maxpx:
            lo = lo[:-1]
        return (lo + "…") if lo else "…"

    # ---------- 选中 / 悬停 ----------
    def _apply_row_bg(self, cid):
        canvas = self._list_canvas
        item = self._row_bg_items.get(str(cid))
        if canvas is None or item is None:
            return
        cid = str(cid)
        if cid == self._sel_id:
            state = "selected"
        elif cid == self._hover_id:
            state = "hover"
        else:
            state = "normal"
        try:
            canvas.itemconfigure(item, image=self._row_card_bg(
                self._card_w, self._row_h, state))
            button = getattr(self, "_favorite_buttons", {}).get(cid)
            self._set_favorite_button_bg(button, state)
        except Exception:
            pass

    def _select(self, cid):
        cid = str(cid) if cid is not None else None
        old = self._sel_id
        if old == cid:
            return
        self._sel_id = cid
        if old is not None:
            self._apply_row_bg(old)
        if cid is not None:
            self._apply_row_bg(cid)
        if self._detail is not None and cid is not None:
            self._detail_hover_id = cid
            self._detail.show(cid, immediate=True)

    def _move_selection(self, delta):
        order = [str(r["id"]) for r in self._rows_displayed]
        if not order:
            return
        if self._sel_id in order:
            i = order.index(self._sel_id)
        else:
            i = 0 if delta > 0 else len(order) - 1
            self._select(order[i])
            self._ensure_visible(order[i])
            return
        i = max(0, min(len(order) - 1, i + delta))
        self._select(order[i])
        self._ensure_visible(order[i])

    def _ensure_visible(self, cid):
        canvas = self._list_canvas
        item = self._row_bg_items.get(str(cid))
        if canvas is None or item is None:
            return
        try:
            bbox = canvas.bbox(item)
            sr = canvas.cget("scrollregion")
            if not bbox or not sr:
                return
            sr = [float(v) for v in canvas.tk.splitlist(sr)]
            total_h = sr[3] - sr[1]
            if total_h <= 0:
                return
            view_h = canvas.winfo_height()
            top = bbox[1] - sr[1]
            bottom = bbox[3] - sr[1]
            first, last = canvas.yview()
            if top < first * total_h:
                canvas.yview_moveto(max(0.0, top / total_h))
            elif bottom > last * total_h:
                canvas.yview_moveto(max(0.0, (bottom - view_h) / total_h))
        except Exception:
            pass

    @staticmethod
    def _fmt_size(r):
        """把行内 size 字段格式化为 B/KB。"""
        try:
            n = int(r["size"] or 0)
        except (ValueError, TypeError, IndexError, KeyError):
            n = 0
        return f"{n // 1024} KB" if n >= 1024 else f"{n} B"

    def _selected_row(self):
        if self._sel_id is None:
            return None
        return self._rows.get(str(self._sel_id))

    # ---------- 动作 ----------
    def _load_full(self, row):
        return self.db.get(row["id"]) if row else None

    def _on_enter(self, _event=None):
        """搜索框内按 Enter = 回贴当前选中项（拦截，避免与刷新重复触发）。"""
        self.paste_back()
        return "break"

    def paste_back(self):
        row = self._selected_row()
        full = self._load_full(row)
        if not full:
            self.status_var.set("未选中记录")
            return
        prev = self._prev_hwnd
        self.hide()
        threading.Thread(target=self._do_paste_back, args=(full, prev),
                         daemon=True).start()

    def _do_paste_back(self, full, prev_hwnd):
        try:
            time.sleep(0.12)
            if not self.ui.actions.copy_to_clipboard(full):
                return
            if prev_hwnd:
                clipboard_api.set_foreground_hwnd(prev_hwnd)
                time.sleep(0.12)
                clipboard_api.send_ctrl_v()
        except Exception:
            traceback.print_exc()

    def copy_only(self):
        full = self._load_full(self._selected_row())
        if not full:
            self.status_var.set("未选中记录")
            return
        ok = self.ui.actions.copy_to_clipboard(full)
        self.status_var.set("已复制到剪贴板" if ok else "复制失败")

    def toggle_pin(self, clip_id=None):
        row = self.db.get(clip_id) if clip_id is not None else None
        if row is None:
            selected = self._selected_row()
            row = self.db.get(selected["id"]) if selected else None
        if not row:
            self.status_var.set("未选中记录")
            return False
        new_pinned = not bool(row["pinned"])
        if not self.db.set_pinned(row["id"], new_pinned):
            self.status_var.set(f"#{row['id']} 收藏状态更新失败")
            return False
        selected_id = row["id"]
        self._sel_id = str(selected_id)
        self.refresh()
        if self._sel_id in self._row_bg_items:
            self._apply_row_bg(self._sel_id)
        self.status_var.set(
            f"#{row['id']} {'已收藏（不可清除）' if new_pinned else '已取消收藏'}")
        return True

    def delete_selected(self):
        row = self._selected_row()
        if not row:
            self.status_var.set("未选中记录")
            return
        if row["pinned"]:
            self.status_var.set(f"#{row['id']} 已收藏，不可删除；请先取消收藏")
            return
        self.db.delete(row["id"])
        self.refresh()
        self.status_var.set(f"#{row['id']} 已删除")

    # ---------- 面板 UI 构建（Ditto 风格卡片拾取器） ----------
    def _build_compact(self):
        tk = self.tk
        try:
            dpi = max(0.8, min(self.win.winfo_fpixels("1i") / 96.0, 3.0))
        except Exception:
            dpi = 1.0
        self._dpi = dpi
        self._cw = int(round(self.COMPACT_W * dpi))
        self._ch = int(round(self.COMPACT_H * dpi))

        # 圆角背景（transparentcolor 键色透出 = 圆角）
        self._bg_canvas = tk.Canvas(self.win, bg=self.TRANS_COLOR,
                                    highlightthickness=0, bd=0)
        self._rounded_bg = self._make_rounded_bg(self._cw, self._ch)
        self._bg_item = self._bg_canvas.create_image(
            0, 0, image=self._rounded_bg, anchor="nw")
        self._bg_canvas.bind("<ButtonPress-1>", self._hdr_press)
        self._bg_canvas.bind("<B1-Motion>", self._hdr_move)

        T, dark = self._theme()
        win_bg = T["window_bg"]
        # 内容容器不透明（避免缝隙透出桌面）；其内缩大于圆角背景的角排除区，故不产生直角溢出
        self._cmp_root = tk.Frame(self.win, bg=win_bg)

        # ---------- 自定义窗口标题栏 + 双栏布局 ----------
        # 无边框窗口没有 Windows 原生标题栏，因此把功能按钮和窗口控制
        # 统一放在这一行。
        self._sh_h = int(round(30 * dpi))
        title_bar = tk.Frame(self._cmp_root, bg=win_bg, height=self._sh_h)
        title_bar.pack(fill="x", padx=0, pady=0)
        title_bar.pack_propagate(False)
        title_bar.bind("<ButtonPress-1>", self._hdr_press)
        title_bar.bind("<B1-Motion>", self._hdr_move)
        tk.Label(title_bar, text="Lumina", bg=win_bg, fg=T["accent"],
                 font=("Segoe UI", max(13, int(round(14 * dpi))), "bold"),
                 anchor="w").pack(side="left", padx=(10, 14))
        menu_btn_kw = dict(
            bd=0, bg=win_bg, activebackground=T["fill_hover"],
            activeforeground=T["label"], relief="flat", cursor="hand2",
            highlightthickness=0, font=self._font(11, "bold"),
            fg=T["label"], padx=6, pady=2)
        self._settings_btn = tk.Button(
            title_bar, text="设置",
            command=lambda: self._toggle_popup_menu("settings", self._settings_btn),
            **menu_btn_kw)
        self._shot_btn = tk.Button(
            title_bar, text="截图",
            command=lambda: self._toggle_popup_menu("shot", self._shot_btn),
            **menu_btn_kw)
        self._shot_btn.pack(side="left", padx=(0, 2))
        self._settings_btn.pack(side="left", padx=(0, 2))

        control_size = max(24, int(round(30 * dpi)))
        normal_fg = T["label"]
        self._control_icons = {
            "minimize": self._make_window_control_icons(
                "minimize", control_size, normal_fg, normal_fg),
            "maximize": self._make_window_control_icons(
                "maximize", control_size, normal_fg, normal_fg),
            "close": self._make_window_control_icons(
                "close", control_size, normal_fg, "#ffffff"),
        }
        self._minimize_btn = self._make_control_button(
            title_bar, "minimize", self._minimize, win_bg, T["fill_hover"])
        self._max_btn = self._make_control_button(
            title_bar, "maximize", self._toggle_maximize, win_bg, T["fill_hover"])
        self._close_btn = self._make_control_button(
            title_bar, "close", self.hide, win_bg, "#e81123")
        self._close_btn.pack(side="right", padx=(2, 0), fill="y")
        self._max_btn.pack(side="right", padx=(2, 0), fill="y")
        self._minimize_btn.pack(side="right", padx=(2, 0), fill="y")

        # ---------- 双栏布局：左(搜索+列表) / 右(详情) ----------
        body_w = self._cw - 60  # cmp_root 内缩32 + body padx28
        left_w = self._left_width(body_w)
        detail_w = self._detail_width(body_w)

        body_split = tk.Frame(self._cmp_root, bg=win_bg)
        body_split.pack(fill="both", expand=True, padx=14, pady=(10, 0))
        body_split.bind("<Configure>", self._on_split_configure)
        body_split.bind("<ButtonPress-1>", self._hdr_press)
        body_split.bind("<B1-Motion>", self._hdr_move)
        left = tk.Frame(body_split, bg=win_bg)
        self._left_frame = left
        left.configure(width=left_w)
        left.pack(side="left", fill="both")
        left.pack_propagate(False)
        left.bind("<ButtonPress-1>", self._hdr_press)
        left.bind("<B1-Motion>", self._hdr_move)

        # 搜索胶囊常驻左栏顶部：默认图标居中（占位态），点击后图标靠左变输入框
        self._search_wrap = tk.Frame(left, bg=win_bg, height=self._sh_h)
        self._search_wrap.pack(fill="x", pady=(0, 6))
        self._search_canvas = tk.Canvas(self._search_wrap, height=self._sh_h,
                                        bg=win_bg, highlightthickness=0, bd=0)
        self._search_canvas.pack(fill="x")
        self._search_icon = self._make_search_icon(dpi, T["label3"])
        self.search_entry_c = tk.Entry(self._search_canvas,
                                       textvariable=self.search_var, relief="flat",
                                       bg=T["field"], fg=T["label"], bd=0,
                                       insertbackground=T["label"],
                                       highlightthickness=0, font=self._font(11))
        self.search_entry_c.bind("<KeyRelease>", self._on_search_key)
        self.search_entry_c.bind("<Return>", self._on_enter)
        self.search_entry_c.bind("<Escape>", self._search_esc)
        self.search_entry_c.bind("<FocusOut>", self._on_search_focus_out)
        self._search_pill_photo = None
        self._search_pill_size = None
        self._search_canvas.bind("<Configure>", lambda e: self._layout_search())
        self._search_canvas.bind("<ButtonPress-1>", self._search_press)
        self._search_canvas.bind("<B1-Motion>", self._hdr_move)

        sep = tk.Frame(body_split, bg=T["hairline"], width=1)
        sep.pack(side="left", fill="y", padx=10, pady=2)
        sep.bind("<ButtonPress-1>", self._hdr_press)
        sep.bind("<B1-Motion>", self._hdr_move)
        self._detail = DetailPane(self, body_split, detail_w)

        # ---------- 过滤胶囊 pills（Canvas 自绘，紧凑） ----------
        chip_wrap = tk.Frame(left, bg=win_bg)
        chip_wrap.pack(fill="x", pady=(0, 6))
        self._chip_h = int(round(28 * dpi))
        self._chip_canvas = tk.Canvas(chip_wrap, height=self._chip_h, bg=win_bg,
                                      highlightthickness=0, bd=0)
        self._chip_canvas.pack(fill="x")
        self._chip_canvas.bind("<Button-1>", self._on_chip_click)
        self._chip_canvas.bind("<Motion>", self._on_chip_motion)
        self._chip_canvas.bind("<Leave>", self._on_chip_leave)
        self._chip_canvas.bind("<Configure>", lambda e: self._layout_chips())
        self._chip_hit = []
        self._chip_photos = []
        self._chip_hover = None

        # 卡片列表（Canvas 替代 Treeview）
        list_frame = tk.Frame(left, bg=self._canvas_bg())
        list_frame.pack(fill="both", expand=True, pady=(0, 4))
        self.cards_canvas = tk.Canvas(list_frame, bg=self._canvas_bg(),
                                          highlightthickness=0, bd=0)
        self.cards_canvas.pack(side="left", fill="both", expand=True)
        self.cards_canvas.configure(yscrollcommand=self._on_list_scroll)
        self._scroll_ind = None
        self.cards_canvas.bind("<Button-1>", self._on_canvas_click)
        self.cards_canvas.bind("<Button-3>", self._on_canvas_rightclick)
        self.cards_canvas.bind("<Motion>", self._on_canvas_motion)
        self.cards_canvas.bind("<Leave>", self._on_canvas_leave)
        self.cards_canvas.bind("<MouseWheel>", self._on_canvas_wheel)
        self.cards_canvas.bind("<Configure>", self._on_canvas_configure)

        # ---------- 底部：细状态行（行级动作由详情区动作栏承担） ----------
        footer = tk.Frame(self._cmp_root, bg=win_bg)
        footer.pack(fill="x", padx=14, pady=(0, 6))
        tk.Label(footer, bg=win_bg, fg=T["label3"], font=self._font(8),
                 text="Enter 回贴 · Ctrl+C 复制 · Ctrl+P 收藏 · Del 删除 · Ctrl+1-9 快速"
                 ).pack(side="left")
        self._cmp_status = tk.Label(footer, textvariable=self.status_var,
                                    bg=win_bg, fg=T["label2"], font=self._font(8))
        self._cmp_status.pack(side="right")

    # ---------- 双栏比例 ----------
    LEFT_RATIO = 0.38
    LEFT_MIN = 400
    LEFT_MAX = 640
    DETAIL_MIN = 380

    def _left_width(self, total):
        """计算列表栏宽度，限制最大值避免最大化时左栏过宽。"""
        avail = max(0, total - 21)
        d = self._dpi
        minimum = self.LEFT_MIN * d
        maximum = self.LEFT_MAX * d
        detail_min = self.DETAIL_MIN * d
        return int(min(max(avail * self.LEFT_RATIO, minimum),
                       maximum, max(minimum, avail - detail_min)))

    def _detail_width(self, total):
        """详情栏占用左栏和分隔线之外的剩余宽度。"""
        avail = max(0, total - 21)
        return int(max(self.DETAIL_MIN * self._dpi,
                       avail - self._left_width(total)))

    def _left_col_width(self):
        """左栏（列表画布）宽度推导：画布尚未映射时的首帧渲染兜底。"""
        body = max(200, self._cw - 60)  # cmp_root 内缩32 + body padx28
        return self._left_width(body)

    def _on_split_configure(self, e):
        """窗口尺寸变化（含全屏切换）时详情栏跟随比例伸缩。"""
        left_w = self._left_width(e.width)
        dw = self._detail_width(e.width)
        try:
            self._left_frame.configure(width=left_w)
            changed = self._detail.frame.winfo_width() != dw
            if changed:
                self._detail.frame.configure(width=dw)
            if changed and self._detail.row is not None \
                    and self._detail.row["kind"] == "image":
                self._detail.invalidate_size()
        except Exception:
            pass

    # ---------- Apple 风格控件绘制助手 ----------
    def _font_path(self):
        windir = os.environ.get("WINDIR", r"C:\Windows")
        for name in ("msyh.ttc", "msyh.ttf", "segoeui.ttf", "simsun.ttc"):
            p = os.path.join(windir, "Fonts", name)
            if os.path.exists(p):
                return p
        return None

    def _load_font(self, size_px):
        from PIL import ImageFont
        path = self._font_path()
        if path:
            try:
                return ImageFont.truetype(path, max(1, int(size_px)))
            except Exception:
                pass
        return ImageFont.load_default()

    def _make_lumina_title_icon(self, size):
        from PIL import Image, ImageDraw, ImageTk

        image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((1, 1, size - 2, size - 2),
                               radius=max(4, size // 4), fill="#243b78")
        draw.ellipse((size * .18, size * .08, size * .82, size * .70),
                     fill="#558dff")
        draw.rounded_rectangle((size * .30, size * .25, size * .70, size * .88),
                               radius=max(2, size // 8), fill="#f6f9ff")
        draw.rounded_rectangle((size * .40, size * .16, size * .60, size * .34),
                               radius=max(2, size // 10), fill="#f6f9ff")
        draw.line((size * .40, size * .50, size * .60, size * .50),
                  fill="#445ca8", width=max(1, size // 10))
        draw.line((size * .40, size * .65, size * .60, size * .65),
                  fill="#445ca8", width=max(1, size // 10))
        return ImageTk.PhotoImage(image)

    def _make_control_button(self, parent, key, command, normal_bg, hover_bg):
        from PIL import Image, ImageDraw, ImageTk

        size = max(30, int(round(32 * self._dpi)))
        canvas = self.tk.Canvas(parent, width=size, height=size,
                                bg=normal_bg, bd=0, highlightthickness=0,
                                cursor="hand2")
        radius = max(6, int(round(8 * self._dpi)))
        def rounded_bg(color):
            image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            ImageDraw.Draw(image).rounded_rectangle(
                (0, 0, size - 1, size - 1), radius=radius, fill=color)
            return ImageTk.PhotoImage(image)

        normal_photo = rounded_bg(normal_bg)
        hover_photo = rounded_bg("#e81123" if key == "close" else hover_bg)
        bg_id = canvas.create_image(size // 2, size // 2, image=normal_photo)
        icon = self._control_icons[key][0]
        icon_id = canvas.create_image(size // 2, size // 2, image=icon)
        canvas._lumina_refs = (normal_photo, hover_photo, icon,
                               self._control_icons[key][1])

        def enter(_event):
            canvas.itemconfigure(bg_id, image=canvas._lumina_refs[1])
            canvas.itemconfigure(icon_id, image=canvas._lumina_refs[3])

        def leave(_event):
            canvas.itemconfigure(bg_id, image=canvas._lumina_refs[0])
            canvas.itemconfigure(icon_id, image=canvas._lumina_refs[2])

        canvas.bind("<Enter>", enter)
        canvas.bind("<Leave>", leave)
        canvas.bind("<ButtonRelease-1>", lambda _event: command())
        return canvas

    def _make_window_control_icons(self, kind, size, normal_color, hover_color):
        """Create matched Windows/WeChat-style title-bar line icons."""
        from PIL import Image, ImageDraw, ImageTk

        scale = 3

        def make(color):
            s = size * scale
            image = Image.new("RGBA", (s, s), (0, 0, 0, 0))
            draw = ImageDraw.Draw(image)
            color = self._hex_to_rgb(color) + (255,)
            width = max(2, round(s * 0.07))
            left, right = s * 0.28, s * 0.72
            center = s * 0.50
            if kind == "minimize":
                draw.line((left, center, right, center), fill=color, width=width)
            elif kind == "maximize":
                draw.rectangle((left, s * 0.28, right, s * 0.72),
                               outline=color, width=width)
            else:
                draw.line((left, left, right, right), fill=color, width=width)
                draw.line((right, left, left, right), fill=color, width=width)
            return ImageTk.PhotoImage(image.resize((size, size), Image.LANCZOS))

        return make(normal_color), make(hover_color)

    def _icon_button(self, parent, photo, cmd, bg, active):
        b = self.tk.Button(parent, image=photo, command=cmd, bd=0, bg=bg,
                           activebackground=active, relief="flat", cursor="hand2",
                           highlightthickness=0)
        b.image = photo
        return b

    def _mono_icon(self, draw_fn, box, color_hex, weight=0.085):
        """绘制单色 SF 风格图标，返回 PhotoImage。"""
        from PIL import Image, ImageDraw, ImageTk
        box = max(8, int(box))
        ss = 2
        s = box * ss
        rgb = self._hex_to_rgb(color_hex) + (255,)
        im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        draw_fn(ImageDraw.Draw(im), s, rgb, max(2, int(round(s * weight))))
        return ImageTk.PhotoImage(im.resize((box, box), Image.LANCZOS))

    def _make_search_icon(self, dpi, color_hex):
        from PIL import Image, ImageDraw, ImageTk
        box = max(14, int(round(16 * dpi)))
        ss = 2
        s = box * ss
        rgb = self._hex_to_rgb(color_hex) + (255,)
        im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        dr = ImageDraw.Draw(im)
        w = max(2, int(round(s * 0.10)))
        cx, cy, r = s * 0.43, s * 0.43, s * 0.25
        dr.ellipse([cx - r, cy - r, cx + r, cy + r], outline=rgb, width=w)
        dr.line([(cx + r * 0.72, cy + r * 0.72), (s * 0.80, s * 0.80)],
                fill=rgb, width=w)
        return ImageTk.PhotoImage(im.resize((box, box), Image.LANCZOS))

    def _make_pill_photo(self, w, h, fill_hex):
        from PIL import Image, ImageDraw, ImageTk
        ss = 2
        im = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
        dr = ImageDraw.Draw(im)
        rgb = self._hex_to_rgb(fill_hex) + (255,)
        dr.rounded_rectangle([0, 0, w * ss - 1, h * ss - 1], radius=(h * ss) // 2,
                             fill=rgb)
        return ImageTk.PhotoImage(im.resize((w, h), Image.LANCZOS))

    def _layout_search(self):
        c = getattr(self, "_search_canvas", None)
        if c is None:
            return
        w = c.winfo_width()
        h = self._sh_h
        if w <= 1:
            return
        T, _dark = self._theme()
        if self._search_pill_size != (w, h):
            self._search_pill_photo = self._make_pill_photo(w, h, T["field"])
            self._search_pill_size = (w, h)
        c.delete("all")
        c.create_image(0, 0, image=self._search_pill_photo, anchor="nw")
        if not self._search_active and not self.search_var.get():
            # 占位态：图标居中，不映射输入框
            c.create_image(w // 2, h // 2, image=self._search_icon,
                           anchor="center")
            return
        pad = int(round(9 * self._dpi))
        iw = self._search_icon.width()
        c.create_image(pad, h // 2, image=self._search_icon, anchor="w")
        ex = pad + iw + int(round(6 * self._dpi))
        clear_w = 0
        if self.search_var.get():
            clear_w = int(round(18 * self._dpi))
            item = c.create_text(w - pad - clear_w // 2, h // 2, text="✕",
                                 fill=T["label3"], font=self._font(9))
            c.tag_bind(item, "<Button-1>", self._search_clear)
        self.search_entry_c.configure(bg=T["field"], fg=T["label"],
                                      insertbackground=T["label"])
        c.create_window(ex, h // 2, anchor="w", window=self.search_entry_c,
                        width=max(20, w - ex - pad - clear_w), height=h - 2)

    # ---------- 搜索激活状态机 ----------
    def _on_search_key(self, e):
        self._layout_search()
        self._debounce_refresh()

    def _activate_search(self):
        if not self._search_active:
            self._search_active = True
            self._layout_search()
        self.search_entry_c.focus_set()

    def _deactivate_search(self, refocus=True):
        if not self._search_active:
            return
        self._search_active = False
        self._layout_search()
        if refocus:
            try:
                self.win.focus_set()
            except Exception:
                pass

    def _search_esc(self, e):
        if self.search_var.get():
            self.search_var.set("")
            self._layout_search()
            self.refresh()
        else:
            self._deactivate_search()
        return "break"

    def _search_clear(self, e=None):
        self.search_var.set("")
        self._layout_search()
        self.refresh()
        self.search_entry_c.focus_set()

    def _on_search_focus_out(self, _event):
        if not self._search_active or self._search_focus_check is not None:
            return
        self._search_focus_check = self.win.after(200, self._check_search_focus)

    def _check_search_focus(self):
        self._search_focus_check = None
        if not self._search_active or self.search_var.get().strip():
            return
        try:
            focused = self.win.focus_get()
        except Exception:
            focused = None
        if focused is not self.search_entry_c:
            self._deactivate_search(refocus=False)

    def _make_chip_photo(self, label, h, sel, T, key="all", hover=False):
        from PIL import Image, ImageDraw, ImageTk
        ss = 2
        fsize = max(10, int(round(11 * self._dpi)))
        fnt = self._load_font(fsize * ss)
        pad_x = int(round(10 * self._dpi)) * ss
        probe = ImageDraw.Draw(Image.new("RGBA", (4, 4)))
        bb = probe.textbbox((0, 0), label, font=fnt)
        tw, th = bb[2] - bb[0], bb[3] - bb[1]
        s_w = tw + pad_x * 2
        s_h = h * ss
        im = Image.new("RGBA", (s_w, s_h), (0, 0, 0, 0))
        dr = ImageDraw.Draw(im)
        if sel:
            fill = self._hex_to_rgb(T["accent"]) + (255,)
            fg = (255, 255, 255, 255)
        elif hover:
            fill = self._hex_to_rgb(self._chip_color(key, T, soft=True)) + (255,)
            fg = self._hex_to_rgb(self._chip_color(key, T)) + (255,)
        else:
            fill = self._hex_to_rgb(T["field"]) + (255,)
            fg = self._hex_to_rgb(T["label"]) + (235,)
        dr.rounded_rectangle([0, 0, s_w - 1, s_h - 1], radius=s_h // 2, fill=fill)
        dr.text(((s_w - tw) / 2 - bb[0], (s_h - th) / 2 - bb[1]), label,
                font=fnt, fill=fg)
        w = max(1, s_w // ss)
        return ImageTk.PhotoImage(im.resize((w, h), Image.LANCZOS)), w

    def _chip_color(self, key, T, soft=False):
        colors = {
            "all": T["accent"],
            "text": self._sys("gray"),
            "code": self._sys("orange"),
            "link": self._sys("blue"),
            "image": self._sys("purple"),
            "file": self._sys("green"),
            "pinned": self._sys("orange"),
        }
        color = colors.get(key, T["accent"])
        if not soft:
            return color
        # Keep hover backgrounds subtle while retaining the category hue.
        return {
            "all": T["accent_soft"],
            "text": "#E7E7EA" if not self._dark else "#3A3A3C",
            "code": "#FFF0D6" if not self._dark else "#4A3820",
            "link": "#E5F0FF" if not self._dark else "#1E3050",
            "image": "#F2E5FA" if not self._dark else "#402650",
            "file": "#E3F6E8" if not self._dark else "#1E4428",
            "pinned": "#FFF0D6" if not self._dark else "#4A3820",
        }.get(key, T["accent_soft"])

    def _layout_chips(self):
        c = getattr(self, "_chip_canvas", None)
        if c is None:
            return
        w = c.winfo_width()
        if w <= 1:
            return
        T, _dark = self._theme()
        c.delete("all")
        self._chip_photos = []
        self._chip_hit = []
        h = self._chip_h
        gap = int(round(4 * self._dpi))
        x = 0
        for key, label in self.FILTERS:
            photo, pw = self._make_chip_photo(
                label, h, key == self._filter, T, key, key == self._chip_hover)
            if x + pw > w:
                break
            self._chip_photos.append(photo)
            c.create_image(x, 0, image=photo, anchor="nw")
            self._chip_hit.append((x, x + pw, key))
            x += pw + gap

    def _on_chip_click(self, e):
        for x0, x1, key in getattr(self, "_chip_hit", []):
            if x0 <= e.x <= x1:
                self._set_filter(key)
                return

    def _on_chip_motion(self, e):
        key = None
        for x0, x1, candidate in getattr(self, "_chip_hit", []):
            if x0 <= e.x <= x1:
                key = candidate
                break
        if key != self._chip_hover:
            self._chip_hover = key
            self._layout_chips()

    def _on_chip_leave(self, _event):
        if self._chip_hover is not None:
            self._chip_hover = None
            self._layout_chips()

    def _make_rounded_bg(self, w, h):
        """窗口材质：圆角背景 + 发丝描边，填充随主题。

        注意：-transparentcolor 是"颜色键"透明（精确匹配、二值），不支持半透明。
        若画投影或做超采样抗锯齿，半透明/混合像素会被颜色键当作不透明深色，
        在外框形成一圈黑边。因此这里用纯 RGB、硬边缘（不超采样缩放），
        透明区=键色，圆角干净无黑边。
        """
        from PIL import Image, ImageDraw, ImageTk
        key = self._hex_to_rgb(self.TRANS_COLOR)
        win_bg = self._hex_to_rgb(self._c("window_bg"))
        hair = self._hex_to_rgb(self._c("hairline"))
        im = Image.new("RGB", (w, h), key)
        ImageDraw.Draw(im).rounded_rectangle(
            [0, 0, w - 1, h - 1], radius=18,
            fill=win_bg, outline=hair, width=1)
        return ImageTk.PhotoImage(im)

    def _set_filter(self, key):
        self._filter = key
        self._style_chips()
        self.refresh()

    def _style_chips(self):
        """过滤胶囊已改为 Canvas 自绘，重绘以反映选中态。"""
        self._layout_chips()

    def _hdr_press(self, e):
        self._drag_off = (e.x_root - self.win.winfo_x(),
                          e.y_root - self.win.winfo_y())

    def _search_press(self, e):
        self._activate_search()
        self._hdr_press(e)

    def _hdr_move(self, e):
        if self._drag_off:
            self.win.geometry(
                f"+{e.x_root - self._drag_off[0]}+{e.y_root - self._drag_off[1]}")

    def _bind_extra_keys(self):
        for i in range(1, 10):
            self.win.bind(f"<Control-Key-{i}>",
                          lambda e, n=i - 1: self._paste_index(n))
            self.cards_canvas.bind(f"<Key-{i}>",
                                   lambda e, n=i - 1: self._paste_index(n))
        self.win.bind("<MouseWheel>", self._on_panel_wheel)

    def _paste_index(self, n):
        if not self.is_visible():
            return
        order = self._rows_displayed or list(self._rows.values())
        if n < len(order):
            row = order[n]
            self._select(row["id"])
            self._ensure_visible(row["id"])
            self._paste_row(row)
            return
        self.paste_back()

    def _on_panel_wheel(self, e):
        if not self.is_visible():
            return
        try:
            self.cards_canvas.yview_scroll(-1 * (e.delta / 120), "units")
        except Exception:
            pass

    def _export_selected(self):
        full = self._load_full(self._selected_row())
        if not full:
            self.status_var.set("未选中记录")
            return
        from tkinter import filedialog
        if full["kind"] == "image":
            path = filedialog.asksaveasfilename(
                parent=self.win, title="导出图片", defaultextension=".png",
                initialfile=f"clip_{full['id']}.png",
                filetypes=[("PNG 图片", "*.png"), ("所有文件", "*.*")])
            data = full["data"]
        else:
            path = filedialog.asksaveasfilename(
                parent=self.win, title="导出文本", defaultextension=".txt",
                initialfile=f"clip_{full['id']}.txt",
                filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")])
            data = (full["content"] or "").encode("utf-8")
        if not path:
            return
        try:
            with open(path, "wb") as f:
                f.write(data)
            self.status_var.set(f"已导出 {os.path.basename(path)}")
        except OSError:
            traceback.print_exc()
            self.status_var.set("导出失败")

    @staticmethod
    def _parse_ts(ts):
        try:
            return datetime.strptime((ts or "")[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None

    @classmethod
    def _group_of(cls, dt, now):
        """日期分组：今天 / 昨天 / 本周(周一起) / 更早。"""
        if dt is None:
            return "更早"
        if dt.date() == now.date():
            return "今天"
        if dt.date() == now.date() - timedelta(days=1):
            return "昨天"
        if dt.date() >= (now - timedelta(days=now.weekday())).date():
            return "本周"
        return "更早"

    @classmethod
    def _fmt_row_time(cls, ts, group, now):
        """行内时间：分组头已给出日期语境，行内只显示最短有效信息。"""
        dt = cls._parse_ts(ts)
        if dt is None:
            return (ts or "")[5:16]
        if group == "今天":
            diff = (now - dt).total_seconds()
            if diff < 60:
                return "刚刚"
            if diff < 3600:
                return f"{int(diff // 60)}分钟前"
            return dt.strftime("%H:%M")
        if group == "昨天":
            return dt.strftime("%H:%M")
        if group == "本周":
            return "周" + "一二三四五六日"[dt.weekday()]
        if dt.year == now.year:
            return dt.strftime("%m-%d")
        return dt.strftime("%Y-%m-%d")

    def _minimize(self):
        self.win.withdraw()

    def _toggle_maximize(self):
        if self._maxed:
            self._maxed = False
            if self._normal_geom:
                self.win.geometry(self._normal_geom)
            return
        self._normal_geom = self.win.geometry()
        self._maxed = True
        wa = self._work_area()
        if wa:
            x, y, w, h = wa
            self.win.geometry(f"{w}x{h}+{x}+{y}")
        else:
            self.win.geometry(f"{self.win.winfo_screenwidth()}x{self.win.winfo_screenheight()}+0+0")

    @staticmethod
    def _work_area():
        try:
            rect = (ctypes.c_long * 4)()
            if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, rect, 0):
                l, t, r, b = rect[0], rect[1], rect[2], rect[3]
                return l, t, r - l, b - t
        except Exception:
            pass
        return None

    def apply_mode(self):
        self.win.overrideredirect(True)
        try:
            self.win.attributes("-transparentcolor", self.TRANS_COLOR)
        except self.tk.TclError:
            pass
        self.win.configure(bg=self.TRANS_COLOR)
        self.win.minsize(1, 1)
        self._bg_canvas.pack(fill="both", expand=True)
        self._cmp_root.place(x=4, y=4, relwidth=1.0, relheight=1.0,
                             width=-8, height=-8)
        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        w = min(self._cw, sw)
        h = min(self._ch, sh)
        px, py = self.win.winfo_pointerxy()
        x = min(max(px - 60, 0), max(0, sw - w))
        y = min(max(py - 24, 0), max(0, sh - h))
        self.win.geometry(f"{w}x{h}+{x}+{y}")
        self.refresh()


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


class DetailPane:
    """右侧常驻详情区（master-detail）：渲染选中/悬停行的完整内容。

    替代旧的悬停气泡浮窗：详情常驻面板右栏，随选中/悬停防抖切换，
    无第二个 Toplevel、无气泡几何与开关轮询。内容可直接交互
    （文本可选中、链接可点击、文件双击打开、代码着色），底部为动作栏。
    """

    TEXT_MAX = 20000
    IMG_MAX_BASE = (380, 430)
    FILE_MAX_ROWS = 20
    DEBOUNCE_MS = 120
    IMG_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp",
                ".ico", ".tif", ".tiff")
    URL_RE = re.compile(r"https?://[^\s<>\"'，。；、）】》」』]+")
    URL_RSTRIP = ".,;:!?)]}>。，；：！？、》」』"
    CODE_TOKEN_RE = re.compile(
        r"(?P<com>#[^\n]*|//[^\n]*|/\*.*?\*/)"
        r"|(?P<str>\"\"\".*?\"\"\"|'''.*?'''|\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*')"
        r"|(?P<num>\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b)"
        r"|(?P<kw>\b(?:def|class|return|if|elif|else|for|while|import|from|as"
        r"|try|except|finally|with|lambda|yield|pass|break|continue|and|or|not"
        r"|in|is|None|True|False|global|nonlocal|raise|assert|del|async|await"
        r"|function|var|let|const|new|delete|typeof|instanceof|this|null|true"
        r"|false|switch|case|default|do|throw|catch|of|export|extends|super)\b)",
        re.S)
    EXT_COLORS = {
        "folder": "gray",
        ".zip": "orange", ".rar": "orange", ".7z": "orange", ".gz": "orange",
        ".tar": "orange", ".doc": "blue", ".docx": "blue", ".txt": "gray",
        ".md": "gray", ".xls": "green", ".xlsx": "green", ".csv": "green",
        ".ppt": "red", ".pptx": "red", ".pdf": "red", ".exe": "purple",
        ".msi": "purple", ".bat": "purple", ".ps1": "purple", ".py": "blue",
        ".js": "orange", ".ts": "blue", ".json": "orange", ".html": "orange",
        ".css": "blue", ".sql": "indigo", ".mp3": "pink", ".wav": "pink",
        ".mp4": "pink", ".avi": "pink", ".mkv": "pink",
    }

    def __init__(self, panel, parent, width):
        self.panel = panel
        self.ui = panel.ui
        self.tk = panel.tk
        T, _dark = panel._theme()
        self.T = T
        self.BG = T["card_bg"]
        self.row = None
        self.cat = "text"
        self._cur_key = None
        self._pending = None
        self._photo = None
        self._urls = []
        self._url_down = None
        self._in_menu = False
        self._flat_menu = None
        self._body = None
        self._image_click_after = None
        tk = self.tk
        self.frame = tk.Frame(parent, bg=self.BG, width=width)
        self.frame.pack(side="left", fill="y")
        self.frame.pack_propagate(False)
        # 三段式详情：顶部元信息、中部预览、底部固定操作栏。
        self._meta_wrap = tk.Frame(self.frame, bg=T["field"], height=58)
        self._meta_wrap.pack(fill="x", padx=0, pady=0)
        self._meta_wrap.pack_propagate(False)
        self._meta_icon = tk.Label(self._meta_wrap, bg=T["field"],
                                   bd=0, highlightthickness=0)
        self._meta_icon.pack(side="left", padx=0)
        meta_text = tk.Frame(self._meta_wrap, bg=T["field"])
        meta_text.pack(side="left", fill="both", expand=True, padx=8, pady=6)
        self._meta_title = tk.Label(meta_text, text="预览", bg=T["field"],
                                    fg=T["label"],
                                    font=("Microsoft YaHei UI", 10, "bold"),
                                    anchor="w")
        self._meta_title.pack(fill="x")
        self._meta = tk.Label(meta_text, text="", bg=T["field"],
                              fg=T["label2"], font=("Microsoft YaHei UI", 8),
                              anchor="w")
        self._meta.pack(fill="x")
        self._host = tk.Frame(self.frame, bg=self.BG)
        self._host.pack(fill="both", expand=True, padx=8, pady=(6, 4))
        self._action_separator = tk.Frame(self.frame, bg=T["hairline"], height=1)
        self._action_separator.pack(fill="x", padx=8)
        self._actions = tk.Frame(self.frame, bg=self.BG, height=42)
        self._actions.pack(fill="x", padx=8, pady=(4, 6))
        self._actions.pack_propagate(False)
        for w in (self.frame, self._meta_wrap, self._meta, self._host, self._actions):
            w.bind("<MouseWheel>", self._on_wheel)
        self.frame.bind("<Return>", self._paste_key)
        self._meta_wrap.bind("<Button-3>", self._menu)
        self._meta_wrap.bind("<ButtonPress-1>", panel._hdr_press)
        self._meta_wrap.bind("<B1-Motion>", panel._hdr_move)
        self.show_placeholder()

    # ---------- 渲染调度 ----------
    def current_id(self):
        return self.row["id"] if self.row is not None else None

    def invalidate_size(self):
        """详情栏宽度变化后重渲染当前行（图片按新宽度重新缩放）。"""
        self._cur_key = None
        if self.row is None:
            return
        try:
            self.frame.after_idle(self._rerender_current)
        except Exception:
            pass

    def _rerender_current(self):
        rid = self.current_id()
        if rid is not None:
            self._render(rid)

    def show(self, row_id, immediate=False):
        """按行 id 渲染详情；默认防抖（悬停跟随），immediate 立即渲染。"""
        if self._pending is not None:
            try:
                self.frame.after_cancel(self._pending)
            except Exception:
                pass
            self._pending = None
        if immediate or row_id is None:
            self._render(row_id)
        else:
            self._pending = self.frame.after(
                self.DEBOUNCE_MS, lambda: self._render(row_id))

    def _render(self, row_id):
        self._pending = None
        if row_id is None:
            self.show_placeholder()
            return
        iid = str(row_id)
        lrow = self.panel._rows.get(iid)
        if lrow is None:
            self.show_placeholder()
            return
        if lrow["kind"] == "image":
            full = self.panel.db.get(lrow["id"])
        else:
            full = self.panel.db.get_text(lrow["id"])
        if not full:
            self.show_placeholder()
            return
        key = (iid, bool(full["pinned"]))
        if key == self._cur_key:
            return
        self._close_menu()
        self._cur_key = key
        self.row = full
        self._photo = None
        self._urls = []
        self._url_down = None
        kind = full["kind"]
        try:
            self.cat = full["category"] or (
                "image" if kind == "image" else "text")
        except (IndexError, KeyError):
            self.cat = "image" if kind == "image" else "text"
        self._update_meta(full, kind)
        for w in self._host.winfo_children():
            w.destroy()
        if kind == "image" and full["data"]:
            self._build_image_body(self._host, full, self.tk)
        elif self.cat == "file":
            self._build_file_body(self._host, full, self.tk)
        else:
            self._build_text_body(self._host, full, self.tk)
        self._body.bind("<Button-3>", self._menu)
        self._update_actions()

    def show_placeholder(self):
        self._close_menu()
        self._cur_key = None
        self.row = None
        self._photo = None
        self._urls = []
        self._body = None
        for w in self._host.winfo_children():
            w.destroy()
        for w in self._actions.winfo_children():
            w.destroy()
        self._meta_title.configure(text="预览")
        self._meta.configure(text="选择一条记录查看详情")
        self._meta_icon.configure(image="")
        self._meta_icon.image = None
        tk = self.tk
        T = self.T
        from PIL import ImageTk
        size = int(round(48 * self.panel._dpi))
        photo = ImageTk.PhotoImage(self.panel._category_tile("text", size))
        box = tk.Frame(self._host, bg=T["card_bg"])
        box.pack(fill="both", expand=True)
        lb = tk.Label(box, image=photo, bg=T["card_bg"])
        lb.image = photo
        lb.pack(expand=True, pady=(0, 6))
        tk.Label(box, text="悬停或选择左侧内容进行预览", bg=T["card_bg"],
                 fg=T["label3"],
                 font=("Microsoft YaHei UI", 10)).pack(pady=(0, 24))

    def _update_meta(self, row, kind):
        cat_label = HistoryPanel.CATEGORY_LABELS.get(self.cat, self.cat)
        if self.cat == "file":
            nbytes = row["data_size"] or 0
        else:
            nbytes = (len(row["data"]) if kind == "image" and row["data"]
                      else len((row["content"] or "").encode("utf-8")))
        if nbytes >= 1024 * 1024:
            size_txt = f"{nbytes / 1024 / 1024:.1f} MB"
        elif nbytes >= 1024:
            size_txt = f"{nbytes / 1024:.1f} KB"
        else:
            size_txt = f"{nbytes} B"
        source_name = self.panel._source_app(row["source"])[0][:18]
        title = cat_label
        if self.cat == "file":
            path = (row["content"] or "").splitlines()[0].strip()
            title = os.path.basename(path.rstrip("\\/")) or path or "文件"
        elif self.cat == "link" and row["content"]:
            title = row["content"].splitlines()[0].strip()[:64]
        elif row["content"]:
            title = " ".join(row["content"].split())[:64]
        meta = (f"{cat_label} · {size_txt} · "
                f"{(row['created_at'] or '')[:19]} · {source_name}")
        if row["pinned"]:
            meta = "[已收藏] " + meta
        if row["tags"]:
            meta += f" · {row['tags'][:16]}"
        if self.cat == "file":
            meta += f" · 归档：{row['file_status'] or 'none'}"
        self._meta_title.configure(text=title)
        self._meta.configure(text=meta)
        source_name, source_color, _style = self.panel._source_app(row["source"])
        from PIL import ImageTk
        icon = ImageTk.PhotoImage(self.panel._source_code_tile(
            row["source"], max(24, int(round(28 * self.panel._dpi)))))
        self._meta_icon.configure(image=icon)
        self._meta_icon.image = icon

    # ---------- 动作栏 ----------
    def _update_actions(self):
        for w in self._actions.winfo_children():
            w.destroy()
        if self.row is None:
            return
        tk = self.tk
        danger = self.panel._sys("red")

        def btn(label, cmd, fg=None, primary=False):
            # 统一深色按钮底，悬停使用蓝色高亮；删除保留独立红色警示。
            if label == "删除":
                fill, hover, pressed = "#B42318", "#DC2626", "#991B1B"
            else:
                fill, hover, pressed = "#334155", "#3B82F6", "#1D4ED8"
            text = "#FFFFFF"

            font = ("Microsoft YaHei UI", 9, "normal")
            text_width = max(30, self.panel._measure(label, font))
            width = text_width + 20
            height = max(30, int(round(30 * self.panel._dpi)))
            radius = max(7, int(round(9 * self.panel._dpi)))

            from PIL import Image, ImageDraw, ImageTk

            def pill(color):
                image = Image.new("RGBA", (width * 2, height * 2), (0, 0, 0, 0))
                ImageDraw.Draw(image).rounded_rectangle(
                    (0, 0, width * 2 - 1, height * 2 - 1),
                    radius=radius * 2, fill=self.panel._hex_to_rgb(color) + (255,))
                return ImageTk.PhotoImage(image.resize((width, height), Image.LANCZOS))

            normal_photo = pill(fill)
            hover_photo = pill(hover)
            pressed_photo = pill(pressed)
            canvas = tk.Canvas(self._actions, width=width, height=height,
                               bg=self.BG, bd=0, highlightthickness=0,
                               cursor="hand2")
            bg_id = canvas.create_image(width // 2, height // 2, image=normal_photo)
            text_id = canvas.create_text(width // 2, height // 2, text=label,
                                         fill=text, font=font)
            canvas._action_refs = (normal_photo, hover_photo, pressed_photo)

            canvas.bind("<Enter>", lambda _e: (
                canvas.itemconfigure(bg_id, image=hover_photo),
                canvas.itemconfigure(text_id, fill=text)))
            canvas.bind("<Leave>", lambda _e: (
                canvas.itemconfigure(bg_id, image=normal_photo),
                canvas.itemconfigure(text_id, fill=text)))
            canvas.bind("<ButtonPress-1>", lambda _e: canvas.itemconfigure(
                bg_id, image=pressed_photo))
            canvas.bind("<ButtonRelease-1>", lambda _e: (
                canvas.itemconfigure(bg_id, image=hover_photo), cmd()))
            canvas.pack(side="right" if label == "删除" else "left",
                        padx=(4, 0), pady=0)
            return canvas

        btn("回贴", self._paste_this, primary=True)
        btn("复制", self._copy)
        if self.cat == "image":
            btn("钉图", self._pin_it)
        if self.cat == "file":
            btn("打开", self._open_file)
        if self._urls:
            btn("打开链接", lambda: self._open_url(self._urls[0]))
        if self.cat in ("text", "code") and (self.row["content"] or "").strip():
            btn("JSON", self._format_json)
        btn("导出", self._save_as)
        btn("删除", self._delete, fg=danger)

    def _format_json(self):
        """格式化当前文本预览中的 JSON，不改写数据库原始剪贴板内容。"""
        if self.row is None:
            return
        raw = self.row["content"] or ""
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            self.panel.status_var.set(f"JSON 格式错误：第 {exc.lineno} 行，第 {exc.colno} 列")
            return
        formatted = json.dumps(value, ensure_ascii=False, indent=2)
        formatted_row = dict(zip(self.row.keys(), self.row)) \
            if hasattr(self.row, "keys") else dict(self.row)
        formatted_row["content"] = formatted
        for widget in self._host.winfo_children():
            widget.destroy()
        self._build_text_body(self._host, formatted_row, self.tk)
        self._body.bind("<Button-3>", self._menu)
        self.panel.status_var.set("已格式化 JSON（仅预览，原始内容未修改）")

    def _delete(self):
        row = self.row
        if row is None:
            return
        if row["pinned"]:
            self.panel.status_var.set(f"#{row['id']} 已收藏，不可删除；请先取消收藏")
            return
        self.panel.db.delete(row["id"])
        self.panel.status_var.set(f"#{row['id']} 已删除")
        self._cur_key = None
        self.panel.refresh()

    def _close_menu(self):
        menu, self._flat_menu = self._flat_menu, None
        self._in_menu = False
        if menu is not None:
            try:
                menu.close()
            except Exception:
                pass

    # ---------- 正文构建 ----------
    def _build_image_body(self, outer, row, tk):
        from PIL import Image, ImageTk
        cache = self.panel._preview_cache
        d = self.panel._dpi
        hw = self.frame.winfo_reqwidth()
        lim_w = (hw - int(20 * d)) if hw > 1 else int(self.IMG_MAX_BASE[0] * d)
        img_max = (max(int(200 * d), min(lim_w, int(680 * d))),
                   int(self.IMG_MAX_BASE[1] * d))
        key = (row["id"], img_max)
        photo = cache.get(key)
        if photo is None:
            img = Image.open(io.BytesIO(row["data"]))
            img.thumbnail(img_max)
            photo = ImageTk.PhotoImage(img)
            if len(cache) > 8:
                cache.pop(next(iter(cache)))
            cache[key] = photo
        self._photo = photo
        self._body = tk.Label(outer, image=photo, bg=self.BG, cursor="hand2")
        self._body.pack(expand=True, padx=6, pady=6)
        self._body.bind("<ButtonRelease-1>", self._image_click)
        self._body.bind("<Double-Button-1>", self._image_double_click)
        self._body.bind("<MouseWheel>", self._on_wheel)

    def _image_click(self, _event=None):
        """延迟处理单击，给双击回贴保留判定时间。"""
        if self._image_click_after is not None:
            try:
                self.frame.after_cancel(self._image_click_after)
            except Exception:
                pass
        self._image_click_after = self.frame.after(220, self._show_image_zoom)

    def _image_double_click(self, _event=None):
        """双击不执行动作，仅取消单击放大，避免重复触发。"""
        if self._image_click_after is not None:
            try:
                self.frame.after_cancel(self._image_click_after)
            except Exception:
                pass
            self._image_click_after = None

    def _show_image_zoom(self, data=None, title="图片预览"):
        self._image_click_after = None
        if data is None:
            if self.row is None:
                return
            data = self.row["data"]
        if not data:
            return
        from PIL import Image, ImageTk
        try:
            image = Image.open(io.BytesIO(data)).convert("RGB")
        except Exception:
            self.panel.status_var.set("图片无法预览")
            return

        win = self.tk.Toplevel(self.panel.win)
        win.title(title)
        win.configure(bg="#111111")
        win.transient(self.panel.win)
        win.bind("<Escape>", lambda _e: win.destroy())
        def close_zoom(_event=None):
            # 等当前鼠标事件处理完再销毁，避免事件落到底层原图再次放大。
            win.after_idle(win.destroy)
            return "break"

        win.bind("<ButtonRelease-1>", close_zoom)

        screen_w = win.winfo_screenwidth()
        screen_h = win.winfo_screenheight()
        max_w = max(320, int(screen_w * 0.88))
        max_h = max(240, int(screen_h * 0.82))
        scale = min(max_w / image.width, max_h / image.height, 1.0)
        shown = image.resize((max(1, int(image.width * scale)),
                              max(1, int(image.height * scale))), Image.LANCZOS)
        photo = ImageTk.PhotoImage(shown)
        # Windows Tk 不支持 zoom-out 光标，使用通用手形光标。
        label = self.tk.Label(win, image=photo, bg="#111111", cursor="hand2")
        label.image = photo
        label.pack(padx=12, pady=12)
        label.bind("<ButtonRelease-1>", close_zoom)
        win.update_idletasks()
        x = max(0, (screen_w - win.winfo_width()) // 2)
        y = max(0, (screen_h - win.winfo_height()) // 2)
        win.geometry(f"+{x}+{y}")
        win.grab_set()
        win.focus_force()

    def _build_text_body(self, outer, row, tk):
        T = self.T
        p = self.panel
        full_text = row["content"] or ""
        truncated = len(full_text) > self.TEXT_MAX
        content = full_text[:self.TEXT_MAX]
        src_name, src_color, src_style = p._source_app(row["source"])
        is_code = self.cat == "code" or src_style in ("ide", "term")
        console = src_style == "term"
        chat = src_style == "chat"
        dark = p._is_dark_mode()

        if chat:
            bg = self._mix(p._sys(src_color), T["card_bg"],
                           0.14 if not dark else 0.30)
            fg, font = T["label"], ("Microsoft YaHei UI", 11)
            padx, pady = 12, 10
        elif console:
            bg, fg, font = "#1F1F1F", "#E8E8E8", ("Consolas", 10)
            padx, pady = 10, 8
        else:
            bg, fg = self.BG, T["label"]
            font = ("Consolas", 10) if is_code else ("Microsoft YaHei UI", 10)
            padx, pady = 8, 6

        def _wlen(s):
            # CJK 等宽字符按 2 列估算，避免中文行被折得过窄
            return sum(2 if ord(ch) > 0x2E80 else 1 for ch in s)

        longest = max((_wlen(s) for s in content.split("\n")), default=10)
        width = max(28, min(longest + 2, 64))
        sel_bg = "#264F78" if console else T["accent_soft"]
        self._body = tk.Text(outer, wrap="word", relief="flat", bd=0,
                             bg=bg, fg=fg, padx=padx, pady=pady,
                             insertbackground=fg,
                             selectbackground=sel_bg,
                             selectforeground=fg if console else T["label"],
                             font=font,
                             width=width,
                             height=2,
                             highlightthickness=0, cursor="xterm",
                             undo=False, autoseparators=False,
                             exportselection=0)
        self._body.insert("1.0", content)
        if truncated:
            notice = (f"\n────\n… 已截断，共 {len(full_text)} 字符，"
                       f"仅显示前 {self.TEXT_MAX}")
            start = self._body.index("end-1c")
            self._body.insert("end", notice)
            self._body.tag_add("dim", start, "end-1c")
        self._body.tag_configure(
            "dim", foreground="#8A8A8E" if console else T["label3"])
        # 只读但可选中：保持 normal 才能鼠标拖选/建立 sel 标签，
        # 通过拦截按键阻止编辑（复制走右键菜单或 Ctrl+C）。
        # exportselection=0：选区纯本地——不抢占系统剪贴板，
        # 也不会因外部剪贴板变动收到"选区丢失"而被清空。
        self._body.bind("<Key>", self._readonly_key)
        self._body.bind("<Return>", self._paste_key)
        self._body.bind("<MouseWheel>", self._on_wheel)
        # 详情区高度固定：Text 直接撑满可用空间，超长内容滚轮滚动
        self._body.pack(fill="both", expand=True, padx=2, pady=2)
        if is_code:
            self._highlight_code(content, console=console)
        self._linkify(content, dark_bg=console)

    # ---------- 来源风格渲染 ----------
    @staticmethod
    def _mix(c1, c2, t):
        """c1 按占比 t 混入 c2，返回 #RRGGBB（气泡底色等派生色用）。"""
        def _p(h):
            h = h.lstrip("#")
            return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
        a, b = _p(c1), _p(c2)
        return "#%02X%02X%02X" % tuple(
            int(a[i] * t + b[i] * (1 - t)) for i in range(3))

    def _app_tile(self, color_name, letter, size):
        """应用色圆角 tile + 白色首字符，返回 PhotoImage。"""
        from PIL import Image, ImageDraw, ImageTk
        p = self.panel
        rgb = p._hex_to_rgb(p._sys(color_name))
        ss = 2
        s = size * ss
        im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        dr = ImageDraw.Draw(im)
        dr.rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.22),
                             fill=rgb + (255,))
        font = p._load_font(int(s * 0.52))
        bb = dr.textbbox((0, 0), letter, font=font)
        dr.text(((s - (bb[2] - bb[0])) / 2 - bb[0],
                 (s - (bb[3] - bb[1])) / 2 - bb[1]),
                letter, font=font, fill=(255, 255, 255, 255))
        photo = ImageTk.PhotoImage(im.resize((size, size), Image.LANCZOS))
        return photo

    def _browser_bar(self, outer, content, name, color, tk):
        """浏览器来源：地址栏风格行，检出首个 URL 可点击直达。"""
        T = self.T
        p = self.panel
        bar = tk.Frame(outer, bg=T["field"])
        bar.pack(fill="x", padx=2, pady=(2, 6))
        size = max(16, int(round(18 * p._dpi)))
        tile = self._app_tile(color, (name[:1] or "?").upper(), size)
        il = tk.Label(bar, image=tile, bg=T["field"])
        il.image = tile
        il.pack(side="left", padx=(8, 6), pady=3)
        m = self.URL_RE.search(content)
        url = m.group(0).rstrip(self.URL_RSTRIP) if m else None
        if url:
            lb = tk.Label(bar, text=url[:96], bg=T["field"], fg=T["accent"],
                          font=("Microsoft YaHei UI", 9), anchor="w",
                          cursor="hand2")
            lb.pack(side="left", fill="x", expand=True, pady=3)
            lb.bind("<Button-1>", lambda e: self._open_url(url))
            go = tk.Button(bar, text="打开", command=lambda: self._open_url(url),
                           relief="flat", bd=0, bg=p._sys("blue"), fg="#FFFFFF",
                           activebackground=p._sys("blue"),
                           activeforeground="#FFFFFF",
                           font=("Microsoft YaHei UI", 8, "bold"),
                           padx=8, pady=1, cursor="hand2",
                           highlightthickness=0)
            go.pack(side="right", padx=6, pady=3)
            self._urls.append(url)
        else:
            tk.Label(bar, text=name, bg=T["field"], fg=T["label2"],
                     font=("Microsoft YaHei UI", 9), anchor="w"
                     ).pack(side="left", fill="x", expand=True, pady=3)
        bar.bind("<MouseWheel>", self._on_wheel)

    def _build_file_body(self, outer, row, tk):
        T = self.T
        paths = [ln.strip() for ln in (row["content"] or "").splitlines()
                 if ln.strip()]
        path = paths[0] if paths else ""
        name = os.path.basename(path.rstrip("\\/")) or path or "未命名文件"
        status = row["file_status"] or "none"
        status_text = {
            "pending": "等待归档",
            "ready": "已归档到数据库",
            "failed": "归档失败",
            "skipped": "未启用归档",
            "none": "未归档",
        }.get(status, status)
        status_color = {
            "ready": self.panel._sys("green"),
            "failed": self.panel._sys("red"),
            "pending": self.panel._sys("orange"),
        }.get(status, T["label3"])

        def size_text(value):
            if not value:
                return "未归档"
            units = ("B", "KB", "MB", "GB")
            n = float(value)
            unit = units[0]
            for unit in units:
                if n < 1024 or unit == units[-1]:
                    break
                n /= 1024
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"

        body = tk.Frame(outer, bg=self.BG)
        self._body = body

        card = tk.Frame(body, bg=T["card_bg"],
                        highlightbackground=T["separator"],
                        highlightthickness=1)
        card.pack(fill="x", padx=8, pady=(8, 6))

        head = tk.Frame(card, bg=T["card_bg"])
        head.pack(fill="x", padx=14, pady=(12, 8))
        icon = tk.Label(head, text="FILE", bg=self.panel._sys("green"),
                        fg="#FFFFFF", font=("Microsoft YaHei UI", 8, "bold"),
                        padx=6, pady=3)
        icon.pack(side="left", padx=(0, 10))
        tk.Label(head, text=name, bg=T["card_bg"], fg=T["label"],
                 font=("Microsoft YaHei UI", 12, "bold"), anchor="w"
                 ).pack(side="left", fill="x", expand=True)
        tk.Label(head, text=status_text, bg=T["card_bg"], fg=status_color,
                 font=("Microsoft YaHei UI", 9, "bold"), anchor="e"
                 ).pack(side="right")

        tk.Frame(card, bg=T["separator"], height=1).pack(fill="x")
        info = tk.Frame(card, bg=T["card_bg"])
        info.pack(fill="x", padx=14, pady=(9, 12))

        def info_row(label, value, color=None):
            line = tk.Frame(info, bg=T["card_bg"])
            line.pack(fill="x", pady=2)
            tk.Label(line, text=label, width=8, anchor="w",
                     bg=T["card_bg"], fg=T["label3"],
                     font=("Microsoft YaHei UI", 9)).pack(side="left")
            tk.Label(line, text=value or "-", anchor="w", justify="left",
                     bg=T["card_bg"], fg=color or T["label2"],
                     font=("Microsoft YaHei UI", 9),
                     wraplength=max(180, int(self.frame.winfo_reqwidth() * .55))
                     ).pack(side="left", fill="x", expand=True)

        info_row("文件名", name, T["label"])
        info_row("源文件路径", path)
        info_row("文件大小", size_text(row["data_size"]))

        image_exts = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff"}
        if (status == "ready" and path
                and os.path.splitext(path)[1].lower() in image_exts):
            file_row = self.panel.db.get_file_data(row["id"])
            image_data = file_row["data"] if file_row else None
            if image_data:
                try:
                    from PIL import Image, ImageTk
                    image = Image.open(io.BytesIO(image_data)).convert("RGB")
                    image.thumbnail((max(220, int(self.frame.winfo_reqwidth() * .72)), 260))
                    photo = ImageTk.PhotoImage(image)
                    preview = tk.Frame(card, bg=T["field"])
                    preview.pack(fill="x", padx=14, pady=(0, 12))
                    image_label = tk.Label(preview, image=photo, bg=T["field"],
                                           cursor="hand2")
                    image_label.image = photo
                    image_label.pack(padx=8, pady=8)
                    image_label.bind(
                        "<ButtonRelease-1>",
                        lambda _e, data=image_data: (
                            self._show_image_zoom(data, "图片文件预览"), "break")[1])
                except Exception:
                    pass

        if not path:
            tk.Label(body, text="没有可用的源文件路径", bg=self.BG,
                     fg=T["label3"], font=("Microsoft YaHei UI", 9),
                     anchor="w").pack(fill="x", padx=10, pady=10)
        body.pack(fill="both", expand=True, padx=2, pady=2)
        body.bind("<MouseWheel>", self._on_wheel)


    def _file_row(self, parent, tk, path, T):
        exists = os.path.exists(path)
        is_dir = os.path.isdir(path)
        row_h = max(30, int(round(30 * self.panel._dpi)))
        fr = tk.Frame(parent, bg=self.BG,
                      cursor="hand2" if exists else "arrow", height=row_h)
        fr.pack(fill="x", padx=2, pady=1)
        fr.pack_propagate(False)
        name = os.path.basename(path.rstrip("\\/")) or path
        d = self.panel._dpi
        hw = self.frame.winfo_reqwidth()
        name_w = (max(int(120 * d), hw - int(104 * d)) if hw > 1
                  else int(260 * d))
        nl = tk.Label(fr,
                      text=self.panel._fit_text(
                          name, ("Microsoft YaHei UI", 10), name_w),
                      bg=self.BG, fg=T["label"] if exists else T["label3"],
                      font=("Microsoft YaHei UI", 10), anchor="w")
        nl.pack(side="left", fill="x", expand=True, pady=2)
        info = ""
        if not exists:
            info = "(已失效)"
        elif not is_dir:
            try:
                n = os.path.getsize(path)
                info = f"{n // 1024} KB" if n >= 1024 else f"{n} B"
            except OSError:
                info = ""
        info_label = None
        if info:
            info_label = tk.Label(fr, text=info, bg=self.BG, fg=T["label3"],
                                  font=("Microsoft YaHei UI", 8),
                                  cursor="hand2" if exists else "arrow")
            info_label.pack(side="right", padx=4)
        for w in (fr, nl, info_label):
            if w is None:
                continue
            w.bind("<Button-1>", lambda e, p=path: self._open_path(p))
            w.bind("<Double-Button-1>", lambda e, p=path: self._open_path(p))
            w.bind("<Button-3>", lambda e, p=path: self._file_menu(p, e))
            w.bind("<MouseWheel>", self._on_wheel)

    # ---------- 代码着色 / 链接 ----------
    def _highlight_code(self, content, console=False):
        body = self._body
        T = self.T
        p = self.panel
        if console:
            # VS Code Dark 风格控制台配色
            kw, st = "#569CD6", "#CE9178"
            num, com = "#B5CEA8", "#6A9955"
        else:
            kw = T["accent"]
            st, num = p._sys("green"), p._sys("orange")
            com = T["label3"]
        body.tag_configure("kw", foreground=kw,
                           font=("Consolas", 10, "bold"))
        body.tag_configure("str", foreground=st)
        body.tag_configure("num", foreground=num)
        body.tag_configure("com", foreground=com)
        st = [0, 1, 0]  # pos, line, col

        def _idx(off):
            seg = content[st[0]:off]
            nl = seg.count("\n")
            if nl:
                st[1] += nl
                st[2] = len(seg) - seg.rfind("\n") - 1
            else:
                st[2] += len(seg)
            st[0] = off
            return f"{st[1]}.{st[2]}"

        try:
            for m in self.CODE_TOKEN_RE.finditer(content):
                kind = m.lastgroup
                if not kind:
                    continue
                body.tag_add(kind, _idx(m.start()), _idx(m.end()))
        except Exception:
            traceback.print_exc()

    def _linkify(self, content, dark_bg=False):
        body = self._body
        T = self.T
        url_fg = self.panel.SYS_COLORS["teal"][1] if dark_bg else T["accent"]
        body.tag_configure("url", foreground=url_fg, underline=True)
        st = [0, 1, 0]

        def _idx(off):
            seg = content[st[0]:off]
            nl = seg.count("\n")
            if nl:
                st[1] += nl
                st[2] = len(seg) - seg.rfind("\n") - 1
            else:
                st[2] += len(seg)
            st[0] = off
            return f"{st[1]}.{st[2]}"

        try:
            for m in self.URL_RE.finditer(content):
                url = m.group(0).rstrip(self.URL_RSTRIP)
                if not url:
                    continue
                start = _idx(m.start())
                end = _idx(m.start() + len(url))
                body.tag_add("url", start, end)
                self._urls.append(url)
        except Exception:
            traceback.print_exc()
        if self._urls:
            body.tag_raise("url")
            body.tag_bind("url", "<ButtonPress-1>", self._url_press)
            body.tag_bind("url", "<ButtonRelease-1>", self._url_release)

    def _url_press(self, e):
        self._url_down = (e.x_root, e.y_root)

    def _url_release(self, e):
        down, self._url_down = self._url_down, None
        if down and (abs(e.x_root - down[0]) > 4
                     or abs(e.y_root - down[1]) > 4):
            return
        try:
            idx = self._body.index(f"@{e.x},{e.y}")
            ranges = self._body.tag_ranges("url")
            for i in range(0, len(ranges), 2):
                if (self._body.compare(ranges[i], "<=", idx)
                        and self._body.compare(idx, "<=", ranges[i + 1])):
                    self._open_url(self._body.get(ranges[i], ranges[i + 1]))
                    return "break"
        except Exception:
            traceback.print_exc()

    def _open_url(self, url):
        try:
            import webbrowser
            webbrowser.open(url)
            self.panel.status_var.set(f"已打开 {url[:60]}")
            # 浏览器会抢前台，但剪贴板面板保持可见并回到上层。
            self.panel.win.after(450, self.panel.win.lift)
        except Exception:
            traceback.print_exc()

    # ---------- 滚轮 / 回贴 ----------
    def _on_wheel(self, e):
        try:
            body = self._body
            if body is not None and body.winfo_class() == "Text":
                body.yview_scroll(int(-1 * (e.delta / 120)), "units")
        except Exception:
            pass
        return "break"

    def _paste_key(self, e):
        if self.row is not None:
            self._paste_this()
        return "break"

    def _paste_this(self):
        if self.row is None:
            return
        try:
            panel = self.panel
            full = self.row
            prev = panel._prev_hwnd
            threading.Thread(target=panel._do_paste_back, args=(full, prev),
                             daemon=True).start()
            # 回贴需要临时激活原窗口，完成后把剪贴板面板重新显示到上层。
            panel.win.after(500, panel.win.deiconify)
            panel.win.after(520, panel.win.lift)
        except Exception:
            traceback.print_exc()

    def _open_path(self, path):
        if not os.path.exists(path):
            self.panel.status_var.set("文件不存在")
            return
        try:
            os.startfile(path)
        except OSError:
            traceback.print_exc()
            self.panel.status_var.set("无法打开文件")

    def _reveal_path(self, path):
        try:
            import subprocess
            subprocess.Popen(["explorer", "/select," + path])
        except OSError:
            traceback.print_exc()

    def _copy_text(self, text):
        try:
            self.ui.actions.copy_to_clipboard(
                {"id": self.row["id"], "kind": "text", "content": text,
                 "image": None})
            self.panel.status_var.set("已复制路径")
        except Exception:
            traceback.print_exc()

    def _file_menu(self, path, e):
        items = []
        if os.path.exists(path):
            items += [("打开", lambda: self._open_path(path)),
                      ("在资源管理器中显示", lambda: self._reveal_path(path))]
        items += [("复制路径", lambda: self._copy_text(path)),
                  ("复制全部路径", self._copy),
                   ("回贴  (Enter)", self._paste_this), None,
                  ("导出…", self._save_as)]
        self._in_menu = True
        self._flat_menu = FlatMenu(self.panel.tk, self.panel.win, items,
                                   theme=self.panel._theme()[0])
        self._flat_menu.popup(e.x_root, e.y_root, on_close=self._menu_closed)
        return "break"

    # ---------- 交互 ----------
    def _readonly_key(self, e):
        # 允许复制/全选/导航/Esc，其余按键拦截以保持只读且可选中
        if (e.state & 0x4) and e.keysym.lower() in ("c", "a"):
            return None
        if e.keysym in ("Left", "Right", "Up", "Down", "Home", "End",
                        "Prior", "Next", "Tab", "Escape"):
            return None
        return "break"

    def _menu(self, e):
        if self.cat == "image":
            items = [("复制图片", self._copy),
                     ("回贴  (Enter)", self._paste_this),
                     ("钉图", self._pin_it),
                     ("另存为…", self._save_as)]
        elif self.cat == "file":
            items = [("打开", self._open_file),
                     ("在资源管理器中显示", self._reveal_file),
                     ("复制路径", self._copy),
                     ("回贴  (Enter)", self._paste_this), None,
                     ("导出…", self._save_as)]
        else:
            items = []
            if self._urls:
                items.append(("打开链接", lambda: self._open_url(self._urls[0])))
            items += [("复制选中", self._copy_sel), ("复制全部", self._copy),
                       ("回贴  (Enter)", self._paste_this), None,
                      ("导出…", self._save_as)]
        self._in_menu = True
        self._flat_menu = FlatMenu(self.panel.tk, self.panel.win, items,
                                   theme=self.panel._theme()[0])
        self._flat_menu.popup(e.x_root, e.y_root, on_close=self._menu_closed)
        return "break"  # 阻断 Text 类绑定，保护当前选区

    def _menu_closed(self):
        self._in_menu = False
        self._flat_menu = None

    def _copy(self):
        try:
            self.ui.actions.copy_to_clipboard(self.row)
        except Exception:
            traceback.print_exc()

    def _first_path(self):
        for line in (self.row["content"] or "").splitlines():
            line = line.strip()
            if line:
                return line
        return ""

    def _open_file(self):
        path = self._first_path()
        if not path:
            return
        try:
            os.startfile(path)
        except OSError:
            traceback.print_exc()
            self.panel.status_var.set("文件不存在或无法打开")

    def _reveal_file(self):
        path = self._first_path()
        if not path:
            return
        try:
            import subprocess
            subprocess.Popen(["explorer", "/select," + path])
        except OSError:
            traceback.print_exc()

    def _copy_sel(self):
        # Tk9 移除了 sel.first/sel.end 索引语法，改用 tag_ranges 读取选区
        sel = ""
        try:
            ranges = self._body.tag_ranges("sel")
            if ranges:
                sel = self._body.get(ranges[0], ranges[1])
        except Exception:
            sel = ""
        if not sel:
            self.panel.status_var.set("预览区无选中文本")
            return
        try:
            # 抑制路径：复制选中只写剪贴板，不产生历史记录
            self.ui.actions.copy_to_clipboard(
                {"id": self.row["id"], "kind": "text", "content": sel,
                 "image": None})
            self.panel.status_var.set(f"已复制选中 {len(sel)} 字符")
        except Exception:
            traceback.print_exc()

    def _pin_it(self):
        try:
            png = self.row["data"]
            x = self.panel.win.winfo_rootx() + self.panel.win.winfo_width() // 2
            y = self.panel.win.winfo_rooty() + 80
            self.ui._pins.append(PinWindow(self.ui, png, (x, y)))
        except Exception:
            traceback.print_exc()

    def _save_as(self):
        from tkinter import filedialog
        row = self.row
        if row["kind"] == "image":
            path = filedialog.asksaveasfilename(
                parent=self.panel.win, title="保存图片", defaultextension=".png",
                initialfile=f"clip_{row['id']}.png",
                filetypes=[("PNG 图片", "*.png"), ("所有文件", "*.*")])
            data = row["data"]
        elif self.cat == "file":
            file_row = self.panel.db.get_file_data(row["id"])
            data = file_row["data"] if file_row else None
            if not data:
                self.panel.status_var.set("文件尚未归档或归档失败")
                return
            path = filedialog.asksaveasfilename(
                parent=self.panel.win, title="恢复文件",
                initialfile=os.path.basename(row["content"] or "") or f"file_{row['id']}",
                filetypes=[("所有文件", "*.*")])
        else:
            path = filedialog.asksaveasfilename(
                parent=self.panel.win, title="导出文本", defaultextension=".txt",
                initialfile=f"clip_{row['id']}.txt",
                filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")])
            data = (row["content"] or "").encode("utf-8")
        if not path:
            return
        try:
            with open(path, "wb") as f:
                f.write(data)
            self.panel.status_var.set(f"已导出 {os.path.basename(path)}")
        except OSError:
            traceback.print_exc()


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
