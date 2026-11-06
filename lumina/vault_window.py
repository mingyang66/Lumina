"""密匣窗口：银行资产的记录与统计。

从剪贴板主面板顶栏「隐域 → 密匣」打开。**独立于剪贴板历史的另一类数据**：
这里记的是结构化的银行存单，不是剪贴内容，两者在数据库里也是不同的表。

## 版式：银行主行 + 存单子行

用户明确定的方向（原话）：

    不用设置50万上限，每家银行有一个主行，存单都在其下面，每一行可以拖拽排序

- **银行主行**（独立蓝灰底、加粗）只放聚合值：笔数、本金合计、资金加权年化、
  最近到期、利息合计。一句话就是「这家银行我一共有多少钱、什么时候回来」。
- **存单子行**（银行内交替底色、缩进一级）放明细：名称、存期月数、本金、年化、到期 +
  倒计时、利息。主行是结论，子行是依据。
- 主行可折叠（点左侧箭头）。

配色采用蓝灰底与交互蓝；状态色独立为已到期赭红、临近到期金棕、正常绿。
「已到期」仍是唯一铺整行底色的状态，浅深主题共用同一套语义令牌。

**刻意不做 50 万存款保险额度对照**（第一版设计稿做了，被用户否掉）。聚合只
按银行分组、只出事实值，不加「距上限还有多少」这类判断。

## 本轮范围：静态版

用户选了「先只做静态版」——增删改与汇总都做，**拖拽排序留到下一轮**。
数据库里的 ``sort_index`` 列已经建好并写入，所以接拖拽时只需改
「重排 sort_index + 按它排序」，不必再动表结构。渲染顺序当前按 ``sort_index, id``，
全为 0 时即插入顺序。

## 为什么不用基类的 ``_build`` 底栏

密匣的底栏与「JSON / JWT / 日期转换」需要的完全不同（那三个是「输入→结果」，
这里是「一张有增删改的清单」）。所以覆写 ``_build_panes``，并用
``has_footer = False`` 关掉基类底栏，自己画一条含「新增存单 / 新增银行」的
操作条——照搬基类底栏会得到一条「清空 / 复制结果」的按钮组，那在这里没有意义。

**仍然复用基类的外壳**：无边框 + 自绘描边 + 任务栏归属 + 八向缩放 + ESC 最小化，
这些在 ``_ToolWindow`` 里已经调好了（含一串 Windows 特有的坑），重写一遍必踩。
## 列宽预算（改尺寸或加列前必读）

窗口宽度的每一像素都是有主的，改任何一个常量都要重算这条链：

```
窗口宽 = 列宽合计 + Label 装饰 + 单元格内边距 + 卡片损耗
660    = 510      + 36         + 72           + 42（含 14px 余量）
                 ↑            ↑              ↑
                 6px × 6 列    12px × 6 列     外壳留白 16 + 描边 2 + 滚动条 10 + 余量
```

- **列宽合计 510** = 150 + 64 + 38 + 156 + 64 + 38（``COL_*``，按实测最坏文案定）。
  2026-11-05 由 492 提到 510：到期列的文案从「逾期 966 天」改成「已到期 966 天」，
  实测宽 12px（142 → 154），这一列因而从 144 提到 156。
- **Label 装饰 36** = ``LABEL_CHROME``(6) × 6 列：``tk.Label`` 默认
  ``padx=1`` + ``borderwidth=2``，需求宽恒为「文字 + 6」
- **单元格内边距 72** = ``CELL_PADX``(6) × 2 × 6 列
- **卡片损耗 28** 固定：``CONTENT_INSET``×2(16) + 卡片描边(2) + ``_MiniScroll.LANE``(10)

**改任何一列的文案或宽度都要重算这条链**，并把窗口宽抬到「列宽合计 + 108」
以上（36 + 72 = 108 是装饰与内边距，再留 28 给卡片损耗）。少算的那几个像素
不会报错，只会让「删除」列被顶出可视区——**能渲染、点不到**。

**表头与数据区必须用同一个 ``_configure_columns``**，且 ``minsize`` 要含
装饰与内边距——两边的文字不同（表头「本金」24px vs 数据「400,000」45px），
只按裸 ``minsize`` 配置会各算各的，实测逐列错位 4~10px。

**内容不会撑开列**：``_fit_text`` 按列宽显式截断（超长加省略号，悬停看全文）。
不截的话长银行名会把内嵌区从 600px 撑到 664px，**「删除」列被顶出画布**。

"""

import datetime as dt
import math
import traceback
from typing import ClassVar

from . import vault_tools
from .tool_window import CONTENT_INSET, GAP, _MiniScroll, _ToolWindow

# 顶部指标卡的高度与卡间距。
#
# **62 是不够的，实测三行要 70**：标题 9pt（行高 17）+ 数字 15pt（行高 27）
# + 副行 8pt（行高 16），再加上 ``pady=(9, 0)`` 与数字上方 1px，合计 70。
# 卡高写 62 时，8pt 的副行会被压成 0 高——**不报错、只是那行字整个不见**，
# 于是「本金 / 资金加权年化 / 本息」这些说明全部凭空消失。取 74 留 4px 余量。
METRIC_HEIGHT = 74
METRIC_GAP = 10

# 两级行的行高。主行略高于子行：它承载的信息更多（笔数 + 聚合值），
# 用高一点的行高把两组值分开，扫视时不会串行。
BANK_ROW_HEIGHT = 38
CERT_ROW_HEIGHT = 34

# 单元格左右内边距。6 列合计占 72px。
CELL_PADX = 6

# **``tk.Label`` 的固有装饰宽：默认 ``padx=1`` + ``borderwidth=2``，实测
# 每个 Label 的需求宽恒为「文字像素宽 + 6」**（九种文字测下来差值全是 6）。
# 这个 6 必须并进列的 ``minsize``，否则表头（「本金」24px）与数据
# （「400,000」45px）算出不同的列宽，两张表逐列错位——误差恰好就是这 6px。
LABEL_CHROME = 6

# 表格列宽（像素）。**这里写的是「文字可用宽度」，不含内边距**——
# 真正配给 grid 的 ``minsize`` 是 ``列宽 + 2 × CELL_PADX``（见
# ``_configure_columns``）。之所以不含 padx：``_fit_text`` 要按
# 「能放几个字」截断，那个数就是这里的值，不必每处都记得再减一次。
#
# **这些是按实测最坏值反推的**（``Microsoft YaHei UI`` 9pt 量字宽），
# 每列都取「最坏文案像素宽 + 2px 余量」，括号里是实测依据。
COL_BANK = 150        # 用户自填名，长度不可控，**允许截断**（见下）
COL_AMOUNT = 64       # 「12,345,678」62 ✓
COL_RATE = 38         # 「3.55%」35 ✓
# **「已到期」比「逾期」长 12px**（154 vs 142，实测 ``Microsoft YaHei UI`` 9pt），
# 所以这一列从 144 提到 156。文案改动会悄悄把列宽预算撑破——这正是
# 「临界列不得截断」那条测试要守的东西，改文案时必须一并重算。
COL_DUE = 156         # 「2026-08-01 · 已到期 966 天」154 ✓（三位数天数）
COL_INTEREST = 64     # 同本金列
COL_ACTION = 38       # 「删银行」36 ✓

# **分配原则：机器算出来的值给足，用户自填的名字允许截断。**
# 倒计时是「什么时候到期」这个唯一的行动信号，已到期时更宽
# （「2026-08-01 · 已到期 966 天」实测 154px），必须完整可见；金额同理。
# 银行名/存单名是用户自己打的，长度不可控（实测「中国工商银行股份有限公司」
# 单是名字就 198px），让它承担截断——窗口加宽即可看全（额外空间全归它），
# 鼠标悬停有原生提示显示全文。
# **截断是显式做的**（见 ``_fit_text``），不是让 grid 硬挤：硬挤时列会被内容
# 撑开，实测内嵌 Frame 需 664px 而画布只有 592px，**「删除」列被顶到画布外，
# 用户根本点不到**。


class VaultWindow(_ToolWindow):
    """密匣：银行资产统计。

    顶部四张指标卡（本金 / 预计利息 / 90 天内到期 / 已到期），
    下面按银行分组的两级清单。
    """

    title = "密匣 · 银行资产"
    placeholder = "点右上角「新增存单」开始记录"

    # **窄版**（用户要求「宽度缩短大概一半，高度扩充点」）：1180 → 640，
    # 高度 760 → 900。窄窗把省下来的横向空间换成了纵向——指标卡因此可以
    # 改成 2×2（见 ``_build_metrics``），清单也能一屏多显示几行。
    #
    # **660 是算出来的，不是随手取的**（整套预算见文件头「列宽预算」）：
    # 列宽 510 + Label 装饰 36 + 单元格内边距 72 = **内嵌区需求 618**，
    # 加上窗口到画布的固定损耗 28（外壳留白 16 + 卡片描边 2 + 滚动条通道 10）
    # 得 646。取 660 留 14px 余量——这部分全给名字列（``weight=1``）。
    # **低于 646 会让内嵌区超出画布，「删除 / 删银行」列被顶到可视区外，
    # 用户点不到**（实测过）。
    #
    # 640 → 660 是 2026-11-05 随「已到期」文案变长一起调的：文案宽了 12px，
    # 窗口不跟着宽，到期列就会去挤名字列或直接溢出。
    width = 660
    height = 900
    min_width = 660
    min_height = 560

    # 自己画操作条，不要基类那条「清空 / 复制结果」底栏（见模块文档）。
    has_footer = False

    # ---------- 设计令牌 ----------
    # 交互蓝与状态赭红分离；装饰色条不承担正文可读性。
    # 银行蓝灰主行、存单交替中性底；已到期（含当天）覆盖条纹，独占状态底色。
    LIGHT_PALETTE: ClassVar[dict] = {
        "surface": "#F4F6F8",
        "card": "#FFFFFF",
        "border": "#DCE2E8",
        "row_border": "#EDF1F5",
        "head_bg": "#EDF2F6",
        "title": "#1E293B",
        "muted": "#64748B",
        "faint": "#64748B",
        "matured_bg": "#FAEFED",
        "matured_fg": "#A34E42",
        "urgent_fg": "#805A24",
        "ok_fg": "#237451",
        "button": "#244A68",
        "button_fg": "#FFFFFF",
        "button_hot": "#193A54",
        "rail_bg": "#F4F6F8",
        "rail_fg": "#1E293B",
        "metric_bg": "#FFFFFF",
        "metric_border": "#DCE2E8",
        "strip_principal": "#244A68",
        "strip_interest": "#9AAEC2",
        "strip_soon": "#B89A63",
        "strip_matured": "#A34E42",
        "row_bank_bg": "#E3EBF3",
        "row_alt": "#F5F8FB",
        "link": "#244A68",
    }
    DARK_PALETTE: ClassVar[dict] = {
        "surface": "#111923",
        "card": "#1B2735",
        "border": "#334155",
        "row_border": "#263548",
        "head_bg": "#223143",
        "title": "#E6EDF5",
        "muted": "#9AAEC2",
        "faint": "#9AAEC2",
        "matured_bg": "#35272A",
        "matured_fg": "#E4A094",
        "urgent_fg": "#D6B77F",
        "ok_fg": "#75C9AB",
        "button": "#75B5E8",
        "button_fg": "#111923",
        "button_hot": "#9ACAF0",
        "rail_bg": "#111923",
        "rail_fg": "#E6EDF5",
        "metric_bg": "#1B2735",
        "metric_border": "#334155",
        "strip_principal": "#75B5E8",
        "strip_interest": "#59728B",
        "strip_soon": "#C8AC78",
        "strip_matured": "#E4A094",
        "row_bank_bg": "#293D53",
        "row_alt": "#202E3E",
        "link": "#75B5E8",
    }

    @property
    def P(self):
        """当前主题下的一套色。用属性而不是在 ``__init__`` 里定一次：
        ``self.dark`` 由基类在 ``__init__`` 里赋值，而这个属性可能在赋值
        完成前的某个钩子里被读到（与 ``DateTimeWindow.P`` 同理）。"""
        return self.DARK_PALETTE if self.dark else self.LIGHT_PALETTE

    # ---------- 整窗外壳 ----------
    def _build_shell(self):
        # 在首次创建控件前设为深色，避免浅色首帧或局部颜色残留。
        # 不改 panel._theme，其他窗口仍沿用各自主题。
        self.dark = True
        super()._build_shell()

    def _surface_bg(self):
        """窗口、外壳、正文容器与缩放热区统一使用 surface。"""
        return self.P["surface"]

    def _edge(self):
        """自绘外壳描边与卡片边框共用 border。"""
        return self.P["border"]

    def _edge_enter(self, name):
        """缩放热区悬停仍用 surface，避免基类系统灰露出杂色。"""
        if self._closed:
            return
        zone = self._resize_edges.get(name)
        if zone is not None:
            zone.configure(bg=self._surface_bg())

    def _rail_theme(self):
        """顶栏融入外壳；几何、拖动绑定与控制按钮顺序仍复用基类。"""
        P = self.P
        return {
            "bg": P["rail_bg"],
            "title_fg": P["rail_fg"],
            "mark_idle": P["muted"],
            "mark_accent": P["link"],
            "divider": P["rail_bg"],
            "hover_bg": P["head_bg"],
            "hover_fg": P["title"],
            "close_hover_bg": P["matured_bg"],
            "close_hover_fg": P["matured_fg"],
        }

    def _accent(self):
        """链接、滚动条悬停与输入光标统一使用本窗交互蓝。"""
        return self.P["link"]

    def __init__(self, panel):
        # 构造期就要有确定取值的属性。**这一点不是洁癖**：基类在 ``__init__``
        # 末尾排了一个 30ms 的 ``_refresh`` 定时器，且 ``_build`` → ``_build_panes``
        # 会立刻调本类的建布局代码；任何一个属性在那一刻缺失，报错都发生在
        # Tk 回调里，表现为「窗口没出来」而不是一行能定位的堆栈。
        self._collapsed = set()          # 折叠起来的银行 id
        self._groups = []                # 渲染用的分组数据
        self._data = vault_tools.summarize([], dt.date.today())
        self._metric_widgets = {}        # 四张指标卡的控件（与 _data 分开！）
        self._load_error = ""
        self._today = None               # 测试注入的基准日
        self._last_deposit_bank_id = None  # 仅记住本窗口成功写入的银行
        super().__init__(panel)

    def _resize_corner_size(self):
        # 12px 方块会侵入从 8px 开始的顶栏，各盖掉一个 4×4px 角。
        return CONTENT_INSET

    def _build_rail(self):
        super()._build_rail()
        rail = self.title_label.master
        self._theme_button = self.tk.Label(
            rail, text="切换浅色" if self.dark else "切换深色",
            bg=self.P["rail_bg"], fg=self.P["rail_fg"],
            font=(self.panel.FONT_FAMILY, 9), padx=10, cursor="hand2")
        self._theme_button.pack(side="right", padx=(4, 8))
        self._theme_button.bind("<Button-1>", lambda _e: self._toggle_theme())
        self._theme_button.bind("<Enter>", lambda _e: self._theme_button.configure(
            bg=self.P["head_bg"], fg=self.P["title"]))
        self._theme_button.bind("<Leave>", lambda _e: self._theme_button.configure(
            bg=self.P["rail_bg"], fg=self.P["rail_fg"]))

    def _toggle_theme(self):
        """仅重建本窗控件，不换 HWND、不修改系统主题、不改数据库。"""
        self.dark = not self.dark
        for child in self.shell.winfo_children():
            child.destroy()
        self._metric_widgets = {}
        self._font_cache = {}
        self.win.configure(bg=self._surface_bg())
        self.shell.configure(bg=self._surface_bg())
        self._paint_shell(self.win.winfo_width(), self.win.winfo_height())
        self._build_rail()
        self._make_resize_edges()
        self.win.unbind("<MouseWheel>")
        self._build()
        self._render()

    # ---------- 数据 ----------
    @property
    def db(self):
        """数据库句柄。走 panel.ui.db，与其他模块一致。"""
        return self.ui.db

    def _today_date(self):
        """基准日。

        做成方法是为了测**可以注入**（``self._today``），也让「今天」只在一处
        被取——散落各处的 ``date.today()`` 会在跨零点的那次刷新里算出两个不同的
        「今天」，逾期天数因此差一天。
        """
        return self._today or dt.date.today()

    def _load(self):
        """从库里读出银行与存单，聚合成渲染用的分组。

        任何异常都不往上抛：这个窗口是用户主动打开的功能窗口，读库失败应该
        在窗口里说清「读不到」并让用户能重试，而不是把异常抛进 Tk 的回调里
        变成一行控制台堆栈。
        """
        try:
            banks = [dict(row) for row in self.db.list_vault_banks()]
            deposits = [dict(row) for row in self.db.list_vault_deposits()]
        except Exception:
            traceback.print_exc()
            self._groups = []
            self._data = vault_tools.summarize([], self._today_date())
            self._load_error = "读取本地数据失败，详情见控制台"
            return
        self._load_error = ""
        self._data = vault_tools.summarize(deposits, self._today_date())
        self._groups = vault_tools.group_by_bank(
            banks, deposits, self._today_date())

    # ---------- 版式 ----------
    def _build_panes(self, body):
        """指标卡在上（固定高），清单在下（吃掉剩余高度并滚动）。

        **清单区必须可滚动**：银行和存单都是用户自己加的，条数没有上限。
        用一个 ``Canvas`` + 内嵌 ``Frame`` 实现——Tk 没有原生滚动容器。
        """
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=0)      # 指标卡
        body.rowconfigure(1, weight=0)      # 操作条
        body.rowconfigure(2, weight=1)      # 清单

        self._build_metrics(body).grid(row=0, column=0, sticky="ew")
        self._build_actions(body).grid(row=1, column=0, sticky="ew",
                                       pady=(GAP, 0))
        self._build_table(body).grid(row=2, column=0, sticky="nsew",
                                     pady=(METRIC_GAP, 0))

    def _build_metrics(self, parent):
        """四张指标卡，**2×2 网格**。

        原来是**一行四张**（1180 宽的窗口里放得下）。窗口缩窄到 600 之后，
        一行四张每张只剩约 130px，而「¥12,345,678」这种七位数就要 120px
        （实测，``Microsoft YaHei UI`` 15pt bold），必然截断；副行那句
        「资金加权年化 2.92%」更是要 100px，挤到换行。
        改成 2×2 既解决宽度，也正好用掉抬高的那部分高度。
        """
        tk = self.tk
        deck = tk.Frame(parent, bg=self._surface_bg())
        deck.rowconfigure(0, weight=0)
        deck.rowconfigure(1, weight=0)

        # **主值给「本金」**（用户要求把「本息合计」改成本金）。这是四张卡里
        # 唯一说明「一共有多少钱在里面」的绝对值，用户的默认关注点就是它。
        # 本息（到期能拿回多少）退到副行——它是本金加一个估算的利息，而利息
        # 是按单利折算的预估值，拿它当主值会让用户以为那是合同上的确定数字。
        specs = (
            ("principal", "本金", "", "strip_principal"),
            ("interest", "预计利息", "", "strip_interest"),
            ("soon", "90 天内到期", "warn", "strip_soon"),
            ("matured", "已到期", "danger", "strip_matured"),
        )
        for index, (key, label, kind, strip_key) in enumerate(specs):
            row, column = divmod(index, 2)
            deck.columnconfigure(column, weight=1, uniform="metric")
            # **已到期那张卡的底色再单独深一档**（淡红 ``matured_bg``）：四张里
            # 只有它承载「需要处理」的语义，与清单里那行已到期存单的底色同色，
            # 上下两处因此能对上。其余三张统一用 ``metric_bg``。
            card_bg = (self.P["matured_bg"] if kind == "danger"
                       else self.P["metric_bg"])
            # 卡片外框：底色 + 1px 统一描边。
            card = tk.Frame(deck, bg=card_bg, height=METRIC_HEIGHT,
                            highlightthickness=1,
                            highlightbackground=self.P["metric_border"])
            card.grid(row=row, column=column, sticky="nsew",
                      padx=(0 if column == 0 else METRIC_GAP // 2,
                            METRIC_GAP // 2 if column == 0 else 0),
                      pady=(0 if row == 0 else METRIC_GAP // 2,
                            METRIC_GAP // 2 if row == 0 else 0))
            # **关掉尺寸传播**：卡片高度要固定（四张等宽等高的卡），而里面的
            # 文字高度是自适应的。不关的话卡片会被文字撑高，四张卡的高度取决于
            # 各自文字的行数，看起来参差不齐。
            card.pack_propagate(False)
            card.grid_propagate(False)

            # 左侧 2px 色条区分四张卡；只有已到期卡另用状态底色。
            # 用 ``place`` 钉在左侧：``pack`` 会与文字抢水平空间，而色条必须
            # 贴住卡片左缘、高度铺满（含 1px 描边内的一圈）。
            strip = tk.Frame(card, bg=self.P[strip_key], width=2)
            strip.place(x=1, y=1, relheight=1.0, height=-2)

            caption = self._caption(card, label, self.P["muted"])
            caption.pack(anchor="w", padx=13, pady=(9, 0))
            value = tk.Label(card, text="—", bg=card_bg,
                             fg=self._metric_color(kind),
                             font=(self.panel.FONT_FAMILY, 15, "bold"),
                             anchor="w")
            value.pack(anchor="w", padx=13, pady=(1, 0))
            sub = self._caption(card, "", self.P["faint"], size=8)
            sub.pack(anchor="w", padx=13)
            self._metric_widgets[key] = {"card": card, "value": value,
                                         "sub": sub, "kind": kind,
                                         "caption": caption, "strip": strip}
        # 固定像素 74 在高 DPI 下不够。保留统一卡高，但按真实字体需求计算。
        required = max(
            sum(item[name].winfo_reqheight() for name in ("caption", "value", "sub"))
            + 18 for item in self._metric_widgets.values())
        for item in self._metric_widgets.values():
            item["card"].configure(height=max(METRIC_HEIGHT, required))
        return deck

    def _metric_color(self, kind):
        """本金与利息主值用 title；临近用金棕，已到期用赭红。"""
        if kind == "danger":
            return self.P["matured_fg"]
        if kind == "warn":
            return self.P["urgent_fg"]
        return self.P["title"]

    def _build_actions(self, parent):
        """操作条：左侧一句说明，右侧「新增银行」「新增存单」。

        「新增存单」是主按钮（深色实心）——它是这个窗口最常用的动作。
        """
        tk = self.tk
        bar = tk.Frame(parent, bg=self._surface_bg())
        self._hint = self._caption(bar, "", self.P["muted"])
        self._text_button(bar, "新增银行", self._add_bank)
        self._primary_button(bar, "新增存单", self._add_deposit)
        self._hint.configure(width=1)
        self._hint.pack(side="left", fill="x", expand=True)
        return bar

    def _caption(self, parent, text, color, size=9):
        return self.tk.Label(parent, text=text, bg=parent["bg"], fg=color,
                             font=(self.panel.FONT_FAMILY, size), anchor="w")

    def _text_button(self, parent, label, command):
        """次按钮使用交互蓝描边与文字，悬停填 head_bg，不借用状态红底。"""
        P = self.P
        box = self.tk.Frame(parent, bg=P["link"])
        btn = self.tk.Button(
            box, text=label, command=command, bd=0, relief="flat",
            bg=self._surface_bg(), fg=P["link"],
            activebackground=P["head_bg"], activeforeground=P["link"],
            font=(self.panel.FONT_FAMILY, 9), padx=12, pady=4,
            cursor="hand2", highlightthickness=0)
        btn.pack(padx=1, pady=1)

        def enter(_event):
            box.configure(bg=P["button"])
            btn.configure(bg=P["head_bg"], fg=P["button"])

        def leave(_event):
            box.configure(bg=P["link"])
            btn.configure(bg=self._surface_bg(), fg=P["link"])

        btn.bind("<Enter>", enter)
        btn.bind("<Leave>", leave)
        box.pack(side="right", padx=(8, 0))
        return box

    def _primary_button(self, parent, label, command):
        """主按钮使用交互蓝实心块，文字按浅深主题配对。

        用 Frame + Label 而不是 tk.Button，避免 Windows 系统主题在按下或
        获得焦点时覆盖自定义底色（见基类 ``_make_action_button``）。
        """
        C = self.P
        box = self.tk.Frame(parent, bg=C["button"], cursor="hand2")
        label_widget = self.tk.Label(
            box, text=label, bg=C["button"], fg=C["button_fg"],
            font=(self.panel.FONT_FAMILY, 9), padx=14, pady=4)
        label_widget.pack()

        def press(_event):
            box.configure(bg=C["button_hot"])
            label_widget.configure(bg=C["button_hot"])
            command()
            return "break"

        def release(_event):
            box.configure(bg=C["button"])
            label_widget.configure(bg=C["button"])

        for widget in (box, label_widget):
            widget.bind("<ButtonPress-1>", press)
            widget.bind("<ButtonRelease-1>", release)
        box.pack(side="right", padx=(8, 0))
        box.label_widget = label_widget
        return box

    # ---------- 清单 ----------
    def _build_table(self, parent):
        """清单：表头 + 可滚动的内容区。

        滚动用 ``Canvas`` + 内嵌 ``Frame``（Tk 没有原生滚动容器）。
        ``Canvas`` 的 ``yscrollcommand`` 接到自绘的 ``_MiniScroll``——
        原生 ``Scrollbar`` 在 Windows 下 ``bg`` 不生效，深色主题里会露白槽。
        """
        tk, T = self.tk, self.T
        card = tk.Frame(parent, bg=self.P["card"], highlightthickness=1,
                        highlightbackground=self.P["border"])
        # 行 0 = 表头（固定 30），行 1 = 数据区（吃掉剩余），行 2 = 口径脚注。
        # **列 0 = 内容，列 1 = 滚动条通道**：表头与数据区**拆到同一套 grid
        # 列里**，两者的列宽、右边界就天然一致，不必再靠「表头减掉 10px」
        # 这类手工对齐（那条路走过，``relwidth`` 与 ``place`` 混用时窗口缩放
        # 会出现表头比数据区宽一点、逐列错位几像素的问题）。
        card.rowconfigure(1, weight=1)
        card.columnconfigure(0, weight=1)
        card.columnconfigure(1, minsize=_MiniScroll.LANE)

        columns = (("bank", "银行 / 存单", "w"), ("principal", "本金", "e"),
                   ("rate", "年化", "e"), ("due", "到期 / 倒计时", "e"),
                   ("interest", "预计利息", "e"), ("action", "", "e"))
        widths = {"bank": COL_BANK, "principal": COL_AMOUNT, "rate": COL_RATE,
                  "due": COL_DUE, "interest": COL_INTEREST,
                  "action": COL_ACTION}
        head = tk.Frame(card, bg=self.P["head_bg"], height=30)
        head.grid(row=0, column=0, sticky="ew")
        # 允许表头按字体行高撑高，30px 在高 DPI 下会裁掉文字。
        self._columns = columns
        self._widths = widths
        for index, (_key, text, anchor) in enumerate(columns):
            cell = self._caption(head, text, self.P["muted"])
            cell.configure(anchor=anchor)
            cell.grid(row=0, column=index, sticky="nsew",
                      padx=CELL_PADX, pady=7)
        # 列宽与数据区**共用同一个配置方法**，两处绝不分叉。
        self._configure_columns(head)
        self._table_head = head

        # 数据区**跨越卡片的列 0 与列 1**（内容 + 通道）：它自己内部用
        # ``grid`` 摆画布与滚动条，外面的两列只是把「表头的右边界」和
        # 「数据区的右边界」框到同一个宽度上。让她只占列 0 会让滚动条
        # 落到卡片列 1、而画布窄掉一个通道宽，两者又对不上。
        lane = tk.Frame(card, bg=self.P["card"])
        lane.grid(row=1, column=0, columnspan=2, sticky="nsew")
        lane.rowconfigure(0, weight=1)
        lane.columnconfigure(0, weight=1)
        canvas = tk.Canvas(lane, bg=self.P["card"], highlightthickness=0,
                           bd=0)
        canvas.grid(row=0, column=0, sticky="nsew")
        # 10px 轨道是独立 Canvas，不继承父容器色；轨道与滑块必须显式
        # 使用 P 的 card / faint，深色下尤其不能回退到基类主题。
        scroll = _MiniScroll(tk, lane, "vertical", canvas.yview, T,
                             self._accent(), gutter=self.P["card"],
                             thumb=self.P["faint"])
        canvas.configure(yscrollcommand=scroll.set)
        scroll.canvas.grid(row=0, column=1, sticky="ns")
        lane.columnconfigure(1, minsize=_MiniScroll.LANE)

        inner = tk.Frame(canvas, bg=self.P["card"])
        window = canvas.create_window((0, 0), window=inner, anchor="nw")
        # 内嵌 Frame 的宽度跟着 Canvas 走（否则它只按内容宽度，右侧会留空）。
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(window, width=e.width))
        inner.bind("<Configure>", lambda _e: canvas.configure(
            scrollregion=canvas.bbox("all")))
        # 滚轮绑在**本窗口**上（toplevel 级），不绑 ``bind_all``。
        # ``bind_all`` 是全局的：会把主面板和其他工具窗口的滚轮一起劫持掉，
        # 实测那种「在密匣里滚，剪贴板列表跟着动」的现象就是这么来的。
        # Tk 的事件沿 bindtags 冒泡到 toplevel，所以指针落在清单的任意
        # 子控件上都能收到——不必逐个 Label 绑。
        self.win.bind("<MouseWheel>", self._on_wheel, add="+")

        # 口径脚注：钉在卡片底部，不随清单滚动，**横跨两列**（内容 + 通道），
        # 否则会被滚动条那 10px 挤得右边多出一条空白。
        # 从操作条挪过来——窗口缩窄后那里放不下（见 ``_render_hint``）。
        footnote = tk.Frame(card, bg=self.P["head_bg"])
        footnote.grid(row=2, column=0, columnspan=2, sticky="ew")
        self._footnote = self._caption(footnote, "", self.P["faint"], size=8)
        self._footnote.pack(fill="x", padx=CELL_PADX + 4, pady=5)
        self._footnote.configure(justify="left")
        footnote.bind("<Configure>", lambda e: self._footnote.configure(
            wraplength=max(1, e.width - 2 * (CELL_PADX + 4))))

        self._table = inner
        self._canvas = canvas
        self._scroll = scroll
        return card

    def _on_wheel(self, event):
        """滚轮滚动清单。

        ``event.delta`` 在 Windows 上是 ±120 的倍数，``//120`` 消掉它；
        不用 ``event.num``（那是 X11 的 Button-4/5，Windows 上恒为 0）。
        """
        # 只在指针确实落在清单区时滚：本窗口别处的滚轮没有实际用途，
        # 但把它限制清楚，将来往这个窗口加别的可滚区域时不会互相抢。
        try:
            self._canvas.yview_scroll(-1 * (event.delta // 120), "units")
        except Exception:
            pass

    def _fit_text(self, text, key, *, bold=False):
        """把文字按列宽截断，末尾加省略号。

        **为什么必须显式截断，而不是让 grid 自己挤**：``grid`` 的
        ``minsize`` 只是**下限**，内容比它宽时列会被撑大。实测一列长银行名
        （「中国工商银行股份有限公司」198px）就把内嵌 Frame 从 560px 撑到
        664px，而画布只有 592px——**「删除」列被顶到画布外，用户根本点不到**，
        而且不会报任何错。

        截断只在「用户自填的名字」上真实发生（``bank`` 列）。数字、日期、
        金额这些列已经按实测最坏值给了足够宽度，正常不会触发；留着这道
        保险是为了防「金额涨到九位数」「已到期天数涨到四位数」这类越界。

        字体度量走 ``tkfont.Font`` 并**缓存**：``measure()`` 每次都要问 Tk，
        而每个单元格每帧都算一次会明显拖慢重建（清单整块重建见 ``_render``）。
        """
        from tkinter import font as tkfont

        available = self._widths[key]
        cache = getattr(self, "_font_cache", None)
        if cache is None:
            cache = self._font_cache = {}
        cache_key = "bold" if bold else "small"
        font = cache.get(cache_key)
        if font is None:
            font = cache[cache_key] = tkfont.Font(
                family=self.panel.FONT_FAMILY, size=9,
                weight="bold" if bold else "normal")
        if font.measure(text) <= available:
            return text
        ellipsis = "…"
        room = available - font.measure(ellipsis)
        if room <= 0:
            return ellipsis
        # 逐字符退，直到放得下。用二分可以更快，但一列最多几十个字符，
        # 线性退更简单、也不会因为字宽不均匀而算错边界。
        cut = len(text)
        while cut > 0 and font.measure(text[:cut]) > room:
            cut -= 1
        return text[:cut] + ellipsis

    def _grid_row(self, parent, row_index, values, *, bg, height,
                  urgent=None, bold=False):
        """摆一行。``values`` 按 ``self._columns`` 顺序给出（第 1 列是文本）。

        返回这一行的控件列表，供后续改内容或绑事件。**行高固定**：
        主行与子行的高度差用来表达层级，如果按内容自适应，同一档行的高度
        会因为文字是否折行而参差，层级感就没了。

        **``self._columns`` 里那个字段叫 ``title`` 不叫 ``text``**：它曾经叫
        ``text``，于是和本方法里代表「单元格内容」的 ``text`` 撞名，循环解包
        ``(key, text, anchor)`` 直接把每行文字**全渲染成了列标题**——界面
        看起来像表头重复了很多遍，而数据一个都没显示。名字分开之后这类
        「解包把入参覆盖掉」的错误不会再悄悄发生。

        **文字按列宽截断**（``_fit_text``）：不截的话长名会把列撑开，
        把右边的操作列顶出可视区。展开被截断的完整文字走 ``title`` 悬停
        提示（原生 tooltip），鼠标停一下就能看全。
        """
        if len(values) != len(self._columns):
            raise ValueError(
                f"行数据列数不匹配：给了 {len(values)} 列，"
                f"表格有 {len(self._columns)} 列")
        anchor = {key: a for key, _title, a in self._columns}
        # 背景跨满共同 grid 的整行，填补 Label 外侧 padding；不改列宽预算。
        background = self.tk.Frame(parent, bg=bg, bd=0, highlightthickness=0)
        background.grid(row=row_index, column=0, columnspan=len(self._columns),
                        sticky="nsew")
        background.lower()
        cells = []
        for index, (key, _title, _a) in enumerate(self._columns):
            fg = self.P["title"]
            # **三档各自上色，判据落在具体档位上。**
            # 这里曾是 ``if key == "due" and urgent:`` —— ``"normal"`` 是
            # 非空字符串、为真，于是**正常状态的到期日也被涂成了警示色**，
            # 三档状态实际只有两种颜色，临近与正常的信号因此混淆。
            # 判据必须落在具体档位上，不能只判「有没有传值」。
            if key == "due" and urgent in ("matured", "urgent"):
                fg = (self.P["matured_fg"] if urgent == "matured"
                      else self.P["urgent_fg"])
            elif key == "due" and urgent == "normal":
                # **正常档第一次有了正向的颜色**（绿）。原方案正常档不上色，
                # 三档里有一档没有信号；本轮重渲染配色时把这一档补齐了。
                fg = self.P["ok_fg"]
            raw = values[index]
            cell_bold = bold and key == "bank"
            label = self.tk.Label(parent, text=self._fit_text(raw, key, bold=cell_bold),
                                  bg=bg, fg=fg,
                                  font=(self.panel.FONT_FAMILY, 9,
                                        "bold" if cell_bold else "normal"),
                                  anchor=anchor[key])
            if label.cget("text") != raw:
                _attach_hint(label, raw)
            label.grid(row=row_index, column=index, sticky="nsew",
                       padx=CELL_PADX, pady=0)
            cells.append(label)
        parent.rowconfigure(row_index, minsize=height)
        return cells

    def _clear_table(self):
        for child in self._table.winfo_children():
            child.destroy()
        # 列宽必须与表头**用同一套规则**配置，否则宽窗下两者分配方式不同、
        # 表头与数据逐列错位（表头给 bank 列 ``weight=1``，数据区若全为 0，
        # 多出来的宽度在两边就分给了不同的列）。列宽本身就是「列宽」，
        # 两边一致是唯一的正确做法。
        self._configure_columns(self._table)

    def _configure_columns(self, parent):
        """给一个 grid 容器配置 6 列的 ``minsize`` 与 ``weight``。

        ``weight`` 只给第一列（银行/存单）：窗口拉宽时**余量全归名字列**，
        数字列保持固定宽，右对齐的数字才不会左右漂。名字是用户自填的、
        长度不可控，多给宽度最有用。

        **``minsize`` 必须算上 Label 的固有装饰与单元格内边距**：``grid``
        里一列的最终宽度是 ``max(minsize, 单元格需求宽)``，而单元格需求宽是
        「文字 + ``LABEL_CHROME`` + 2×``CELL_PADX``」。表头与数据两边的**文字
        不同**（表头「本金」24px，数据「400,000」45px），不减清楚就会算出
        不同的列宽、两张表逐列错位（实测表头 c0=190 而数据 c0=183）。
        把装饰与内边距都并进 ``minsize``，两边就落到同一个下限上，列宽恒等。
        """
        for index, (key, _title, _a) in enumerate(self._columns):
            parent.columnconfigure(
                index, minsize=self._widths[key] + LABEL_CHROME + 2 * CELL_PADX,
                weight=1 if key == "bank" else 0)

    def _render(self):
        """按 ``self._groups`` 重建整张清单。

        整块重建而不是增量更新：条数不大（个人存款记录几十条量级），
        重建换来的是「界面永远是数据的函数」——不会出现某处改了数据但忘了
        刷某个控件造成的界面与数据不一致，那类 bug 最难查。
        """
        self._clear_table()
        self._render_metrics()
        self._render_footnote()
        if self._load_error:
            self._render_empty(self._load_error)
            return
        if not self._groups:
            self._render_empty("还没有记录。点右上角「新增银行」建一家，"
                               "再「新增存单」往里加。")
            return
        row = 0
        for group in self._groups:
            row = self._render_group(group, row)
        self._render_hint()

    def _render_empty(self, text):
        label = self.tk.Label(self._table, text=text, bg=self.P["card"],
                              fg=self.P["muted"],
                              font=(self.panel.FONT_FAMILY, 10),
                              anchor="center", justify="center")
        label.grid(row=0, column=0, columnspan=len(self._columns),
                   sticky="nsew", pady=44)
        self._table.rowconfigure(0, minsize=90)

    def _render_group(self, group, row):
        """一家银行：主行 + （未折叠时）它的存单子行。

        **没有存款的银行也渲染**（``urgent`` 为 ``None``，到期列显示「暂无存单」）：
        用户刚建完银行就会看到它，如果这时候因为「没有子行」而不显示，用户会
        以为建失败了，接着再建一次，得到重复记录。
        """
        bank = group["bank"]
        bank_id = bank["id"]
        urgent = group["urgent"]
        state = None
        due_text = "暂无存单"
        if urgent is not None:
            days = vault_tools.days_left(urgent["end_date"], self._today_date())
            state, due_text = vault_tools.due_state(days)
            due_text = f"{urgent['end_date']} · {due_text}"
        collapsed = bank_id in self._collapsed
        # **孤儿分组（bank_id 为 None）不给折叠箭头**：它的 id 是 None，
        # 点上去只会落进 ``_toggle_collapse`` 的早退分支，用户看到的是一个
        # 有箭头但点不动的行。也不用「删银行」——那行本来就没有对应的记录。
        collapsible = bank_id is not None
        arrow = ("▸" if collapsed else "▾") if collapsible else " "
        # 主行用独立蓝灰底；底色与字重共同区分主行、表头和子行。
        head_bg = self.P["row_bank_bg"]
        cells = self._grid_row(
            self._table, row,
            [f"{arrow}  {bank['name']}    {group['count']} 笔",
             vault_tools.format_amount(group["principal"]),
             vault_tools.format_rate(group["rate"]),
             due_text,
             vault_tools.format_amount(group["interest"]),
             "删银行" if collapsible else ""],
            bg=head_bg, height=BANK_ROW_HEIGHT, urgent=state, bold=True)
        # 整行可点即折叠/展开。绑在每个 Label 上而不是行容器：
        # 用的是 grid，没有「一行」这个容器。
        if collapsible:
            for cell in cells[:1]:
                cell.configure(cursor="hand2")
                cell.bind("<Button-1>",
                          lambda _e, b=bank_id: self._toggle_collapse(b))
        for cell in cells[5:]:
            if collapsible:
                cell.configure(fg=self._accent(), cursor="hand2")
                cell.bind("<Button-1>",
                          lambda _e, b=bank_id: self._remove_bank(b))
        self._table.grid_rowconfigure(row, minsize=BANK_ROW_HEIGHT)
        row += 1

        if collapsed:
            return row
        for index, item in enumerate(group["deposits"]):
            row = self._render_deposit(item, row, index=index)
        return row

    def _render_deposit(self, item, row, *, index=0):
        """一张存单。已到期时**整行铺赭红状态底色**——这是全表唯一需要一眼看到的信息。

        **状态底色只给「已到期」一档**：临近到期仅日期着色。
        中性斑马纹按银行内存单 index 交替（含已到期的位置），不受折叠及其他银行影响。
        已到期底色优先覆盖斑马纹。
        """
        days = vault_tools.days_left(item["end_date"], self._today_date())
        state, countdown = vault_tools.due_state(days)
        bg = (self.P["matured_bg"] if state == "matured"
              else self.P["row_alt" if index % 2 else "card"])
        cells = self._grid_row(
            self._table, row,
            [f"      {item['name']}    {vault_tools.format_term(item['months'])}",
             vault_tools.format_amount(item["principal"]),
             vault_tools.format_rate(item["rate"]),
             f"{item['end_date']} · {countdown}",
             vault_tools.format_amount(vault_tools.deposit_interest(item)),
             "删除"],
            bg=bg, height=CERT_ROW_HEIGHT, urgent=state)
        for cell in cells[5:]:
            cell.configure(fg=self._accent(), cursor="hand2")
            cell.bind("<Button-1>",
                      lambda _e, i=item["id"]: self._remove_deposit(i))
        # 上边框：用一条 1px 的 Frame 占一行会把行号搞乱，改用行内的
        # 顶部描边——Tk 的 Label 没有 border 能力，所以这里靠底色与间距分隔。
        return row + 1

    def _render_metrics(self):
        """把 ``self._data`` 写进四张指标卡。

        ``self._data`` 是纯数据（``vault_tools.summarize`` 的返回值），
        ``self._metric_widgets`` 是控件。两者分开命名：混用同一份字典时，
        「某处把数据写进了控件字典」这类错误不会有任何报错，只会静默不住。
        """
        m = self._metric_widgets
        data = self._data
        m["principal"]["value"].configure(
            text=f"¥{vault_tools.format_amount(data['principal'])}")
        m["principal"]["sub"].configure(
            text=f"本息 ¥{vault_tools.format_amount(data['total'])}")
        m["interest"]["value"].configure(
            text=f"¥{vault_tools.format_amount(data['interest'])}")
        m["interest"]["sub"].configure(
            text=f"资金加权年化 {vault_tools.format_rate(data['rate'])}")
        m["soon"]["value"].configure(text=f"{data['soon_count']} 笔")
        m["soon"]["sub"].configure(
            text=f"本金 ¥{vault_tools.format_amount(data['soon_principal'])}")
        m["matured"]["value"].configure(text=f"{data['matured_count']} 笔")
        m["matured"]["sub"].configure(
            text=f"本息 ¥{vault_tools.format_amount(data['matured_total'])}")

    def _render_hint(self):
        """操作条左侧那句说明：只报计数。

        **口径那句挪到了表格下方**（见 ``_build_table`` 里的 ``_footnote``）：
        窗口缩窄一半后，操作条放不下「共 2 家银行 · 4 张存单 · 利息＝本金×
        年利率×月数÷12（单利）」这 341px 的文字（实测），硬留会把两个按钮
        挤到换行。口径本身是用户明确要求要写出来的，不能删，所以改放在
        清单下面那条通栏说明里——那里宽度充足，也不会与按钮抢位置。
        """
        total = sum(group["count"] for group in self._groups)
        self._hint.configure(
            text=f"共 {len(self._groups)} 家银行 · {total} 张存单")

    def _render_footnote(self):
        """表格下方的口径说明（用户要求把利息算法写在界面上）。"""
        self._footnote.configure(
            text="利息＝本金×年利率×月数÷12（单利）；年化为资金加权")

    # ---------- 动作 ----------
    def _toggle_collapse(self, bank_id):
        if bank_id is None:
            return
        if bank_id in self._collapsed:
            self._collapsed.discard(bank_id)
        else:
            self._collapsed.add(bank_id)
        self._render()

    def _add_bank(self):
        from tkinter import simpledialog
        name = simpledialog.askstring("新增银行", "银行名称：", parent=self.win)
        if not name or not name.strip():
            return
        try:
            self.db.add_vault_bank(name.strip())
        except Exception as exc:
            self._fail("新增银行失败", exc)
            return
        self._reload()

    def _remove_bank(self, bank_id):
        """删银行：**连它的存单一起删**，所以必须先问一句、并说清会删几张。"""
        group = next((g for g in self._groups
                      if g["bank"]["id"] == bank_id), None)
        if group is None:
            return
        from tkinter import messagebox
        count = group["count"]
        detail = f"（含 {count} 张存单）" if count else ""
        if not messagebox.askyesno(
                "删除银行", f"删除「{group['bank']['name']}」{detail}？\n"
                            "这个操作不能撤销。", parent=self.win):
            return
        try:
            self.db.delete_vault_bank(bank_id)
        except Exception as exc:
            self._fail("删除失败", exc)
            return
        self._collapsed.discard(bank_id)
        self._reload()

    def _add_deposit(self):
        """新增存单：弹一个六字段的对话框，用户点「添加」后写库。

        **必须 ``wait_window`` 等对话框关闭**。``_DepositDialog.__init__``
        只负责把界面搭好就返回，此刻用户还没输入任何东西，``dialog.result``
        必然是 ``None``。不等待就直接读，等于「刚弹出来就问结果」——永远读到
        空值，**存单一条都写不进数据库**，而界面上不报任何错（用户以为存了）。

        这个缺陷在真实库上留下的痕迹：``vault_bank`` 有行、``vault_deposit``
        恒为 0——银行走 ``simpledialog.askstring``，它**自带模态循环**所以能存；
        存单走自建 ``Toplevel``，少了这一句就全部丢失。
        """
        if not self._groups:
            from tkinter import messagebox
            messagebox.showinfo("新增存单", "先建一家银行，再把存单加进去。",
                                parent=self.win)
            self._add_bank()
            return
        dialog = _DepositDialog(self)
        self.win.wait_window(dialog.win)
        data = dialog.result
        if not data:
            return
        try:
            self.db.add_vault_deposit(**data)
        except Exception as exc:
            self._fail("新增存单失败", exc)
            return
        self._last_deposit_bank_id = data["bank_id"]
        self._reload()

    def _remove_deposit(self, deposit_id):
        item = None
        for group in self._groups:
            for candidate in group["deposits"]:
                if candidate["id"] == deposit_id:
                    item = candidate
        if item is None:
            return
        from tkinter import messagebox
        if not messagebox.askyesno(
                "删除存单", f"删除「{item['name']}」？\n这个操作不能撤销。",
                parent=self.win):
            return
        try:
            self.db.delete_vault_deposit(deposit_id)
        except Exception as exc:
            self._fail("删除失败", exc)
            return
        self._reload()

    def _reload(self):
        self._load()
        self._render()

    def _fail(self, prefix, exc):
        """把异常显示在操作条上。不弹模态框：这类错误多半是环境问题，
        模态框会挡住用户去看控制台里的堆栈。"""
        traceback.print_exc()
        self._hint.configure(text=f"{prefix}：{exc}")

    # ---------- 基类契约 ----------
    def _initial_focus_target(self):
        """不指定焦点。这个窗口里没有「一进来就该开始打字」的输入框——
        数据都通过对话框录入，主界面是只读的清单。"""
        return None

    def _after_build(self):
        # 基类那个 30ms 的 ``_refresh`` 会在构造期跑一次；那个钩子在本类里
        # 被覆写成空操作，所以这里主动加载一次并渲染。
        self._reload()

    def _analyze(self, raw):
        """基类契约的实现。密匣没有「一段文本进、一段结果出」这条路径，
        保留它只是为了不偏离 ``_ToolWindow`` 的约定。"""
        raise NotImplementedError("密匣不解析文本，数据走对话框录入")

    def _refresh(self):
        """基类的刷新钩子。**这里只取消防抖定时器**，不做任何解析。

        基类 ``_refresh`` 会去读 ``self.input``（默认两栏版式才有），
        这个窗口没有那个控件。覆写成空操作是最省事也最不容易出错的做法——
        比在基类里加 ``hasattr`` 判断要干净。
        """
        if self._after_id is not None:
            try:
                self.win.after_cancel(self._after_id)
            except Exception:
                pass
        self._after_id = None

    def close(self):
        super().close()


def _looks_dark(color):
    """判断一个 ``#RRGGBB`` 底色是否偏暗（用来选气泡文字色）。

    按感知亮度加权（人眼对绿最敏感、蓝最不敏感）而不是简单取平均：
    ``#3A3A3E`` 这类深灰取平均会算成 60/255 勉强算暗，但 ``#1F1F22``
    与 ``#4444AA`` 用平均分不出该配深字还是浅字。
    """
    text = str(color).lstrip("#")
    if len(text) != 6:
        return False
    try:
        r, g, b = (int(text[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return False
    return (0.299 * r + 0.587 * g + 0.114 * b) < 128


def _attach_hint(entry, hint):
    """给控件挂一个提示气泡（原生没有 placeholder / tooltip）。

    两个使用场景：``_DepositDialog`` 的输入框（提示填什么格式），
    以及清单里**被截断的单元格**（悬停显示完整文字）。
    **两种控件的 ``cget("font")`` 返回值不同**：``Entry`` 给字体描述
    字符串，``Label`` 可能给元组——两种都要接住，否则悬停时抛
    ``TclError`` 而用户看到的是「气泡一闪没了」。

    不自己造一个 ``Toplevel`` 气泡：那需要跟着鼠标移动、处理多层窗口的
    z-order、还要在销毁时收干净，为一个「鼠标停一下才看」的提示不值得。
    用 Tk 内建的提示式实现——``<Enter>`` 时建立一个小 ``Toplevel``，
    ``<Leave>`` 时销毁它。

    **实现放在模块级函数而不是 ``_DepositDialog`` 的方法**：它不依赖对话框
    的任何状态，写成方法只是多一个 ``self``。
    """
    import tkinter as tk

    state = {"tip": None}

    def show(_event=None):
        if state["tip"] is not None or not hint:
            return
        try:
            tip = tk.Toplevel(entry)
        except tk.TclError:
            return
        tip.overrideredirect(True)
        tip.attributes("-topmost", True)
        try:
            bg = entry.cget("bg")
        except tk.TclError:
            bg = "#FFFFFF"
        palette = (VaultWindow.DARK_PALETTE if _looks_dark(bg)
                   else VaultWindow.LIGHT_PALETTE)
        fg = palette["muted"]
        try:
            master_bg = entry.master.cget("bg")
            # 气泡底色与所在区域同色、文字用一个中性灰：深色主题下
            # 场景的 ``master`` 底色偏暗，写死浅色文字会看不见。
            palette = (VaultWindow.DARK_PALETTE if _looks_dark(master_bg)
                       else VaultWindow.LIGHT_PALETTE)
            fg = palette["muted"]
        except tk.TclError:
            pass
        font = entry.cget("font")
        if not isinstance(font, str):
            font = "TkDefaultFont"
        label = tk.Label(tip, text=hint, bg=bg, fg=fg,
                         font=(font, 8), padx=6, pady=3,
                         highlightthickness=1, highlightbackground=palette["border"])
        label.pack()
        tip.update_idletasks()
        tip.geometry(f"+{entry.winfo_rootx()}"
                     f"+{entry.winfo_rooty() + entry.winfo_height() + 2}")
        state["tip"] = tip

    def hide(_event=None):
        tip, state["tip"] = state["tip"], None
        if tip is not None:
            try:
                tip.destroy()
            except tk.TclError:
                pass

    entry.bind("<Enter>", show, add="+")
    entry.bind("<Leave>", hide, add="+")
    # 对话框可能在鼠标还悬着的时候就被销毁（按回车提交），残留的气泡会
    # 孤零零留在桌面上。所以随控件一起收掉。
    entry.bind("<Destroy>", hide, add="+")


class _DepositDialog:
    """新增存单：常驻示例、实时预览、显式日期计算与行内校验。

    **用普通 ``Toplevel`` 而不是 ``simpledialog``**：需要六个字段与一处
    错误提示，``simpledialog`` 只能一个字段一句提示。

    校验走 ``vault_tools.build_deposit``，所以「用户填了什么 → 存进库的什么」
    这条路径与单元测试测的是同一段代码，不存在「测试里过了、界面上不过」。
    """

    FIELDS = (
        ("name", "存单名称", "如：大额存单 3 年"),
        ("amount", "本金", "如：40万 或 400000"),
        ("rate", "年利率 %", "如：3.10"),
        ("term", "存期", "如：36 个月 / 3年 / 半年"),
        ("date", "到期日", "如：2027-03-15"),
    )

    def __init__(self, window):
        import tkinter as tk
        self.window = window
        self.result = None
        self.tk = tk
        # 对话框与主窗口共用 P，避免基类系统色混入浅深主题。
        P = window.P
        bg = P["surface"]
        self.win = tk.Toplevel(window.win)
        self.win.title("新增存单")
        self.win.configure(bg=bg)
        self.win.transient(window.win)
        self.win.resizable(False, False)

        pad = {"padx": 24}
        tk.Label(self.win, text="新增存单", bg=bg,
                 fg=P["title"],
                 font=(window.panel.FONT_FAMILY, 11, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(14, 8), **pad)

        self.entries = {}
        row = 1
        # 银行选择用下拉：存单必须挂在某家银行下，让它变成一次点选而不是
        # 一次手打，省得打错字写出一家不存在的银行（那会变成孤儿记录）。
        tk.Label(self.win, text="所属银行", bg=bg,
                 fg=P["muted"],
                 font=(window.panel.FONT_FAMILY, 9)).grid(
            row=row, column=0, sticky="w", pady=4, **pad)
        self.bank_names = [g["bank"]["name"] for g in window._groups
                           if g["bank"]["id"] is not None]
        self.bank_ids = [g["bank"]["id"] for g in window._groups
                         if g["bank"]["id"] is not None]
        selected = getattr(window, "_last_deposit_bank_id", None)
        index = self.bank_ids.index(selected) if selected in self.bank_ids else 0
        self.bank_var = tk.StringVar(value=self.bank_names[index]
                                     if self.bank_names else "")
        option = tk.OptionMenu(self.win, self.bank_var,
                               *(self.bank_names or ["（无银行）"]))
        option.configure(bg=P["card"], fg=P["title"],
                         activebackground=P["head_bg"],
                         highlightthickness=0, bd=0, font=(
                             window.panel.FONT_FAMILY, 9))
        option.configure(highlightthickness=1, highlightbackground=P["border"],
                         highlightcolor=P["link"], takefocus=1)
        option["menu"].configure(bg=P["card"], fg=P["title"],
                                 activebackground=P["head_bg"],
                                 activeforeground=P["title"])
        option.grid(row=row, column=1, sticky="ew", pady=6, padx=(0, 24))
        self.bank_option = option
        row += 1

        self.variables = {}
        self.field_hints = {}
        self.term_buttons = {}
        fields = (*self.FIELDS[:4],
                  ("start", "起存日", "YYYY-MM-DD；默认今天，仅用于计算到期日"),
                  self.FIELDS[4])
        for key, label, hint in fields:
            caption = tk.Label(self.win, text=label, bg=bg,
                               fg=P["muted"],
                               font=(window.panel.FONT_FAMILY, 9))
            caption.grid(row=row, column=0, sticky="w", pady=4, **pad)
            variable = tk.StringVar(value=window._today_date().isoformat()
                                    if key == "start" else "")
            entry = tk.Entry(self.win, bd=0, relief="flat", textvariable=variable,
                             bg=P["card"], fg=P["title"],
                             highlightthickness=1, highlightbackground=P["border"],
                             highlightcolor=P["link"], insertbackground=window._accent(),
                             font=(window.panel.FONT_FAMILY, 10), width=26)
            entry.grid(row=row, column=1, sticky="ew", ipady=6,
                       pady=(8, 0), padx=(0, 24))
            _attach_hint(entry, hint)
            self.entries[key] = entry
            self.variables[key] = variable
            row += 1
            helper = tk.Label(self.win, text=hint, bg=bg, fg=P["muted"],
                              font=(window.panel.FONT_FAMILY, 8), anchor="w", width=1)
            helper.grid(row=row, column=1, sticky="ew", pady=(3, 2), padx=(0, 24))
            self.field_hints[key] = helper
            row += 1
            if key == "term":
                shortcuts = tk.Frame(self.win, bg=bg)
                shortcuts.grid(row=row, column=1, sticky="w", padx=(0, 24), pady=4)
                for months in (3, 6, 12, 24, 36):
                    button = self._button(shortcuts, f"{months}月",
                                          lambda m=months: self.variables["term"].set(str(m)))
                    button.pack(side="left", padx=(0, 6))
                    self.term_buttons[months] = button
                row += 1
            if key == "start":
                self.calculate_button = self._button(
                    self.win, "计算到期日", self._calculate_date)
                self.calculate_button.grid(row=row, column=1, sticky="w",
                                           padx=(0, 24), pady=4)
                row += 1
        preview = tk.Frame(self.win, bg=P["head_bg"], padx=12, pady=10)
        preview.grid(row=row, column=0, columnspan=2, sticky="ew", padx=24, pady=(12, 0))
        self.preview = tk.Label(preview, text="等待填写本金、年利率和存期", width=1,
                                bg=P["head_bg"], fg=P["title"], anchor="w",
                                font=(window.panel.FONT_FAMILY, 10, "bold"))
        self.preview.pack(fill="x")
        tk.Label(preview, text="预计值按单利、月份折算，实际以存单约定为准",
                 bg=P["head_bg"], fg=P["muted"], anchor="w",
                 font=(window.panel.FONT_FAMILY, 8)).pack(fill="x", pady=(4, 0))
        row += 1

        # 校验错误使用状态赭红，与操作按钮的交互蓝分离。
        self.error = tk.Label(self.win, text="", bg=bg,
                              fg=P["matured_fg"], anchor="w", width=1,
                              font=(window.panel.FONT_FAMILY, 9))
        self.error.grid(row=row, column=0, columnspan=2, sticky="ew",
                        pady=(6, 0), **pad)
        row += 1

        buttons = tk.Frame(self.win, bg=bg)
        buttons.grid(row=row, column=0, columnspan=2, sticky="e",
                     padx=14, pady=(10, 14))
        # 普通对话框保留 tk.Button；底色与按下态文字都按主题配对。
        tk.Button(buttons, text="取消", command=self._cancel, padx=14, pady=3,
                  bd=1, relief="solid", bg=P["card"],
                  fg=P["link"], activebackground=P["head_bg"],
                  activeforeground=P["link"], cursor="hand2",
                  highlightthickness=0,
                  font=(window.panel.FONT_FAMILY, 9)).pack(side="right",
                                                           padx=(8, 0))
        tk.Button(buttons, text="添加", command=self._submit, padx=14, pady=3,
                  bd=0, relief="flat", bg=P["button"], fg=P["button_fg"],
                  activebackground=P["button_hot"], activeforeground=P["button_fg"],
                  cursor="hand2", highlightthickness=0,
                  font=(window.panel.FONT_FAMILY, 9)).pack(side="right")

        self.variables["name"].set("定期存款")
        self._error_field = None
        for key, variable in self.variables.items():
            variable.trace_add("write", lambda *_args, k=key: self._on_change(k))
        self._tab_order = [self.bank_option, self.entries["name"], self.entries["amount"],
                           self.entries["rate"], self.entries["term"],
                           *self.term_buttons.values(), self.entries["start"],
                           self.calculate_button, self.entries["date"],
                           *buttons.winfo_children()]
        for index, widget in enumerate(self._tab_order):
            widget.bind("<Tab>", lambda _e, i=index: self._tab_focus(i + 1))
            widget.bind("<Shift-Tab>", lambda _e, i=index: self._tab_focus(i - 1))
        self.win.bind("<Return>", lambda _e: self._submit())
        self.win.bind("<Escape>", lambda _e: self._cancel())
        self.win.protocol("WM_DELETE_WINDOW", self._cancel)
        self.win.columnconfigure(1, weight=1)
        self.win.update_idletasks()
        self.win.geometry(f"500x{self.win.winfo_reqheight()}")
        self.win.update_idletasks()
        self._center()
        self.win.grab_set()
        self.win.focus_force()
        self.entries["amount"].focus_set()

    def _center(self):
        parent = self.window.win
        x = parent.winfo_rootx() + max(
            0, (parent.winfo_width() - self.win.winfo_width()) // 2)
        y = parent.winfo_rooty() + max(
            0, (parent.winfo_height() - self.win.winfo_height()) // 3)
        self.win.geometry(f"+{x}+{y}")

    def _button(self, parent, text, command):
        P = self.window.P
        button = self.tk.Label(parent, text=text, bg=P["card"], fg=P["link"],
                               padx=9, pady=5, cursor="hand2", takefocus=1,
                               highlightthickness=1, highlightbackground=P["border"],
                               font=(self.window.panel.FONT_FAMILY, 9))
        button.bind("<Button-1>", lambda _e: (command(), "break")[-1])
        button.bind("<space>", lambda _e: (command(), "break")[-1])
        button.bind("<FocusIn>", lambda _e: button.configure(
            highlightbackground=P["link"]))
        button.bind("<FocusOut>", lambda _e: button.configure(
            highlightbackground=P["border"]))
        return button

    def _tab_focus(self, index):
        self._tab_order[index % len(self._tab_order)].focus_set()
        return "break"

    def _mark_error(self, key, message):
        if self._error_field is not None:
            previous = (self.bank_option if self._error_field == "bank"
                        else self.entries[self._error_field])
            previous.configure(highlightbackground=self.window.P["border"],
                               highlightcolor=self.window.P["link"])
        self._error_field = key
        widget = self.bank_option if key == "bank" else self.entries[key]
        widget.configure(highlightbackground=self.window.P["matured_fg"],
                         highlightcolor=self.window.P["matured_fg"])
        self.error.configure(text=message)
        widget.focus_set()

    def _on_change(self, key):
        if self._error_field == key:
            self.entries[key].configure(highlightbackground=self.window.P["border"],
                                        highlightcolor=self.window.P["link"])
            self.error.configure(text="")
            self._error_field = None
        self._update_preview()

    def _update_preview(self):
        values = self._values()
        try:
            amount = vault_tools.parse_amount(values["amount"])
        except vault_tools.VaultError:
            amount = None
        self.field_hints["amount"].configure(
            text=f"{self.FIELDS[1][2]}  ·  ¥{amount:,.2f}" if amount is not None else self.FIELDS[1][2])
        try:
            rate = vault_tools.parse_rate(values["rate"])
            months = vault_tools.parse_term(values["term"])
            if amount is None or rate is None or months is None:
                text = "等待填写本金、年利率和存期"
            else:
                interest = vault_tools.interest(amount, rate, months)
                # 极端输入不应在 Tk 回调里溢出或留下旧的预计值。
                if not math.isfinite(interest) or not math.isfinite(amount + interest):
                    raise vault_tools.VaultError("数值超出范围")
                text = f"预计利息 ¥{interest:,.2f}   本息 ¥{amount + interest:,.2f}"
        except (vault_tools.VaultError, OverflowError):
            text = "请完善本金、年利率和存期"
        self.preview.configure(text=text)
        try:
            selected = vault_tools.parse_term(values["term"])
        except vault_tools.VaultError:
            selected = None
        for months, button in self.term_buttons.items():
            button.configure(bg=self.window.P["head_bg" if months == selected else "card"])

    def _calculate_date(self):
        values = self._values()
        try:
            end = vault_tools.calculate_end_date(values["start"], values["term"])
        except vault_tools.VaultError as exc:
            key = "term" if "存期" in str(exc) else "start"
            self._mark_error(key, str(exc).split("：")[0])
            return
        self.variables["date"].set(end.isoformat())
        self.error.configure(text="已按起存日和存期计算到期日")

    def _values(self):
        return {key: variable.get() for key, variable in self.variables.items()}

    def _submit(self):
        values = self._values()
        try:
            bank_id = self.bank_ids[
                self.bank_names.index(self.bank_var.get())]
        except (ValueError, IndexError):
            self._mark_error("bank", "请选择所属银行")
            return
        checks = (("name", lambda text: text.strip() or None, "存单名称"),
                  ("amount", vault_tools.parse_amount, "本金"),
                  ("rate", vault_tools.parse_rate, "年利率"),
                  ("term", vault_tools.parse_term, "存期"),
                  ("date", lambda text: vault_tools.parse_date(text, "到期日"), "到期日"))
        for key, parser, label in checks:
            try:
                if parser(values[key]) is None:
                    raise vault_tools.VaultError(f"请填{label}")
            except vault_tools.VaultError as exc:
                self._mark_error(key, f"{label}：{str(exc).split('：')[0]}")
                return
        try:
            record = vault_tools.build_deposit(
                values["name"], bank_id, values["amount"], values["rate"],
                values["term"], values["date"])
        except vault_tools.VaultError as exc:
            # 校验失败**不关对话框**：用户填了六个字段，关掉就得重填。
            self.error.configure(text=str(exc))
            return
        self.result = record
        self.win.destroy()

    def _cancel(self):
        self.result = None
        self.win.destroy()
