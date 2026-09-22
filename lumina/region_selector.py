"""Region selector for screenshot area capture."""
import io
import math
import os
import time
import traceback
import ctypes
from ctypes import wintypes

from PIL import Image, ImageEnhance, ImageFilter, ImageFont, ImageTk

from . import clipboard_api

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
                x0, y0, x1, y1, outline="#a6e3a1", width=2)
            self._size_id = self.canvas.create_text(
                x0, y0 - 12, text="", anchor="sw", fill="#a6e3a1",
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

