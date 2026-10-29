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
- 当前用例数：356 项通过、2 项跳过（跳过项依赖符号链接权限）。

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
