"""``json_modes`` 的自洽性守卫：模式清单、文案、菜单项必须互相对得上。

这个模块是 JSON 下拉的**唯一定义处**。它一旦自相矛盾（例如添了模式却忘了
补 ``MODE_LABELS``），症状是运行时 ``KeyError`` —— 而两侧的 UI 测试只能覆盖
各自走到的那几条路径，覆盖不到「模式清单本身是否完整」。所以这里单独守。

没有一条测试去比对「两侧文案是否一致」——那是上一代的做法（两份字典 +
交叉断言）。现在两侧引用的是同一个对象，交叉比对成了同义反复。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lumina import json_modes, json_tools  # noqa: E402


class ModeDefinitionTests(unittest.TestCase):
    def test_every_declared_mode_is_transformable(self):
        """``json_modes`` 里出现的每个模式，``transform_json`` 都得能处理。

        ``transform_json`` 对非法模式是**抛 ``ValueError``**（不是返回带
        ``error`` 字段的结果），所以这里不捕获就代表通过。
        """
        for mode in json_modes.MODE_ORDER:
            with self.subTest(mode=mode):
                result = json_tools.transform_json('{"a":1}', mode)
                self.assertTrue(result.text)

    def test_mode_set_matches_json_tools(self):
        """``json_tools.MODES`` 与 ``json_modes.MODE_ORDER`` 是同一个集合。

        两处各有用途：前者是「哪些值能通过校验」，后者是「按什么顺序摆」。
        集合不一致说明有一边漏改。
        """
        self.assertEqual(set(json_modes.MODE_ORDER), set(json_tools.MODES))
        self.assertEqual(len(json_modes.MODE_ORDER), len(json_tools.MODES),
                         'MODE_ORDER 里有重复项')

    def test_every_mode_has_label_item_and_action(self):
        for mode in json_modes.MODE_ORDER:
            with self.subTest(mode=mode):
                self.assertIn(mode, json_modes.MODE_LABELS)
                self.assertIn(mode, json_modes.MODE_ITEMS)
                self.assertIn(mode, json_modes.ACTION_LABELS)
                label, hint, kind = json_modes.MODE_ITEMS[mode]
                self.assertTrue(label)
                self.assertTrue(hint, f'{mode} 的 hint 为空，菜单里会空一块')
                # kind 是 JsonPopup._icon 的图标键，也用于「恢复原文」的分隔线
                # 判定，必须与模式名一一对应，不能复用一个通用值。
                self.assertEqual(kind, mode)

    def test_no_stale_entries_for_removed_modes(self):
        """反向：字典里不能残留已不在清单里的模式（改名后常见）。"""
        declared = set(json_modes.MODE_ORDER)
        for name in ('MODE_LABELS', 'MODE_ITEMS', 'ACTION_LABELS'):
            with self.subTest(mapping=name):
                self.assertEqual(set(getattr(json_modes, name)), declared)

    def test_state_labels_and_action_labels_are_not_merged(self):
        """结果态名与动作名措辞不同，**不能为了少写一个字典合并**。

        菜单里写「已格式化」会读成「已经是这个状态了」，状态栏写「已格式化
        JSON」才通顺。这两组值刻意不同，合并即歧义。
        """
        different = [m for m in json_modes.MODE_ORDER
                     if json_modes.MODE_LABELS[m] != json_modes.ACTION_LABELS[m]]
        self.assertTrue(different, '两组文案退化成一样了，说明有人合并了它们')
        self.assertTrue(all(json_modes.MODE_LABELS[m].startswith('已')
                            for m in json_modes.MODE_ORDER),
                        '结果态名应以「已」开头')


class BuildMenuItemsTests(unittest.TestCase):
    def _items(self, **kwargs):
        return json_modes.build_menu_items(
            lambda mode: None, lambda: None, kwargs.pop('active_mode', None),
            **kwargs)

    def test_order_and_labels(self):
        items = self._items()
        self.assertEqual([i['label'] for i in items],
                         [json_modes.MODE_ITEMS[m][0]
                          for m in json_modes.MODE_ORDER]
                         + [json_modes.RESTORE_LABEL])
        self.assertEqual([i['kind'] for i in items],
                         [*json_modes.MODE_ORDER, 'restore'])

    def test_restore_is_last_and_separated_by_kind(self):
        """「恢复原文」固定最后一项，``kind='restore'`` 是分隔线的唯一依据。

        ``JsonPopup`` 不靠下标判断——靠下标的话，加模式就要去改弹窗代码。
        """
        items = self._items()
        self.assertEqual(items[-1]['label'], json_modes.RESTORE_LABEL)
        self.assertEqual(items[-1]['kind'], 'restore')
        self.assertEqual([i['kind'] for i in items].count('restore'), 1)

    def test_selected_marks_active_mode_only(self):
        for active in json_modes.MODE_ORDER:
            with self.subTest(active=active):
                items = self._items(active_mode=active)
                self.assertEqual([i['selected'] for i in items],
                                 [m == active for m in json_modes.MODE_ORDER]
                                 + [False])
        self.assertEqual([i['selected'] for i in self._items()],
                         [False] * (len(json_modes.MODE_ORDER) + 1))

    def test_busy_disables_only_the_transforms(self):
        """``busy`` 只关转换项，不动「恢复原文」。

        恢复是新一代请求的起点（详情栏靠它递增请求序号取消在途工作），
        busy 时禁掉反而让用户卡住。
        """
        items = self._items(busy=True, can_restore=True)
        self.assertEqual([i['enabled'] for i in items],
                         [False] * len(json_modes.MODE_ORDER) + [True])

    def test_can_restore_gates_the_last_item_independently(self):
        """``can_restore`` 与 ``busy`` 是两个独立开关，不能复用一个参数。

        工具弹框从不 busy，但仍需在「还没转换过」时禁用恢复项。
        """
        items = self._items(busy=False, can_restore=False)
        self.assertEqual([i['enabled'] for i in items],
                         [True] * len(json_modes.MODE_ORDER) + [False])
        items = self._items(busy=False, can_restore=True)
        self.assertEqual([i['enabled'] for i in items],
                         [True] * len(json_modes.MODE_ORDER) + [True])

    def test_commands_call_the_right_handler_with_the_right_mode(self):
        """闭包绑定必须绑**当前** mode，不能是循环残留的最后一个值。"""
        called = []
        items = json_modes.build_menu_items(
            called.append, lambda: called.append('restore'), None)
        for item in items:
            item['command']()
        self.assertEqual(called, [*json_modes.MODE_ORDER, 'restore'])

    def test_items_are_fresh_objects_each_call(self):
        """每次调用返回新的 items 与新的闭包，共用会串味。

        详情栏每次弹菜单都重新构造；若复用同一批 dict，``selected`` 会被
        上一次的状态拖着走。
        """
        a = self._items(active_mode='format')
        b = self._items(active_mode='compress')
        self.assertIsNot(a[0], b[0])
        self.assertNotEqual([i['selected'] for i in a],
                            [i['selected'] for i in b])


if __name__ == '__main__':
    unittest.main()
