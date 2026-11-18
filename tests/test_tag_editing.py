"""标签功能的回归测试：归一化、清空、检索联动、不越权改动其它字段。

详情栏的「＋标签」入口与保存/取消依赖 Tk 主循环，这里只覆盖不启动 Tk
的那一半；接线本身由 ``panel_detail.DetailPane._commit_tags`` 调到这里测的
``Database.set_tags``。
"""

import tempfile
import unittest
from pathlib import Path

from lumina.database import Database


class TagEditingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Database(str(self.root / "history.db"))
        self.db.init_schema()
        self.clip = self.db.add_text("hello 标签测试", source="test")

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def tags(self, clip_id=None):
        row = self.db.conn.execute(
            "SELECT tags FROM clipboard WHERE id=?",
            (self.clip if clip_id is None else clip_id,)).fetchone()
        return row["tags"]

    def test_set_tags_normalizes_fullwidth_comma_and_whitespace(self):
        self.assertTrue(self.db.set_tags(self.clip, "  接口 ， 待办 ,, "))
        self.assertEqual(self.tags(), "接口,待办")

    def test_empty_text_clears_tags(self):
        self.db.set_tags(self.clip, "临时")
        self.assertEqual(self.tags(), "临时")
        self.assertTrue(self.db.set_tags(self.clip, "   "))
        self.assertEqual(self.tags(), "")

    def test_unknown_id_reports_failure(self):
        missing = int(self.clip) + 999
        self.assertFalse(self.db.set_tags(missing, "随便"))

    def test_tags_are_searchable(self):
        self.db.set_tags(self.clip, "发票,报销")
        hits = [row["id"] for row in self.db.search("发票", 20)]
        self.assertIn(self.clip, hits)
        # 反例：没打过的词不应命中
        self.assertNotIn(self.clip, [row["id"] for row in self.db.search("不存在的标签词", 20)])

    def test_set_tags_keeps_content_and_pin_state(self):
        self.db.set_pinned(self.clip, True)
        before = self.db.conn.execute(
            "SELECT content, pinned FROM clipboard WHERE id=?", (self.clip,)).fetchone()
        self.db.set_tags(self.clip, "保留")
        after = self.db.conn.execute(
            "SELECT content, pinned FROM clipboard WHERE id=?", (self.clip,)).fetchone()
        self.assertEqual(after["content"], before["content"])
        self.assertEqual(after["pinned"], before["pinned"])


if __name__ == "__main__":
    unittest.main()