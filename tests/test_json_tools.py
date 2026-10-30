import json
import pathlib
import shutil
import subprocess
import tempfile
import unittest
import uuid
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


class JavaStringBodyTests(unittest.TestCase):
    """``java_string_body``：把文本转成 Java 字符串字面量内容。

    这层的每条规则都有 javac 实测背书（见 ``JavaCompileTests``），这里先用
    纯字符串比较把边界钉死，跑得快、失败信息也直观。
    """

    def test_escapes_backslash_quote_and_control_chars(self):
        cases = {
            'a\\b': 'a\\\\b',
            'a"b': 'a\\"b',
            'a\nb': 'a\\nb',
            'a\rb': 'a\\rb',
            'a\tb': 'a\\tb',
            'a\bb': 'a\\bb',
            'a\fb': 'a\\fb',
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(json_tools.java_string_body(text), expected)

    def test_other_control_and_del_become_unicode_escapes(self):
        self.assertEqual(json_tools.java_string_body('a\x00b'),
                         'a\\u0000b')
        self.assertEqual(json_tools.java_string_body('a\x01b'),
                         'a\\u0001b')
        self.assertEqual(json_tools.java_string_body('a\x1fb'),
                         'a\\u001fb')
        self.assertEqual(json_tools.java_string_body('a\x7fb'),
                         'a\\u007fb')

    def test_lone_surrogates_become_unicode_escapes(self):
        # surrogatepass 让孤立代理字符能进入处理流程；进 Java 源码前必须转义，
        # 裸代理字符既写不进 UTF-8 源文件，Java 也不认。
        for text in ('x\ud800y', '\udfff', '\ud800\udc00'):
            with self.subTest(text=repr(text)):
                out = json_tools.java_string_body(text)
                out.encode('ascii')  # 必须能编成 ASCII，说明确实转义了
                self.assertNotIn('\ud800', out)

    def test_cjk_kept_verbatim(self):
        # 中文不转 \uXXXX：可读性优先，现代 Java 源码统一 UTF-8。
        self.assertEqual(json_tools.java_string_body('中文"x"'),
                         '中文\\"x\\"')

    def test_nested_escaping_is_single_layer(self):
        # 输入已经含 \\ 与 \" 时，再转义一层后 Java 解析回来正好是原值。
        self.assertEqual(json_tools.java_string_body(r'a\"b'), r'a\\\"b')
        self.assertEqual(json_tools.java_string_body(r'a\\b'), r'a\\\\b')

    def test_no_u_escape_triggered_by_literal_backslash_u(self):
        # 字面 \u0041 必须变成 \\u0041（源码里两个反斜杠）。javac 按前导反斜杠
        # 的奇偶判定，这个形态是安全的；只有裸 \u005c 才会炸。
        self.assertEqual(json_tools.java_string_body(r'\u0041'), r'\\u0041')

    def test_empty_string_stays_empty(self):
        self.assertEqual(json_tools.java_string_body(''), '')


class EscapeJavaTests(unittest.TestCase):
    """``transform_json(raw, 'escape_java')``：紧凑 JSON 的 Java 字面量内容。"""

    def test_output_is_escaped_compact_json(self):
        raw = '{\n  "a": 1,\n  "b": [1, 2]\n}'
        expected = r'{\"a\":1,\"b\":[1,2]}'
        self.assertEqual(transform_json(raw, 'escape_java').text, expected)

    def test_starts_from_compact_not_from_format(self):
        raw = '{ "a" : [ 1 , 2 ] }'
        result = transform_json(raw, 'escape_java').text
        self.assertNotIn('\n', result)
        self.assertNotIn('  ', result)
        # 去掉转义层后必须恰好等于 compress 的结果——证明它确实是先压缩再转义。
        self.assertEqual(result.replace('\\"', '"').replace('\\\\', '\\'),
                         transform_json(raw, 'compress').text)

    def test_keeps_duplicate_keys_and_validation(self):
        self.assertEqual(
            transform_json('{"a":1,"a":2}', 'escape_java').duplicate_keys,
            ('a',))
        with self.assertRaises(json.JSONDecodeError):
            transform_json('{"a": @}', 'escape_java')
        with self.assertRaisesRegex(ValueError, '嵌套过深'):
            transform_json('[' * 129 + '0' + ']' * 129, 'escape_java')
        with self.assertRaisesRegex(ValueError, '输入过大'):
            transform_json('"' + 'a' * json_tools.MAX_INPUT_BYTES + '"',
                           'escape_java')

    def test_escaped_quote_and_backslash_survive(self):
        # JSON 原文里的 \" 与 \\ 在字面量内容里各再多一层反斜杠。
        raw = r'{"q":"he said \"hi\"","p":"C:\\tmp"}'
        out = transform_json(raw, 'escape_java').text
        self.assertIn(r'he said \\\"hi\\\"', out)
        self.assertIn(r'C:\\\\tmp', out)

    def test_literal_newline_escape_stays_two_char(self):
        # JSON 里的 \n（反斜杠+n）在 Java 字面量里要写成 \\n，这样 Java 侧得到的
        # String 里仍是「反斜杠 + n」两个字符，而不是真的换行。
        raw = r'{"m":"a\nb"}'
        out = transform_json(raw, 'escape_java').text
        self.assertEqual(out, r'{\"m\":\"a\\nb\"}')
        self.assertNotIn('\n', out)
        # 从字面量反解回来应等于原 JSON 的字符串值。
        self.assertEqual(out.replace('\\\\', '\\').replace('\\"', '"'),
                         transform_json(raw, 'compress').text)

    def test_output_limit_applies_after_escaping(self):
        raw = '{"k":"' + '\\"' * 40 + '"}'
        compact = transform_json(raw, 'compress').text
        escaped = transform_json(raw, 'escape_java').text
        # 转义确实让输出变长了，上限要按转义后的长度算。
        self.assertGreater(len(escaped), len(compact))
        with patch.object(json_tools, 'MAX_OUTPUT_BYTES',
                          len(escaped.encode('utf-8'))):
            self.assertEqual(transform_json(raw, 'escape_java').text, escaped)
        with patch.object(json_tools, 'MAX_OUTPUT_BYTES',
                          len(escaped.encode('utf-8')) - 1):
            with self.assertRaisesRegex(ValueError, '输出过大'):
                transform_json(raw, 'escape_java')

    def test_mode_name_is_accepted_but_restore_is_not(self):
        self.assertEqual(transform_json('{}', 'escape_java').text, '{}')
        with self.assertRaisesRegex(ValueError, '模式'):
            transform_json('{}', 'restore')


JAVAC = shutil.which('javac')
JAVA = shutil.which('java')


@unittest.skipIf(JAVAC is None or JAVA is None,
                 '本机没有 javac/java，跳过真实编译验证')
class JavaCompileTests(unittest.TestCase):
    """用**真实的 javac 编译 + 运行回读**验证转义正确性。

    为什么值得让测试依赖外部 javac：转义规则的正确性只有「Java 真的把这段
    字面量解析成什么」能证明。纯字符串断言只能证明「我按我理解的规则拼了字符
    串」，而 JLS 3.3 的 unicode 预处理、`\\uXXXX` 的前导反斜杠奇偶规则这类
    东西，光看代码是看不出对错的——本项目已为此专门做过三组实测。

    没有 javac 时整体跳过（不是失败）：这条证据是「加强」，不是「必需」，
    在没装 JDK 的机器上不该红。
    """

    def _run(self, literal_body, value):
        """编译并运行 ``String s = "<literal_body>";``，返回 Java 侧的值。"""
        work = pathlib.Path(tempfile.mkdtemp(prefix='lumjava_test_'))
        self.addCleanup(shutil.rmtree, work, True)
        cls = 'J' + uuid.uuid4().hex[:10]
        source = work / f'{cls}.java'
        # 用 Base64 传期望值不必要；直接把字面量嵌进源码，这才是要验证的东西。
        source.write_text(
            f'public class {cls} {{\n'
            '  public static void main(String[] args) {\n'
            f'    String s = "{literal_body}";\n'
            '    System.out.print(s);\n'
            '  }\n'
            '}\n', encoding='utf-8')
        compiled = subprocess.run(
            [JAVAC, '-encoding', 'UTF-8', '-d', str(work), str(source)],
            capture_output=True, text=True, encoding='utf-8', errors='replace')
        self.assertEqual(compiled.returncode, 0,
                         f'javac 编译失败：{compiled.stderr.strip()}')
        run = subprocess.run(
            # 中文必须显式指定 stdout 编码：JDK 18+ 默认取系统区域（本机 GBK），
            # 拿 UTF-8 解会得到一堆替换字符——那是**测试读出方式**的问题，不是
            # 字面量转义的问题，不修好会误判成转义错误。
            [JAVA, '-Dstdout.encoding=UTF-8', '-cp', str(work), cls],
            # **必须走二进制读**：``text=True`` 会用通用换行把 ``\r`` 归一成
            # ``\n``，于是「CR 是否正确还原」这条永远测不出来（曾因此得到
            # ``'a\nb\nc' != 'a\nb\rc'`` 的假失败）。
            capture_output=True)
        self.assertEqual(run.returncode, 0,
                         f'java 运行失败：{run.stderr.decode("utf-8", "replace")}')
        self.assertEqual(run.stdout.decode('utf-8', 'replace'), value)

    def test_escaped_json_round_trips_through_javac(self):
        """最能说明问题的一条：拼出的字面量在 Java 里解出的 String == 紧凑 JSON。"""
        raw = ('{"name":"张三","quote":"he said \\"hi\\"","path":"C:\\\\tmp",'
               '"esc":"\\u4e2d","lines":"a\\nb","n":1,"ok":true,"nil":null}')
        body = transform_json(raw, 'escape_java').text
        self._run(body, transform_json(raw, 'compress').text)

    def test_literal_backslash_u_is_safe(self):
        """逐字符转义产出的 ``\\\\u0041`` 不会触发 unicode 预处理。

        JLS 3.3 按前导反斜杠的奇偶判定：两个反斜杠后跟 u 是「字面反斜杠 +
        普通 u」，安全；裸 ``\\u005c`` 才会炸成「非法的 Unicode 转义」。
        """
        raw = r'{"p":"C:\\u0041"}'
        body = transform_json(raw, 'escape_java').text
        self.assertIn(r'\\\\u0041', body)
        self._run(body, transform_json(raw, 'compress').text)

    def test_control_characters_compile(self):
        """含控制字符的来源也要能编译并原值回读。

        **这条本身判别力有限，别高估它**：实测 javac 21 允许裸 TAB / BS / FF /
        NUL / SOH / DEL 直接出现在字符串字面量里，所以「能编译」并不能证明那些
        分支的必要性——真正必需的是 LF / CR（裸写 javac 报「未结束的字符串」）
        和孤立代理字符（UTF-8 编不出来，源文件都写不下去）。那两条由
        ``JavaStringBodyTests`` 直接断言。这里保留它只为守住「含这类字符的
        输入不会让整条链路炸掉」。
        """
        # JSON 不允许 C0 裸字符，只能以 \u0001 转义形式进来。
        raw = '{"c":"a\\u0001b","t":"a\\tb","d":"a\\u007fb"}'
        body = transform_json(raw, 'escape_java').text
        self._run(body, transform_json(raw, 'compress').text)

    def test_real_lf_and_cr_escape_is_what_makes_compile_succeed(self):
        """LF / CR 是**必需**转义的：裸写进字面量 javac 直接编译失败。

        对照组：同一段文本，转义后编译通过；把 ``\\n`` 换回裸换行后编译失败。
        没有对照组的话，「能编译」可能只是 javac 恰好容忍——曾据此类推把
        TAB/BS/FF 也写成「必需」，实测才发现不对。
        """
        escaped = json_tools.java_string_body('a\nb\rc')
        self.assertEqual(escaped, 'a\\nb\\rc')
        self._run(escaped, 'a\nb\rc')
        # 对照组：裸换行必须编译失败。
        with self.assertRaises(AssertionError):
            self._run('a\nb\rc', 'a\nb\rc')

    def test_chinese_literal_compiles(self):
        raw = '{"城市":"上海","区":"浦东新区"}'
        body = transform_json(raw, 'escape_java').text
        self._run(body, transform_json(raw, 'compress').text)


if __name__ == '__main__':
    unittest.main()
