import json
import unittest
from unittest.mock import patch

from lumina import json_tools
from lumina.json_tools import JsonTransformResult, transform_json


class JsonToolsTests(unittest.TestCase):
    def test_defaults_and_constants(self):
        self.assertEqual(json_tools.MAX_INPUT_BYTES, 2 * 1024 * 1024)
        self.assertEqual(json_tools.MAX_DEPTH, 128)
        self.assertEqual(json_tools.MAX_OUTPUT_BYTES, 8 * 1024 * 1024)
        result = transform_json('{"中文": [1,true,null], "空": {}}')
        self.assertIsInstance(result, JsonTransformResult)
        self.assertEqual(result.text, '{\n  "中文": [\n    1,\n    true,\n    null\n  ],\n  "空": {}\n}')
        self.assertEqual(result.duplicate_keys, ())

    def test_token_preservation(self):
        raw = r' { "中文" : "  a  b\t\n\r 中文 ", "esc" : "\u4e2d\/\\\"", "n" : [-0,1E+009,0.123456789012345678901234567890,-2.00e-003] } '
        compact = r'{"中文":"  a  b\t\n\r 中文 ","esc":"\u4e2d\/\\\"","n":[-0,1E+009,0.123456789012345678901234567890,-2.00e-003]}'
        self.assertEqual(transform_json(raw, 'compress').text, compact)
        self.assertEqual(transform_json(transform_json(raw).text, 'compress').text, compact)

    def test_large_integer_and_exponent(self):
        number = '9' * 10000
        raw = '[ ' + number + ', 1e999999, -0.000000000000000000000000000001 ]'
        expected = '[' + number + ',1e999999,-0.000000000000000000000000000001]'
        self.assertEqual(transform_json(raw, 'compress').text, expected)
        self.assertEqual(transform_json(transform_json(raw).text, 'compress').text, expected)

    def test_all_scalars_and_empty_containers(self):
        for raw in ('null', 'true', 'false', '123', '-0', '1.0e+02', '"中文"', '""', '[]', '{}'):
            for mode in ('format', 'compress'):
                with self.subTest(raw=raw, mode=mode):
                    self.assertEqual(transform_json(' \t\r\n' + raw + '\n ', mode).text, raw)

    def test_duplicate_keys_and_order_preserved(self):
        raw = r'{"z":0,"a":1,"a":2,"\u0061":3,"nested":{"x":1,"x":2},"other":{"a":4,"a":5}}'
        for mode in ('format', 'compress'):
            result = transform_json(raw, mode)
            self.assertEqual(result.duplicate_keys, ('x', 'a'))
            self.assertEqual(transform_json(result.text, 'compress').text, raw)

    def test_same_keys_in_distinct_objects_not_duplicates(self):
        self.assertEqual(transform_json('[{"a":1},{"a":2}]').duplicate_keys, ())

    def test_string_structure_and_escaped_quote_do_not_affect_depth(self):
        raw = json.dumps('[' * 1000 + '"\\{} , :  \t\n' + ']' * 1000, ensure_ascii=False)
        self.assertEqual(transform_json(raw).text, raw)
        self.assertEqual(transform_json('"\\ud800"').text, '"\\ud800"')
        self.assertEqual(transform_json('"\ud800"').text, '"\ud800"')

    def test_invalid_json(self):
        invalid = (
            '', ' \t\r\n ', 'NaN', 'Infinity', '-Infinity', '[NaN]', '{"a":Infinity}',
            '// 注释\n{}', '{/* 注释 */}', '[1,]', '{"a":1,}', '{"a" 1}',
            '[1 2]', '01', '+1', '.1', '1.', '1e', 'undefined', 'True', 'null false',
            "{'a':1}", '"未结束', r'"\x20"', '"a\nb"', '"a\tb"', '[}',
            '\ufeff{}', '\u00a0{}', '"a\x00b"', '{} trailing', ']',
        )
        for raw in invalid:
            for mode in ('format', 'compress'):
                with self.subTest(raw=raw, mode=mode):
                    with self.assertRaises(json.JSONDecodeError) as caught:
                        transform_json(raw, mode)
                    self.assertEqual(caught.exception.doc, raw)
                    self.assertGreaterEqual(caught.exception.lineno, 1)
                    self.assertGreaterEqual(caught.exception.colno, 1)

    def test_error_line_and_column(self):
        for raw, position, line, column in (
            ('', 0, 1, 1),
            ('{\n  "a": @\n}', 9, 2, 8),
            ('[\n  NaN\n]', 4, 2, 3),
            ('["NaN",\n Infinity]', 9, 2, 2),
            ('[\n -Infinity]', 3, 2, 2),
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(json.JSONDecodeError) as caught:
                    transform_json(raw)
                error = caught.exception
                self.assertEqual((error.pos, error.lineno, error.colno), (position, line, column))

    def test_depth_boundary(self):
        raw = '[' * 128 + '0' + ']' * 128
        self.assertEqual(transform_json(raw, 'compress').text, raw)
        self.assertEqual(transform_json(transform_json(raw).text, 'compress').text, raw)
        for count in (129, 10000):
            for mode in ('format', 'compress'):
                with self.subTest(count=count, mode=mode):
                    with self.assertRaisesRegex(ValueError, '嵌套过深.*128'):
                        transform_json('[' * count + '0' + ']' * count, mode)

    def test_object_and_mixed_depth(self):
        raw = '{"a":[' * 64 + '0' + ']}' * 64
        self.assertEqual(transform_json(raw, 'compress').text, raw)
        with self.assertRaisesRegex(ValueError, '嵌套过深'):
            transform_json('{"a":' + raw + '}')

    def test_input_size_boundary(self):
        raw = '"' + 'a' * (json_tools.MAX_INPUT_BYTES - 2) + '"'
        self.assertEqual(transform_json(raw, 'compress').text, raw)
        with self.assertRaisesRegex(ValueError, '输入过大'):
            transform_json(raw + ' ')

    def test_input_limit_counts_utf8_bytes(self):
        raw = '"中文"'
        size = len(raw.encode('utf-8'))
        with patch.object(json_tools, 'MAX_INPUT_BYTES', size):
            self.assertEqual(transform_json(raw).text, raw)
            with self.assertRaisesRegex(ValueError, '输入过大'):
                transform_json(raw + ' ')
        with self.assertRaisesRegex(ValueError, '输入过大'):
            transform_json('"' + '中' * (json_tools.MAX_INPUT_BYTES // 3 + 1) + '"')

    def test_output_limit_boundary_and_utf8(self):
        expected = '{\n  "中": 1\n}'
        size = len(expected.encode('utf-8'))
        with patch.object(json_tools, 'MAX_OUTPUT_BYTES', size):
            self.assertEqual(transform_json('{"中":1}').text, expected)
        with patch.object(json_tools, 'MAX_OUTPUT_BYTES', size - 1):
            with self.assertRaisesRegex(ValueError, '输出过大.*压缩'):
                transform_json('{"中":1}')
            self.assertEqual(transform_json('{"中":1}', 'compress').text, '{"中":1}')
        with patch.object(json_tools, 'MAX_OUTPUT_BYTES', 3):
            with self.assertRaisesRegex(ValueError, '输出过大'):
                transform_json('true', 'compress')

    def test_actual_output_limit(self):
        raw = '[' * 128 + ','.join(['0'] * 34000) + ']' * 128
        self.assertLess(len(raw), json_tools.MAX_INPUT_BYTES)
        with self.assertRaisesRegex(ValueError, '输出过大'):
            transform_json(raw)
        self.assertEqual(transform_json(raw, 'compress').text, raw)

    def test_idempotence(self):
        samples = (
            ' {"b": [1, {}, [], {"中文": "  空白  "}], "a": -0.00e+2} ',
            '{"x":1,"x":2}', '[[],{},[true,null]]', '1E+99',
            r'"\u4e2d\/\t"',
        )
        for raw in samples:
            for mode in ('format', 'compress'):
                with self.subTest(raw=raw, mode=mode):
                    first = transform_json(raw, mode)
                    self.assertEqual(transform_json(first.text, mode), first)

    def test_invalid_mode_and_type(self):
        for mode in ('restore', 'unknown', '', None):
            with self.subTest(mode=mode):
                with self.assertRaisesRegex(ValueError, '模式'):
                    transform_json('{}', mode)
        for raw in (None, b'{}', 123):
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(TypeError, '字符串'):
                    transform_json(raw)


if __name__ == '__main__':
    unittest.main()
