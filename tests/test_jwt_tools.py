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
    """字段结构摘要已被移除，不得再出现在结果里。

    原先 ``tool_window._field_summary`` 会把 JSON 摊平成
    ``a.b[0] : 字符串 = 'x'`` 这样的清单追加在格式化结果末尾。用户要求去掉
    （「JSON格式化右侧的字段结构就不要了去掉」），实现与函数一并删除。

    这里**保留为源码级守卫**而不是整类删掉：纯行为断言（「输出里不含『字段
    结构』」）拦不住有人把函数重新加回来但暂未接上调用点，下次顺手接上就回退了。
    """

    SOURCE = Path(__file__).resolve().parent.parent / 'lumina' / 'tool_window.py'

    def test_field_summary_helper_is_gone(self):
        self.assertNotIn('_field_summary', self.SOURCE.read_text(encoding='utf-8'),
                         '字段结构摘要的辅助函数被加回来了')

    def test_field_summary_no_longer_importable(self):
        import lumina.tool_window as tool_window
        self.assertFalse(hasattr(tool_window, '_field_summary'),
                         'tool_window 仍导出 _field_summary')


if __name__ == '__main__':
    unittest.main()
