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
