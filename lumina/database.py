import hashlib
import os
import sqlite3
import threading

FILE_STORE_DIR = "file_store"
FILE_STORE_THRESHOLD = 1 * 1024 * 1024  # 1MB

SCHEMA = """
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
    "CASE WHEN content IS NULL THEN length(data) ELSE length(content) END AS size, "
    "substr(replace(replace(content, char(13), ' '), char(10), ' '), 1, 60) AS preview "
)

JOIN_COLUMNS = (
    "c.id, c.kind, c.category, c.source, c.pinned, c.tags, c.created_at, "
    "CASE WHEN c.content IS NULL THEN length(c.data) "
    "ELSE length(c.content) END AS size, "
    "substr(replace(replace(c.content, char(13), ' '), char(10), ' '), 1, 60) AS preview "
)


class Database:
    def __init__(self, path):
        self.path = path
        self._local = threading.local()
        self._fts = None
        db_dir = os.path.dirname(os.path.abspath(path))
        self._file_store_dir = os.path.join(db_dir, FILE_STORE_DIR)
        os.makedirs(self._file_store_dir, exist_ok=True)

    def _store_file_on_disk(self, clip_id, data):
        """Write file data to disk, return relative path string."""
        ext = ".bin"
        store_path = os.path.join(self._file_store_dir, f"{clip_id}{ext}")
        with open(store_path, "wb") as f:
            f.write(data)
        return os.path.relpath(store_path, os.path.dirname(os.path.abspath(self.path)))

    def _get_file_data(self, row):
        """Get file bytes from either BLOB or file_path. Returns (bytes_or_none, is_stored_on_disk)."""
        data = row["data"]
        file_path = row["file_path"] if "file_path" in row.keys() else ""
        if file_path:
            abs_path = os.path.abspath(os.path.join(
                os.path.dirname(os.path.abspath(self.path)), file_path))
            if os.path.isfile(abs_path):
                with open(abs_path, "rb") as stream:
                    return stream.read(), True
        if data:
            return data, False
        return None, False

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
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    def init_schema(self):
        self.conn.executescript(SCHEMA)
        self._migrate_columns()
        self._migrate_unique_constraint()
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
        for name, definition in (
                ("file_status", "TEXT NOT NULL DEFAULT 'none'"),
                ):
            if name not in cols:
                self.conn.execute(
                    f"ALTER TABLE clipboard ADD COLUMN {name} {definition}")
        if "data" not in cols:
            self.conn.execute("ALTER TABLE clipboard ADD COLUMN data BLOB")
        sources = [name for name in ("image", "file_data") if name in cols]
        if sources:
            self.conn.execute(
                "UPDATE clipboard SET data=COALESCE(" + ",".join(sources) + ") "
                "WHERE data IS NULL")
        for old_name in ("image", "file_data"):
            if old_name in cols:
                try:
                    self.conn.execute(f"ALTER TABLE clipboard DROP COLUMN {old_name}")
                except sqlite3.OperationalError:
                    pass
        for old_name in ("file_name", "file_error"):
            if old_name in cols:
                try:
                    self.conn.execute(f"ALTER TABLE clipboard DROP COLUMN {old_name}")
                except sqlite3.OperationalError:
                    pass
        if "file_size" in cols:
            try:
                self.conn.execute("ALTER TABLE clipboard DROP COLUMN file_size")
            except sqlite3.OperationalError:
                pass
        if "file_path" not in cols:
            self.conn.execute(
                "ALTER TABLE clipboard ADD COLUMN file_path TEXT DEFAULT ''")
        self.conn.commit()

    def _migrate_unique_constraint(self):
        """Add UNIQUE(category, hash) constraint by recreating the table."""
        schema_row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='clipboard'").fetchone()
        if schema_row and "UNIQUE(category, hash)" in schema_row[0]:
            return

        self._backfill_category()

        self.conn.execute("""
            DELETE FROM clipboard
            WHERE id NOT IN (
                SELECT MIN(id) FROM clipboard GROUP BY category, hash
            )
        """)
        self.conn.commit()

        self.conn.execute("PRAGMA legacy_alter_table=ON")
        self.conn.executescript("""
            CREATE TABLE clipboard_new (
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
            INSERT INTO clipboard_new (id, kind, content, data, file_path,
                file_status, hash, source, category, pinned, tags, created_at)
            SELECT id, kind, content, data, file_path,
                file_status, hash, source, category, pinned, tags, created_at
            FROM clipboard;
            DROP TABLE clipboard;
            ALTER TABLE clipboard_new RENAME TO clipboard;
        """)
        self.conn.execute("PRAGMA legacy_alter_table=OFF")

        self.conn.execute(
            "DROP TABLE IF EXISTS clipboard_fts")
        self._fts = None
        self.conn.commit()

    def _backfill_category(self):
        """给旧记录补分类（category 为空的行），幂等，可反复执行。"""
        from .text_classifier import classify_text
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

    def _add(self, kind, category, content, blob, source):
        digest_data = content.encode("utf-8") if content is not None else blob
        h = hashlib.sha256(digest_data).hexdigest()
        try:
            cur = self.conn.execute(
                "INSERT INTO clipboard(kind, category, content, data, hash, "
                "source, created_at) "
                "VALUES (?,?,?,?,?,?,strftime('%Y-%m-%d %H:%M:%f','now','localtime')) "
                "ON CONFLICT(category, hash) DO UPDATE SET "
                "source=excluded.source, "
                "created_at=strftime('%Y-%m-%d %H:%M:%f','now','localtime')",
                (kind, category, content, blob, h, source or ""),
            )
            self.conn.commit()
            row = self.conn.execute(
                "SELECT id FROM clipboard WHERE category=? AND hash=? "
                "ORDER BY id DESC LIMIT 1", (category, h)
            ).fetchone()
            return row["id"]
        except Exception:
            self.conn.rollback()
            raise

    def add_text(self, text, source="", max_bytes=512 * 1024, category=None):
        data = text.encode("utf-8")
        if len(data) > max_bytes:
            text = data[:max_bytes].decode("utf-8", "ignore")
        if category is None:
            from .text_classifier import classify_text
            category = classify_text(text)
        return self._add("text", category, text, None, source)

    def add_image(self, png, source=""):
        return self._add("image", "image", None, png, source)

    def add_files(self, paths, source="", max_bytes=512 * 1024):
        """兼容旧接口：记录一批文件路径，不归档文件内容。"""
        return [self.add_file_pending(path, source) for path in paths]

    def add_file_pending(self, path, source="", status="pending"):
        """Insert file metadata quickly; the worker fills file_data later."""
        path = os.path.abspath(path)
        h = hashlib.sha256(path.encode("utf-8")).hexdigest()
        cur = self.conn.execute(
            "INSERT INTO clipboard(kind, category, content, "
            "file_status, hash, source, created_at) "
            "VALUES ('text', 'file', ?, ?, ?, ?, "
            "strftime('%Y-%m-%d %H:%M:%f','now','localtime'))",
            (path, status, h, source or ""))
        self.conn.commit()
        return cur.lastrowid

    def complete_file(self, clip_id, data):
        digest = hashlib.sha256(data).hexdigest()
        if len(data) > FILE_STORE_THRESHOLD:
            rel_path = self._store_file_on_disk(clip_id, data)
            with self.conn:
                self.conn.execute(
                    "UPDATE clipboard SET data=?, file_path=?, "
                    "file_status='ready', hash=? "
                    "WHERE id=?",
                    (None, rel_path, digest, clip_id))
        else:
            with self.conn:
                self.conn.execute(
                    "UPDATE clipboard SET data=?, file_path='', "
                    "file_status='ready', hash=? "
                    "WHERE id=?",
                    (data, digest, clip_id))

    def fail_file(self, clip_id, error):
        with self.conn:
            self.conn.execute(
                "UPDATE clipboard SET file_status='failed' WHERE id=?", (clip_id,))

    def cleanup(self, retention_days, max_rows):
        deleted = 0
        try:
            old_files = []
            if retention_days and retention_days > 0:
                rows = self.conn.execute(
                    "SELECT id, file_path FROM clipboard WHERE pinned=0 "
                    "AND julianday(created_at) < julianday('now','localtime',?)",
                    (f"-{int(retention_days)} days",),
                ).fetchall()
                for r in rows:
                    if r["file_path"]:
                        old_files.append(r["file_path"])
                cur = self.conn.execute(
                    "DELETE FROM clipboard WHERE pinned=0 "
                    "AND julianday(created_at) < julianday('now','localtime',?)",
                    (f"-{int(retention_days)} days",),
                )
                deleted += cur.rowcount
            if max_rows and max_rows > 0:
                rows = self.conn.execute(
                    "SELECT id, file_path FROM clipboard WHERE pinned=0 AND id NOT IN "
                    "(SELECT id FROM clipboard WHERE pinned=0 "
                    "ORDER BY created_at DESC, id DESC LIMIT ?)",
                    (int(max_rows),),
                ).fetchall()
                for r in rows:
                    if r["file_path"]:
                        old_files.append(r["file_path"])
                cur = self.conn.execute(
                    "DELETE FROM clipboard WHERE pinned=0 AND id NOT IN "
                    "(SELECT id FROM clipboard WHERE pinned=0 "
                    "ORDER BY created_at DESC, id DESC LIMIT ?)",
                    (int(max_rows),),
                )
                deleted += cur.rowcount
            for fp in old_files:
                try:
                    abs_path = os.path.join(
                        os.path.dirname(os.path.abspath(self.path)), fp)
                    if os.path.exists(abs_path):
                        os.remove(abs_path)
                except OSError:
                    pass
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

    def count(self, query="", category=None, pinned=False):
        """Count records for the history status line."""
        clauses = []
        params = []
        query = (query or "").strip()
        if query:
            like = f"%{query}%"
            clauses.append("(content LIKE ? OR tags LIKE ? OR source LIKE ?)")
            params.extend((like, like, like))
        if pinned:
            clauses.append("pinned=1")
        elif category:
            clauses.append("category=?")
            params.append(category)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        return self.conn.execute(
            "SELECT COUNT(*) FROM clipboard" + where, params).fetchone()[0]

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
        # Kept as a compatibility no-op; DELETE journal mode has no WAL checkpoint.
        return None

    def close(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def get(self, clip_id):
        return self.conn.execute(
            "SELECT * FROM clipboard WHERE id=?", (clip_id,)
        ).fetchone()

    def get_text(self, clip_id):
        """文本和文件记录的轻量读取，不加载图片或文件 BLOB。"""
        return self.conn.execute(
            "SELECT id, kind, category, source, pinned, tags, created_at, content, "
            "file_path, file_status, length(data) AS data_size "
            "FROM clipboard WHERE id=?", (clip_id,)
        ).fetchone()

    def get_file_data(self, clip_id):
        row = self.conn.execute(
            "SELECT content AS source_path, file_path, data, file_status "
            "FROM clipboard WHERE id=? AND category='file'", (clip_id,)
        ).fetchone()
        if not row:
            return None

        result = dict(row)
        stored_path = result.get("file_path") or ""
        if stored_path:
            abs_path = os.path.abspath(os.path.join(
                os.path.dirname(os.path.abspath(self.path)), stored_path))
            result["file_path"] = abs_path
            if not result.get("data") and os.path.isfile(abs_path):
                try:
                    with open(abs_path, "rb") as stream:
                        result["data"] = stream.read()
                except OSError:
                    pass
        return result

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
