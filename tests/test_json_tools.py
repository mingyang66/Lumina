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

    def test_json_syntax_spans_cover_strings_across_real_newlines(self):
        # expand 输出的字符串体含真实换行，跨度必须完整覆盖而非在换行处断裂。
        raw = r'{"m":"a\nb\nc","n":[1,true,null],"s":"尾部"}'
        expanded = transform_json(raw, 'expand').text
        spans = json_tools.json_syntax_spans(expanded)
        strings = [(s, e) for k, s, e in spans if k == 'str']
        # 3 个对象键（m/n/s）+ 2 个字符串值 = 5 段
        self.assertEqual(len(strings), 5)
        texts = [expanded[s:e] for s, e in strings]
        # 含裸换行的那个字符串值必须被完整覆盖，而不是断在换行处。
        self.assertIn('"a\nb\nc"', texts)
        self.assertIn('"尾部"', texts)
        self.assertTrue(all(expanded[s] == '"' and expanded[e - 1] == '"'
                            for s, e in strings))
        # 数字与字面量各自归类
        kinds = {}
        for kind, start, end in spans:
            kinds.setdefault(kind, []).append(expanded[start:end])
        self.assertEqual(kinds.get('num'), ['1'])
        self.assertEqual(kinds.get('lit'), ['true', 'null'])

    def test_json_syntax_spans_are_monotonic_and_ignore_structure(self):
        raw = '{"a": [1, {"b": "x"}], "c": false}'
        spans = json_tools.json_syntax_spans(raw)
        offsets = [start for _, start, _ in spans]
        # 偏移单调递增，UI 才能用单游标顺序换算行列。
        self.assertEqual(offsets, sorted(offsets))
        # 结构字符（括号、逗号、冒号）不入 span，非字符串 token 只剩数字/字面量
        self.assertNotIn(':', [raw[s:e] for _, s, e in spans])
        self.assertEqual({k for k, _, _ in spans}, {'str', 'num', 'lit'})

    def test_json_syntax_spans_do_not_break_on_newline_like_code_regex(self):
        # 代码高亮正则遇裸换行即断，这里必须不受影响（回归护栏）。
        import re
        code_string_re = re.compile(r'"(?:\\.|[^"\\\n])*"')
        expanded = transform_json(r'{"m":"第一行\n第二行"}', 'expand').text
        code_hits = code_string_re.findall(expanded)
        json_hits = [(s, e) for k, s, e in json_tools.json_syntax_spans(expanded)
                     if k == 'str']
        # 代码正则只能匹配到残缺的短段，token 规则仍覆盖完整字符串。
        self.assertLess(max(len(h) for h in code_hits), 8)
        self.assertIn('"第一行\n第二行"',
                      [expanded[s:e] for s, e in json_hits])

    def test_invalid_mode_and_type(self):
        for mode in ('restore', 'unknown', '', None):
            with self.subTest(mode=mode):
                with self.assertRaisesRegex(ValueError, '模式'):
                    transform_json('{}', mode)
        for raw in (None, b'{}', 123):
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(TypeError, '字符串'):
                    transform_json(raw)

    def test_expand_turns_newline_escapes_into_real_lines(self):
        raw = r'{"msg":"第一行\n第二行\n第三行","n":1}'
        expected = '{\n  "msg": "第一行\n第二行\n第三行",\n  "n": 1\n}'
        self.assertEqual(transform_json(raw, 'expand').text, expected)
        # 结构与 format 一致，只有字符串体多出裸换行。
        self.assertEqual(transform_json(raw, 'expand').text,
                         transform_json(raw, 'format').text.replace('\\n', '\n'))
        # 压缩模式不动字符串体。
        self.assertEqual(transform_json(raw, 'compress').text,
                         r'{"msg":"第一行\n第二行\n第三行","n":1}')

    def test_expand_normalizes_crlf_cr_and_u000a_to_lf(self):
        # \r\n、\r、\u000a 与 \n 在 JSON 里都表示换行，统一收敛为 LF。
        for escape in (r'\n', r'\r', r'\r\n', r'\u000a', r'\u000A'):
            with self.subTest(escape=escape):
                raw = '{"m":"a' + escape + 'b"}'
                self.assertEqual(transform_json(raw, 'expand').text,
                                 '{\n  "m": "a\nb"\n}')

    def test_expand_keeps_other_escapes_and_literal_backslash_n(self):
        # 字面量 \\n（反斜杠 + n）不得被当作换行展开。
        raw = r'{"lit":"a\\nb","tab":"x\ty","quote":"p\"q","slash":"r\/s"}'
        self.assertEqual(
            transform_json(raw, 'expand').text,
            '{\n  "lit": "a\\\\nb",\n  "tab": "x\\ty",\n'
            '  "quote": "p\\"q",\n  "slash": "r\\/s"\n}')

    def test_expand_does_not_expand_object_keys(self):
        raw = r'{"a\nb":1,"k":"v\nw"}'
        self.assertEqual(transform_json(raw, 'expand').text,
                         '{\n  "a\\nb": 1,\n  "k": "v\nw"\n}')

    def test_expand_non_string_and_no_escape_inputs_match_format(self):
        for raw in ('{}', '[]', '[1,2,3]', '{"a":[1,{"b":null}]}', 'true', '123',
                    '"单个字符串"', '{"m":"没有换行"}', '{"m":"\\ud800"}'):
            with self.subTest(raw=raw):
                self.assertEqual(transform_json(raw, 'expand').text,
                                 transform_json(raw, 'format').text)

    def test_expand_output_is_not_parseable_json(self):
        # expand 是展示态：字符串内出现裸换行后不再是合法 JSON。
        expanded = transform_json(r'{"m":"a\nb"}', 'expand').text
        with self.assertRaises(json.JSONDecodeError):
            json.loads(expanded)
        # 原始输入仍是合法 JSON，恢复原文不依赖反向解析。
        json.loads(r'{"m":"a\nb"}')

    def test_expand_still_validates_input_and_limits(self):
        with self.assertRaises(json.JSONDecodeError):
            transform_json('{"a": @}', 'expand')
        with self.assertRaisesRegex(ValueError, '嵌套过深'):
            transform_json('[' * 129 + '0' + ']' * 129, 'expand')
        with self.assertRaisesRegex(ValueError, '输入过大'):
            transform_json('"' + 'a' * json_tools.MAX_INPUT_BYTES + '"', 'expand')

    def test_expand_respects_output_limit(self):
        # 展开只减字节，故用极大输入验证输出上限仍生效。
        raw = '{"m":"' + 'a' * 400 + '"}'
        formatted = transform_json(raw, 'format').text
        with patch.object(json_tools, 'MAX_OUTPUT_BYTES',
                          len(formatted.encode('utf-8'))):
            self.assertEqual(transform_json(raw, 'expand').text, formatted)
        with patch.object(json_tools, 'MAX_OUTPUT_BYTES', 3):
            with self.assertRaisesRegex(ValueError, '输出过大'):
                transform_json(raw, 'expand')

    def test_expand_reproduces_from_format_for_plain_newlines(self):
        # 输入只含 \n 时，expand 结果等于把 format 结果里的 \n 转义换成真实换行。
        raw = r'{"a":"x\ny\nz","b":["p\nq"],"c":{"d":"\n"}}'
        formatted = transform_json(raw, 'format').text
        expanded = transform_json(raw, 'expand').text
        self.assertEqual(expanded, formatted.replace('\\n', '\n'))
        # 因为不额外加缩进，行数差正好等于字符串体内的 \n 个数（4 个）。
        self.assertEqual(expanded.count('\n'), formatted.count('\n') + 4)


if __name__ == '__main__':
    unittest.main()
