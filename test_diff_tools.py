import dataclasses
import json
from difflib import SequenceMatcher
import random
import unittest
from unittest.mock import patch

from lumina import diff_tools, json_tools
from lumina.diff_tools import AlignedRow, DiffResult, compare_text


class DiffToolsTests(unittest.TestCase):
    def assert_reconstructs(self, result):
        self.assertEqual(''.join(row.left_text + row.left_eol for row in result.rows
                                 if row.left_no is not None), result.left_text)
        self.assertEqual(''.join(row.right_text + row.right_eol for row in result.rows
                                 if row.right_no is not None), result.right_text)
        for side in ('left', 'right'):
            numbers = [getattr(row, side + '_no') for row in result.rows
                       if getattr(row, side + '_no') is not None]
            self.assertEqual(numbers, list(range(1, len(numbers) + 1)))
            for row in result.rows:
                text = getattr(row, side + '_text')
                for start, end in getattr(row, side + '_spans'):
                    self.assertTrue(0 <= start < end <= len(text))
                self.assertNotIn('\n', text)
                self.assertNotIn('\r', text)

    def test_empty_and_immutable_contract(self):
        result = compare_text('', '')
        self.assertIsInstance(result, DiffResult)
        self.assertEqual(result.rows, ())
        self.assertEqual(result.blocks, ())
        self.assertEqual(result.unified_diff, '')
        self.assertTrue(result.exact_equal)
        self.assertTrue(result.rule_equal)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.left_text = 'x'
        row = compare_text('中文', '中文').rows[0]
        self.assertIsInstance(row, AlignedRow)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            row.kind = 'replace'

    def test_chinese_character_spans(self):
        result = compare_text('你好世界\n', '你好地球\n')
        row = result.rows[0]
        self.assertEqual((row.kind, row.left_spans, row.right_spans),
                         ('replace', ((2, 4),), ((2, 4),)))
        self.assertEqual((row.left_no, row.right_no), (1, 1))
        self.assertEqual(result.blocks, (0,))
        self.assert_reconstructs(result)

    def test_character_insert_and_delete(self):
        row = compare_text('中文', '中间文').rows[0]
        self.assertEqual((row.left_spans, row.right_spans), ((), ((1, 2),)))
        row = compare_text('中间文', '中文').rows[0]
        self.assertEqual((row.left_spans, row.right_spans), (((1, 2),), ()))

    def test_line_insert_delete_replace_and_blocks(self):
        result = compare_text('a\nb\nc\nd\ne\n', 'a\nx\nc\ny\ne\nextra')
        self.assertEqual([row.kind for row in result.rows],
                         ['equal', 'replace', 'equal', 'replace', 'equal', 'insert'])
        self.assertEqual(result.blocks, (1, 3, 5))
        self.assertEqual(result.rows[-1].left_no, None)
        self.assertEqual(result.rows[-1].right_spans, ((0, 5),))
        deleted = compare_text('a\nb\nc\n', 'a\n')
        self.assertEqual([row.kind for row in deleted.rows], ['equal', 'delete', 'delete'])
        self.assertEqual(deleted.blocks, (1,))
        self.assert_reconstructs(result)
        self.assert_reconstructs(deleted)

    def test_empty_line_and_final_terminator_are_not_lost(self):
        for left, right in (('', '\n'), ('', '\r\n'), ('a', 'a\n'),
                            ('a\n', 'a\n\n'), ('\n', '\n\n'), ('a\r', 'a')):
            for ignored in (False, True):
                with self.subTest(left=left, right=right, ignored=ignored):
                    result = compare_text(left, right, ignore_line_endings=ignored)
                    self.assertFalse(result.exact_equal)
                    self.assertFalse(result.rule_equal)
                    self.assertTrue(result.blocks)
                    self.assert_reconstructs(result)
        self.assertEqual(len(compare_text('a\n', 'a\n').rows), 1)
        self.assertEqual(compare_text('', '\n').rows[0].kind, 'insert')
        self.assertEqual(compare_text('\n', '').rows[0].kind, 'delete')

    def test_eol_only_replace_has_hint_without_fake_spans(self):
        for left_eol in ('', '\n', '\r', '\r\n'):
            for right_eol in ('', '\n', '\r', '\r\n'):
                with self.subTest(left=left_eol, right=right_eol):
                    result = compare_text('相同' + left_eol, '相同' + right_eol)
                    row = result.rows[0]
                    self.assertEqual(row.kind, 'equal' if left_eol == right_eol else 'replace')
                    self.assertEqual((row.left_eol, row.right_eol), (left_eol, right_eol))
                    self.assertEqual((row.left_spans, row.right_spans), ((), ()))
                    self.assert_reconstructs(result)

    def test_non_eol_unicode_separators_remain_text(self):
        text = 'a\vb\fc\x85d\u2028e\u2029f'
        result = compare_text(text, text)
        self.assertEqual(len(result.rows), 1)
        self.assertEqual(result.rows[0].left_text, text)

    def test_whitespace_rules_and_original_copies(self):
        left, right = '中文 \t\r\n尾\t\r', '中文\n尾\n'
        result = compare_text(left, right, True, True)
        self.assertFalse(result.exact_equal)
        self.assertTrue(result.rule_equal)
        self.assertEqual((result.left_text, result.right_text), (left, right))
        self.assertEqual([row.kind for row in result.rows], ['replace', 'replace'])
        self.assertEqual(result.blocks, (0,))
        self.assertIn('normalized copies', result.unified_diff)
        self.assertNotIn('@@', result.unified_diff)
        self.assertFalse(compare_text(left, right, True).rule_equal)
        self.assertFalse(compare_text(left, right, ignore_line_endings=True).rule_equal)
        for left, right in ((' x', 'x'), ('x y', 'xy'), ('x\u00a0', 'x')):
            self.assertFalse(compare_text(left, right, True).rule_equal)
        self.assertTrue(compare_text(' \t\n', '\n', True).rule_equal)
        self.assert_reconstructs(result)

    def test_normalized_export_is_not_original_patch(self):
        result = compare_text('a \r\nold\t', 'a\nnew ', True, True)
        self.assertEqual(result.unified_diff,
                         '# 规范化副本差异（normalized copies）：不可作为原文 patch。\n'
                         '--- left\n+++ right\n@@ -1,2 +1,2 @@\n a\n'
                         '-old\n\\ No newline at end of file\n'
                         '+new\n\\ No newline at end of file\n')

    def test_unified_final_newline_markers_exact(self):
        cases = [
            ('a', 'a\n', '--- left\n+++ right\n@@ -1 +1 @@\n-a\n\\ No newline at end of file\n+a\n'),
            ('a\n', 'a', '--- left\n+++ right\n@@ -1 +1 @@\n-a\n+a\n\\ No newline at end of file\n'),
            ('', 'x', '--- left\n+++ right\n@@ -0,0 +1 @@\n+x\n\\ No newline at end of file\n'),
            ('x', '', '--- left\n+++ right\n@@ -1 +0,0 @@\n-x\n\\ No newline at end of file\n'),
            ('a\r\n', 'a\n', '--- left\n+++ right\n@@ -1 +1 @@\n-a\r\n+a\n'),
            ('a\r', 'b\r', '--- left\n+++ right\n@@ -1 +1 @@\n-a\r+b\r'),
            ('a\ntail', 'b\ntail', '--- left\n+++ right\n@@ -1,2 +1,2 @@\n-a\n+b\n tail\n\\ No newline at end of file\n'),
        ]
        for left, right, expected in cases:
            with self.subTest(left=left, right=right):
                self.assertEqual(compare_text(left, right).unified_diff, expected)

    def test_unified_separated_hunks(self):
        left = ''.join(f'{i}\n' for i in range(20))
        right = left.replace('1\n', 'x\n', 1).replace('18\n', 'y\n', 1)
        result = compare_text(left, right)
        self.assertIn('@@ -1,5 +1,5 @@\n', result.unified_diff)
        self.assertIn('@@ -16,5 +16,5 @@\n', result.unified_diff)
        self.assertEqual(result.blocks, (1, 18))

    def test_json_copies_validate_both_sides_and_preserve_tokens(self):
        number = '9' * 3000
        left = '{ "n" : ' + number + ', "n":1E+009, "tiny":-0.00000000000001 }'
        right = '{"n":' + number + ',"n":1E+009,"tiny":-0.00000000000001}'
        original = (left, right)
        result = compare_text(left, right, json_format=True)
        self.assertEqual((left, right), original)
        self.assertFalse(result.exact_equal)
        self.assertTrue(result.rule_equal)
        self.assertEqual(result.left_duplicate_keys, ('n',))
        self.assertEqual(result.right_duplicate_keys, ('n',))
        self.assertEqual(result.left_text, json_tools.transform_json(left).text)
        self.assertIn(number, result.left_text)
        self.assertIn('1E+009', result.left_text)
        self.assertIn('normalized copies', result.unified_diff)
        self.assert_reconstructs(result)
        for left, right in (('NaN', '{}'), ('{}', '[1,]'), ('NaN', 'Infinity')):
            with self.subTest(left=left, right=right):
                with patch.object(json_tools, 'transform_json', wraps=json_tools.transform_json) as transform:
                    with self.assertRaises(ValueError) as caught:
                        compare_text(left, right, json_format=True)
                    self.assertEqual(transform.call_count, 2)
                    if left == 'NaN':
                        self.assertIn('左侧 JSON', str(caught.exception))
                    if right != '{}':
                        self.assertIn('右侧 JSON', str(caught.exception))

    def test_json_duplicate_keys_and_numeric_lexemes_are_not_collapsed(self):
        for left, right in (('{"a":1,"a":2}', '{"a":2}'),
                            ('1.0', '1'), ('-0', '0'), ('1E+009', '1e9'),
                            ('{"b":1,"a":2}', '{"a":2,"b":1}')):
            with self.subTest(left=left, right=right):
                self.assertFalse(compare_text(left, right, json_format=True).rule_equal)

    def test_input_type_and_character_limit(self):
        for left, right in ((None, ''), ('', b'x'), (1, 2)):
            with self.assertRaises(TypeError):
                compare_text(left, right)
        text = '中' * diff_tools.MAX_CHARACTERS
        self.assertTrue(compare_text(text, text).exact_equal)
        for left, right in ((text + 'x', ''), ('', text + 'x')):
            with self.assertRaisesRegex(ValueError, '1 MiB'):
                compare_text(left, right)

    def test_total_line_limit_including_json_copies(self):
        text = 'x\n' * 5000
        self.assertEqual(len(compare_text(text, text).rows), 5000)
        for left, right in ((text + 'x', text), ('x\n' * 10001, '')):
            with self.assertRaisesRegex(ValueError, '10000'):
                compare_text(left, right)
        with self.assertRaisesRegex(ValueError, '10000'):
            compare_text('[{}' + ',{}' * 9999 + ']', '[]', json_format=True)
        with patch.object(json_tools, 'transform_json', return_value=json_tools.JsonTransformResult('x' * (diff_tools.MAX_CHARACTERS + 1), ())):
            with self.assertRaisesRegex(ValueError, '有效副本'):
                compare_text('{}', '{}', json_format=True)

    def test_repeated_lines_fail_explicitly_instead_of_autojunk(self):
        left = 'a\n' * 3000 + 'left\n'
        right = 'a\n' * 3000 + 'right\n'
        with self.assertRaisesRegex(ValueError, '复杂度|时间预算'):
            compare_text(left, right)
        result = compare_text('a\n' * 300, 'prefix\n' + 'a\n' * 300)
        self.assertEqual(result.rows[0].kind, 'insert')
        self.assertEqual(sum(row.kind == 'equal' for row in result.rows), 300)
        self.assert_reconstructs(result)

    def test_operation_budget_shared_by_matching_phases(self):
        with patch.object(diff_tools, 'MAX_MATCH_OPERATIONS', 10):
            with self.assertRaisesRegex(ValueError, '复杂度'):
                compare_text('a\n' * 10, 'b\n' + 'a\n' * 10)
        with patch.object(diff_tools, 'MAX_MATCH_OPERATIONS', 100):
            with self.assertRaisesRegex(ValueError, '复杂度'):
                compare_text('a' * 100, 'a' * 99 + 'b')

    def test_time_budget_checks_preprocessing_and_inner_matching(self):
        with patch.object(diff_tools, 'monotonic', side_effect=[0, 3]):
            with self.assertRaisesRegex(ValueError, '时间预算'):
                compare_text('', '')
        budget = diff_tools._Budget()
        matcher = diff_tools._BoundedMatcher(['a'] * 500, ['a'] * 500, budget)
        with patch.object(diff_tools, 'monotonic', side_effect=[budget.deadline - 1, budget.deadline]):
            with self.assertRaisesRegex(ValueError, '时间预算'):
                matcher.get_opcodes()
        with patch.object(json_tools, 'transform_json', wraps=json_tools.transform_json), \
                patch.object(diff_tools, 'monotonic', side_effect=[0, 0, 3]):
            with self.assertRaisesRegex(ValueError, '时间预算'):
                compare_text('{}', '{}', json_format=True)

    def test_character_length_and_product_fallbacks(self):
        for left, right in (('a' * 2001, 'b'), ('a' * 501, 'b' * 500)):
            with self.subTest(lengths=(len(left), len(right))):
                with patch.object(diff_tools, '_BoundedMatcher', wraps=diff_tools._BoundedMatcher) as matcher:
                    row = compare_text(left, right).rows[0]
                    self.assertEqual(matcher.call_count, 1)
                    self.assertEqual(row.left_spans, ((0, len(left)),))
                    self.assertEqual(row.right_spans, ((0, len(right)),))
        with patch.object(diff_tools, '_BoundedMatcher', wraps=diff_tools._BoundedMatcher) as matcher:
            compare_text('a' * 2000, 'b')
            self.assertEqual(matcher.call_count, 2)
        with patch.object(diff_tools, '_BoundedMatcher', wraps=diff_tools._BoundedMatcher) as matcher:
            compare_text('a' * 500, 'b' * 500)
            self.assertEqual(matcher.call_count, 2)

    def test_bounded_matcher_matches_standard_without_autojunk(self):
        rng = random.Random(104)
        for _ in range(300):
            left = [rng.choice('abc') for _ in range(rng.randrange(30))]
            right = [rng.choice('abc') for _ in range(rng.randrange(30))]
            matcher = diff_tools._BoundedMatcher(left, right, diff_tools._Budget())
            self.assertEqual(matcher.get_opcodes(), SequenceMatcher(None, left, right, autojunk=False).get_opcodes())

    def test_random_mixed_eol_reconstruction_and_flags(self):
        rng = random.Random(42)
        for _ in range(150):
            texts = [''.join(rng.choice(('中', 'a', ' \t', '\n', '\r', '\r\n'))
                             for _ in range(rng.randrange(20))) for _ in range(2)]
            for trailing, endings in ((False, False), (True, False), (False, True), (True, True)):
                result = compare_text(*texts, trailing, endings)
                self.assertEqual(result.exact_equal, texts[0] == texts[1])
                if not trailing and not endings:
                    self.assertEqual(result.rule_equal, result.exact_equal)
                self.assert_reconstructs(result)

    def test_json_members_align_by_key_not_by_row_position(self):
        """同名字段按字段名配对：同一行两侧都标黄，单侧字段仍标红。"""
        left = ('{\n "a": 1,\n "op_station": "old",\n "query_mode": null,\n'
                ' "query_direction": "sd"\n}')
        right = ('{\n "a": 1,\n "query_mode": null,\n "op_station": "new",\n'
                 ' "position_str": null,\n "query_direction": null\n}')
        result = compare_text(left, right, json_format=True)
        self.assert_reconstructs(result)
        by_key = {}
        for row in result.rows:
            match = diff_tools._JSON_MEMBER_RE.match(row.left_text or row.right_text)
            if match:
                by_key.setdefault(match.group(1), []).append(row)
        # op_station 两侧都有且值不同：配到同一显示行，两侧都标黄。
        station = by_key['op_station']
        self.assertEqual(len(station), 1)
        self.assertEqual(station[0].kind, 'replace')
        self.assertEqual(station[0].left_no, 3)
        self.assertTrue(station[0].left_spans and station[0].right_spans)
        # query_direction 两侧都有：即使原文行号不同，也对齐到同一显示行都标黄。
        self.assertEqual([row.kind for row in by_key['query_direction']], ['replace'])
        # query_mode 两侧值相同：不属于差异，两侧都不标色。
        self.assertEqual([row.kind for row in by_key['query_mode']], ['neutral', 'neutral'])
        # position_str 只有右侧存在：保持红色语义。
        self.assertEqual(by_key['position_str'][0].kind, 'insert')

    def test_same_field_at_different_depth_splits_into_two_rows(self):
        """同名字段层级不同、无法对齐时，各占一行都标黄，不再标红。"""
        left = '{\n "outer": {\n  "target": "1"\n }\n}'
        right = '{\n "target": "2"\n}'
        result = compare_text(left, right, json_format=True)
        self.assert_reconstructs(result)
        targets = [row for row in result.rows
                   if diff_tools._member_key(row.left_text or row.right_text) == 'target']
        self.assertEqual([row.kind for row in targets], ['shared_change', 'shared_change'])
        self.assertEqual([(row.left_no, row.right_no) for row in targets],
                         [(None, 2), (3, None)])
        # 两侧都标黄：存在侧有整行强调，另一侧留空。
        self.assertEqual(targets[0].right_spans, ((0, len(targets[0].right_text)),))
        self.assertEqual(targets[0].left_spans, ())
        self.assertEqual(targets[1].left_spans, ((0, len(targets[1].left_text)),))

    def test_plain_text_and_invalid_json_keep_row_position_alignment(self):
        """非 JSON 输入、或只有一侧是 JSON 时，完全沿用按行位置对齐。"""
        result = compare_text('x\nop_station: old\ny\n', 'x\ny\nop_station: new\n')
        self.assertEqual([row.kind for row in result.rows],
                         ['equal', 'delete', 'equal', 'insert'])
        self.assertEqual(result.blocks, (1, 3))
        result = compare_text('{"a":1}', 'not json\n{"a":2}')
        self.assertNotIn('neutral', [row.kind for row in result.rows])


class JsonFieldDiffTests(unittest.TestCase):
    def compare(self, left, right):
        return diff_tools.compare_json_fields(left, right)

    def test_order_decoded_values_copy_and_immutable_contract(self):
        left = ' { "z": 1e0, "a": "\\u4e2d", "empty": {} }\r\n'
        right = '{"empty":{},"a":"中","z":1.0}'
        result = compare_text(left, right, True, True, True, json_fields=True)
        self.assertEqual(result, self.compare(left, right))
        self.assertEqual(result.mode, 'json_fields')
        self.assertEqual(compare_text('', '').mode, 'text')
        self.assertFalse(result.exact_equal)
        self.assertTrue(result.rule_equal)
        self.assertEqual((result.left_text, result.right_text), (left, right))
        self.assertEqual(result.blocks, ())
        self.assertEqual(result.unified_diff, '')
        self.assertEqual([r.left_text.split(' = ')[0] for r in result.rows],
                         ['$.a', '$.empty', '$.z'])
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.mode = 'text'
        with self.assertRaises(dataclasses.FrozenInstanceError):
            result.rows[0].left_text = '修改'

    def test_array_index_order_and_independent_navigation(self):
        result = self.compare('[1,2,3]', '[3,2,1,4]')
        self.assertEqual([r.kind for r in result.rows], ['replace', 'equal', 'replace', 'insert'])
        self.assertEqual(result.blocks, (0, 2, 3))
        self.assertEqual(result.rows[-1].right_text, '$[3] = 4')
        self.assertEqual(result.rows[-1].left_no, None)
        consecutive = self.compare('[1,2,3]', '[4,5,6]')
        self.assertEqual(consecutive.blocks, (0, 1, 2))
        for side in ('left', 'right'):
            values = [getattr(r, side + '_no') for r in result.rows
                      if getattr(r, side + '_no') is not None]
            self.assertEqual(values, list(range(1, len(values) + 1)))

    def test_missing_null_empty_and_single_subtree(self):
        result = self.compare('{"null":null,"s":"","obj":{},"arr":[]}', '{}')
        self.assertEqual([r.kind for r in result.rows], ['delete'] * 4)
        self.assertEqual([r.left_text for r in result.rows],
                         ['$.arr = []', '$.null = null', '$.obj = {}', '$.s = ""'])
        for value in ('null', '""', '{}', '[]'):
            self.assertFalse(self.compare('{"x":' + value + '}', '{}').rule_equal)
        result = self.compare('{}', '{"x":{"z":[1,2],"a":true}}')
        self.assertEqual(len(result.rows), 1)
        self.assertEqual(result.rows[0].right_text, '$.x = {"a":true,"z":[1,2]}')
        self.assertTrue(self.compare('{}', '{}').rule_equal)
        self.assertTrue(self.compare('[]', '[]').rule_equal)
        self.assertEqual(self.compare('{}', '{}').rows[0].left_text, '$ = {}')

    def test_nested_same_type_containers_recurse_stably(self):
        result = self.compare('{"x":[{"b":2,"a":1},[]],"y":{}}',
                              '{"y":{},"x":[{"a":1,"b":3},[]]}')
        self.assertEqual([r.left_text for r in result.rows],
                         ['$.x[0].a = 1', '$.x[0].b = 2', '$.x[1] = []', '$.y = {}'])
        self.assertEqual(result.blocks, (1,))
        self.assertFalse(result.rule_equal)
        for left, right in (('[{}]', '[]'), ('{"x":{}}', '{"x":{"a":1}}'),
                            ('[]', '[null]')):
            result = self.compare(left, right)
            self.assertEqual(len(result.rows), 1)
            self.assertIn(result.rows[0].kind, ('insert', 'delete'))

    def test_type_changes_do_not_expand(self):
        for left, right in (('{"a":[1]}', '[1,2]'), ('[1]', '1'), ('{}', 'null'),
                            ('[]', '{}'), ('true', '1'), ('false', '0'), ('"1"', '1'),
                            ('null', '""')):
            with self.subTest(left=left, right=right):
                result = self.compare(left, right)
                self.assertFalse(result.rule_equal)
                self.assertEqual(len(result.rows), 1)
                self.assertEqual(result.rows[0].kind, 'replace')
                self.assertTrue(result.rows[0].left_text.startswith('$ = '))

    def test_decimal_precision_no_rounding_or_float_overflow(self):
        for left, right in (('1', '1.0'), ('1.0', '1e0'), ('-0', '0.000e99'),
                            ('1e10000', '10e9999'), ('0.0001', '1e-4'),
                            ('1234567890123456789012345678901', '1234567890123456789012345678901.0'),
                            ('1e' + '0' * 5000 + '1', '10')):
            with self.subTest(left=left):
                self.assertTrue(self.compare(left, right).rule_equal)
        self.assertFalse(self.compare('9007199254740992', '9007199254740993').rule_equal)
        self.assertFalse(self.compare('1.00000000000000000000001', '1').rule_equal)
        for value in ('1e999999999999999999999999999', '1e-999999999999999999999', '1e1048577'):
            with self.assertRaisesRegex(ValueError, '左侧.*指数.*行.*列'):
                self.compare(value, '0')

    def test_paths_escaping_and_display_is_single_line(self):
        keys = ['simple', 'a.b', 'a"b', '', '中', '0', 'a[b]', 'line\nkey', 'slash\\key']
        raw = json.dumps({k: 'line\n"\\\t' for k in keys})
        result = self.compare(raw, raw)
        for row, key in zip(result.rows, sorted(keys)):
            suffix = '.simple' if key == 'simple' else '[' + json.dumps(key) + ']'
            self.assertTrue(row.left_text.startswith('$' + suffix + ' = '))
            self.assertNotIn('\n', row.left_text)
            self.assertNotIn('\r', row.left_text)
        self.assertFalse(self.compare('{"a.b":1}', '{"a":{"b":1}}').rule_equal)
        self.assertTrue(self.compare('"\\ud83d\\ude00"', '"😀"').rule_equal)

    def test_invalid_both_sides_and_duplicate_decoded_keys(self):
        for left, right in (('NaN', 'Infinity'), ('[1,]', '{"a":}'),
                            ('{"a":1,"\\u0061":2}', '{"x":{"k":1,"k":2}}'),
                            ('-Infinity', '{"a":1,}')):
            with self.subTest(left=left, right=right):
                with self.assertRaises(ValueError) as caught:
                    self.compare(left, right)
                error = str(caught.exception)
                self.assertIn('左侧 JSON', error)
                self.assertIn('右侧 JSON', error)
                self.assertEqual(error.count('行'), 2)
                self.assertEqual(error.count('列'), 2)
        with self.assertRaisesRegex(ValueError, '第 3 行，第 2 列'):
            self.compare('{\n "a":1,\n "a":2}', '{}')
        self.assertTrue(self.compare('{"a":{"k":1},"b":{"k":2}}',
                                     '{"b":{"k":2},"a":{"k":1}}').rule_equal)

    def test_input_depth_fields_output_and_shared_budgets(self):
        for left, right in ((None, '{}'), ('{}', b'{}')):
            with self.assertRaises(TypeError):
                self.compare(left, right)
        with self.assertRaisesRegex(ValueError, '1 MiB'):
            self.compare(' ' * (diff_tools.MAX_CHARACTERS + 1), '{}')
        with self.assertRaisesRegex(ValueError, '输入过大'):
            self.compare('"' + '中' * 700000 + '"', 'null')
        depth = json_tools.MAX_DEPTH
        self.assertTrue(self.compare('[' * depth + '0' + ']' * depth, '0').rows)
        with self.assertRaisesRegex(ValueError, '嵌套过深'):
            self.compare('[' * (depth + 1) + '0' + ']' * (depth + 1), '0')
        raw = '[' + ','.join(['0'] * 5000) + ']'
        self.assertEqual(len(self.compare(raw, raw).rows), 5000)
        with self.assertRaisesRegex(ValueError, '10000'):
            self.compare(raw, raw[:-1] + ',0]')
        # 缺失子树也计入字段预算，不能折叠显示绕过上限。
        with self.assertRaisesRegex(ValueError, '10000'):
            self.compare('[]', '[' + ','.join(['0'] * 10001) + ']')
        with patch.object(diff_tools, 'MAX_JSON_OUTPUT_CHARACTERS', 10):
            with self.assertRaisesRegex(ValueError, '展示输出'):
                self.compare('{"long":{"key":[1,2,3]}}', '{}')
        with patch.object(diff_tools, 'MAX_MATCH_OPERATIONS', 2):
            with self.assertRaisesRegex(ValueError, '复杂度'):
                self.compare('[1,2]', '[]')
        with patch.object(diff_tools, 'monotonic', side_effect=[0, 3]):
            with self.assertRaisesRegex(ValueError, '时间预算'):
                self.compare('{}', '{}')
        with patch.object(json_tools, 'transform_json', wraps=json_tools.transform_json) as transform:
            self.compare('{"x":1}', '{}')
            self.assertEqual(transform.call_count, 2)


if __name__ == '__main__':
    unittest.main()
