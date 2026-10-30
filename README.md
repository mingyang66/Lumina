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

标题栏的「工具 ▾」下拉提供两个独立的调试小工具，粘贴即解析，结果可一键复制
（只读展示，不写剪贴板历史）：

- **JSON 参数** — 格式化接口入参，并摊平列出字段结构与数组长度，重复键告警。
- **JWT 解析** — 解码 header / payload 并换算 `exp`/`iat` 为本地时间，标注是否过期。
  **只解码不验签**：本地没有密钥，签名原样列出并明确标注「未校验」。
  兼容 `Bearer ` 前缀、`Authorization:` 头、外层引号、换行折断、标准/URL-safe base64 混用。

工具窗口是**无边框窗口，但不留白**：顶部一条 30px 操作条，左上角是信号条标识 +
**工具名**（`JSON 参数` / `JWT 解析`，次级文字色 + 小一号字号，紧跟一道发丝线），
右侧是**最小化 / 最大化 / 关闭**三个线条图形按钮（无文字）。工具名同时作为窗口标题，
任务栏与 Alt+Tab 显示的也是它。**「输入 / 结果」这类分区小标题仍不出现**——左栏蓝条
= 可编辑输入，右栏青条 = 只读结果；右下角斜纹为缩放抓手。
**左栏按字符折行**（占满宽度自动换行，不出现横向滚动条），**右栏不折行**、保留横向
滚动——结果是给人读的结构化输出，强行折行会破坏缩进层级与列对齐。
底部只有**清空**与**复制结果**两个按钮：左栏本身可编辑，Ctrl+V 直接粘贴即可，不另设
「粘贴」按钮；「复制结果」复制的是右栏所见文本。

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
   属于多余动作，实测把最大化几何 `1920x1032` 冲成 `1160x720`。现在先判
   `is_minimized()`，还原路径跳过 `apply_mode()`。

另外**同一工具只保留一个窗口实例**（`panel._tool_windows`）：ESC 改成最小化后，
最小化的窗口仍挂在任务栏上，若再从工具菜单点一次就新建第二个，用户会看到两个同名
窗口、各存一份输入。现在 `open_tool` 命中实例表就只 `summon()` 唤回；点叉关闭时
`close()` 会把自己从表里摘掉，下次重开得到全新实例。

无边框窗口**收不到键盘焦点时 ESC 不会触发**，所以两处都在显示后 `focus_force()`。

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
