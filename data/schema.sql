CREATE TABLE IF NOT EXISTS clipboard (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK(kind IN ('text','image')),
    content TEXT,
    data BLOB,
    file_path TEXT DEFAULT '',
    file_status TEXT NOT NULL DEFAULT 'none',
    hash TEXT NOT NULL,
    source TEXT DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    pinned INTEGER NOT NULL DEFAULT 0,
    tags TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(category, hash)
);
CREATE INDEX IF NOT EXISTS idx_clipboard_created ON clipboard(created_at);
CREATE INDEX IF NOT EXISTS idx_clipboard_hash ON clipboard(hash);

-- ---------- 密匣：银行资产 ----------
-- 两张表而不是一张：银行是可复用的主体（同一家银行下挂多张存单），
-- 存单是明细。「银行主行 + 存单子行」的两级版式直接对应这个结构。
--
-- **不用外键 + 级联删除**：本项目其余部分都不用外键（sqlite 默认
-- `PRAGMA foreign_keys` 是关的，写了也不生效），混用会让人误以为约束被保证。
-- 删除走显式语句（见 `Database.delete_vault_bank`），删银行时先删它的存单。
--
-- `sort_index` 是**为拖拽排序预留的落点**。本轮（静态版）只写入不读取，
-- 渲染顺序仍按 id。下一轮接拖拽时只需改「重排 sort_index + 按它 ORDER BY」，
-- 不需要再动表结构——表结构改动要迁就已有数据，越晚做代价越大。
CREATE TABLE IF NOT EXISTS vault_bank (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    sort_index INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS vault_deposit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bank_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    principal REAL NOT NULL,
    rate REAL NOT NULL,
    months INTEGER NOT NULL,
    end_date TEXT NOT NULL,
    sort_index INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_vault_deposit_bank ON vault_deposit(bank_id);
CREATE INDEX IF NOT EXISTS idx_vault_deposit_end ON vault_deposit(end_date);
