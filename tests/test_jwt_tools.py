"""JWT 解析工具与 KEY/VALUE 无关的纯逻辑测试。

不依赖 Tk：``jwt_tools`` 刻意与 UI 解耦，便于这里直接断言。
"""
import base64
import datetime
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lumina.jwt_tools import (  # noqa: E402
    JwtError,
    format_epoch,
    parse_jwt,
    strip_wrapping,
    summarize_claims,
)
from lumina.tool_window import _field_summary  # noqa: E402


def b64(obj):
    """把对象编码成 JWT 用法的 base64url（去掉 = 填充）。"""
    raw = json.dumps(obj, ensure_ascii=False).encode('utf-8')
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')


def make_token(header=None, payload=None, signature='sig'):
    header = {'alg': 'HS256', 'typ': 'JWT'} if header is None else header
    payload = {'sub': '1'} if payload is None else payload
    token = f'{b64(header)}.{b64(payload)}'
    return f'{token}.{signature}' if signature is not None else token


class StripWrappingTests(unittest.TestCase):
    def test_bearer_prefix_removed(self):
        self.assertEqual(strip_wrapping('Bearer abc.def.ghi'), 'abc.def.ghi')

    def test_authorization_header_removed(self):
        self.assertEqual(
            strip_wrapping('Authorization: Bearer a.b.c'), 'a.b.c')

    def test_quotes_removed(self):
        self.assertEqual(strip_wrapping('"a.b.c"'), 'a.b.c')
        self.assertEqual(strip_wrapping("'a.b.c'"), 'a.b.c')

    def test_whitespace_and_newlines_removed(self):
        self.assertEqual(strip_wrapping('  a.\nb.  c  '), 'a.b.c')

    def test_empty_input(self):
        self.assertEqual(strip_wrapping(''), '')
        self.assertEqual(strip_wrapping(None), '')


class ParseJwtTests(unittest.TestCase):
    def test_three_segment_token(self):
        token = make_token(payload={'sub': '42', 'name': '张三'})
        result = parse_jwt(token)
        self.assertEqual(result.segments, 3)
        self.assertEqual(result.signature, 'sig')
        self.assertEqual(json.loads(result.payload_json)['name'], '张三')

    def test_two_segment_unsigned_token(self):
        # alg=none 的未签名 JWS 只有两段，签名应为空串。
        result = parse_jwt(make_token(signature=None))
        self.assertEqual(result.segments, 2)
        self.assertEqual(result.signature, '')

    def test_header_json_is_pretty_printed(self):
        result = parse_jwt(make_token())
        self.assertIn('\n', result.header_json)
        self.assertIn('  "alg": "HS256"', result.header_json)

    def test_accepts_standard_base64_alphabet(self):
        # 有些系统输出的是标准 base64（含 + /），不是 URL-safe 的 - _。
        token = make_token()
        parse_jwt(token.replace('-', '+').replace('_', '/'))

    def test_accepts_missing_padding(self):
        # b64url 的 = 常被裁掉，解码必须自动补齐。
        token = make_token(payload={'pad': 'a' * 10})
        self.assertNotIn('=', token)
        parse_jwt(token)

    def test_rejects_five_segment_jwe(self):
        with self.assertRaises(JwtError) as ctx:
            parse_jwt('a.b.c.d.e')
        self.assertIn('JWE', str(ctx.exception))

    def test_rejects_single_segment(self):
        with self.assertRaises(JwtError):
            parse_jwt('onlyonepart')

    def test_rejects_empty(self):
        with self.assertRaises(JwtError) as ctx:
            parse_jwt('   ')
        self.assertIn('请输入', str(ctx.exception))

    def test_rejects_non_base64_segment(self):
        with self.assertRaises(JwtError) as ctx:
            parse_jwt('@@@.@@@.sig')
        self.assertIn('base64', str(ctx.exception))

    def test_rejects_non_json_payload(self):
        header = b64({'alg': 'none'})
        payload = base64.urlsafe_b64encode(b'not json').decode().rstrip('=')
        with self.assertRaises(JwtError) as ctx:
            parse_jwt(f'{header}.{payload}.x')
        self.assertIn('JSON', str(ctx.exception))

    def test_rejects_non_object_header(self):
        # header 语法合法但不是 JSON 对象
        header = base64.urlsafe_b64encode(b'[1,2]').decode().rstrip('=')
        with self.assertRaises(JwtError) as ctx:
            parse_jwt(f'{header}.{b64({"a": 1})}.x')
        self.assertIn('对象', str(ctx.exception))

    def test_non_object_payload_yields_empty_claims(self):
        # 数组型 payload 不常见但语法合法，claims 退化为空字典而非崩溃。
        payload = base64.urlsafe_b64encode(b'[1,2]').decode().rstrip('=')
        result = parse_jwt(f'{b64({"alg": "none"})}.{payload}.x')
        self.assertEqual(result.claims, {})


class EpochFormatTests(unittest.TestCase):
    def test_seconds_and_milliseconds_both_supported(self):
        seconds = 1700000000
        self.assertEqual(format_epoch(seconds),
                         format_epoch(seconds * 1000))

    def test_invalid_values_return_empty(self):
        for value in (None, 'abc', '', [], {}):
            self.assertEqual(format_epoch(value), '')

    def test_summarize_only_known_time_claims(self):
        lines = summarize_claims({'exp': 1700000000, 'sub': 'x'})
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith('exp = '))

    def test_summarize_skips_unformattable(self):
        self.assertEqual(summarize_claims({'exp': 'not-a-number'}), [])

    def test_format_epoch_roundtrip(self):
        stamp = 1700000000
        expected = datetime.datetime.fromtimestamp(stamp).strftime(
            '%Y-%m-%d %H:%M:%S')
        self.assertEqual(format_epoch(stamp), expected)


class FieldSummaryTests(unittest.TestCase):
    """JSON 参数工具的字段结构摘要。"""

    def _pretty(self, obj):
        return json.dumps(obj, ensure_ascii=False, indent=2)

    def test_nested_paths_are_flattened(self):
        summary = _field_summary(self._pretty(
            {'user': {'name': '张三', 'age': 30}}))
        self.assertIn('user.name : 字符串', summary)
        self.assertIn('user.age : 数字', summary)

    def test_array_shows_first_element_and_count(self):
        summary = _field_summary(self._pretty({'ids': [1, 2, 3]}))
        self.assertIn('ids[0] : 数字', summary)
        self.assertIn('ids : 共 3 项', summary)

    def test_single_element_array_omits_count(self):
        summary = _field_summary(self._pretty({'ids': [1]}))
        self.assertIn('ids[0] : 数字', summary)
        self.assertNotIn('共 1 项', summary)

    def test_null_and_bool_types(self):
        summary = _field_summary(self._pretty({'a': None, 'b': True}))
        self.assertIn('a : null', summary)
        self.assertIn('b : bool', summary)

    def test_empty_containers(self):
        summary = _field_summary(self._pretty({'o': {}, 'a': []}))
        self.assertIn('o : 空对象', summary)
        self.assertIn('a : 空数组', summary)

    def test_root_scalar(self):
        self.assertIn('字符串', _field_summary(self._pretty('hi')))


if __name__ == '__main__':
    unittest.main()
