"""隐域记账：默认闭眼，表单只返回校验结果，窗口唯一负责写库。"""
import datetime as dt
from decimal import Decimal
from tkinter import messagebox

from . import ledger_tools as tools
from .tool_window import _MiniScroll
from .vault_window import VaultWindow

# 类型符号紧贴金额；历史转账、退款保留各自语义，不引入红绿状态色。
KIND_MARKS = {"expense": "−", "income": "＋", "transfer": "⇄", "refund": "↩"}
ENTRY_KINDS = ("expense", "income")
PAYMENT_ACCOUNTS = ("支付宝", "微信", "京东支付", "银联云闪付")


class LedgerWindow(VaultWindow):
    title = "记账 · 私人账本"
    width = VaultWindow.width
    height = 880
    min_width = VaultWindow.min_width
    min_height = 560
    DEFAULT_DARK = False
    # 浅深两套配色完全继承 VaultWindow（墨蓝金 / 暖米白金），不在这里覆写
    # muted/faint：覆写会让记账窗口在换肤后留下一块与密匣不同的旧灰。

    def __init__(self, panel):
        today = dt.date.today()
        self.mode, self.year, self.month = "month", today.year, today.month
        self.tab = "entries"
        self.chart_mode = "收支"
        self._rows = []
        self._annual = {}
        self._poll_job = None
        self._query = ""
        self._kind = "全部"
        self._category = "全部"
        self._expanded = set()
        self._entry_widgets = {}
        self._filters_open = False
        self._months_open = False
        self._explanation_open = False
        super().__init__(panel)
        self._poll_job = self.win.after(50, self._poll_auth)

    def _center(self):
        # 初始窗口保留屏幕上下边距，低分辨率时不让最小高度顶出屏幕。
        available = max(1, self.win.winfo_screenheight() - 120)
        self.height = min(type(self).height, available)
        self.min_height = min(type(self).min_height, available)
        super()._center()

    def _poll_auth(self):
        self._poll_job = None
        if self._closed:
            return
        self._sync_auth_revocation()
        self._poll_job = self.win.after(50, self._poll_auth)

    def _clear_sensitive_data(self):
        self._hide_note_tip()
        super()._clear_sensitive_data()
        self._rows = []
        self._annual = {}
        self._query, self._kind, self._category = "", "全部", "全部"
        self._expanded.clear()
        self._entry_widgets = {}
        self._entry_header = None
        self._amount_width = 100
        self._filters_open = False
        self._months_open = False
        self._explanation_open = False
        if hasattr(self, "query_var"):
            self.query_var.set("")
            self.kind_var.set("全部")
            self.category_var.set("全部")
            self.category_option["menu"].delete(0, "end")
            self.category_option["menu"].add_command(
                label="全部", command=lambda: self.category_var.set("全部"))

    def _load(self):
        if not self._can_access():
            self._clear_sensitive_data()
            return
        try:
            today = self._today_date()
            start, end = tools.period_bounds(self.mode, self.year, self.month, today)
            rows = [dict(row) for row in self.db.list_ledger_entries(start, end)]
            self._data = tools.summarize(rows, self.mode, self.year, self.month, today)
            self._rows = rows
            start, end = tools.period_bounds("year", self.year, self.month, today)
            annual = [dict(row) for row in self.db.list_ledger_entries(start, end)]
            self._annual = tools.summarize(annual, "year", self.year, self.month, today)
            self._load_error = ""
            menu = self.category_option["menu"]
            menu.delete(0, "end")
            for category in ["全部", *sorted({r["category"] for r in rows})]:
                menu.add_command(label=category, command=lambda c=category: (
                    self.category_var.set(c), self._apply_filters()))
        except Exception:
            self._rows, self._data, self._annual = [], {}, {}
            self._load_error = "读取记账数据失败，请重试"

    def _caption(self, parent, text, color=None, size=9):
        return self.tk.Label(parent, text=text, bg=parent["bg"],
                             fg=color or self.P["title"], anchor="w",
                             font=(self.panel.FONT_FAMILY, size))

    def _button(self, parent, text, command):
        button = self.tk.Button(parent, text=text, command=command, relief="flat", bd=0,
                                bg=self.P["head_bg"], fg=self.P["link"],
                                activebackground=self.P["button"],
                                activeforeground=self.P["button_fg"], padx=10, pady=5,
                                font=(self.panel.FONT_FAMILY, 9), cursor="hand2")
        return button

    def _option(self, parent, variable, values, command):
        option = self.tk.OptionMenu(parent, variable, *values, command=command)
        option.configure(bg=self.P["card"], fg=self.P["title"], bd=0,
                         highlightthickness=0, activebackground=self.P["head_bg"],
                         activeforeground=self.P["title"], font=(self.panel.FONT_FAMILY, 9))
        option["menu"].configure(bg=self.P["card"], fg=self.P["title"],
                                 activebackground=self.P["head_bg"],
                                 activeforeground=self.P["title"])
        return option

    def _build_panes(self, body):
        tk = self.tk
        body.columnconfigure(0, weight=1)
        body.rowconfigure(4, weight=1)
        period = tk.Frame(body, bg=self.P["surface"])
        period.grid(row=0, column=0, sticky="ew", pady=(4, 10))
        period.columnconfigure(1, weight=1)
        modes = tk.Frame(period, bg=self.P["surface"])
        modes.grid(row=0, column=0, sticky="w")
        self.mode_buttons = {}
        for mode, text in (("month", "月"), ("year", "年")):
            button = self._button(modes, text, lambda m=mode: self._set_mode(m))
            button.pack(side="left")
            self.mode_buttons[mode] = button
        navigation = tk.Frame(period, bg=self.P["surface"])
        navigation.grid(row=0, column=1)
        self.previous_button = self._button(navigation, "‹", lambda: self._move_period(-1))
        self.previous_button.pack(side="left")
        self.period_label = self._caption(navigation, "")
        self.period_label.pack(side="left", padx=8)
        self.next_button = self._button(navigation, "›", lambda: self._move_period(1))
        self.next_button.pack(side="left")
        self.current_button = self._button(period, "本月", self._current_period)
        self.current_button.grid(row=0, column=2, sticky="e")
        self.footer = tk.Frame(body, bg=self.P["surface"])
        self.footer.grid(row=5, column=0, sticky="ew", pady=(8, 0))
        self._add_entry_button = self._button(self.footer, "记一笔", self._add_entry)
        self._add_entry_button.pack(side="right")
        self._hint = self._caption(self.footer, "", self.P["muted"])
        self._hint.configure(width=1)
        self._hint.pack(side="left", fill="x", expand=True, padx=8)

        metrics = tk.Frame(body, bg=self.P["surface"])
        metrics.grid(row=1, column=0, sticky="ew")
        self._metric_labels = {}
        for index, (key, label) in enumerate((("income_cents", "收入"),
                                             ("expense_cents", "净支出"),
                                             ("balance_cents", "结余"))):
            metrics.columnconfigure(index, weight=1, uniform="metric")
            card = tk.Frame(metrics, bg=self.P["card"], highlightthickness=1,
                            highlightbackground=self.P["border"])
            card.grid(row=0, column=index, sticky="ew", padx=3, pady=3)
            self._caption(card, label, self.P["muted"]).pack(anchor="w", padx=12, pady=(7, 0))
            value = self._caption(card, "******", size=14)
            value.configure(width=1, justify="left")
            value.pack(fill="x", padx=12, pady=(2, 7))
            value.bind("<Configure>", lambda e, w=value, k=key: self._fit_money(w, self._data.get(k)))
            value.bind("<Button-1>", lambda e, k=key: self._show_metric(k))
            self._metric_labels[key] = value
        self._footnote = self._caption(body, "", self.P["muted"], 8)
        self._footnote.grid(row=2, column=0, sticky="ew", padx=4, pady=6)
        body.bind("<Configure>", lambda e: self._footnote.configure(wraplength=max(1, e.width - 8)))
        tabs = tk.Frame(body, bg=self.P["surface"])
        tabs.grid(row=3, column=0, sticky="ew", pady=5)
        self.tab_buttons = {}
        for tab, text in (("entries", "流水"), ("stats", "统计")):
            button = self._button(tabs, text, lambda t=tab: self._select_tab(t))
            button.pack(side="left", padx=(0, 8))
            self.tab_buttons[tab] = button
        self.filter_button = self._button(tabs, "搜索 / 筛选", self._toggle_filters)
        self.filter_button.pack(side="right")
        self.filter_tags = self._caption(tabs, "", self.P["muted"], 8)
        self.filter_tags.configure(width=1)
        self.filter_tags.pack(side="left", fill="x", expand=True)
        self.clear_filters_button = self._button(tabs, "清空", self._clear_filters)
        self.content = tk.Frame(body, bg=self.P["card"])
        self.content.grid(row=4, column=0, sticky="nsew")
        self.content.rowconfigure(1, weight=1)
        self.content.columnconfigure(0, weight=1)
        self.filters = tk.Frame(self.content, bg=self.P["head_bg"])
        self.query_var = tk.StringVar(value=self._query)
        self.kind_var = tk.StringVar(value=self._kind)
        self.category_var = tk.StringVar(value=self._category)
        self.search_entry = tk.Entry(self.filters, textvariable=self.query_var, width=18,
                                     bg=self.P["card"], fg=self.P["title"], relief="flat",
                                     insertbackground=self.P["title"])
        self.search_entry.pack(side="left", fill="x", expand=True, padx=8, pady=8)
        self.search_entry.bind("<Return>", lambda e: self._apply_filters())
        self.kind_option = self._option(self.filters, self.kind_var,
                                        ["全部", *tools.KINDS.values()], lambda v: self._apply_filters())
        self.kind_option.pack(side="left", padx=4)
        self.category_option = self._option(self.filters, self.category_var, ["全部"],
                                            lambda v: self._apply_filters())
        self.category_option.pack(side="left", padx=4)
        self._button(self.filters, "搜索", self._apply_filters).pack(side="left", padx=8)
        self.chart_controls = tk.Frame(self.content, bg=self.P["head_bg"])
        self.chart_caption = self._caption(self.chart_controls, "每日收支趋势 · ")
        self.chart_caption.pack(side="left", padx=8)
        self.chart_var = tk.StringVar(value=self.chart_mode)
        self._option(self.chart_controls, self.chart_var, ["收支", "结余"],
                     lambda v: self._change_chart()).pack(side="left")
        lane = tk.Frame(self.content, bg=self.P["card"])
        lane.grid(row=1, column=0, sticky="nsew")
        lane.rowconfigure(0, weight=1)
        lane.columnconfigure(0, weight=1)
        self._canvas = tk.Canvas(lane, bg=self.P["card"], bd=0, highlightthickness=0)
        self._canvas.grid(row=0, column=0, sticky="nsew")
        self._scroll = _MiniScroll(tk, lane, "vertical", self._canvas.yview, self.T,
                                  self._accent(), gutter=self.P["card"], thumb=self.P["muted"])
        self._scroll.canvas.grid(row=0, column=1, sticky="ns")
        self._canvas.configure(yscrollcommand=self._scroll.set)
        self._table = tk.Frame(self._canvas, bg=self.P["card"])
        self._table.columnconfigure(0, weight=1)
        self._table_id = self._canvas.create_window(0, 0, window=self._table, anchor="nw")
        self._canvas.bind("<Configure>", self._resize_content)
        self._table.bind("<Configure>", lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all")))
        self.win.bind("<MouseWheel>", self._on_wheel, add="+")

    def _fit_money(self, label, cents, size=14):
        from tkinter import font as tkfont
        text = tools.format_money(cents) if self._can_access() and cents is not None else "******"
        available = max(1, label.winfo_width() - 4)
        font = tkfont.Font(family=self.panel.FONT_FAMILY, size=size)
        while size > 9 and font.measure(text) > available:
            size -= 1
            font.configure(size=size)
        if font.measure(text) > available and text != "******":
            text = f"{Decimal(cents) / 1000000:,.2f}万元"
            while size > 6 and font.measure(text) > available:
                size -= 1
                font.configure(size=size)
        label.configure(text=text, font=(self.panel.FONT_FAMILY, size), wraplength=0)

    def _show_metric(self, key):
        if self._can_access() and key in self._data:
            messagebox.showinfo("完整金额", tools.format_money(self._data[key]), parent=self.win)

    def _toggle_filters(self):
        self._filters_open = not self._filters_open
        self._render()

    def _clear_filters(self):
        self.query_var.set("")
        self.kind_var.set("全部")
        self.category_var.set("全部")
        self._apply_filters()

    def _toggle_months(self):
        self._months_open = not self._months_open
        self._render()

    def _toggle_explanation(self):
        self._explanation_open = not self._explanation_open
        self._render()

    def _resize_content(self, event):
        self._canvas.itemconfigure(self._table_id, width=event.width)
        if hasattr(self, "trend_canvas") and self.trend_canvas.winfo_exists() and self._can_access():
            self._draw_trend()
            self._draw_categories()

    def _on_wheel(self, event):
        # 只滚本窗内容，表单与下拉菜单不抢主窗滚轮。
        if str(event.widget).startswith(str(self._table)) or event.widget is self._canvas:
            self._canvas.yview_scroll(-int(event.delta / 120), "units")
            return "break"

    def _select_tab(self, tab):
        self.tab = tab
        self._render()
        self._canvas.yview_moveto(0)

    def _change_chart(self):
        self.chart_mode = self.chart_var.get()
        if self._can_access():
            self._draw_trend()

    def _valid_period(self, mode, year, month):
        try:
            tools.period_bounds(mode, year, month, self._today_date())
            return True
        except ValueError:
            return False

    def _set_mode(self, mode):
        self.mode = mode
        if not self._valid_period(mode, self.year, self.month):
            self._current_period()
        else:
            self._reload()

    def _current_period(self):
        today = self._today_date()
        self.year, self.month = today.year, today.month
        self._reload()

    def _candidate(self, delta):
        if self.mode == "year":
            return self.year + delta, self.month
        index = (self.year - 1) * 12 + self.month - 1 + delta
        return index // 12 + 1, index % 12 + 1

    def _move_period(self, delta):
        year, month = self._candidate(delta)
        if self._valid_period(self.mode, year, month):
            self.year, self.month = year, month
            self._reload()

    def _drill_month(self, month):
        if self._can_access() and self._valid_period("month", self.year, month):
            self.mode, self.month, self.tab = "month", month, "entries"
            self._reload()

    def _apply_filters(self):
        if not self._can_access():
            return
        self._query = self.query_var.get().strip()
        self._kind, self._category = self.kind_var.get(), self.category_var.get()
        self._render()

    def _filtered_rows(self):
        kind = next((k for k, v in tools.KINDS.items() if v == self._kind), None)
        query = self._query.casefold()
        return [r for r in self._rows if (kind is None or r["kind"] == kind)
                and (self._category == "全部" or r["category"] == self._category)
                and (not query or query in " ".join(str(r[k]) for k in (
                    "note", "category", "account", "entry_date")).casefold())]

    def _render(self):
        self._hide_note_tip()
        self._entry_widgets = {}
        self._entry_header = None
        if not self._can_access():
            self._privacy_visible = False
            self._clear_sensitive_data()
        for child in self._table.winfo_children():
            child.destroy()
        self._paint_privacy_eye()
        period_title = f"{self.year}年" + (f"{self.month}月" if self.mode == "month" else "")
        self.period_label.configure(text=period_title)
        self.current_button.configure(text="本月" if self.mode == "month" else "本年")
        for buttons, selected in ((self.mode_buttons, self.mode), (self.tab_buttons, self.tab)):
            for key, button in buttons.items():
                button.configure(relief="sunken" if key == selected else "flat",
                                 font=(self.panel.FONT_FAMILY, 9, "bold" if key == selected else "normal"))
        for button, delta in ((self.previous_button, -1), (self.next_button, 1)):
            button.configure(state="normal" if self._valid_period(self.mode, *self._candidate(delta)) else "disabled")
        self._add_entry_button.configure(state="normal" if self._can_edit() else "disabled")
        for key, label in self._metric_labels.items():
            self._fit_money(label, self._data.get(key))
        tags = [value for value in (self._query, self._kind, self._category) if value and value != "全部"]
        self.filter_tags.configure(text=" · ".join(tags) if self._can_access() else "")
        self.clear_filters_button.pack_forget()
        if tags and self._can_access():
            self.clear_filters_button.pack(side="right")
        self._footnote.configure(text=f"截至 {self._data.get('cutoff', '')}" if self._can_access() else "")
        self.filters.grid_forget()
        self.chart_controls.grid_forget()
        self._hint.configure(text="已隐藏" if not self._can_access() else self._load_error)
        if not self._can_access():
            self._caption(self._table, "内容已隐藏；点击顶部眼睛显示，也可直接记一笔。",
                          self.P["muted"]).pack(padx=16, pady=50)
            return
        self._hint.configure(text=self._load_error or "已显示 · 金额可点击查看完整值")
        if self._load_error:
            return
        if self.tab == "entries":
            if self._filters_open:
                self.filters.grid(row=0, column=0, sticky="ew")
            self._render_entries()
        else:
            self.chart_controls.grid(row=0, column=0, sticky="ew")
            self._render_stats()

    def _render_entries(self):
        rows = sorted(self._filtered_rows(),
                      key=lambda r: (r["entry_date"], r["id"]), reverse=True)
        self._entry_widgets = {}
        if not rows:
            filtered = bool(self._query or self._kind != "全部" or self._category != "全部")
            text = "没有符合筛选条件的流水" if filtered else "本期还没有流水，先记一笔吧"
            self._caption(self._table, text, self.P["muted"]).pack(pady=(36, 8))
            self._button(self._table, "清空筛选" if filtered else "记一笔",
                         self._clear_filters if filtered else self._add_entry).pack(pady=(0, 36))
            return
        from tkinter import font as tkfont
        font = tkfont.Font(family=self.panel.FONT_FAMILY, size=11)
        self._amount_width = max(100, min(154, max(
            font.measure(KIND_MARKS[r["kind"]] + tools.format_money(r["amount_cents"]))
            for r in rows) + 8))
        self._entry_header = self.tk.Frame(self._table, bg=self.P["surface"])
        self._entry_header.pack(fill="x", padx=8)
        self._configure_entry_columns(self._entry_header)
        for column, text in enumerate(("分类", "账户", "备注", "金额", "操作")):
            label = self._caption(self._entry_header, text, self.P["muted"], 8)
            label.configure(width=1, anchor="e" if column == 3 else "w")
            label.grid(row=0, column=column, columnspan=2 if column == 4 else 1,
                       sticky="ew", padx=4, pady=4)
        groups = {}
        for item in rows:
            groups.setdefault(item["entry_date"], []).append(item)
        index = 0
        for date, items in groups.items():
            self._date_head(date, items)
            for item in items:
                self._entry_row(item, index)
                index += 1
        self._expanded.intersection_update(self._entry_widgets)

    def _date_head(self, date, items):
        """小计只累加筛选后本日流水；退款冲减净支出，转账不计收支。"""
        head = self.tk.Frame(self._table, bg=self.P["head_bg"])
        head.pack(fill="x", padx=8, pady=(8, 2))
        head.columnconfigure(0, minsize=150)
        head.columnconfigure(1, weight=1)
        day = dt.date.fromisoformat(date)
        weekday = "一二三四五六日"[day.weekday()]
        title = self._caption(head, f"{day.month}月{day.day}日 周{weekday} · {len(items)} 笔")
        title.configure(width=1)
        title.grid(row=0, column=0, sticky="ew", padx=10, pady=2)
        income = sum(i["amount_cents"] for i in items if i["kind"] == "income")
        expense = sum(i["amount_cents"] * (-1 if i["kind"] == "refund" else 1)
                      for i in items if i["kind"] in ("expense", "refund"))
        filtered = bool(self._query or self._kind != "全部" or self._category != "全部")
        text = (f"{'筛选后' if filtered else '本日'} · 收入 {tools.format_money(income)}"
                f" · 净支出 {tools.format_money(expense)}")
        summary = self._caption(head, text, self.P["muted"], 8)
        summary.configure(width=1, anchor="e")
        summary.grid(row=0, column=1, sticky="ew", padx=10, pady=2)
        def fit_summary(event):
            from tkinter import font as tkfont
            size = 8
            font = tkfont.Font(family=self.panel.FONT_FAMILY, size=size)
            while size > 6 and font.measure(text) > event.width - 4:
                size -= 1
                font.configure(size=size)
            summary.configure(font=(self.panel.FONT_FAMILY, size))
            self._elide(summary, text)

        summary.bind("<Configure>", fit_summary)
        summary.bind("<Enter>", lambda e: self._schedule_text_tip(summary, text))
        summary.bind("<Leave>", lambda e: self._leave_note_tip())

    def _row_button(self, parent, text, command, bg):
        """Label 版轻按钮：视觉是按钮，但行内不放 tk.Button。"""
        P = self.P
        button = self.tk.Label(parent, text=text, bg=bg, fg=P["muted"], padx=6, pady=2,
                               font=(self.panel.FONT_FAMILY, 8), cursor="hand2",
                               takefocus=1, highlightthickness=1,
                               highlightbackground=P["border"])

        def invoke(_event=None):
            if self._can_access():
                command()
            return "break"

        for key in ("<Button-1>", "<Return>", "<space>"):
            button.bind(key, invoke)

        def emphasis(on):
            button.configure(fg=P["link"] if on else P["muted"],
                             highlightbackground=P["link"] if on else P["border"])

        button.bind("<Enter>", lambda _e: emphasis(True))
        button.bind("<Leave>", lambda _e: emphasis(False))
        button.bind("<FocusIn>", lambda _e: emphasis(True))
        button.bind("<FocusOut>", lambda _e: emphasis(False))
        return button

    def _entry_detail_text(self, item):
        return (f"{item['entry_date']} · {item['category']} · {tools.KINDS[item['kind']]}"
                f" · {item['account']}\n"
                f"精确金额 {tools.format_money(item['amount_cents'])}\n"
                f"备注：{item['note'] or '（无）'}")

    def _toggle_entry_detail(self, detail, item):
        """完整备注按需展开；隐藏态下不泄露明文。"""
        if not self._can_access():
            return "break"
        if item["id"] in self._expanded:
            self._expanded.discard(item["id"])
            detail.grid_remove()
        else:
            self._expanded.add(item["id"])
            detail.configure(text=self._entry_detail_text(item))
            detail.grid()
        return "break"

    def _configure_entry_columns(self, row):
        # 固定列使用像素宽度，全部剩余空间只交给备注；标题与每行共享此布局。
        for column, width in ((0, 86), (1, 96), (3, self._amount_width), (4, 44), (5, 44)):
            row.columnconfigure(column, minsize=width)
        row.columnconfigure(2, weight=1, minsize=24)

    def _fit_entry_amount(self, label, item):
        from tkinter import font as tkfont
        text = KIND_MARKS[item["kind"]] + tools.format_money(item["amount_cents"])
        size = 11
        font = tkfont.Font(family=self.panel.FONT_FAMILY, size=size)
        while size > 6 and font.measure(text) > label.winfo_width() - 4:
            size -= 1
            font.configure(size=size)
        label.configure(text=text if self._can_access() else "******",
                        font=(self.panel.FONT_FAMILY, size), wraplength=0)

    def _entry_row(self, item, index):
        P = self.P
        bg = P["row_alt" if index % 2 else "card"]
        row = self.tk.Frame(self._table, bg=bg)
        row.pack(fill="x", padx=8, pady=1)
        self._configure_entry_columns(row)
        row.rowconfigure(0, minsize=38)
        cells = {}
        for column, key in enumerate(("category", "account", "note")):
            cell = self._caption(row, "", size=9)
            cell.configure(width=1)
            cell.grid(row=0, column=column, sticky="ew", padx=4)
            text = " ".join(item[key].splitlines())
            cell.bind("<Configure>", lambda e, w=cell, t=text: self._elide(w, t))
            cell.bind("<Enter>", lambda e, w=cell, t=item[key]: self._schedule_text_tip(w, t))
            cell.bind("<Leave>", lambda e: self._leave_note_tip())
            cells[key] = cell
        note = cells["note"]
        note.configure(cursor="hand2", takefocus=1)
        amount = self._caption(row, "", size=11)
        amount.configure(anchor="e", width=1)
        amount.grid(row=0, column=3, sticky="ew", padx=4)
        amount.bind("<Configure>", lambda e, w=amount, i=item: self._fit_entry_amount(w, i))
        amount.bind("<Button-1>", lambda e, i=item:
                    messagebox.showinfo("完整金额", self._entry_detail_text(i), parent=self.win)
                    if self._can_access() else None)
        edit = self._row_button(row, "编辑", lambda i=item["id"]: self._edit_entry(i), bg)
        edit.grid(row=0, column=4, padx=2)
        delete = self._row_button(row, "删除", lambda i=item["id"]: self._delete_entry(i), bg)
        delete.grid(row=0, column=5, padx=2)
        detail = self._caption(row, "", P["muted"], 8)
        detail.configure(justify="left", anchor="w", width=1)
        detail.grid(row=1, column=0, columnspan=6, sticky="ew", padx=10, pady=(0, 6))
        detail.grid_remove()
        detail.bind("<Configure>", lambda e, w=detail:
                    w.configure(wraplength=max(1, w.winfo_width() - 4)))
        for key in ("<Button-1>", "<Return>", "<space>"):
            note.bind(key, lambda e, d=detail, i=item: self._toggle_entry_detail(d, i))
        note.bind("<FocusIn>", lambda _e, w=note: w.configure(fg=P["link"]))
        note.bind("<FocusOut>", lambda _e, w=note: w.configure(fg=P["title"]))
        if item["id"] in self._expanded:
            detail.configure(text=self._entry_detail_text(item))
            detail.grid()

        def hover(on):
            if row.winfo_exists():
                color = P["head_bg"] if on and self._can_access() else bg
                for widget in (row, *row.winfo_children()):
                    widget.configure(bg=color)

        def leave(event):
            # 跨子控件不退出整行悬停，避免按钮或备注边界闪烁。
            x, y = event.x_root, event.y_root
            inside = (row.winfo_rootx() <= x < row.winfo_rootx() + row.winfo_width()
                      and row.winfo_rooty() <= y < row.winfo_rooty() + row.winfo_height())
            hover(inside)

        for widget in (row, *row.winfo_children()):
            widget.bind("<Enter>", lambda e: hover(True), add="+")
            widget.bind("<Leave>", leave, add="+")
        self._entry_widgets[item["id"]] = (
            row, cells["category"], cells["account"], note, amount, edit, delete, detail)

    def _hide_note_tip(self):
        job = getattr(self, "_note_tip_job", None)
        if job is not None:
            self.win.after_cancel(job)
        self._note_tip_job = None
        tip = getattr(self, "_note_tip", None)
        if tip is not None and tip.winfo_exists():
            tip.destroy()
        self._note_tip = None

    def _leave_note_tip(self):
        job = getattr(self, "_note_tip_job", None)
        if job is not None:
            self.win.after_cancel(job)
        self._note_tip_job = self.win.after(150, self._check_note_tip_pointer)

    def _check_note_tip_pointer(self):
        self._note_tip_job = None
        tip = getattr(self, "_note_tip", None)
        if tip is not None and tip.winfo_exists():
            x, y = self.win.winfo_pointerxy()
            if (tip.winfo_rootx() <= x < tip.winfo_rootx() + tip.winfo_width()
                    and tip.winfo_rooty() <= y < tip.winfo_rooty() + tip.winfo_height()):
                return
        self._hide_note_tip()

    def _schedule_text_tip(self, widget, text):
        self._hide_note_tip()
        if self._can_access() and text:
            self._note_tip_job = self.win.after(
                400, lambda: self._show_text_tip(widget, text))

    def _show_text_tip(self, widget, full_text):
        self._note_tip_job = None
        if not self._can_access() or not widget.winfo_exists():
            return
        self._hide_note_tip()
        p = self.P
        tip = self.tk.Toplevel(self.win)
        self._note_tip = tip
        tip.overrideredirect(True)
        tip.transient(self.win)
        tip.configure(bg=p["border"])
        # 长备注完整保留，用可滚动文本避免气泡超出屏幕。
        text = self.tk.Text(tip, wrap="word", width=48, height=6, bd=0,
                            bg=p["card"], fg=p["title"],
                            font=(self.panel.FONT_FAMILY, 9), padx=8, pady=6)
        text.pack(side="left", fill="both", expand=True, padx=1, pady=1)
        scroll = self.tk.Scrollbar(tip, command=text.yview)
        scroll.pack(side="right", fill="y")
        text.configure(yscrollcommand=scroll.set)
        text.insert("1.0", full_text)
        text.configure(state="disabled")
        tip.update_idletasks()
        x = min(self.win.winfo_pointerx() + 12,
                max(0, tip.winfo_screenwidth() - tip.winfo_reqwidth() - 8))
        y = min(widget.winfo_rooty() + widget.winfo_height(),
                max(0, tip.winfo_screenheight() - tip.winfo_reqheight() - 8))
        tip.geometry(f"+{max(0, x)}+{max(0, y)}")
        tip.bind("<Leave>", lambda e: self._hide_note_tip() if e.widget is tip else None)

    def _elide(self, label, text):
        from tkinter import font as tkfont
        font = tkfont.Font(font=label.cget("font"))
        available = max(1, label.winfo_width() - 4)
        shortened = text
        while shortened and font.measure(shortened) > available:
            shortened = shortened[:-1]
        if shortened != text:
            while shortened and font.measure(shortened + "...") > available:
                shortened = shortened[:-1]
            shortened += "..."
        label.configure(text=shortened)

    def _render_stats(self):
        self.chart_caption.configure(text="每日收支趋势 · " if self.mode == "month" else "全年12月趋势 · ")
        self._caption(self._table, "本期分类净支出", self.P["muted"]).pack(fill="x", padx=12, pady=8)
        self.category_canvas = self.tk.Canvas(self._table, bg=self.P["card"], highlightthickness=0,
                                              height=max(80, 38 * len(self._data["categories"]) + 30))
        self.category_canvas.pack(fill="x", padx=8)
        self.category_canvas.bind("<Configure>", lambda e: self._draw_categories())
        self._caption(self._table, "每日收支趋势" if self.mode == "month" else "全年12月趋势",
                      self.P["muted"]).pack(fill="x", padx=12, pady=8)
        self.trend_canvas = self.tk.Canvas(self._table, height=250, bg=self.P["card"], highlightthickness=0)
        self.trend_canvas.pack(fill="x", padx=8)
        self.trend_canvas.bind("<Configure>", lambda e: self._draw_trend())
        self.average_label = self._caption(self._table,
            f"月均净支出 {tools.format_money(self._annual['average_expense_cents'])} · "
            f"按 {self._annual['months_elapsed']} 个月计算", self.P["muted"])
        self.average_label.pack(fill="x", padx=12, pady=8)
        self.months_button = self._button(self._table, "月度表 ▴" if self._months_open else "月度表 ▾",
                                         self._toggle_months)
        self.months_button.pack(anchor="w", padx=12, pady=4)
        self.month_rows = []
        for item in self._annual["months"] if self._months_open else []:
            row = self.tk.Frame(self._table, bg=self.P["head_bg"])
            row.pack(fill="x", padx=8, pady=2)
            self.month_rows.append(row)
            label = self._caption(row, f"{item['month']:02d}月")
            label.pack(side="left", padx=12, pady=6)
            text = "未来期间" if item["future"] else "    ".join(
                f"{caption} {tools.format_money(item[key])}" for key, caption in (
                    ("income_cents", "收入"), ("expense_cents", "支出"), ("balance_cents", "结余")))
            value = self._caption(row, text, self.P["muted"])
            value.configure(width=1, justify="left")
            value.pack(side="left", fill="x", expand=True, padx=12, pady=6)
            value.bind("<Configure>", lambda e, w=value: w.configure(wraplength=max(1, e.width)))
            if not item["future"]:
                for widget in (row, label, value):
                    widget.configure(cursor="hand2")
                    widget.bind("<Button-1>", lambda e, m=item["month"]: self._drill_month(m))
        self.explanation_button = self._button(self._table,
            "统计说明 ▴" if self._explanation_open else "统计说明 ▾", self._toggle_explanation)
        self.explanation_button.pack(anchor="w", padx=12, pady=4)
        if self._explanation_open:
            explanation = self._caption(self._table,
                "当前期间截至今天，历史期间截至期末；筛选仅影响流水。\n"
                "退款按发生日期及分类冲减净支出，转账不计收支；结余不是账户余额。\n"
                "本年月均包含未结束的本月，历史年度按12个月计算。", self.P["muted"], 8)
            explanation.configure(justify="left", width=1)
            explanation.pack(fill="x", padx=12, pady=8)
            explanation.bind("<Configure>", lambda e: explanation.configure(wraplength=max(1, e.width)))
        self._draw_trend()
        self._draw_categories()

    def _trend_periods(self):
        if self.mode == "year":
            return self._annual["months"]
        import calendar
        days = calendar.monthrange(self.year, self.month)[1]
        periods = [{"day": day, "income_cents": 0, "expense_cents": 0, "balance_cents": 0,
                    "future": dt.date(self.year, self.month, day) > self._today_date()}
                   for day in range(1, days + 1)]
        for row in self._rows:
            period = periods[dt.date.fromisoformat(row["entry_date"]).day - 1]
            if row["kind"] == "income":
                period["income_cents"] += row["amount_cents"]
            elif row["kind"] in ("expense", "refund"):
                period["expense_cents"] += row["amount_cents"] * (-1 if row["kind"] == "refund" else 1)
            period["balance_cents"] = period["income_cents"] - period["expense_cents"]
        return periods

    def _draw_trend(self):
        canvas = self.trend_canvas
        canvas.delete("all")
        if not self._can_access():
            return
        width = max(1, canvas.winfo_width())
        months = self._trend_periods()
        keys = ["balance_cents"] if self.chart_mode == "结余" else ["income_cents", "expense_cents"]
        values = [m[k] for m in months for k in keys]
        low, high = min([0, *values]), max([1, *values])
        scale = 170 / (high - low)
        yzero = 205 + low * scale
        from tkinter import font as tkfont
        axis_font = tkfont.Font(family=self.panel.FONT_FAMILY, size=8)
        axis_texts = [tools.format_money(value) for value in (low, high)]
        left = max(axis_font.measure(text) for text in axis_texts) + 12
        canvas.create_line(left, yzero, width - 10, yzero, fill=self.P["border"], tags="zero")
        for value, text in zip((low, high), axis_texts):
            canvas.create_text(left - 6, 205 - (value - low) * scale, text=text,
                               anchor="e", fill=self.P["muted"], font=axis_font, tags="axis")
        colors = [self.P["link"], self.P["ok_fg"]]
        caption = "结余" if len(keys) == 1 else "收入（蓝） / 净支出（绿）"
        canvas.create_text(left, 12, text=caption, anchor="w", fill=self.P["title"])
        step = max(1, width - left - 10) / len(months)
        for i, item in enumerate(months):
            number = item["month"] if self.mode == "year" else item["day"]
            tag = f"month{number}" if self.mode == "year" else f"day{number}"
            x = left + step * i
            for j, key in enumerate(keys):
                y = 205 - (item[key] - low) * scale
                canvas.create_rectangle(x + step * .1 + j * step * .3, min(y, yzero),
                                        x + step * .1 + (j + 1) * step * .3, max(y, yzero),
                                        fill=colors[j], outline="", tags=tag)
            if self.mode == "year" or number == 1 or number % 5 == 0 or number == len(months):
                canvas.create_text(x + step / 2, 228, text=str(number), font=axis_font,
                                   fill=self.P["muted"] if item["future"] else self.P["title"], tags=tag)
            if self.mode == "year" and not item["future"]:
                canvas.tag_bind(tag, "<Button-1>", lambda e, m=number: self._drill_month(m))

    def _draw_categories(self):
        canvas = self.category_canvas
        canvas.delete("all")
        if not self._can_access():
            return
        rows = self._data["categories"]
        if not rows:
            canvas.create_text(20, 30, text="本期没有分类支出", anchor="w", fill=self.P["muted"])
            return
        width = max(1, canvas.winfo_width())
        low = min([0, *(r["amount_cents"] for r in rows)])
        high = max([1, *(r["amount_cents"] for r in rows)])
        from tkinter import font as tkfont
        value_font = tkfont.Font(family=self.panel.FONT_FAMILY, size=8)
        valid_shares = low >= 0 and sum(r["amount_cents"] for r in rows) > 0
        texts = [tools.format_money(row["amount_cents"]) + (
            f" · {row['share']:.1%}" if valid_shares and row["share"] is not None else "") for row in rows]
        left = min(155, width * .25)
        right = max(left + 1, width - max(value_font.measure(text) for text in texts) - 22)
        scale = (right - left) / (high - low)
        zero = left - low * scale
        canvas.create_line(zero, 5, zero, len(rows) * 38 + 15, fill=self.P["muted"], tags="zero")
        for i, row in enumerate(rows):
            y = 22 + i * 38
            x = zero + row["amount_cents"] * scale
            canvas.create_text(8, y, text=row["category"], anchor="w", width=max(1, left - 16),
                               fill=self.P["title"], font=value_font)
            canvas.create_rectangle(min(zero, x), y - 9, max(zero, x), y + 9,
                                    fill=self.P["link"] if row["amount_cents"] >= 0 else self.P["matured_fg"],
                                    outline="", tags="category_bar")
            canvas.create_text(width - 10, y, text=texts[i], anchor="e", fill=self.P["title"],
                               font=value_font, tags="category_value")

    def _add_entry(self):
        self._entry_dialog()

    def _edit_entry(self, entry_id):
        if not self._can_access():
            return
        try:
            item = self.db.get_ledger_entry(entry_id)
            if item is not None:
                self._entry_dialog(dict(item))
        except Exception:
            self._hint.configure(text="读取流水失败")

    def _entry_dialog(self, item=None):
        if not self._can_edit() or self._deposit_dialog is not None:
            return
        token = self._write_token()
        try:
            accounts = self.db.list_ledger_accounts()
            categories = {kind: self.db.list_ledger_categories(kind) for kind in tools.KINDS}
        except Exception:
            self._hint.configure(text="读取账户分类失败")
            return
        if not self._can_write(token) or (item is not None and not self._can_access()):
            return
        dialog = _LedgerDialog(self, accounts, categories, item,
                               submit_callback=lambda d: self._save_dialog(d, item))
        self._deposit_dialog = dialog
        try:
            self.win.wait_window(dialog.win)
        finally:
            self._deposit_dialog = None

    def _save_dialog(self, dialog, item=None):
        # 表单只校验；父窗是唯一写库点，模态窗口存续到成功或取消。
        if not self._can_write(dialog._write_session) or (item is not None and not self._can_access()):
            dialog._cancel()
            return
        record = dialog.result
        if record is None:
            return
        try:
            if item is None:
                self.db.add_ledger_entry(**record)
            else:
                self.db.update_ledger_entry(item["id"], **record)
        except Exception as exc:
            dialog.result = None
            if not self._can_write(dialog._write_session) or not dialog.win.winfo_exists():
                if dialog.win.winfo_exists():
                    dialog._cancel()
                return
            dialog.error.configure(text=str(exc) if isinstance(exc, ValueError)
                                   else "保存失败，请重试；输入已保留")
            return
        if not self._can_write(dialog._write_session) or (item is not None and not self._can_access()):
            if dialog.win.winfo_exists():
                dialog._cancel()
            return
        if dialog.again and item is None:
            dialog._reset_after_save()
        else:
            dialog._destroy()
        self._reload()

    def _delete_entry(self, entry_id):
        if not self._can_access():
            return
        if not messagebox.askyesno("删除流水", "删除这笔流水？此操作不能撤销。", parent=self.win):
            return
        if not self._can_access():
            return
        try:
            self.db.delete_ledger_entry(entry_id)
        except Exception:
            self._hint.configure(text="删除失败，请刷新重试")
            return
        self._reload()

    def close(self):
        if self._closed:
            return
        self._hide_note_tip()
        if self._poll_job is not None:
            self.win.after_cancel(self._poll_job)
            self._poll_job = None
        super().close()


class _LedgerDialog:
    def __init__(self, window, accounts, categories, item=None, submit_callback=None):
        self.window, self.result, self.again = window, None, False
        self.submit_callback = submit_callback
        self._submitting = False
        self.categories = categories
        self._write_session = window._write_token()
        tk, p = window.tk, window.P
        self.win = tk.Toplevel(window.win)
        self.win.title("编辑流水" if item else "记一笔")
        self.win.transient(window.win)
        self.win.configure(bg=p["surface"])
        self.win.resizable(False, False)
        self.variables, self.entries = {}, {}
        item = item or {}
        initial = {"kind": tools.KINDS[item.get("kind", "expense")], "amount": "", "category": "",
                   "account": accounts[0] if accounts else "现金",
                   "entry_date": window._today_date().isoformat(), "note": ""}
        for key in ("category", "account", "entry_date", "note"):
            initial[key] = item.get(key, initial[key])
        if item:
            initial["amount"] = str(Decimal(item["amount_cents"]) / 100)
        fields = (("kind", "类型"), ("amount", "金额（元）"), ("entry_date", "日期"),
                  ("category", "分类"), ("account", "账户"), ("note", "备注"))
        for i, (key, title) in enumerate(fields):
            window._caption(self.win, title, p["muted"]).grid(row=i, column=0, sticky="w", padx=20, pady=7)
            variable = tk.StringVar(value=initial[key])
            self.variables[key] = variable
            if key == "kind":
                entry = tk.Frame(self.win, bg=p["surface"])
                self.kind_buttons = {}
                choices = list(ENTRY_KINDS)
                # 旧记录原类型保留可编辑，不把退款或转账静默改成收支。
                if item.get("kind") not in (*ENTRY_KINDS, None):
                    choices.append(item["kind"])
                for index, kind in enumerate(choices):
                    entry.columnconfigure(index, weight=1, uniform="kind")
                    button = window._button(entry, tools.KINDS[kind],
                                            lambda k=kind: self._select_kind(k))
                    button.grid(row=0, column=index, sticky="ew", padx=(0, 4))
                    self.kind_buttons[kind] = button
            else:
                entry = tk.Entry(self.win, textvariable=variable, width=34, bg=p["card"], fg=p["title"],
                                 insertbackground=p["title"], relief="flat", highlightthickness=1,
                                 highlightbackground=p["border"], highlightcolor=p["link"],
                                 font=(window.panel.FONT_FAMILY, 10))
            entry.grid(row=i, column=1, sticky="ew", padx=(0, 20), pady=6, ipady=4)
            self.entries[key] = entry
        suggestions = tk.Frame(self.win, bg=p["surface"])
        suggestions.grid(row=6, column=1, sticky="ew", padx=(0, 20), pady=(4, 0))
        suggestions.columnconfigure((0, 1), weight=1, uniform="suggestion")
        self.category_choice = tk.StringVar(value="选择分类")
        self.category_menu = window._option(suggestions, self.category_choice, ["选择分类"],
                                            lambda v: self.variables["category"].set(v))
        self.category_menu.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.account_choice = tk.StringVar(value="选择账户")
        account_values = list(dict.fromkeys(["现金", *accounts, *PAYMENT_ACCOUNTS]))
        self.account_menu = window._option(
            suggestions, self.account_choice, ["选择账户", *account_values],
            lambda v: self.variables["account"].set(v) if v != "选择账户" else None)
        self.account_menu.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        self.error = window._caption(self.win, "", p["matured_fg"])
        self.error.configure(wraplength=410, justify="left")
        self.error.grid(row=7, column=0, columnspan=2, sticky="ew", padx=20, pady=6)
        buttons = tk.Frame(self.win, bg=p["surface"])
        buttons.grid(row=8, column=0, columnspan=2, padx=20, pady=14, sticky="e")
        self.cancel_button = window._button(buttons, "取消", self._cancel)
        self.cancel_button.pack(side="left", padx=4)
        self.save_button = window._button(buttons, "保存", self._submit)
        self.save_button.pack(side="left", padx=4)
        self.again_button = window._button(buttons, "保存并再记", lambda: self._submit(True))
        self.again_button.pack(side="left", padx=4)
        if item:
            self.again_button.configure(state="disabled")
        self._kind_changed()
        self.win.protocol("WM_DELETE_WINDOW", self._cancel)
        self.win.bind("<Return>", lambda e: self._submit())
        self.win.bind("<Escape>", lambda e: self._cancel())
        for entry in self.entries.values():
            entry.bind("<Return>", lambda e: self._submit())
            entry.bind("<Escape>", lambda e: self._cancel())
        self.win.columnconfigure(1, weight=1)
        self.win.update_idletasks()
        self.win.geometry(f"500x{self.win.winfo_reqheight()}"
                          f"+{window.win.winfo_rootx() + max(0, (window.win.winfo_width() - 500) // 2)}"
                          f"+{window.win.winfo_rooty() + 40}")
        self.win.grab_set()
        self.win.focus_force()
        self.entries["amount"].focus_force()

    def _kind(self):
        return next(k for k, v in tools.KINDS.items() if v == self.variables["kind"].get())

    def _select_kind(self, kind):
        previous = self._kind()
        if kind != previous and self.variables["category"].get() in tools.DEFAULT_CATEGORIES[previous]:
            self.variables["category"].set(tools.DEFAULT_CATEGORIES[kind][0])
        self.variables["kind"].set(tools.KINDS[kind])
        self._kind_changed()

    def _kind_changed(self):
        kind = self._kind()
        p = self.window.P
        for key, button in self.kind_buttons.items():
            selected = key == kind
            button.configure(bg=p["button"] if selected else p["head_bg"],
                             fg=p["button_fg"] if selected else p["link"],
                             relief="sunken" if selected else "flat")
        menu = self.category_menu["menu"]
        menu.delete(0, "end")
        values = list(dict.fromkeys([*tools.DEFAULT_CATEGORIES[kind], *self.categories[kind]]))
        for value in values:
            menu.add_command(label=value, command=lambda v=value: self.variables["category"].set(v))
        if not self.variables["category"].get():
            self.variables["category"].set(values[0])

    def _submit(self, again=False):
        if self._submitting or self.result is not None:
            return "break"
        if not self.window._can_write(self._write_session):
            self._cancel()
            return "break"
        values = {k: v.get() for k, v in self.variables.items()}
        try:
            record = tools.build_entry(self._kind(), values["amount"], values["category"],
                                       values["account"], values["entry_date"], values["note"])
        except ValueError as exc:
            self.error.configure(text=str(exc))
            return "break"
        if not self.window._can_write(self._write_session):
            self._cancel()
            return "break"
        self.result, self.again = record, again
        self._submitting = True
        try:
            if self.submit_callback is not None:
                self.submit_callback(self)
            else:
                self._destroy()
        finally:
            self._submitting = False
        return "break"

    def _reset_after_save(self):
        self.result, self.again = None, False
        for key in ("amount", "note"):
            self.variables[key].set("")
        self.variables["entry_date"].set(self.window._today_date().isoformat())
        self.error.configure(text="")
        self.entries["amount"].focus_set()

    def _destroy(self):
        if not self.win.winfo_exists():
            return
        for variable in self.variables.values():
            variable.set("")
        self.win.destroy()

    def _cancel(self):
        self.result = None
        self._destroy()
        return "break"
