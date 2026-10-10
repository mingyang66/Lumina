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
- `--clean`：清理 PyInstaller 的临时缓存后再开始构建，避免旧缓存影响打包结果。
- `--noconfirm`：自动确认覆盖并删除已有的输出目录，不再显示确认提示。
- `--windowed`：生成窗口程序，运行 GUI 应用时不显示命令行窗口。该参数也可以写成 `--noconsole`。
- `--onefile`：将程序及其依赖打包成一个独立的 `Lumina.exe` 文件。
- `--name "Lumina"`：将生成的应用程序命名为 `Lumina`。
- `--add-data "config.json;."`：将 `config.json` 一起复制到打包程序的顶层目录。Windows 使用分号分隔源文件和目标目录。
- `--hidden-import`：显式包含动态导入的 `tkinter`、`_tkinter` 和 Pillow 模块。
- `main.py`：指定应用程序的入口文件。

打包结果默认生成在 `dist/` 目录中，可执行文件为 `dist/Lumina.exe`。

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
