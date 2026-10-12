import dataclasses
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


if __name__ == '__main__':
    unittest.main()
