# Lumina

Windows 剪贴板管理器。常驻托盘，自动记录文本 / 图片 / 文件的复制历史，支持全文检索、
分类标签、Pin 固定、JSON 格式化查看，以及带标注（矩形、圆形、箭头、画笔、马赛克、
文字）的区域截图。

技术栈：Python 3.13 + Tkinter，数据库 SQLite（含 FTS5 全文索引）。主入口 `main.py`，
业务代码在 `lumina/`，测试在 `tests/`，数据库在 `data/lumina.db`。

# 快速开始

依赖只装在项目 `.venv` 里，**系统 `python` 没有**，所以下面统一用虚拟环境解释器。
先装依赖：

```powershell
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

再启动（不带子命令即等价于 `run`）：

```powershell
.venv\Scripts\python.exe main.py
```

常用快捷键在 `config.json` 里配置，默认 `ctrl+alt+v` 采集、`ctrl+alt+s` 区域截图、
`alt+v` 打开历史面板。

标题栏的「工具」下拉提供两个独立的调试小工具，粘贴即解析，结果可一键复制
（只读展示，不写剪贴板历史）：

- **JSON 格式化** — 格式化接口入参，重复键告警。结果栏**只放转换后的 JSON 本身**，
  不附字段结构摘要（用户明确要求去掉）。
  底部左下角的「JSON ▾」下拉与剪贴板详情栏那颗按钮**是同一份代码**：
  模式清单、文案、顺序、图标、勾选语义全部来自 `lumina/json_modes.py`，
  只有「怎么转换」的调用流程各自实现（详见下面「JSON 下拉的唯一来源」）。
  五项依次是 **格式化**（2 空格）、**压缩**（紧凑单行）、
  **展开转义**（把字符串值里的 `\n` 展开成真实换行，用来读被挤成一行的异常栈）、
  **Java 转义**、**恢复原文**（原样回显左栏输入）。
  **Java 转义**先压成紧凑单行，再整体转成 **Java 字符串字面量内容**——可直接贴进实体类的
  `setPayload("...")`，由用户自己在两侧补双引号（状态栏会写明这一点）。
  逐字符转义 `\` → `\\`、`"` → `\"`、LF/CR/TAB/BS/FF → `\n` 等，
  其余控制字符与孤立代理字符走 `\uXXXX`，中文原样保留。
  **不用特判 `u` 陷阱**：JLS 3.3 按前导反斜杠奇偶判定，转义产出的 `\\u0041`
  实测 javac 编译通过且值为字面 `\u0041`；测试里用**真实 javac 编译 + 运行回读**
  验证 Java 侧 `String` 的值恰好等于紧凑 JSON。
  命名依据：JLS §3.10.7 的官方术语是 **Escape Sequences**，Java 生态的事实标准
  API 是 `StringEscapeUtils.escapeJava()`（早先叫「参数化」，无规范出处且易与
  parameterized query 混淆，已改）。**注意与 `escapeJava` 有一处刻意不同**：
  它会把中文转成 `\uXXXX`，这里保留原文以便阅读。
  模式是窗口状态而非一次性动作——切了压缩之后继续输入仍按压缩展示。
  JWT 窗口没有这个下拉（它没有可转换的模式，多一个空入口只会让人以为漏了什么）。
- **JWT 解密** — 解码 header / payload 并换算 `exp`/`iat` 为本地时间，标注是否过期。
  **只解码不验签**：本地没有密钥，签名原样列出并明确标注「未校验」。工具名叫「解密」
  是用户选定的文案，但行为上**从不做密码学验证**，界面上那句「未校验」不能去掉。
  兼容 `Bearer ` 前缀、`Authorization:` 头、外层引号、换行折断、标准/URL-safe base64 混用。

工具窗口是**无边框窗口，但不留白**：顶部一条 30px 操作条，左上角是信号条标识 +
**工具名**（`JSON 格式化` / `JWT 解密`，次级文字色 + 小一号字号，紧跟一道发丝线），
右侧是**最小化 / 最大化 / 关闭**三个线条图形按钮（无文字）。工具名同时作为窗口标题，
任务栏与 Alt+Tab 显示的也是它。**「输入 / 结果」这类分区小标题仍不出现**——左栏蓝条
= 可编辑输入，右栏青条 = 只读结果；右下角斜纹为缩放抓手。
**左右两栏都按字符折行，行为完全一致**：超过栏宽自动换行，不出现横向滚动条。
两栏内容区宽度严格相等，同一段文本在两栏折在同一处。用 `char` 而非 `word` 是因为
JSON 里没有空格，`word` 对一整行压缩 JSON 完全无效；折行只是显示层行为，
`get("1.0", "end")` 取到的仍是原始文本，不会往内容里插入换行符。
底部只有**清空**与**复制结果**两个按钮（JSON 窗口在最左另加一个「JSON ▾」下拉）：
左栏本身可编辑，Ctrl+V 直接粘贴即可，不另设「粘贴」按钮；「复制结果」复制的是
右栏所见文本。

**双击顶栏切换最大化/还原**（工具窗口与主面板一致），符合 Windows 原生习惯。最大化
用的是工作区矩形，不会盖住任务栏。实现要点：`<Double-Button-1>` 只送到「收到第一次
press」的那个控件，所以工具窗口的容器 `rail`、信号条 `mark`、标题 `title_label`
三块要各自绑定；主面板标题栏的大片空白由容器 `title_bar` 接收，绑容器即可。双击后
必须清掉拖动锚点（`_drag_off`），否则最大化窗口会被随后的 `<B1-Motion>` 拖走。
窗口四角是**尖角**，与剪贴板主面板保持一致：主面板 `_make_rounded_bg` 只画矩形
填充、从不调 `SetWindowRgn`，两个窗口并排看若一圆一直就风格割裂。工具窗口的
`_apply_round_region` 相应改用 `CreateRectRgn`（方法名沿用旧名，只为不动调用点），
`_paint_shell` 也改用 `draw.rectangle` 而非 `rounded_rectangle`——圆角时描边要沿弧线
走，视觉上比主面板「软」一档。**注意区域裁剪这一层不能直接删掉**：外壳是按客户区
尺寸绘制的，缺了 `SetWindowRgn` 时 DWM 会在边缘留一圈未绘制的黑边；它现在不产生
圆角，只负责把窗口区域钉成精确的 `width x height`。顶边保留一条冷光扫描线（纯装饰）。

**工具窗口支持八向拖拽改尺寸**：四边各 6px、四角各 12px 的透明热区，光标按方向
变成上下 / 左右 / 斜向箭头。热区压在 `CONTENT_INSET`（8px）那一圈外壳缝隙里，
不会盖住正文与顶栏，所以点边框附近的文字仍是选中文字而不是改尺寸。右下角那道
斜纹抓手只是**视觉提示**（告诉用户「这个角可以拉」），真正的事件由角热区接管。

这里**刻意没有**用主面板那套 Win32 方案（`WS_THICKFRAME` + `WM_NCHITTEST`）。
实测给 `winfo_id()` 那个 TkChild 加上 `WS_THICKFRAME` 后：① 客户区从 920x640 被
压成 **906x626**（多出 14px 非客户区）；② `PrintWindow` 抓到的最外圈像素由干净的自绘
描边色变成 `(227,227,227)` / `(255,255,255)`——**系统灰边露出来了**，与自绘的圆角
裁剪打架（当时四角还是圆角）。本窗口是「整块 Canvas 自绘 + 区域裁剪」，露系统边框
就毁了观感，因此改用纯 Tk 热区，不动任何窗口样式。左侧与上侧缩放会**同步移动原点**（移动量按「实际
改掉的尺寸」算，不是原始鼠标位移），否则窗口会一边缩一边飘。

窗口**正常出现在任务栏与 Alt+Tab 里**。无边框会让 Tk 给窗口打上
`WS_EX_TOOLWINDOW`（该标志的语义就是「不进任务栏」），因此要换成
`WS_EX_APPWINDOW`，并在每次 `<Map>` 时重新声明一次——构造期设置会被 Tk 在映射那
一刻覆盖。工具窗口与主面板共用同一套实现，见 `lumina/taskbar.py`。

**最小化绝对不能翻转 `overrideredirect`。** 这一条是本项目踩得最深的一个坑：

Tk 的 `iconify()` 对无边框窗口直接抛 `TclError: override-redirect flag is set`，
自然的绕法是「临时 `overrideredirect(False)` → `iconify()` → 唤回时再翻回来」。
但**每次翻转都会重建 HWND**（实测 `0x3a2e1480` → `0x559a07c8` → `0x1f40160c`），
新窗口带着 Tk 刚重设的 `WS_EX_TOOLWINDOW` 重新进任务栏，explorer 于是撤掉任务栏
按钮，`WM_SETICON` 设过的图标也一并丢失。用户看到的就是**「窗口一显示，任务栏图标
就没了，点按钮也不切换最小化」**。

正确做法是走 Win32 `ShowWindow(SW_MINIMIZE)` / `SW_RESTORE`：无边框窗口能正常最小化
（矩形移到 `-32000`），而 HWND、扩展样式、框位标志、图标句柄**全程不变**。显隐同理，
用 `ShowWindow(SW_SHOW)` / `SW_HIDE` 而不是 `deiconify()` / `withdraw()`——后者会重新
应用 `overrideredirect` 对应的扩展样式。

主面板的最小化按钮原先调的是 `withdraw()`，那是**隐藏**而不是最小化，窗口会从任务栏
彻底消失，用户再也找不回来。

**ESC 是「最小化到任务栏」，不是关闭。** 主面板与工具弹框一致：`<Escape>` 绑到各自的
`escape()`，内部复用最小化按钮那条路径。三种语义要分清，不要混：

| 动作 | 入口 | 窗口去向 |
|---|---|---|
| 最小化 | ESC / 顶栏最小化按钮 | 留在任务栏，点一下回来 |
| 隐藏 | `hide()`（截图前让位、热键切换） | 从任务栏彻底消失 |
| 关闭 | 顶栏的叉 / `close()` | 销毁窗口，工具弹框释放实例 |

**这条改动牵出两个必须一起修的问题**，否则 ESC 一点就「回不来」或「开出第二个窗」：

1. `show_window` 原来只发 `SW_SHOW`，而**它对最小化窗口完全无效**——实测调完
   `IsIconic` 仍为 True、`winfo_viewable()` 仍为 0，窗口挂在任务栏上永远回不来。
   现在先判 `IsIconic`，为真改发 `SW_RESTORE`。
2. `show()` 原本无条件调 `apply_mode()`，它会按屏幕尺寸重新居中。最小化还原时这
   属于多余动作，实测把最大化几何 `1920x1032` 冲成当时的默认尺寸 `1160x720`
   （默认值现已提到 1360×840，见下面「窗口默认尺寸」一节；这里要记的是
   **「最大化几何被默认值覆盖」这个行为**，不是那个具体数字）。现在先判
   `is_minimized()`，还原路径跳过 `apply_mode()`。

另外**同一工具只保留一个窗口实例**（`panel._tool_windows`）：ESC 改成最小化后，
最小化的窗口仍挂在任务栏上，若再从工具菜单点一次就新建第二个，用户会看到两个同名
窗口、各存一份输入。现在 `open_tool` 命中实例表就只 `summon()` 唤回；点叉关闭时
`close()` 会把自己从表里摘掉，下次重开得到全新实例。

无边框窗口**收不到键盘焦点时 ESC 不会触发**，所以两处都在显示后 `focus_force()`。

### JSON 下拉的唯一来源

剪贴板详情栏与工具弹框都有「JSON ▾」下拉。**它们不是两套平行实现，模式定义只有
一份**：`lumina/json_modes.py`。此前这段东西散在三个地方——`DetailPane` 的
`JSON_MODE_LABELS`、`_transform_json` 里的 `{'format': '格式化', ...}`、
`tool_window` 的 `JSON_MODE_LABELS` 与 `_json_menu` 的 items 列表——靠两条测试
互相比对来防漂移，那其实是「用测试补架构缺口」。

`json_modes` 提供：模式顺序 `MODE_ORDER`、结果态名 `MODE_LABELS`（「已格式化」）、
动作名 `ACTION_LABELS`（「格式化」）、菜单三元组 `MODE_ITEMS`（label / hint / 图标
kind），以及 `build_menu_items()`——直接吐出可交给 `JsonPopup` 的 items 列表。
两侧都调它，菜单因此**必然一致**；测试改成断言 identity（`assertIs`），谁再在
本地抄一份就立刻失败。

**只抽「模式是什么」，不抽「怎么转换」。** 两侧的线程模型与状态机本质不同，
强行合并会破坏详情栏的竞态保护：

| 维度 | 剪贴板详情栏 | 工具弹框 |
|---|---|---|
| 线程 | 后台线程 + 轮询 `collect` | 同线程直接算 |
| 输入 | 历史记录 `row["content"]` | 左栏控件文本 |
| 忙碌/竞态 | `_json_busy` + `_json_request` 序号 | 无（同步） |
| 结果去向 | `_json_text` → 重建预览 | `output` 控件 |

所以 `build_menu_items` 有两个独立开关而**不复用一个**：`busy`（转换中禁用四个
转换项，工具弹框恒传 `False`）与 `can_restore`（是否处于转换态）。合并成一个参数
会让工具弹框在「还没转换过」时也禁用恢复项，或反过来把转换项禁成一片灰。

两侧初始状态差异是**预期之内**的：工具弹框出生即 `format` 态（它生来看格式化
结果），详情栏初始是「原文」态、恢复项禁用。

**Java 转义的结果不做语法着色。** 它的产物不是 JSON，引号全变成 `\"`，
`json_syntax_spans` 的字符串规则整段失配，只剩裸数字与 `true` 被误标成
`num`/`lit`——深色主题下表现为「大部分白字、个别词突然有色」。所以
`_build_text_body` 里 `escape_java` 走纯文本路径（`if self._json_mode !=
"escape_java"` 才调 `_highlight_json`）。

### 顶栏菜单：悬停展开，不带箭头，且要先解锁

工具 / 截图 / 设置三个按钮**文案里没有下拉箭头**（用户要求「统一去掉下拉的箭头
样式」），改为**鼠标悬停自动展开**。可发现性完全由悬停提供，别再以「提示可点」
为理由把 `▾` 加回来；`tests/test_panel_menu_hover.py` 会直接失败。

**但悬停默认是关闭的**——必须先**点过一次**顶栏菜单按钮才解锁（用户要求「只有
第一次点击上面菜单某一项后才触发鼠标放到某一个菜单上自动弹出下拉框」）。
`_hover_armed` 控制这道门，只挂一次、跨菜单共享：

| 时机 | `_hover_armed` |
|---|---|
| 面板刚构造 | `False` |
| 点任一顶栏菜单按钮（开或收都算） | `True` |
| **点菜单按钮与弹窗以外的任何地方** | 复位 `False` |
| 隐藏 / 最小化面板 | 复位 `False` |

**「点其他地方就取消」是刻意的**（用户要求「点击激活后 如果点击其他地方取消
激活」）：激活只管这一轮操作，用完随手点到列表 / 详情就回到纯点击状态，避免
鼠标偶然扫过顶栏又弹菜单。所以 `_hover_outside_click` 挂在 `win` 的
`<ButtonPress-1>` 上，并且要**放行三类点击**：三颗菜单按钮（那是激活本身）、
弹窗内部（那是菜单自己的交互，否则点一下选项就把激活打掉）、以及任何非 widget
目标（`winfo_toplevel` 会抛，整段包 `try`）。

**离开面板要复位，不是洁癖。** 面板被热键唤出时光标常正好压在按钮上，Tk 会补发
一个 `<Enter>`；若解锁状态一直留着，就会出现「叫出剪贴板就自动蹦出菜单」——这正是
上一轮加延迟想压掉、但根本压不住的那个现象（延迟只挡首次、挡不住之后的每次唤出）。

点击路径当然保留：键鼠习惯不同，触控板用户也没有可靠的悬停。

实现上有五个不显眼但必需的处理：

1. **`<Enter>` 延迟 180ms 再展开**（`HOVER_OPEN_DELAY`），不是收到事件就开。
   解锁后延迟到期还要**再校验一次指针确实仍在按钮矩形内**，否则指针已划走
   也会误开。
2. **解锁门的判断放在 `_hover_enter()`，不放在 `_bind_hover_menu` 的闭包里。**
   闭包拿不到，测试只能 eval Tk 绑定脚本——既脆弱，又**测不出这道门到底拦没
   拦住**（变异验证会显示「未捕获」）。抽成方法后可直接调用。
3. **收起判定要覆盖「三颗菜单按钮 + 弹窗」的整体，不能只看当前锚点那一个。**
   这条踩过一个**方向性 bug**：只算锚点时，打开*设置*的合并区是 `x[586,764]`，
   往左划到*截图*（`x[535,584]`）落在区外 → 轮询判定「已离开」→
   `_close_popup_menu()` 顺带取消了截图排队中的 180ms 展开任务，于是
   **从右往左划不弹**；打开*工具*时合并区 `x[484,660]` 恰好盖住截图，往右划
   就正常。用户实测报的就是这个单边现象，所以测试必须**逐个锚点 × 逐个目标
   按钮做全组合**，只测一个方向拦不住。
4. **收起判定还要把弹窗并进来**，鼠标从按钮往下划进弹窗必然先离开按钮，
   在 `<Leave>` 里收起等于弹窗永远点不到。弹窗定位在按钮下沿 `+4`，穿过这道缝
   时也不能误收起，所以 `_popup_hover_contains()` 把若干矩形并成一个外接矩形，
   再留 6/10px 容差。收起靠 `_poll_popup_hover` 轮询（和 `FlatMenu` 的
   `_check_outside_click` 同思路），不用事件——跨两个 Toplevel 的 `<Leave>`/
   `<Enter>` 序列既不可靠也不连续。
5. **切换到另一个按钮时必须先 `_close_popup_menu()`。** 否则划过
   「工具→截图→设置」时每个按钮都开一个 Toplevel，屏幕上留下**三个弹窗残影**
   （实测 176x102 / 148x141 / 178x155 叠在一起）。这是实测抓到的真实缺陷。

测试有个易踩的坑：`_hover_open` 会读**真实指针**做前置校验，而测试环境的
光标位置不受控。直接用 `SetCursorPos` 挪真实光标在**全量跑时会因顺序污染失败**
——前面的任务栏测试隐藏/最小化过窗口，按钮屏幕坐标随之失效（出现过单项通过、
全量 2 红）。正确做法是把 `winfo_pointerxy` **打桩**到按钮中心：被测逻辑
（校验、关旧、开新）全都照走，但不依赖光标与窗口可见性。

`_reset_hover_armed()` 会**同时取消排队中的 `after` 任务**，不只清标志——否则
面板离场 / 点了别处之后那笔延迟任务仍会到期执行，用户会看到「点了别处还是弹了」。
`tearDown` 里也要调它，`_close_popup_menu()` 只清轮询、不碰 `_hover_open_job`，
残留会串到下一个测试实例。

### 主面板的留白（顶栏 / 搜索胶囊 / 控制按钮三处耦合）

**「边框显宽」的真实成因是顶栏比例失衡，不是四周普遍宽。** 实测（当时的默认
窗口 1160×720，像素采样）原始留白是左 22 / 右 22 / 底 36 / **顶 48**。顶部之所以
特别突出，
是因为顶栏与内容区同为 `window_bg`（`#F2F2F7`）且**中间没有分隔线**——`x=窗口
中线` 一路扫下来 `y=0..48` 是一整段连续同色，用户看到的是**一块 48px 空白**，
而不是「30px 顶栏 + 10px 间隙」两段。

所以这里最有效的手段不是继续缩尺寸，而是**加一条 1px 发丝线**（`separator`
色）把同色块切开。当前值（方案 A，轻度收紧）：

| 项 | 原值 | 现值 |
|---|---|---|
| 顶栏高度 `_hdr_h` | 30 | **26** |
| 顶栏→内容间隙 | 10 | **6** |
| 内容左右内边距 | 14 | **12** |
| 底部内边距 | (0,6) | **(0,4)** |
| 顶栏发丝线 | 无 | **`separator` 1px** |

实测结果：顶部 **48 → 34**（含 1px 线，净空白 33）、左右 **22 → 20**、
底部 36 → 34。改这几处时有三个**必踩的耦合**：

1. **`_hdr_h` 与 `_search_h` 必须分开**。原先共用一个 `_sh_h`，同时喂给顶栏、
   `_search_wrap`、`_search_canvas` 和 `_layout_search` 的 `h = ...`。直接改小
   会把搜索胶囊一起压扁，占位图标随即贴边。
2. **窗口控制按钮的画布尺寸必须跟随顶栏**。`_make_control_button` 里原先写死
   `size = max(30, int(round(32 * dpi)))`，而按钮是 `pack(fill="y")` 进顶栏的；
   顶栏 26 时画布被裁到 26，但图标仍按 26 居中画在中心点，下半截直接缺失。
   现在画布直接取 `self._hdr_h`。
3. **`body_w = self._cw - 56` 是手写魔数**，与 `body_split.pack(padx=12)` 是两份
   独立写死的值，`_left_col_width` 里还有一份同样的公式作首帧兜底。改 padx
   必须同步改这两处减数，否则首帧左右栏宽度偏 4px，要等第一次 `<Configure>`
   才自我纠正（表现为打开瞬间右栏内容闪一下）。

`tests/test_panel_layout_compact.py` 用**运行时几何**钉住这三点（不是扫源码正则，
避免「注释里写了就算通过」）：搜索胶囊实际渲染高度独立于顶栏、按钮的**请求**
高度不超顶栏、图标不超画布、顶栏下确有 separator 色 1px 线、左右留白未回弹。
注意按钮那条必须查 `btn['height']`（请求值）而不是 `winfo_height()`——后者报的
是 `fill="y"` **裁剪后**的值，正好把缺陷掩盖掉（首次写测试时确实漏过，靠变异
验证才发现）。

### 窗口默认尺寸：1360×840，且必须按工作区兜底

默认窗口从 **1160×720 提到 1360×840**（逻辑像素，实际会乘 DPI）。在 1920×1080
的机器上占工作区从 60%×70% 提到 **70.8%×81.4%**，左右栏各宽约 76px，列表不必那么挤，
详情区读长文本也不用频繁滚动。实测该环境下窗口为 `1360×840 @(280,96)`，
左栏 493 / 详情 806，水平与垂直居中偏差都是 0。

`config.json` 可以覆盖，两个键都是**逻辑像素**（与默认值同单位，之后才乘 DPI）：

```json
{ "panel_width": 1500, "panel_height": 900 }
```

两个键**都可以不写**，缺失或留空即用默认值。取值规则：

| 输入 | 结果 | 原因 |
|---|---|---|
| 缺省 / `""` | 1360×840 | 没填就是没填 |
| `1200` | 1200×840 | 只覆盖宽，高保持默认 |
| `"1400"` / `1400.9` | 1400 | 字符串与浮点都吃，按整数处理 |
| `"abc"` / `null` / `[1400]` / `{"w":1400}` | 1360×840 | **非法值静默回落**，不抛错 |
| `0` / `100` | 880×520 | 抬到硬下限 `MIN_PANEL_W/H` |
| `99999` | 不超过默认值 2 倍 | 挡住手滑把窗口撑爆 |

**非法值必须静默回落而不是抛错**：配置文件是用户手改的，为一行拼错的值让剪贴板
打不开是本末倒置。硬下限存在的原因是**双栏布局会被压塌**——左栏 `LEFT_MIN=400`
加详情 `DETAIL_MIN=380`，再算上 `_left_col_width()` 里的 `-56` 内边距减数和分隔占的
21px，至少要 857 才不挤，所以下限取 `MIN_PANEL_W=880`。`test_hard_minimum_covers_two_column_layout`
把这四项加起来反向校验，任何一处调大而下限没跟着走都会红。

**最容易改回去的坑：高度必须按「工作区」而不是「整屏」兜底。** 整屏高含任务栏
（本机 1920×1080 的任务栏占 48px，工作区只有 1032）。早先 `apply_mode` 用的是
`h = min(self._ch, self.win.winfo_screenheight())`，默认高度一旦调大就会**把窗口底边
塞到任务栏底下**——1366×768 的机器上尤其明显（整屏 768 / 工作区约 720）。现在窗口
尺寸与居中位置统一由 `_window_size_limits()` 给出，它内部走 `_work_area()`
（`SystemParametersInfoW(SPI_GETWORKAREA)`），并在工作区报出 0 或负数时
（远程桌面 / 多屏未就绪）退回整屏，避免拿到 0 尺寸。

`tests/test_panel_window_size.py` 用 `mock` 替换 `_work_area()` 来**模拟小屏**，
所以在 1920×1080 的机器上也能验证 1366×768 的兜底路径，不依赖本机分辨率。
9 个变异体（高度退回整屏、去掉上下限、非法值抛错、apply_mode 退回整屏居中、
默认值改回 1160×720 等）逐一验证过，全部被捕获。

**注意 `_work_area()` 与 `_window_size_limits()` 的返回顺序不同**：
前者是 `(x, y, 宽, 高)`，后者是 `(宽, 高, x, y)`。写测试时直接把工作区元组
透传给 `_window_size_limits` 会得到「宽=0」——实测踩过，别省那一步转换。

**任务栏按钮的「点一下最小化、再点一下还原」需要额外的样式标志。** 无边框窗口是
纯 `WS_POPUP`，`WS_MINIMIZEBOX` / `WS_MAXIMIZEBOX` / `WS_SYSMENU` **全为 0**
（实测 `STYLE=0x96000008`），缺了它们点任务栏按钮只会把窗口提到前台。这三个是
**纯行为标志**，补上后 Windows 自己接管该交互，实测不会因此露出原生标题栏
（`WS_CAPTION` 仍为 `0x0`）。注意这些标志在 `overrideredirect` 翻转时会被冲掉——
这也是上面「不要翻转」的另一个理由。

**任务栏图标必须按尺寸分槽设置，`iconphoto` 与 `iconbitmap` 都不可靠。**
`iconphoto` 对无边框窗口完全无效（实测 `WM_GETICON` 三个槽位全返回 0）；
`iconbitmap(<ico>)` 能设上，但**不区分尺寸**——实测把 ico 里 32x32 那张塞进本该放
16x16 的 `ICON_SMALL` 槽位，三个槽位拿到同一个 HICON，`GetIconInfo` 读出来是
`(32, 32)`。任务栏按钮取 `ICON_SMALL` 绘制，尺寸对不上就画不出图标。

正确做法是 `LoadImageW` 按尺寸各加载一次，`WM_SETICON` 分别写进 `ICON_SMALL`(16px)
与 `ICON_BIG`(32px)——实测分别得到 `(16, 16)` 与 `(32, 32)`，`ICON_SMALL2` 由系统
自动跟随 SMALL。HICON 在进程内缓存，避免每次显示都新加载一个句柄造成泄漏。

图标由 `lumina/app_icon.py` 在运行时绘制（深色玻璃底 + 蓝紫光晕 + 剪贴板剪影 +
LUMINA 字样，与托盘图标同源），存成含 16/32/48/256 四种尺寸的 ico 后复用，因此
仓库里不需要提交任何二进制资源。

除图形界面外还带一组命令行子命令（`-c` 可指定配置文件）：

```powershell
.venv\Scripts\python.exe main.py list      # 列出最近记录
.venv\Scripts\python.exe main.py search "关键字"   # 全文检索
.venv\Scripts\python.exe main.py stats     # 数据库统计
```

其余子命令：`pin` / `unpin` / `tag` / `export` / `clean`。

# 打包命令

使用 PyInstaller 将 Lumina 打包为 Windows 应用程序：

```powershell
python -m PyInstaller --clean --noconfirm --onefile --windowed --name "Lumina" `
    --add-data "config.json;." `
    --add-data "data/schema.sql;data" `
    --hidden-import "tkinter" `
    --hidden-import "_tkinter" `
    --hidden-import "PIL" `
    --hidden-import "PIL.Image" `
    main.py
```

参数说明：

- `python -m PyInstaller`：使用当前 Python 环境中的 PyInstaller 打包工具。建议在项目 `.venv` 激活后执行。
  PyInstaller 属于**打包时的开发依赖，不在 `requirements.txt` 里**（运行时并不需要它），首次打包前需单独安装：
  ```powershell
  pip install pyinstaller
  ```
- `--clean`：清理 PyInstaller 的临时缓存后再开始构建，避免旧缓存影响打包结果。
- `--noconfirm`：自动确认覆盖并删除已有的输出目录，不再显示确认提示。
- `--windowed`：生成窗口程序，运行 GUI 应用时不显示命令行窗口。该参数也可以写成 `--noconsole`。
- `--onefile`：将程序及其依赖打包成一个独立的 `Lumina.exe` 文件。
- `--name "Lumina"`：将生成的应用程序命名为 `Lumina`。
- `--add-data "config.json;."`：将 `config.json` 一起复制到打包程序的顶层目录。Windows 使用分号分隔源文件和目标目录。
- `--hidden-import`：显式包含动态导入的 `tkinter`、`_tkinter` 和 Pillow 模块。
- `main.py`：指定应用程序的入口文件。

打包结果默认生成在 `dist/` 目录中，可执行文件为 `dist/Lumina.exe`。

> **打包前务必检查 `config.json` 里的 `db_path`。**
> 该文件会被原样嵌进 exe，并在用户首次启动时复制到 `%LOCALAPPDATA%/Lumina/config.json`
> 长期生效。如果它是开发机的绝对路径（如 `D:\workplace-python\Lumina\data\lumina.db`），
> 在别人机器上会指向一个不存在的盘符目录。发布前请确认它是相对路径：
>
> ```json
> { "db_path": "data/lumina.db" }
> ```
>
> 相对路径会按配置文件所在目录解析（打包后即 `%LOCALAPPDATA%/Lumina/data/lumina.db`），
> 具体见下文 Path Configuration。

# 运行测试

单元测试集中在 `tests/` 目录，全部基于标准库 `unittest`（不依赖 pytest）。

测试本身不装第三方包，但会被测代码在导入时就要 `PIL` / `mss`，而这两个依赖只装在
项目 `.venv` 里。**务必用虚拟环境的解释器跑，直接用系统 `python` 会 `ModuleNotFoundError`**：

```powershell
.venv\Scripts\python.exe -m unittest discover
```

在项目根目录执行（下方命令中的 `python` 均指 `.venv\Scripts\python.exe`）：

```bash
python -m unittest discover
```

也可显式指定起始目录与顶层目录：

```bash
python -m unittest discover -s tests -t .
```

或单独运行某个测试模块（包路径形式，需带 `tests.` 前缀）：

```bash
python -m unittest tests.test_storage_regressions
```

说明：

- `tests/__init__.py` 不可删除。`unittest discover` 只递归进入包含 `__init__.py`
  的子目录，移除后整个测试集会被静默跳过。
- 测试文件中通过 `Path(__file__).resolve().parent.parent` 定位项目根，新增测试
  文件时请沿用该写法。
- 当前用例数：468 项通过、2 项跳过（跳过项依赖符号链接权限）。

# Lumina Clipboard Manager - Database Documentation

## Overview

Lumina uses a single SQLite database (`data/lumina.db`) to store clipboard history items. The database is created automatically on first run and is located at the path specified in `settings.py` (`data/lumina.db` by default).

## Schema

### `clipboard` table

This is the only user-defined table in the database. It stores all clipboard entries (text and images).

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | INTEGER | PRIMARY KEY, AUTOINCREMENT | Unique identifier for each entry |
| `kind` | TEXT | NOT NULL, CHECK(kind IN ('text','image')) | Type of content: 'text' or 'image' |
| `content` | TEXT | | Text content (for text entries) |
| `data` | BLOB | | Image or file binary data; NULL for disk-archived files |
| `file_path` | TEXT | DEFAULT '' | Relative path inside the database's `file_store` directory |
| `file_status` | TEXT | NOT NULL, DEFAULT 'none' | File archive state: `none`, `pending`, `ready`, or `failed` |
| `hash` | TEXT | NOT NULL | SHA256 hash of content/image, used for deduplication |
| `source` | TEXT | DEFAULT '' | Source filename or path where the clip was captured |
| `created_at` | TEXT | NOT NULL, DEFAULT `(datetime('now','localtime'))` | Creation timestamp |
| `pinned` | INTEGER | NOT NULL, DEFAULT 0 | Whether the entry is pinned (1) or not (0) |
| `tags` | TEXT | NOT NULL, DEFAULT '' | Comma-separated user tags for filtering |
| `category` | TEXT | NOT NULL, DEFAULT '' | Classification: `text`, `code`, `link`, `image`, or `file` |

### Current-format initialization

`init_schema()` initializes the current schema from `data/schema.sql`. There are no legacy column migrations, constraint migrations, or historical category backfills. Existing databases must already use the current schema; incompatible older databases are not automatically upgraded or cleared.

### FTS5 Full-Text Search tables

When search is available, SQLite FTS5 creates these internal tables automatically:

- `clipboard_fts` — Virtual FTS5 table with columns `content, tags, source`
- `clipboard_fts_data`, `clipboard_fts_idx`, `clipboard_fts_docsize`, `clipboard_fts_config` — Internal FTS index structures

These are **implementation details** managed by SQLite; users should not interact with them directly. They enable fast full-text search across clip content, tags, and source. If FTS initialization fails (uncommon), the app falls back to LIKE-based search.

### 数据库表

下表列出了数据库中全部 7 张表（含系统表和 FTS5 内部表），按表的使用角色分类：

| 表名 | 类别 | 主要用途 | 关键列 |
|------|------|----------|--------|
| `clipboard` | **业务表** | 存储所有剪贴记录（文本/图片），每条记录一行 | `id`, `kind`, `content`, `data`, `file_path`, `file_status`, `hash`, `source`, `created_at`, `pinned`, `tags`, `category` |
| `sqlite_sequence` | **系统表** | `AUTOINCREMENT` 计数器，记录 `clipboard.id` 的序列 | `name`, `seq`（内部使用，用户无需关注） |
| `clipboard_fts` | **FTS5 虚表** | 全文搜索索引表，全文检索的基础 | `content`, `tags`, `source`, `rowid`（映射到 `clipboard.id`） |
| `clipboard_fts_data` | **FTS5 内部** | 存储 FTS5 的文档数据、标记及位置信息 | 内部字段，由 SQLite 管理 |
| `clipboard_fts_idx` | **FTS5 内部** | FTS5 的倒排索引，加速文本匹配查询 | 内部字段，由 SQLite 管理 |
| `clipboard_fts_docsize` | **FTS5 内部** | 缓存每篇文档的大小，用于排名排序 | 内部字段，由 SQLite 管理 |
| `clipboard_fts_config` | **FTS5 内部** | 存储 FTS5 的分词器配置（tokenize 设置等） | 内部字段，由 SQLite 管理 |

**说明**：

- 仅有 **`clipboard`** 一张表是应用程序直接读写的业务表，包含所有剪贴历史、分类、标签、Pin 状态等信息。
- `sqlite_sequence` 是 SQLite 自动创建的系统表，用于支持 `AUTOINCREMENT`，通常不需要手动查询。
- `clipboard_fts` 及其内部表是全文搜索功能的组件。如果搜索可用（取决于 FTS5 编译情况），它们会在 `init_schema()` 中自动创建；否则应用会退回到 LIKE 方式模糊查询。
- 当前表和索引由 `lumina/database.py` 的 `init_schema()` 初始化；不再支持旧表结构自动迁移。

## Data Operations

### Adding clips

- `add_text(text, source)` — Stores text content with automatic classification
- `add_image(png, source)` — Stores raw PNG image data
- `add_file_pending(path, source)` — Records one file's metadata for asynchronous archiving
- `complete_file(clip_id, data)` — Stores file data in `data` or an independent disk archive
- `fail_file(clip_id, error)` — Marks a failed file archive

### Querying

- `list_recent(n=20)` — Returns newest clips ordered by creation time
- `search(query, n=20)` — Full-text search (if available) or LIKE fallback
- `get(clip_id)` — Retrieve a specific clip by ID
- `get_text(clip_id)` — Lightweight read without the image BLOB

### Maintenance

- `cleanup(retention_days, max_rows)` — Delete old/unpinned clips based on age or count
- `set_pinned(clip_id, pinned)` — Toggle pin status
- `set_tags(clip_id, tags)` — Update tags for a clip
- `delete(clip_id)` — Remove a clip permanently

## Current Format Only

Lumina is maintained as a new project. Legacy database fields (`image`, `file_data`, etc.), old file-list APIs, old annotation tuples missing style fields, and automatic discovery of historical configuration/database locations are not supported.

Removing compatibility code does not delete existing databases, configurations, or archives. To start fresh, explicitly configure a new database path; do not point the application at an incompatible old database.

## Path Configuration

Database path is configured by `db_path` in `config.json` and resolved by `lumina/settings.py`:

```json
{
    "db_path": "data/lumina.db"
}
```

Relative `db_path` values are resolved only against the configuration file's directory. Lumina does not search the executable directory or current working directory for historical databases.

In source runs, the default configuration is the project's `config.json`. In packaged runs, the default writable configuration is `%LOCALAPPDATA%/Lumina/config.json`; the first run copies defaults from the bundled template, and later runs use this stable file. The database directory is created when needed.

Screenshot annotations use only these complete tuples:

```python
("rect", start, end, width, color)  # also oval / arrow
("brush", points, width, color)
("mosaic", points, radius)
("text", position, text, font_size, color)
```

Missing or extra style fields are not interpreted as older formats.
