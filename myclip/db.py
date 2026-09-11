import hashlib
import os
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS clipboard (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK(kind IN ('text','image')),
    content TEXT,
    image BLOB,
    hash TEXT NOT NULL,
    source TEXT DEFAULT '',
    pinned INTEGER NOT NULL DEFAULT 0,
    tags TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_clipboard_created ON clipboard(created_at);
CREATE INDEX IF NOT EXISTS idx_clipboard_hash ON clipboard(hash);
"""

FTS_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS clipboard_ai AFTER INSERT ON clipboard BEGIN
    INSERT INTO clipboard_fts(rowid, content, tags, source)
    VALUES (new.id, new.content, new.tags, new.source);
END;
CREATE TRIGGER IF NOT EXISTS clipboard_ad AFTER DELETE ON clipboard BEGIN
    INSERT INTO clipboard_fts(clipboard_fts, rowid, content, tags, source)
    VALUES ('delete', old.id, old.content, old.tags, old.source);
END;
CREATE TRIGGER IF NOT EXISTS clipboard_au AFTER UPDATE OF content, tags, source ON clipboard BEGIN
    INSERT INTO clipboard_fts(clipboard_fts, rowid, content, tags, source)
    VALUES ('delete', old.id, old.content, old.tags, old.source);
    INSERT INTO clipboard_fts(rowid, content, tags, source)
    VALUES (new.id, new.content, new.tags, new.source);
END;
"""

LIST_COLUMNS = (
    "id, kind, category, source, pinned, tags, created_at, "
    "CASE WHEN content IS NULL THEN length(image) ELSE length(content) END AS size, "
    "substr(replace(replace(content, char(13), ' '), char(10), ' '), 1, 60) AS preview "
)

JOIN_COLUMNS = (
    "c.id, c.kind, c.category, c.source, c.pinned, c.tags, c.created_at, "
    "CASE WHEN c.content IS NULL THEN length(c.image) "
    "ELSE length(c.content) END AS size, "
    "substr(replace(replace(c.content, char(13), ' '), char(10), ' '), 1, 60) AS preview "
)


class Database:
    def __init__(self, path):
        self.path = path
        self._local = threading.local()
        self._fts = None  # None=uninitialized, False=unavailable, True=available

    @property
    def conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            conn = sqlite3.connect(self.path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA temp_store=MEMORY")
            conn.execute("PRAGMA wal_autocheckpoint=250")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    def init_schema(self):
        self.conn.executescript(SCHEMA)
        self._migrate_columns()
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_clipboard_pinned ON clipboard(pinned)")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_clipboard_category ON clipboard(category)")
        self._backfill_category()
        self._init_fts()
        self.conn.commit()

    def _migrate_columns(self):
        cols = {r["name"] for r in
                self.conn.execute("PRAGMA table_info(clipboard)").fetchall()}
        if "pinned" not in cols:
            self.conn.execute(
                "ALTER TABLE clipboard ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
        if "tags" not in cols:
            self.conn.execute(
                "ALTER TABLE clipboard ADD COLUMN tags TEXT NOT NULL DEFAULT ''")
        if "category" not in cols:
            # 分类列：text/code/link/image/file。kind 保留存储语义(text/image)
            # 不动 CHECK 约束，避免整表重建。
            self.conn.execute(
                "ALTER TABLE clipboard ADD COLUMN category TEXT NOT NULL DEFAULT ''")
        self.conn.commit()

    def _backfill_category(self):
        """给旧记录补分类（category 为空的行），幂等，可反复执行。"""
        from .classify import classify_text
        rows = self.conn.execute(
            "SELECT id, kind, content FROM clipboard "
            "WHERE category IS NULL OR category=''").fetchall()
        if not rows:
            return 0
        for r in rows:
            if r["kind"] == "text" and r["content"] and "\\" in r["content"]:
                cat = "file"
            elif r["kind"] == "image":
                cat = "image"
            else:
                cat = classify_text(r["content"] or "")
            self.conn.execute("UPDATE clipboard SET category=? WHERE id=?",
                              (cat, r["id"]))
        self.conn.commit()
        return len(rows)

    def _init_fts(self):
        if self._fts is not None:
            return
        token = None
        for candidate in ("trigram", "unicode61"):
            try:
                self.conn.execute(
                    "CREATE VIRTUAL TABLE temp.fts_probe_tmp "
                    f"USING fts5(x, tokenize='{candidate}')")
                self.conn.execute("DROP TABLE temp.fts_probe_tmp")
                token = candidate
                break
            except sqlite3.OperationalError:
                continue
        if token is None:
            self._fts = False
            return
        try:
            has_fts = self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='clipboard_fts'").fetchone()
            if not has_fts:
                self.conn.execute(
                    "CREATE VIRTUAL TABLE clipboard_fts USING fts5("
                    "content, tags, source, content='clipboard', content_rowid='id', "
                    f"tokenize='{token}')")
                self.conn.execute(
                    "INSERT INTO clipboard_fts(clipboard_fts) VALUES('rebuild')")
            self.conn.executescript(FTS_TRIGGERS)
            self._fts = True
        except sqlite3.OperationalError:
            self._fts = False

    def _add(self, kind, category, content, image, source):
        data = content.encode("utf-8") if content is not None else image
        h = hashlib.sha256(data).hexdigest()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            row = self.conn.execute(
                "SELECT id FROM clipboard WHERE category=? AND hash=? "
                "ORDER BY id DESC LIMIT 1", (category, h)
            ).fetchone()
            if row:
                self.conn.execute(
                    "UPDATE clipboard SET source=?, "
                    "created_at=strftime('%Y-%m-%d %H:%M:%f','now','localtime') "
                    "WHERE id=?",
                    (source or "", row["id"]),
                )
                self.conn.commit()
                return row["id"]
            cur = self.conn.execute(
                "INSERT INTO clipboard(kind, category, content, image, hash, "
                "source, created_at) "
                "VALUES (?,?,?,?,?,?,strftime('%Y-%m-%d %H:%M:%f','now','localtime'))",
                (kind, category, content, image, h, source or ""),
            )
            self.conn.commit()
            return cur.lastrowid
        except Exception:
            self.conn.rollback()
            raise

    def add_text(self, text, source="", max_bytes=512 * 1024, category=None):
        data = text.encode("utf-8")
        if len(data) > max_bytes:
            text = data[:max_bytes].decode("utf-8", "ignore")
        if category is None:
            from .classify import classify_text
            category = classify_text(text)
        return self._add("text", category, text, None, source)

    def add_image(self, png, source=""):
        return self._add("image", "image", None, png, source)

    def add_files(self, paths, source="", max_bytes=512 * 1024):
        """记录资源管理器复制的文件（存路径列表，kind 仍为 text）。"""
        content = "\n".join(paths)
        data = content.encode("utf-8")
        if len(data) > max_bytes:
            content = data[:max_bytes].decode("utf-8", "ignore")
        return self._add("text", "file", content, None, source)

    def cleanup(self, retention_days, max_rows):
        deleted = 0
        try:
            if retention_days and retention_days > 0:
                cur = self.conn.execute(
                    "DELETE FROM clipboard WHERE pinned=0 "
                    "AND julianday(created_at) < julianday('now','localtime',?)",
                    (f"-{int(retention_days)} days",),
                )
                deleted += cur.rowcount
            if max_rows and max_rows > 0:
                cur = self.conn.execute(
                    "DELETE FROM clipboard WHERE pinned=0 AND id NOT IN "
                    "(SELECT id FROM clipboard WHERE pinned=0 "
                    "ORDER BY created_at DESC, id DESC LIMIT ?)",
                    (int(max_rows),),
                )
                deleted += cur.rowcount
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        self.checkpoint()
        return deleted

    def list_recent(self, n=20):
        return self.conn.execute(
            f"SELECT {LIST_COLUMNS} FROM clipboard "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (n,),
        ).fetchall()

    def search(self, query, n=20):
        query = (query or "").strip()
        if not query:
            return []
        if self._fts and len(query) >= 3:
            match = '"' + query.replace('"', '""') + '"'
            return self.conn.execute(
                f"SELECT {JOIN_COLUMNS} "
                "FROM clipboard_fts f JOIN clipboard c ON c.id = f.rowid "
                "WHERE clipboard_fts MATCH ? "
                "ORDER BY c.created_at DESC, c.id DESC LIMIT ?",
                (match, n),
            ).fetchall()
        like = f"%{query}%"
        return self.conn.execute(
            f"SELECT {LIST_COLUMNS} FROM clipboard "
            "WHERE content LIKE ? OR tags LIKE ? OR source LIKE ? "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (like, like, like, n),
        ).fetchall()

    def set_pinned(self, clip_id, pinned=True):
        with self.conn:
            cur = self.conn.execute(
                "UPDATE clipboard SET pinned=? WHERE id=?",
                (1 if pinned else 0, clip_id))
        return cur.rowcount > 0

    def set_tags(self, clip_id, tags):
        norm = ",".join(t.strip() for t in str(tags).replace("\uff0c", ",").split(",")
                        if t.strip())
        with self.conn:
            cur = self.conn.execute(
                "UPDATE clipboard SET tags=? WHERE id=?", (norm, clip_id))
        return cur.rowcount > 0

    def delete(self, clip_id):
        with self.conn:
            cur = self.conn.execute("DELETE FROM clipboard WHERE id=?", (clip_id,))
        return cur.rowcount > 0

    def checkpoint(self):
        try:
            self.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except sqlite3.OperationalError:
            pass

    def close(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def get(self, clip_id):
        return self.conn.execute(
            "SELECT * FROM clipboard WHERE id=?", (clip_id,)
        ).fetchone()

    def stats(self):
        rows = self.conn.execute(
            "SELECT CASE WHEN category IS NULL OR category='' THEN kind "
            "ELSE category END AS cat, "
            "COUNT(*) AS cnt, MIN(created_at) AS oldest, "
            "MAX(created_at) AS newest FROM clipboard GROUP BY cat"
        ).fetchall()
        pinned = self.conn.execute(
            "SELECT COUNT(*) AS cnt FROM clipboard WHERE pinned=1").fetchone()["cnt"]
        size = sum(
            os.path.getsize(self.path + suffix)
            for suffix in ("", "-wal", "-shm")
            if os.path.exists(self.path + suffix)
        )
        return rows, size, pinned
