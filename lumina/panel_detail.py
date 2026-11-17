"""右侧常驻详情区（从 history_panel 拆出）。

按类边界原样搬移，未改任何行为：面板只负责布局与选中态，详情区负责渲染
单条记录的完整内容（文本/图片/文件/代码着色）与底部动作栏。
"""
import io
import json
import os
import re
import threading
import traceback
from typing import ClassVar

from .json_modes import (
    ACTION_LABELS,
    ESCAPE_JAVA_NOTICE,
    MODE_LABELS,
    build_menu_items,
)
from .panel_menu import FlatMenu


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
    # 详情栏上下两条固定条的高度（逻辑像素，**不乘 DPI**——与原先写死的
    # 58 / 42 保持同一口径，避免高 DPI 下改变既有布局）。
    #
    # 收紧缩减明细（实测内容需求）：
    # - META_H：顶部元信息条。「预览」19 + 元信息 16 + 上下 pady 各 6 = 47，
    #   原 58 留了 11px 冗余；取 50 既贴紧又留 3px 余量。
    # - ACTION_H：底部动作栏。按钮固定 30 高，pack 在交叉轴居中，
    #   原 42 让上下各空 6px；取 38 → 上下各 4px，仍不贴边。
    META_H = 50
    ACTION_H = 38
    # JSON 预览标题 / 状态文案用的模式名。**唯一来源是 ``json_modes.MODE_LABELS``**，
    # 这里只做再导出（``DetailPane.JSON_MODE_LABELS`` 是既有引用点）。
    #
    # 历史：这份文案曾在这里与 ``tool_window`` 各写一份，``_transform_json`` 里还有
    # 第三份 ``{'format': '格式化', ...}``。用户要求两边同步后，统一收敛到
    # ``json_modes``；菜单项也改由 ``json_modes.build_menu_items`` 生成。
    JSON_MODE_LABELS: ClassVar[dict] = MODE_LABELS
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
    EXT_COLORS: ClassVar[dict] = {
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
        self._json_text = None
        self._json_mode = None
        self._json_request = 0
        self._json_busy = False
        self.cat = "text"
        self._cur_key = None
        self._pending = None
        self._photo = None
        self._urls = []
        self._url_down = None
        self._in_menu = False
        self._flat_menu = None
        self._body = None
        tk = self.tk
        self.frame = tk.Frame(parent, bg=self.BG, width=width)
        self.frame.pack(side="left", fill="y")
        self.frame.pack_propagate(False)
        # 三段式详情：顶部元信息、中部预览、底部固定操作栏。
        self._meta_wrap = tk.Frame(self.frame, bg=T["field"], height=self.META_H)
        self._meta_wrap.pack(fill="x", padx=0, pady=0)
        self._meta_wrap.pack_propagate(False)
        self._meta_icon = tk.Label(self._meta_wrap, bg=T["field"],
                                   bd=0, highlightthickness=0)
        self._meta_icon.pack(side="left", padx=0)
        meta_text = tk.Frame(self._meta_wrap, bg=T["field"])
        # pady 6→4：两行需求高 35 + 上下各 4 = 43，落在 META_H=50 内且余量合适
        meta_text.pack(side="left", fill="both", expand=True, padx=8, pady=4)
        self._meta_title = tk.Label(meta_text, text="预览", bg=T["field"],
                                    fg=T["label"],
                                    font=("Microsoft YaHei UI", 10, "bold"),
                                    anchor="w", bd=0, highlightthickness=0,
                                    pady=0)
        self._meta_title.pack(fill="x")
        self._meta = tk.Label(meta_text, text="", bg=T["field"],
                              fg=T["label2"], font=("Microsoft YaHei UI", 8),
                              anchor="w", bd=0, highlightthickness=0, pady=0)
        self._meta.pack(fill="x")
        self._host = tk.Frame(self.frame, bg=self.BG)
        self._host.pack(fill="both", expand=True, padx=8, pady=(6, 4))
        self._action_separator = tk.Frame(self.frame, bg=T["hairline"], height=1)
        self._action_separator.pack(fill="x", padx=8)
        self._actions = tk.Frame(self.frame, bg=self.BG, height=self.ACTION_H)
        # 下间距 6→4：动作栏按钮本身已固定 30 高、在 ACTION_H 里居中，
        # 底下再叠 6px 会与整窗 footer 那段留白连成一条更厚的空白。
        self._actions.pack(fill="x", padx=8, pady=(4, 4))
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
        if (key == self._cur_key and self.row is not None
                and self.row["content"] == full["content"]):
            return
        self._close_menu()
        if self.row is None or self.row["id"] != full["id"] or self.row["content"] != full["content"]:
            self._reset_json_state()
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
        try:
            self._update_meta(full, kind)
            if self._json_text is not None:
                self._meta_title.configure(
                    text="JSON · " + self.JSON_MODE_LABELS.get(
                        self._json_mode, "已转换"))
            for w in self._host.winfo_children():
                w.destroy()
            if kind == "image" and full["data"]:
                self._build_image_body(self._host, full, self.tk)
            elif self.cat == "file":
                self._build_file_body(self._host, full, self.tk)
            else:
                self._build_text_body(self._host, self._effective_row(), self.tk)
            if self._body is not None:
                self._body.bind("<Button-3>", self._menu)
            self._update_actions()
            # Only suppress future renders after the complete body succeeded.
            self._cur_key = key
        except Exception:
            self._cur_key = None
            for w in self._host.winfo_children():
                w.destroy()
            self._body = None
            self.panel.status_var.set("内容预览失败，请重新选择")
            traceback.print_exc()

    def show_placeholder(self):
        self._reset_json_state()
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
        # 惰性导入：history_panel 会导入本模块，顶层互相引用会成环。
        from .history_panel import HistoryPanel
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
        source_name, _source_color, _style = self.panel._source_app(row["source"])
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
                    radius=radius * 2, fill=(*self.panel._hex_to_rgb(color), 255))
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
            self._json_anchor = btn("JSON ▾", self._json_menu)
        btn("导出", self._save_as)
        btn("删除", self._delete, fg=danger)

    def _reset_json_state(self):
        self._json_request += 1
        self._json_text = None
        self._json_mode = None
        self._json_busy = False

    def _effective_row(self):
        """完整输出取临时结果，不从截断后的预览控件取文本。"""
        if self.row is None or self._json_text is None:
            return self.row
        row = dict(self.row)
        row["content"] = self._json_text
        return row

    def _json_menu(self):
        from .json_popup import JsonPopup
        if isinstance(self._flat_menu, JsonPopup):
            self._close_menu()
            return
        self._close_menu()
        # 菜单项由 ``json_modes.build_menu_items`` 生成——与工具弹框**同一份代码**。
        # 两侧区别只在传进来的两个处理器与本地的忙碌/可恢复状态。
        items = build_menu_items(
            self._transform_json, self._restore_json, self._json_mode,
            busy=self._json_busy,
            can_restore=self._json_text is not None or self._json_busy)
        self._in_menu = True
        self._flat_menu = JsonPopup(self.tk, self.panel.win, items,
                                   self.panel._theme()[0], dpi=self.panel._dpi)
        self._flat_menu.popup(self._json_anchor, on_close=self._menu_closed)

    def _format_json(self):
        self._transform_json("format")

    def _transform_json(self, mode):
        if self.row is None or self.cat not in ("text", "code"):
            return
        from .json_tools import MAX_INPUT_BYTES, transform_json
        raw = self.row["content"] or ""
        # 当前记录仅允许一个任务；忙时不改变有效请求编号。
        if self._json_busy:
            self.panel.status_var.set("JSON 正在处理中，请稍候或恢复原文")
            return
        if len(raw) > MAX_INPUT_BYTES:
            self.panel.status_var.set("JSON 输入过大，最多允许 2 MiB")
            return
        self._json_request += 1
        request, row_id = self._json_request, self.row["id"]
        self._json_busy = True
        self.panel.status_var.set("正在处理 JSON…")
        box = []

        def work():
            try:
                box.append((transform_json(raw, mode), None))
            except json.JSONDecodeError as exc:
                box.append((None, f"JSON 格式错误：第 {exc.lineno} 行，第 {exc.colno} 列：{exc.msg}"))
            except (ValueError, TypeError, RecursionError) as exc:
                box.append((None, str(exc)))
            except Exception:
                traceback.print_exc()
                box.append((None, "JSON 转换失败"))

        def collect():
            if request != self._json_request or self.row is None or self.row["id"] != row_id:
                return
            if not self.frame.winfo_exists():
                return
            if not box:
                self.frame.after(40, collect)
                return
            self._json_busy = False
            result, error = box[0]
            if error:
                self.panel.status_var.set(error)
                return
            self._json_text, self._json_mode = result.text, mode
            self._refresh_json_preview()
            if mode == "escape_java":
                # 产物是 Java 源码片段，不是 JSON——不能套「已{动作} JSON」的
                # 模板，否则用户会以为右栏还是可解析的 JSON。
                message = (f"已{ACTION_LABELS[mode]}为 Java 字符串字面量；"
                           f"复制/回贴/导出使用结果，原文未修改；"
                           f"{ESCAPE_JAVA_NOTICE}")
            else:
                message = (f"已{ACTION_LABELS[mode]} JSON；"
                           f"复制/回贴/导出使用结果，原文未修改")
            if mode == "expand":
                message += "；展开结果非合法 JSON，换行已统一为 LF"
            if result.duplicate_keys:
                message += f"；发现 {len(result.duplicate_keys)} 个重复键，已保留"
            self.panel.status_var.set(message)

        threading.Thread(target=work, name="lumina-json", daemon=True).start()
        self.frame.after(40, collect)

    def _refresh_json_preview(self):
        self._urls = []
        self._url_down = None
        for widget in self._host.winfo_children():
            widget.destroy()
        self._build_text_body(self._host, self._effective_row(), self.tk)
        self._body.bind("<Button-3>", self._menu)
        self._meta_title.configure(
            text="JSON · " + self.JSON_MODE_LABELS.get(self._json_mode, "已转换"))

    def _restore_json(self):
        if self.row is None:
            return
        self._reset_json_state()
        self._refresh_json_preview()
        self._update_meta(self.row, self.row["kind"])
        self.panel.status_var.set("已恢复原文")

    def _delete(self):
        row = self.row
        if row is None:
            return
        if self.panel._delete_record(row["id"]):
            self._cur_key = None

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
        self._body.bind("<ButtonRelease-1>", self._open_image_preview)
        self._body.bind("<MouseWheel>", self._on_wheel)

    def _open_image_preview(self, _event=None):
        """历史图片使用单实例预览，普通文件的打开动作仍使用系统关联。"""
        if self.row is None:
            return "break"
        try:
            if self.cat == "file":
                archived = self.panel.db.get_file_data(self.row["id"])
                data = archived["data"] if archived else None
            else:
                data = self.row["data"]
            if not data:
                self.panel.status_var.set("图片数据不可用")
                return "break"
            self.panel.ui.show_image_preview(data, (self.cat, self.row["id"]))
        except Exception:
            traceback.print_exc()
            self.panel.status_var.set("图片无法打开")
        return "break"

    def _build_text_body(self, outer, row, tk):
        T = self.T
        p = self.panel
        full_text = row["content"] or ""
        truncated = len(full_text) > self.TEXT_MAX
        content = full_text[:self.TEXT_MAX]
        _src_name, src_color, src_style = p._source_app(row["source"])
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
        if self._json_text is not None:
            # JSON 展示态优先用 token 规则着色：内容本就是 JSON，token 才权威。
            # 展开转义后字符串含真实换行，代码高亮正则（遇换行即断）会失配，
            # 整段字符串失去着色、回落默认前景（深色主题下即白字），
            # 还会把栈文本里的数字/单词误判成 num/kw。
            #
            # **但 ``escape_java`` 的产物不是 JSON**：字符串被转义成 ``\"...\"``
            # 之后，``_TOKEN_RE`` 的字符串分支整段失配（引号前有反斜杠），
            # 实测只有裸数字和 ``true`` 被错标成 num/lit，其余全部无跨度——
            # 效果就是「大部分文字没有着色」。所以它走纯文本路径，不做任何
            # token 着色。**不要为了「看起来一致」给它套 JSON 着色**。
            if self._json_mode != "escape_java":
                self._highlight_json(content, console=console)
        elif is_code:
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
        return "#{:02X}{:02X}{:02X}".format(
            *(int(a[i] * t + b[i] * (1 - t)) for i in range(3)))

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
                             fill=(*rgb, 255))
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
                    image_label.bind("<ButtonRelease-1>", self._open_image_preview)
                except Exception:
                    pass

        if not path:
            tk.Label(body, text="没有可用的源文件路径", bg=self.BG,
                     fg=T["label3"], font=("Microsoft YaHei UI", 9),
                     anchor="w").pack(fill="x", padx=10, pady=10)
        body.pack(fill="both", expand=True, padx=2, pady=2)
        body.bind("<MouseWheel>", self._on_wheel)


    def _file_row(self, parent, tk, path, T):
        resolved_path = self.panel._resolve_file_path(self.row)
        open_path = resolved_path or path
        exists = os.path.exists(open_path)
        is_dir = os.path.isdir(open_path)
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
                n = os.path.getsize(open_path)
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
            w.bind("<Button-1>", lambda e, p=open_path: self._open_path(p))
            w.bind("<Double-Button-1>", lambda e, p=open_path: self._open_path(p))
            w.bind("<Button-3>", lambda e, p=open_path: self._file_menu(p, e))
            w.bind("<MouseWheel>", self._on_wheel)

    # ---------- 代码着色 / 链接 ----------
    def _highlight_json(self, content, console=False):
        """JSON 展示态着色：按 token 取跨度，字符串可跨真实换行。

        代码高亮正则的字符串模式 ``"(?:\\.|[^"\\\n])*"`` 明确排除裸换行
        （避免未闭合引号吞掉整篇代码），而 ``expand`` 输出的字符串体内正是
        真实换行 —— 用它会整段失配：字符串失去 ``str`` 标签、回落到控件
        默认前景（深色主题下即白字），还会把栈文本里的数字/单词误判成
        ``num``/``kw``。这里改用 ``json_tools`` 的 token 规则，与转换逻辑
        共用同一套边界定义。
        """
        body = self._body
        try:
            from .json_tools import json_syntax_spans
            spans = json_syntax_spans(content)
        except Exception:
            traceback.print_exc()
            return
        if console:
            colors = {'str': "#CE9178", 'num': "#B5CEA8", 'lit': "#569CD6"}
        else:
            colors = {'str': self.panel._sys("green"), 'num': self.panel._sys("orange"),
                      'lit': self.T["accent"]}
        for kind, color in colors.items():
            body.tag_configure(kind, foreground=color)
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
            # 偏移相对原文单调递增，可复用同一个游标顺序换算行列。
            for kind, start, end in spans:
                body.tag_add(kind, _idx(start), _idx(end))
        except Exception:
            traceback.print_exc()

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
            full = self._effective_row()
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
        path = self.panel._resolve_file_path(self.row) or path
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
            self.ui.actions.copy_to_clipboard(self._effective_row())
        except Exception:
            traceback.print_exc()

    def _first_path(self):
        for line in (self.row["content"] or "").splitlines():
            line = line.strip()
            if line:
                return line
        return ""

    def _open_file(self):
        path = self.panel._resolve_file_path(self.row)
        if not path:
            return
        try:
            os.startfile(path)
        except OSError:
            traceback.print_exc()
            self.panel.status_var.set("文件不存在或无法打开")

    def _reveal_file(self):
        path = self.panel._resolve_file_path(self.row)
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
            from .ui import PinWindow
            self.ui._pins.append(PinWindow(self.ui, png, (x, y)))
        except Exception:
            traceback.print_exc()

    def _save_as(self):
        from tkinter import filedialog
        row = self._effective_row()
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
