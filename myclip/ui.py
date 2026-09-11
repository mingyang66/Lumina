"""统一 UI 服务：单线程单 Tk root，同时管理 toast 提示与常驻历史面板。

tkinter 要求所有窗口操作在创建它的同一线程内进行，且一个进程最好只有一个
Tk 解释器。因此 toast 与面板共用这里的一个 UI 线程，通过队列接收外部指令。
"""
import io
import gc
import os
import queue
import re
import threading
import time
import traceback
from datetime import datetime, timedelta

from . import win32clip


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
        self._thread = threading.Thread(target=self._run, name="myclip-ui",
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
                print("[myclip] UI thread did not stop in time", flush=True)
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
            print("[myclip] UI disabled: tkinter unavailable", flush=True)
            self.toast_enabled = self.panel_enabled = False
            return
        self._tk = tk
        try:
            root = tk.Tk()
        except Exception as e:
            print(f"[myclip] UI disabled: {e}", flush=True)
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
                            self._prev_hwnd = win32clip.get_foreground_hwnd()
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
        if self._panel.is_visible():
            self._panel.hide()
        else:
            self._prev_hwnd = win32clip.get_foreground_hwnd()
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
                         name="myclip-region-save", daemon=True).start()

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

    HINT = "拖拽框选区域 · 松手后 ✓保存(Enter) / 钉图 / ✕取消(Esc)"

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

        self._start = None
        self._rect_id = None
        self._shot_id = None
        self._size_id = None
        self._shot_photo = None
        self._last_draw = 0.0
        self._sel_box = None      # 松手后的选区（逻辑坐标）
        self._tb_buttons = []     # 工具栏按钮
        self._tb_item_ids = []    # 工具栏 canvas 项（圆角背景图 + 按钮窗口）
        self._tb_bg_photo = None  # 圆角背景 PhotoImage（防 GC）
        self._tb_icons = {}       # 图标 PhotoImage 引用（防 GC）

        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Button-3>", lambda e: self.cancel())
        self.win.bind("<Escape>", lambda e: self.cancel())
        self.win.bind("<Return>", lambda e: self._confirm())
        self.win.focus_force()

    def _coords(self, e):
        x0, y0 = self._start
        x1, y1 = max(0, min(e.x, self.sw)), max(0, min(e.y, self.sh))
        return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)

    def _on_press(self, e):
        self._start = (max(0, min(e.x, self.sw)), max(0, min(e.y, self.sh)))
        self._sel_box = None
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

    def _crop_box(self):
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
        buf = io.BytesIO()
        self.src.crop(box).save(buf, "PNG")
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

        self._tb_icons = {
            "x": (icon_x("#e5484d"), size),
            "check": (icon_check("#2f9e44"), size),
            "pin": (icon_pin(), size),
            "download": (icon_download("#5f6368"), size),
        }

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
        n_btns = 4
        bw = size * n_btns + gap * (n_btns - 1) + pad * 2 + border_w * 2
        bh = size + pad * 2 + border_w * 2
        radius = min(radius, bh // 2)

        ss = 3
        bg = Image.new("RGBA", (bw * ss, bh * ss), (0, 0, 0, 0))
        dr = ImageDraw.Draw(bg)
        dr.rounded_rectangle([0, 0, bw * ss - 1, bh * ss - 1], radius=radius * ss,
                             fill="#ffffff", outline="#c9cdd4", width=border_w * ss)
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
        for i, (key, cmd) in enumerate((("pin", self._pin),
                                        ("download", self._download),
                                        ("x", self.cancel),
                                        ("check", self._confirm))):
            photo = self._tb_icons[key][0]
            b = tk.Button(self.canvas, image=photo, command=cmd, bd=0,
                          bg="#ffffff", activebackground="#e6eaf2", relief="flat",
                          cursor="hand2", highlightthickness=0)
            b.image = photo
            self._tb_buttons.append(b)
            self._tb_item_ids.append(self.canvas.create_window(
                inner_x + i * (size + gap), inner_y, window=b, anchor="nw",
                width=size, height=size))

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
        self._tb_bg_photo = None

    def _confirm(self):
        if self.done or not self._sel_box:
            return
        png = self._crop_box()
        if png is None:
            self.cancel()
            return
        self._finish(png, pin=False)

    def _pin(self):
        if self.done or not self._sel_box:
            return
        png = self._crop_box()
        if png is None:
            self.cancel()
            return
        self._finish(png, pin=True, pos=(self._sel_box[0], self._sel_box[1]))

    def _download(self):
        """另存为对话框下载当前选区；取消则保留选区可继续操作。"""
        if self.done or not self._sel_box:
            return
        png = self._crop_box()
        if png is None:
            self.cancel()
            return
        from tkinter import filedialog
        default_dir = self.ui.config.get("download_dir") or os.path.join(
            os.path.expanduser("~"), "Pictures", "MyClip")
        try:
            os.makedirs(default_dir, exist_ok=True)
        except OSError:
            default_dir = os.path.expanduser("~")
        fname = time.strftime("myclip_%Y%m%d_%H%M%S.png")
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
        try:
            self.win.destroy()
        except Exception:
            pass
        self.on_done(png, pin, pos, source)


class HistoryPanel:
    COMPACT_W = 460
    COMPACT_H = 640
    TRANS_COLOR = "#010203"
    FILTERS = (("all", "全部"), ("text", "文本"), ("code", "代码"),
               ("link", "链接"), ("image", "图片"), ("file", "文件"),
               ("pinned", "钉住"))
    CATEGORY_LABELS = {"text": "文本", "code": "代码", "link": "链接",
                        "image": "图片", "file": "文件"}

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
        self._drag_off = None
        self._shot_anchor = None
        self._sel_id = None
        self._hover_id = None
        self._row_rects = []
        self._row_bg_items = {}
        self._list_canvas = None
        self._card_w = 0
        self._row_h = 0
        self._rows_displayed = []
        self._hover_iid = None
        self._hover_after = None
        self._hover_poll_id = None
        self._hover_pop = None
        self._hover_miss = 0
        self._flat_menu = None

        self.win = tk.Toplevel(root)
        self.win.title("MyClip 历史面板")
        self.win.attributes("-topmost", True)
        self.win.protocol("WM_DELETE_WINDOW", self.hide)
        self._build_widgets()
        self.win.withdraw()

    def _build_widgets(self):
        tk = self.tk
        self.search_var = tk.StringVar()
        self.status_var = tk.StringVar(value="就绪")
        self._hide_on_capture = tk.BooleanVar(
            value=bool(self.ui.config.get("hide_panel_on_capture", True)))
        self._shot_popup = None
        self._shot_open = False
        self._shot_focus_check = None

        self.win.bind("<F5>", lambda e: self.refresh())
        self.win.bind("<Escape>", lambda e: self.hide())
        self.win.bind("<Delete>", lambda e: self.delete_selected())
        self.win.bind("<Control-p>", lambda e: self.toggle_pin())
        self.win.bind("<Control-c>", lambda e: self.copy_only())
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
        self._close_shot_menu()
        self._close_hover()
        try:
            self.win.destroy()
        except Exception:
            pass
        for cache in (self._thumb_cache, self._tile_cache,
                      self._cardbg_cache, self._marker_cache):
            cache.clear()
        self.__init__(root, tk, ui)
        self._sel_id = sel
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

    def _on_canvas_click(self, event):
        canvas = self._active_canvas()
        if canvas is None or event.widget is not canvas:
            return
        clip_id = self._get_clip_id_from_event(canvas, event)
        if clip_id is None:
            return
        self._select(clip_id)
        row = self._rows.get(str(clip_id))
        if row:
            self._paste_row(row)

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
        self._track_hover_preview(canvas, cid)

    def _on_canvas_leave(self, event):
        self._track_hover_preview(self._active_canvas(), None)

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
        self._close_hover()
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

    def _row_anchor(self, iid):
        """行的屏幕坐标框 (x0, y0, x1, y1)，复用 _row_rects 命中数据。"""
        canvas = self._active_canvas()
        if canvas is None:
            return None
        try:
            cid = int(iid)
            off = canvas.canvasy(0)
            x0 = canvas.winfo_rootx()
            y0 = canvas.winfo_rooty() - off
            for ry0, ry1, rid in self._row_rects:
                if rid == cid:
                    return (x0, y0 + ry0,
                            x0 + canvas.winfo_width(), y0 + ry1)
        except Exception:
            pass
        return None

    def _context_menu(self, clip_id, x, y):
        row = self._rows.get(str(clip_id))
        if not row:
            return
        cat = self._cat_of(row)
        items = [("仅复制  (Ctrl+C)", self.copy_only),
                 ("钉住/取消钉住  (Ctrl+P)", self.toggle_pin),
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
        self.apply_mode()  # 重设边框/尺寸/位置（光标附近）并刷新
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()
        self.search_entry_c.focus_set()
        self.ui.panel_visible = True

    def hide(self):
        self._close_shot_menu()
        self._close_hover()
        try:
            self.win.withdraw()
        except Exception:
            pass
        self.ui.panel_visible = False

    # ---------- ✂ 截图下拉弹窗 ----------
    # 不用 Tk 原生菜单：Windows 下 menu.post 会进入模态跟踪循环，
    # 在其 Unmap 回调里重新 post 会让 UI 线程死锁，且无法自动化验证。
    # 自绘 overrideredirect 弹窗行为完全可控：勾选项点击不收起，Esc/失焦收起。
    def _toggle_shot_menu(self, anchor=None):
        self._shot_anchor = anchor
        if self._shot_open:
            self._close_shot_menu()
        else:
            self._open_shot_menu()

    def _open_shot_menu(self):
        tk = self.tk
        T, dark = self._theme()
        if self._shot_popup is not None:
            try:
                self._shot_popup.destroy()
            except Exception:
                pass
            self._shot_popup = None
        pop = tk.Toplevel(self.win)
        pop.overrideredirect(True)
        pop.attributes("-topmost", True)
        pop.configure(bg=T["hairline"])
        frame = tk.Frame(pop, bg=T["card_bg"])
        frame.pack(fill="both", expand=True, padx=1, pady=1)
        f = self._font(10)
        btn_kw = dict(anchor="w", relief="flat", font=f, bd=0, cursor="hand2",
                      bg=T["card_bg"], fg=T["label"],
                      activebackground=T["fill_hover"], activeforeground=T["label"],
                      highlightthickness=0, padx=14, pady=6)
        tk.Button(frame, text="全屏截图",
                  command=lambda: self._shot_action(self.capture_fullscreen),
                  **btn_kw).pack(fill="x", padx=4, pady=(4, 1))
        tk.Button(frame, text="区域截图",
                  command=lambda: self._shot_action(self.capture_region),
                  **btn_kw).pack(fill="x", padx=4, pady=1)
        tk.Frame(frame, bg=T["separator"], height=1).pack(fill="x", padx=6, pady=4)
        tk.Checkbutton(frame, text="隐藏此窗口", variable=self._hide_on_capture,
                       command=self._hide_on_capture_changed, anchor="w", font=f,
                       bg=T["card_bg"], fg=T["label"], selectcolor=T["field"],
                       activebackground=T["card_bg"], activeforeground=T["label"],
                       highlightthickness=0, cursor="hand2",
                       ).pack(fill="x", padx=6, pady=(0, 5))
        pop.bind("<Escape>", lambda e: self._close_shot_menu())
        pop.bind("<FocusOut>", self._on_shot_focus_out)
        self._shot_popup = pop
        btn = self._shot_anchor or self._shot_btn_c
        btn.update_idletasks()
        self._shot_popup.geometry(
            f"+{btn.winfo_rootx()}+{btn.winfo_rooty() + btn.winfo_height() + 4}")
        self._shot_popup.deiconify()
        self._shot_popup.lift()
        self._shot_popup.focus_force()
        self._shot_open = True

    def _shot_action(self, fn):
        self._close_shot_menu()
        fn()

    def _close_shot_menu(self):
        self._shot_open = False
        pop = self._shot_popup
        self._shot_popup = None
        if pop is not None:
            try:
                pop.destroy()
            except Exception:
                pass

    def _on_shot_focus_out(self, _event):
        if not self._shot_open or self._shot_popup is None:
            return
        if self._shot_focus_check is not None:
            try:
                self._shot_popup.after_cancel(self._shot_focus_check)
            except Exception:
                pass
        self._shot_focus_check = self._shot_popup.after(150, self._check_shot_focus)

    def _check_shot_focus(self):
        self._shot_focus_check = None
        if not self._shot_open:
            return
        try:
            focused = self.win.focus_get()
        except (KeyError, self.tk.TclError):
            focused = None
        if focused is None:
            self._close_shot_menu()
            return
        try:
            if focused.winfo_toplevel() is not self._shot_popup:
                self._close_shot_menu()
        except Exception:
            self._close_shot_menu()

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
            delay = 0.35  # 等面板真正从屏幕消失再抓，避免拍进截图
        else:
            delay = 0.15  # 只等菜单收起
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
        self._close_hover()
        q = self.search_var.get().strip()
        limit = 60
        rows = self.db.search(q, limit) if q else self.db.list_recent(limit)
        rows = self._apply_filter(rows)
        self._rows = {str(r["id"]): r for r in rows}
        self._refresh_cards(rows)
        self.status_var.set(f"共 {len(rows)} 条")

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
        self._render_list(canvas, rows, now, row_h=int(round(58 * d)),
                          tile_size=int(round(38 * d)), margin=int(round(12 * d)),
                          title_size=11, meta_size=9)

    def _render_list(self, canvas, rows, now, row_h, tile_size, margin,
                     title_size, meta_size):
        """Apple 分组卡片列表：分类彩色 tile + 标题/副标题 + 分组标题 + 选中态。"""
        T, dark = self._theme()
        cw = canvas.winfo_width()
        if cw <= 1:
            cw = max(200, self._cw - 20)
        card_w = max(80, cw - 2 * margin)
        gap = int(round(8 * self._dpi))
        self._card_w, self._row_h = card_w, row_h
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

            if r["pinned"]:
                pin = self._pin_marker()
                self._card_photos.append(pin)
                canvas.create_image(margin + card_w - int(round(12 * self._dpi)),
                                    y + row_h // 2, image=pin, anchor="e",
                                    tags=("card", cid))
            y += row_h + gap

        canvas.configure(scrollregion=(0, 0, cw, y + int(round(4 * self._dpi))))
        self._rows_displayed = rows
        ids = [str(r["id"]) for r in rows]
        if self._sel_id not in ids:
            self._sel_id = ids[0] if ids else None
            if self._sel_id is not None:
                self._apply_row_bg(self._sel_id)

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
        src = (r["source"] or "").strip()
        if src.lower().endswith(".exe"):
            src = src[:-4]
        src = src or "未知来源"
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
        if cat == "image":
            key = f"thumb:{r['id']}:{size}:{dark}"
            hit = self._thumb_cache.get(key)
            if hit is not None:
                return hit
            thumb = self._image_thumb(r["id"], size)
            base = thumb if thumb is not None else self._category_tile("image", size)
            photo = ImageTk.PhotoImage(base)
            if len(self._thumb_cache) > 200:
                self._thumb_cache.pop(next(iter(self._thumb_cache)))
            self._thumb_cache[key] = photo
            return photo
        key = f"tile:{cat}:{size}:{dark}"
        hit = self._thumb_cache.get(key)
        if hit is None:
            hit = ImageTk.PhotoImage(self._category_tile(cat, size))
            self._thumb_cache[key] = hit
        return hit

    def _image_thumb(self, clip_id, size):
        from PIL import Image, ImageDraw
        full = self.db.get(clip_id)
        if not full or not full["image"]:
            return None
        try:
            img = Image.open(io.BytesIO(full["image"])).convert("RGBA")
        except Exception:
            return None
        img.thumbnail((size, size), Image.LANCZOS)
        out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        out.paste(img, ((size - img.width) // 2, (size - img.height) // 2), img)
        ss = 2
        mask = Image.new("L", (size * ss, size * ss), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            [0, 0, size * ss - 1, size * ss - 1],
            radius=int(size * 0.24 * ss), fill=255)
        out.putalpha(mask.resize((size, size), Image.LANCZOS))
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
        rgb = self._hex_to_rgb(self._c("accent")) + (255,)
        ss = 2
        s = size * ss
        im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        dr = ImageDraw.Draw(im)
        w = max(2, int(round(s * 0.11)))
        cx, cy, r = s * 0.42, s * 0.40, s * 0.19
        dr.ellipse([cx - r, cy - r, cx + r, cy + r], fill=rgb)
        dr.line([(cx + r * 0.55, cy + r * 0.55), (s * 0.80, s * 0.82)],
                fill=rgb, width=w)
        photo = ImageTk.PhotoImage(im.resize((size, size), Image.LANCZOS))
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
                win32clip.set_foreground_hwnd(prev_hwnd)
                time.sleep(0.12)
            win32clip.send_ctrl_v()
        except Exception:
            traceback.print_exc()

    def copy_only(self):
        full = self._load_full(self._selected_row())
        if not full:
            self.status_var.set("未选中记录")
            return
        ok = self.ui.actions.copy_to_clipboard(full)
        self.status_var.set("已复制到剪贴板" if ok else "复制失败")

    def toggle_pin(self):
        row = self._selected_row()
        if not row:
            self.status_var.set("未选中记录")
            return
        self.db.set_pinned(row["id"], not row["pinned"])
        self.refresh()
        self.status_var.set(
            f"#{row['id']} {'已钉住（不可清除）' if not row['pinned'] else '已取消钉住'}")

    def delete_selected(self):
        row = self._selected_row()
        if not row:
            self.status_var.set("未选中记录")
            return
        if row["pinned"]:
            self.status_var.set(f"#{row['id']} 已钉住，不可删除；请先取消钉住")
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
        self._bg_canvas.create_image(0, 0, image=self._rounded_bg, anchor="nw")

        T, dark = self._theme()
        win_bg = T["window_bg"]
        # 内容容器不透明（避免缝隙透出桌面）；其内缩大于圆角背景的角排除区，故不产生直角溢出
        self._cmp_root = tk.Frame(self.win, bg=win_bg)

        # ---------- 标题栏（可拖动） ----------
        header = tk.Frame(self._cmp_root, bg=win_bg)
        header.pack(fill="x", padx=14, pady=(12, 4))
        title = tk.Label(header, text="剪贴板", bg=win_bg, fg=T["label"],
                         font=self._font(13, "bold"))
        title.pack(side="left")
        self._hdr_icons = self._make_header_icons(dpi, T["label2"])
        close_b = self._icon_button(header, self._hdr_icons["close"], self.hide,
                                    win_bg, T["fill_hover"])
        close_b.pack(side="right")
        self._shot_btn_c = self._icon_button(
            header, self._hdr_icons["shot"],
            lambda: self._toggle_shot_menu(self._shot_btn_c), win_bg, T["fill_hover"])
        self._shot_btn_c.pack(side="right", padx=(0, 2))
        spacer = tk.Frame(header, bg=win_bg)
        spacer.pack(side="left", fill="x", expand=True)
        for wgt in (header, title, spacer):
            wgt.bind("<ButtonPress-1>", self._hdr_press)
            wgt.bind("<B1-Motion>", self._hdr_move)

        # ---------- 胶囊搜索框 ----------
        search_wrap = tk.Frame(self._cmp_root, bg=win_bg)
        search_wrap.pack(fill="x", padx=14, pady=(2, 8))
        self._sh_h = int(round(34 * dpi))
        self._search_canvas = tk.Canvas(search_wrap, height=self._sh_h, bg=win_bg,
                                        highlightthickness=0, bd=0)
        self._search_canvas.pack(fill="x")
        self._search_icon = self._make_search_icon(dpi, T["label3"])
        self.search_entry_c = tk.Entry(self._search_canvas,
                                       textvariable=self.search_var, relief="flat",
                                       bg=T["field"], fg=T["label"], bd=0,
                                       insertbackground=T["label"],
                                       highlightthickness=0, font=self._font(11))
        self.search_entry_c.bind("<KeyRelease>", lambda e: self._debounce_refresh())
        self.search_entry_c.bind("<Return>", self._on_enter)
        self._search_pill_photo = None
        self._search_pill_size = None
        self._search_canvas.bind("<Configure>", lambda e: self._layout_search())

        # ---------- 过滤胶囊 pills（Canvas 自绘） ----------
        chip_wrap = tk.Frame(self._cmp_root, bg=win_bg)
        chip_wrap.pack(fill="x", padx=14, pady=(0, 8))
        self._chip_h = int(round(28 * dpi))
        self._chip_canvas = tk.Canvas(chip_wrap, height=self._chip_h, bg=win_bg,
                                      highlightthickness=0, bd=0)
        self._chip_canvas.pack(fill="x")
        self._chip_canvas.bind("<Button-1>", self._on_chip_click)
        self._chip_canvas.bind("<Configure>", lambda e: self._layout_chips())
        self._chip_hit = []
        self._chip_photos = []

        # 卡片列表（Canvas 替代 Treeview）
        list_frame = tk.Frame(self._cmp_root, bg=self._canvas_bg())
        list_frame.pack(fill="both", expand=True, padx=8, pady=(0, 4))
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

        # ---------- 底部：动作图标 + 提示 + 状态 ----------
        footer = tk.Frame(self._cmp_root, bg=win_bg)
        footer.pack(fill="x", padx=14, pady=(2, 10))
        self._ficons = self._make_footer_icons()
        for key, cmd in (("paste", self.paste_back), ("copy", self.copy_only),
                          ("pin", self.toggle_pin), ("trash", self.delete_selected)):
            b = self._icon_button(footer, self._ficons[key], cmd, win_bg,
                                  T["fill_hover"])
            b.pack(side="left", padx=(0, 2))
        self._cmp_status = tk.Label(footer, textvariable=self.status_var,
                                    bg=win_bg, fg=T["label2"], font=self._font(8))
        self._cmp_status.pack(side="right")
        tk.Label(footer, bg=win_bg, fg=T["label3"], font=self._font(8),
                 text="Enter 回贴 · Ctrl+1-9 快速").pack(side="right", padx=(0, 8))

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

    def _make_header_icons(self, dpi, color_hex):
        box = max(20, int(round(24 * dpi)))

        def close(dr, s, c, w):
            m = s * 0.34
            dr.line([(m, m), (s - m, s - m)], fill=c, width=w)
            dr.line([(s - m, m), (m, s - m)], fill=c, width=w)

        def shot(dr, s, c, w):
            dr.rounded_rectangle([s * 0.22, s * 0.37, s * 0.78, s * 0.73],
                                 radius=s * 0.07, outline=c, width=w)
            dr.line([(s * 0.39, s * 0.37), (s * 0.42, s * 0.29), (s * 0.58, s * 0.29),
                     (s * 0.61, s * 0.37)], fill=c, width=w, joint="curve")
            dr.ellipse([s * 0.43, s * 0.46, s * 0.57, s * 0.60], outline=c, width=w)

        return {"close": self._mono_icon(close, box, color_hex),
                "shot": self._mono_icon(shot, box, color_hex)}

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
        pad = int(round(9 * self._dpi))
        iw = self._search_icon.width()
        c.create_image(pad, h // 2, image=self._search_icon, anchor="w")
        ex = pad + iw + int(round(6 * self._dpi))
        self.search_entry_c.configure(bg=T["field"], fg=T["label"],
                                      insertbackground=T["label"])
        c.create_window(ex, h // 2, anchor="w", window=self.search_entry_c,
                        width=max(20, w - ex - pad), height=h - 2)

    def _make_chip_photo(self, label, h, sel, T):
        from PIL import Image, ImageDraw, ImageTk
        ss = 2
        fsize = max(9, int(round(10 * self._dpi)))
        fnt = self._load_font(fsize * ss)
        pad_x = int(round(12 * self._dpi)) * ss
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
        else:
            fill = self._hex_to_rgb(T["fill"]) + (255,)
            fg = self._hex_to_rgb(T["label"]) + (235,)
        dr.rounded_rectangle([0, 0, s_w - 1, s_h - 1], radius=s_h // 2, fill=fill)
        dr.text(((s_w - tw) / 2 - bb[0], (s_h - th) / 2 - bb[1]), label,
                font=fnt, fill=fg)
        w = max(1, s_w // ss)
        return ImageTk.PhotoImage(im.resize((w, h), Image.LANCZOS)), w

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
        gap = int(round(6 * self._dpi))
        x = 0
        for key, label in self.FILTERS:
            photo, pw = self._make_chip_photo(label, h, key == self._filter, T)
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

    def _make_rounded_bg(self, w, h):
        """窗口材质：大圆角 + 发丝描边，填充随主题。

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
        ImageDraw.Draw(im).rounded_rectangle([0, 0, w - 1, h - 1], radius=20,
                                             fill=win_bg, outline=hair, width=1)
        return ImageTk.PhotoImage(im)

    def _make_footer_icons(self):
        from PIL import Image, ImageDraw, ImageTk
        size = max(20, int(round(26 * self._dpi)))
        ss = 2
        col = self._hex_to_rgb(self._c("label2")) + (255,)
        sheet = self._hex_to_rgb(self._c("window_bg")) + (255,)

        def make(fn):
            s = size * ss
            im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
            fn(ImageDraw.Draw(im), s, col)
            return ImageTk.PhotoImage(im.resize((size, size), Image.LANCZOS))

        def paste(dr, s, c):  # ↩ 回贴
            w = max(2, s // 10)
            dr.line([(s * 0.22, s * 0.28), (s * 0.22, s * 0.72)], fill=c, width=w)
            dr.line([(s * 0.80, s * 0.50), (s * 0.40, s * 0.50)], fill=c, width=w)
            dr.polygon([(s * 0.46, s * 0.34), (s * 0.46, s * 0.66),
                        (s * 0.26, s * 0.50)], fill=c)

        def copy(dr, s, c):  # 两张叠放的纸
            w = max(2, s // 12)
            dr.rounded_rectangle([s * 0.36, s * 0.18, s * 0.82, s * 0.64],
                                 radius=s * 0.06, outline=c, width=w)
            dr.rounded_rectangle([s * 0.18, s * 0.36, s * 0.64, s * 0.82],
                                 radius=s * 0.06, fill=sheet,
                                 outline=c, width=w)

        def pin_icon(dr, s, c):  # 📌 实心字形，缺字体回退简笔钉
            sym = os.path.join(os.environ.get("WINDIR", r"C:\Windows"),
                               "Fonts", "seguisym.ttf")
            drawn = False
            if os.path.exists(sym):
                try:
                    from PIL import ImageFont
                    fnt = ImageFont.truetype(sym, int(s * 0.76))
                    bb = dr.textbbox((0, 0), "\U0001F4CC", font=fnt)
                    if bb[2] - bb[0] > 4:
                        dr.text(((s - bb[2] + bb[0]) / 2 - bb[0],
                                 (s - bb[3] + bb[1]) / 2 - bb[1]),
                                "\U0001F4CC", font=fnt, fill=c)
                        drawn = True
                except Exception:
                    drawn = False
            if not drawn:
                cx = s / 2
                r = s * 0.18
                dr.ellipse([cx - r, s * 0.20, cx + r, s * 0.20 + 2 * r], fill=c)
                dr.rectangle([cx - s * 0.05, s * 0.55, cx + s * 0.05, s * 0.72],
                             fill=c)
                dr.line([(cx, s * 0.72), (cx, s * 0.86)], fill=c,
                        width=max(2, s // 12))

        def trash(dr, s, c):  # 垃圾桶
            w = max(2, s // 12)
            dr.line([(s * 0.24, s * 0.30), (s * 0.76, s * 0.30)], fill=c, width=w)
            dr.line([(s * 0.42, s * 0.30), (s * 0.42, s * 0.20)], fill=c, width=w)
            dr.line([(s * 0.58, s * 0.30), (s * 0.58, s * 0.20)], fill=c, width=w)
            dr.line([(s * 0.42, s * 0.20), (s * 0.58, s * 0.20)], fill=c, width=w)
            dr.rounded_rectangle([s * 0.30, s * 0.38, s * 0.70, s * 0.82],
                                 radius=s * 0.05, outline=c, width=w)

        return {"paste": make(paste), "copy": make(copy),
                "pin": make(pin_icon), "trash": make(trash)}

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
            data = full["image"]
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

    # ---------- 悬停浮动预览 ----------
    HOVER_SHOW_MS = 450
    HOVER_POLL_MS = 120

    def _cancel_hover_timer(self):
        if self._hover_after is not None:
            try:
                canvas = getattr(self, 'cards_canvas', None)
                if canvas:
                    canvas.after_cancel(self._hover_after)
            except Exception:
                pass
            self._hover_after = None

    def _close_hover_popup(self):
        pop = self._hover_pop
        self._hover_pop = None
        if pop is not None:
            try:
                pop.destroy()
            except Exception:
                pass

    def _close_hover(self):
        self._cancel_hover_timer()
        if self._hover_poll_id is not None:
            try:
                self.win.after_cancel(self._hover_poll_id)
            except Exception:
                pass
            self._hover_poll_id = None
        self._close_hover_popup()
        self._hover_iid = None

    def _start_hover_poll(self):
        if self._hover_poll_id is None:
            self._hover_poll_id = self.win.after(self.HOVER_POLL_MS, self._hover_tick)

    def _track_hover_preview(self, canvas, iid):
        if canvas is None or not iid or iid not in self._rows:
            self._hover_iid = None
            self._cancel_hover_timer()
            return
        if iid == self._hover_iid:
            return
        self._hover_iid = iid
        self._cancel_hover_timer()
        if self._hover_pop is not None:
            self._show_hover(iid)
        else:
            self._hover_after = canvas.after(
                self.HOVER_SHOW_MS, lambda: self._show_hover(iid))

    def _show_hover(self, iid):
        self._hover_after = None
        if not self.is_visible() or iid not in self._rows:
            return
        full = self.db.get(self._rows[iid]["id"])
        if not full:
            return
        self._close_hover_popup()
        anchor = self._row_anchor(iid)
        try:
            self._hover_pop = HoverPopup(self, full, anchor)
        except Exception:
            traceback.print_exc()
            self._hover_pop = None
            return
        self._hover_miss = 0
        self._start_hover_poll()

    def _pointer_over_same_row(self, px, py):
        canvas = self._active_canvas()
        if canvas is None or not self._hover_iid:
            return False
        try:
            x0, y0 = canvas.winfo_rootx(), canvas.winfo_rooty()
            w, h = canvas.winfo_width(), canvas.winfo_height()
            if not (x0 <= px <= x0 + w and y0 <= py <= y0 + h):
                return False
            cid = self._hit_row(canvas, py - y0)
            return cid is not None and str(cid) == self._hover_iid
        except Exception:
            return False

    def _hover_tick(self):
        self._hover_poll_id = None
        pop = self._hover_pop
        if pop is None:
            return
        if not self.is_visible():
            self._close_hover()
            return
        if getattr(pop, "_in_menu", False):
            self._hover_miss = 0
            self._start_hover_poll()
            return
        try:
            px, py = self.win.winfo_pointerxy()
        except Exception:
            self._close_hover()
            return
        if pop.contains_point(px, py) or self._pointer_over_same_row(px, py):
            self._hover_miss = 0
            self._start_hover_poll()
        else:
            # 连续 3 次(约 360ms)不在浮窗/卡片行上才关闭，
            # 留出指针从卡片移入浮窗的穿越时间
            self._hover_miss += 1
            if self._hover_miss >= 3:
                self._close_hover()
            else:
                self._start_hover_poll()

    # ---------- 窗口模式 ----------
    def apply_mode(self):
        self.win.overrideredirect(True)
        try:
            self.win.attributes("-transparentcolor", self.TRANS_COLOR)
        except self.tk.TclError:
            pass
        self.win.configure(bg=self.TRANS_COLOR)
        self.win.minsize(1, 1)
        self._bg_canvas.pack(fill="both", expand=True)
        self._cmp_root.place(x=16, y=16, relwidth=1.0, relheight=1.0,
                             width=-32, height=-32)
        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        w = min(self._cw, sw)
        h = min(self._ch, sh)
        px, py = self.win.winfo_pointerxy()
        x = min(max(px - 60, 0), max(0, sw - w))
        y = min(max(py - 24, 0), max(0, sh - h))
        self.win.geometry(f"{w}x{h}+{x}+{y}")
        self.win.attributes("-topmost", True)
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
        self.win.attributes("-topmost", True)
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
            if win32clip.mouse_button_down():
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


class HoverPopup:
    """紧凑卡片悬停浮窗：气泡样式，侧边小箭头指向对应卡片行。

    - 图片弹大图（可拖动移位，右键复制/钉图/另存为）；
      文本弹可选中只读文本框（右键复制选中/复制全部/回贴/导出）
    - 面板右侧放不下自动翻到左侧，箭头换边并始终对准行中心
    - 关闭判定由面板轮询驱动：指针连续离开浮窗与对应卡片行才关闭
    """

    TEXT_MAX = 4000
    IMG_MAX = (520, 400)
    BG = "#ffffff"
    BORDER = "#c9cdd6"
    ARROW_D = 10      # 箭头深度
    ARROW_HALF = 9    # 箭头半宽
    RADIUS = 12
    BORDER_W = 2
    PAD = 8           # 内容内边距
    GAP = 6           # 与面板的间距

    def __init__(self, panel, row, anchor=None):
        self.panel = panel
        self.ui = panel.ui
        self.row = row
        self._drag = None
        self._photo = None
        self._in_menu = False
        self._flat_menu = None
        T, _dark = panel._theme()
        self.T = T
        self.BG = T["card_bg"]
        self.BORDER = T["hairline"]
        tk = panel.tk
        self.win = tk.Toplevel(panel.win)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        try:
            self.win.attributes("-transparentcolor", panel.TRANS_COLOR)
        except tk.TclError:
            pass
        self.win.configure(bg=panel.TRANS_COLOR)

        outer = tk.Frame(self.win, bg=self.BG)
        self._content = outer

        kind = row["kind"]
        try:
            self.cat = row["category"] or ("image" if kind == "image" else "text")
        except (IndexError, KeyError):
            self.cat = "image" if kind == "image" else "text"
        cat_label = HistoryPanel.CATEGORY_LABELS.get(self.cat, self.cat)
        nbytes = (len(row["image"]) if kind == "image" and row["image"]
                  else len((row["content"] or "").encode("utf-8")))
        size_txt = f"{nbytes // 1024} KB" if nbytes >= 1024 else f"{nbytes} B"
        meta = (f"#{row['id']} · {cat_label} · "
                f"{size_txt} · {(row['created_at'] or '')[:19]}"
                f" · {(row['source'] or '?')[:18]}")
        if row["pinned"]:
            meta = "[已钉] " + meta
        if row["tags"]:
            meta += f" · {row['tags'][:16]}"
        self._meta = tk.Label(outer, text=meta, bg=T["field"], fg=T["label2"],
                              font=("Microsoft YaHei UI", 8), anchor="w",
                              padx=8, pady=3, cursor="fleur")
        self._meta.pack(fill="x")
        self._meta.bind("<ButtonPress-1>", self._press)
        self._meta.bind("<B1-Motion>", self._move)

        if kind == "image" and row["image"]:
            from PIL import Image, ImageTk
            img = Image.open(io.BytesIO(row["image"]))
            img.thumbnail(self.IMG_MAX)
            self._photo = ImageTk.PhotoImage(img)
            self._body = tk.Label(outer, image=self._photo, bg=self.BG,
                                  cursor="fleur")
            self._body.pack(padx=6, pady=6)
            self._body.bind("<ButtonPress-1>", self._press)
            self._body.bind("<B1-Motion>", self._move)
        else:
            content = (row["content"] or "")[:self.TEXT_MAX]

            def _wlen(s):
                # CJK 等宽字符按 2 列估算，避免中文行被折得过窄
                return sum(2 if ord(ch) > 0x2E80 else 1 for ch in s)

            longest = max((_wlen(s) for s in content.split("\n")), default=10)
            width = max(28, min(longest + 2, 64))
            self._body = tk.Text(outer, wrap="word", relief="flat", bd=0,
                                 bg=self.BG, fg=T["label"], padx=8, pady=6,
                                 insertbackground=T["label"],
                                 selectbackground=T["accent_soft"],
                                 selectforeground=T["label"],
                                 font=("Microsoft YaHei UI", 10),
                                 width=width,
                                 height=2,
                                 highlightthickness=0, cursor="xterm",
                                 undo=False, autoseparators=False,
                                 exportselection=0)
            self._body.insert("1.0", content)
            # 只读但可选中：保持 normal 才能鼠标拖选/建立 sel 标签，
            # 通过拦截按键阻止编辑（复制走右键菜单或 Ctrl+C）。
            # exportselection=0：选区纯本地——不抢占系统剪贴板，
            # 也不会因外部剪贴板变动收到"选区丢失"而被清空。
            self._body.bind("<Key>", self._readonly_key)
            self._body.pack(padx=6, pady=6)
            # 高度按换行后的实际显示行数自适应（长单行折行也能撑开）。
            # Text.count(-displaylines) 需控件真实映射绘制后才准确，
            # 浮窗构建阶段尚未显示，故用字体度量离线估算。
            nlines = self._estimate_display_lines(content, width)
            self._body.configure(height=max(1, min(nlines, 18)))

        self.win.bind("<Escape>", lambda e: self.panel._close_hover())
        self._body.bind("<Button-3>", self._menu)
        self._meta.bind("<Button-3>", self._menu)

        # ---------- 气泡几何：箭头对准对应卡片行的垂直中心 ----------
        self.win.update_idletasks()
        w0 = outer.winfo_reqwidth()
        h0 = outer.winfo_reqheight()
        A, PAD, GAP, R = self.ARROW_D, self.PAD, self.GAP, self.RADIUS
        bw = A + w0 + 2 * PAD
        bh = h0 + 2 * PAD
        pw = panel.win
        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        side = "left"  # 箭头在气泡左侧 => 气泡位于面板右侧
        x = pw.winfo_rootx() + pw.winfo_width() + GAP
        if x + bw > sw:
            side = "right"
            x = max(0, pw.winfo_rootx() - GAP - bw)
        row_cy = ((anchor[1] + anchor[3]) / 2) if anchor else pw.winfo_rooty() + 80
        y = int(row_cy - bh / 2)
        y = max(4, min(y, sh - bh - 4))
        ah = self.ARROW_HALF
        ay = int(row_cy - y)
        ay = max(R + ah + 2, min(ay, bh - R - ah - 2))
        self._side = side
        self._arrow_y = ay

        self._bg_photo = self._bubble_bg(bw, bh, side, ay)
        self.canvas = tk.Canvas(self.win, width=bw, height=bh,
                                bg=panel.TRANS_COLOR, highlightthickness=0, bd=0)
        self.canvas.pack()
        self.canvas.create_image(0, 0, image=self._bg_photo, anchor="nw")
        cx = A + PAD if side == "left" else PAD
        outer.place(x=cx, y=PAD, width=w0, height=h0)
        outer.lift()
        self.win.geometry(f"{bw}x{bh}+{x}+{y}")

    def _bubble_bg(self, wb, hb, side, ay):
        """圆角气泡 + 侧边小箭头一体绘制：外层描边色，内层卡片填充色。

        与面板窗口同因：-transparentcolor 只认精确键色、无半透明，
        超采样缩放会在边缘留下半透明像素，合成到键色画布上即一圈黑边。
        故按最终尺寸纯 RGB 硬边缘绘制，透明区=键色。
        """
        from PIL import Image, ImageDraw, ImageTk
        key = self.panel._hex_to_rgb(self.panel.TRANS_COLOR)
        A = self.ARROW_D
        AH = self.ARROW_HALF
        B = self.BORDER_W
        R = self.RADIUS
        W, H = wb, hb
        ayc = ay
        bc = self.panel._hex_to_rgb(self.BORDER)
        wc = self.panel._hex_to_rgb(self.BG)
        im = Image.new("RGB", (W, H), key)
        dr = ImageDraw.Draw(im)
        if side == "left":
            dr.rounded_rectangle([A, 0, W - 1, H - 1], radius=R, fill=bc)
            dr.polygon([(0, ayc), (A + B, ayc - AH), (A + B, ayc + AH)], fill=bc)
            dr.rounded_rectangle([A + B, B, W - 1 - B, H - 1 - B],
                                 radius=max(2, R - B), fill=wc)
            dr.polygon([(B + 1, ayc), (A + B, ayc - AH + B + 2),
                        (A + B, ayc + AH - B - 2)], fill=wc)
        else:
            dr.rounded_rectangle([0, 0, W - 1 - A, H - 1], radius=R, fill=bc)
            dr.polygon([(W - 1, ayc), (W - 1 - A - B, ayc - AH),
                        (W - 1 - A - B, ayc + AH)], fill=bc)
            dr.rounded_rectangle([B, B, W - 1 - A - B, H - 1 - B],
                                 radius=max(2, R - B), fill=wc)
            dr.polygon([(W - 2 - B, ayc), (W - 1 - A - B, ayc - AH + B + 2),
                        (W - 1 - A - B, ayc + AH - B - 2)], fill=wc)
        return ImageTk.PhotoImage(im)

    # ---------- 交互 ----------
    _measure_font = None

    @classmethod
    def _estimate_display_lines(cls, text, width_chars):
        """按字体度量估算 word-wrap 后的显示行数（近似 Tk wrap=word）。

        规则：按空白切词贪心填行；超宽 token（如连续中文）按字符折行；
        空段落记 1 行。与 Text.count(-displaylines) 的实测误差在 ±1 行内。
        """
        try:
            from tkinter import font as tkfont
            if cls._measure_font is None:
                cls._measure_font = tkfont.Font(family="Microsoft YaHei UI",
                                                size=10)
            fnt = cls._measure_font
            budget = max(40, width_chars * fnt.measure("0"))
            total = 0
            for para in text.split("\n"):
                if not para.strip():
                    total += 1
                    continue
                lines, cur = 1, 0
                for tok in re.findall(r"\S+|\s+", para):
                    wpx = fnt.measure(tok)
                    if wpx <= budget:
                        if cur + wpx > budget and cur > 0:
                            lines += 1
                            cur = 0 if tok.isspace() else wpx
                        else:
                            cur += wpx
                    else:
                        for ch in tok:  # 超宽 token 按字符折行（CJK 场景）
                            cw = fnt.measure(ch)
                            if cur + cw > budget and cur > 0:
                                lines += 1
                                cur = 0
                            cur += cw
                total += lines
            return max(1, total)
        except Exception:
            return text.count("\n") + 1

    def _readonly_key(self, e):
        # 允许复制/全选/导航/Esc，其余按键拦截以保持只读且可选中
        if (e.state & 0x4) and e.keysym.lower() in ("c", "a"):
            return None
        if e.keysym in ("Left", "Right", "Up", "Down", "Home", "End",
                        "Prior", "Next", "Tab", "Escape"):
            return None
        return "break"

    def contains_point(self, x, y):
        try:
            if not self.win.winfo_viewable():
                return False
            x0, y0 = self.win.winfo_rootx(), self.win.winfo_rooty()
            return (x0 - 4 <= x <= x0 + self.win.winfo_width() + 4 and
                    y0 - 4 <= y <= y0 + self.win.winfo_height() + 4)
        except Exception:
            return False

    def _press(self, e):
        self._drag = (e.x_root - self.win.winfo_x(), e.y_root - self.win.winfo_y())

    def _move(self, e):
        if self._drag:
            self.win.geometry(
                f"+{e.x_root - self._drag[0]}+{e.y_root - self._drag[1]}")

    def _menu(self, e):
        if self.cat == "image":
            items = [("复制图片", self._copy), ("钉图", self._pin_it),
                     ("另存为…", self._save_as)]
        elif self.cat == "file":
            items = [("打开", self._open_file),
                     ("在资源管理器中显示", self._reveal_file),
                     ("复制路径", self._copy), None, ("导出…", self._save_as)]
        else:
            items = [("复制选中", self._copy_sel), ("复制全部", self._copy),
                     None, ("导出…", self._save_as)]
        self._in_menu = True
        self._flat_menu = FlatMenu(self.panel.tk, self.win, items,
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
            self.panel.status_var.set("浮窗中无选中文本")
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
            png = self.row["image"]
            x, y = self.win.winfo_rootx(), self.win.winfo_rooty()
            self.panel._close_hover()
            self.ui._pins.append(PinWindow(self.ui, png, (x, y)))
        except Exception:
            traceback.print_exc()

    def _save_as(self):
        from tkinter import filedialog
        row = self.row
        if row["kind"] == "image":
            path = filedialog.asksaveasfilename(
                parent=self.win, title="保存图片", defaultextension=".png",
                initialfile=f"clip_{row['id']}.png",
                filetypes=[("PNG 图片", "*.png"), ("所有文件", "*.*")])
            data = row["image"]
        else:
            path = filedialog.asksaveasfilename(
                parent=self.win, title="导出文本", defaultextension=".txt",
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

    def destroy(self):
        try:
            self.win.destroy()
        except Exception:
            pass


class PinWindow:
    """钉在屏幕上的截图（类似 Snipaste 贴图）：无边框、置顶。

    - 左键拖动移动位置
    - 滚轮缩放（0.1x - 5x）
    - 双击 / 右键 关闭
    """

    MIN_SCALE = 0.1
    MAX_SCALE = 5.0

    def __init__(self, ui, png, pos=None):
        from PIL import Image, ImageTk
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
        self._label.bind("<Button-3>", lambda e: self.close())
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
        img = self._img.resize((w, h)) if self._scale != 1.0 else self._img
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
