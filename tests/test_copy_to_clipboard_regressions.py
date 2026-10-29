"""copy_to_clipboard 必须使用调用方传入的 content（JSON 格式化结果）。"""
import io
import sqlite3
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lumina import clipboard_api  # noqa: E402
from lumina.application import LuminaApp  # noqa: E402

SCHEMA = """CREATE TABLE clipboard (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL, content TEXT, data BLOB,
    file_path TEXT DEFAULT '', file_status TEXT NOT NULL DEFAULT 'none',
    hash TEXT NOT NULL DEFAULT '', source TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '', pinned INTEGER NOT NULL DEFAULT 0,
    tags TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT ''
)"""


def make_app(row_sql, params=()):
    """构造只带 db/listener 的最小 LuminaApp。"""
    app = LuminaApp.__new__(LuminaApp)
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA)
    if row_sql:
        conn.execute(row_sql, params)
        conn.commit()
    app.db = types.SimpleNamespace(
        get=lambda cid: conn.execute(
            'SELECT * FROM clipboard WHERE id=?', (cid,)).fetchone(),
        get_file_data=lambda cid: None,
    )
    app._conn = conn
    app.written = []

    def write_clipboard(writer, payload):
        app.written.append((writer, payload))
        return True

    app.listener = types.SimpleNamespace(write_clipboard=write_clipboard)
    return app


class CopyUsesDisplayedContentTests(unittest.TestCase):
    def setUp(self):
        self.app = make_app(
            "INSERT INTO clipboard (kind, content, category) VALUES ('text', ?, 'code')",
            ('{"a":1,"b":[2,3]}',))

    def tearDown(self):
        self.app._conn.close()

    def test_formatted_content_is_copied_instead_of_db_original(self):
        # 详情区把 JSON 格式化结果放进临时 row 交给动作层。
        formatted = '{\n  "a": 1,\n  "b": [\n    2,\n    3\n  ]\n}'
        payload = {'id': 1, 'kind': 'text', 'category': 'code',
                   'content': formatted, 'data': None, 'file_path': ''}
        self.assertTrue(self.app.copy_to_clipboard(payload))
        writer, copied = self.app.written[-1]
        self.assertIs(writer, clipboard_api.set_clipboard_text)
        # 关键：复制到剪贴板的是格式化后的文本，不是库里的原文。
        self.assertEqual(copied, formatted)
        self.assertNotEqual(copied, '{"a":1,"b":[2,3]}')

    def test_compressed_content_is_copied(self):
        self.app._conn.execute(
            "UPDATE clipboard SET content=? WHERE id=1",
            (' { "a" : 1 , "b" : [ 2 , 3 ] } ',))
        self.app._conn.commit()
        payload = {'id': 1, 'kind': 'text', 'category': 'code',
                   'content': '{"a":1,"b":[2,3]}', 'data': None, 'file_path': ''}
        self.assertTrue(self.app.copy_to_clipboard(payload))
        self.assertEqual(self.app.written[-1][1], '{"a":1,"b":[2,3]}')

    def test_unchanged_content_still_copies_and_row_shape_unchanged(self):
        # content 与库中一致时不应多做转换，行为与修改前完全一致。
        payload = {'id': 1, 'kind': 'text', 'category': 'code',
                   'content': '{"a":1,"b":[2,3]}', 'data': None, 'file_path': ''}
        self.assertTrue(self.app.copy_to_clipboard(payload))
        self.assertEqual(self.app.written[-1][1], '{"a":1,"b":[2,3]}')

    def test_missing_id_uses_passed_content(self):
        # id 为空（diff 窗口的既有用法）时不查库，直接用传入内容。
        payload = {'id': None, 'kind': 'text', 'category': 'text',
                   'content': '来自差异窗口的文本', 'data': None, 'file_path': ''}
        self.assertTrue(self.app.copy_to_clipboard(payload))
        self.assertEqual(self.app.written[-1][1], '来自差异窗口的文本')

    def test_empty_content_falls_back_to_empty_string_not_none(self):
        payload = {'id': None, 'kind': 'text', 'category': 'text',
                   'content': None, 'data': None, 'file_path': ''}
        self.assertTrue(self.app.copy_to_clipboard(payload))
        self.assertEqual(self.app.written[-1][1], '')

    def test_image_row_still_copies_as_dib(self):
        from PIL import Image
        buf = io.BytesIO()
        Image.new('RGB', (4, 4), 'red').save(buf, 'PNG')
        self.app._conn.execute(
            "INSERT INTO clipboard (kind, content, data, category) "
            "VALUES ('image', '', ?, 'image')", (buf.getvalue(),))
        self.app._conn.commit()
        payload = {'id': 2, 'kind': 'image', 'category': 'image',
                   'content': '', 'data': None, 'file_path': ''}
        self.assertTrue(self.app.copy_to_clipboard(payload))
        writer, copied = self.app.written[-1]
        self.assertIs(writer, clipboard_api.set_clipboard_dib)
        self.assertTrue(copied)

    def test_content_override_does_not_break_image(self):
        # 即便误传了派生 content，图片记录仍应按图片处理。
        from PIL import Image
        buf = io.BytesIO()
        Image.new('RGB', (2, 2), 'blue').save(buf, 'PNG')
        self.app._conn.execute(
            "INSERT INTO clipboard (kind, content, data, category) "
            "VALUES ('image', '', ?, 'image')", (buf.getvalue(),))
        self.app._conn.commit()
        payload = {'id': 2, 'kind': 'image', 'category': 'image',
                   'content': '不该用于图片', 'data': None, 'file_path': ''}
        self.assertTrue(self.app.copy_to_clipboard(payload))
        self.assertIs(self.app.written[-1][0], clipboard_api.set_clipboard_dib)


if __name__ == '__main__':
    unittest.main()
