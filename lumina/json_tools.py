import json
import re
from dataclasses import dataclass


MAX_INPUT_BYTES = 2 * 1024 * 1024
MAX_DEPTH = 128
MAX_OUTPUT_BYTES = 8 * 1024 * 1024

# 字符串整体匹配，绝不将其中的空白、括号或转义当成结构。
_TOKEN_RE = re.compile(r'"(?:[^"\\]|\\[\s\S])*"|[ \t\r\n]+|[{}\[\],:]|[^" \t\r\n{}\[\],:]+|[\s\S]')
_WHITESPACE = ' \t\r\n'


@dataclass(frozen=True)
class JsonTransformResult:
    text: str
    duplicate_keys: tuple[str, ...]


def _tokens(raw):
    for match in _TOKEN_RE.finditer(raw):
        if match.group()[0] not in _WHITESPACE:
            yield match


def transform_json(raw: str, mode: str = 'format') -> JsonTransformResult:
    """严格校验并保留 token 原文；重复键按校验发现顺序去重返回。"""
    if not isinstance(raw, str):
        raise TypeError('JSON 输入必须是字符串。')
    if mode not in ('format', 'compress'):
        raise ValueError('不支持的 JSON 模式，请选择 format 或 compress；restore 由 UI 处理。')
    # surrogatepass 使标准库允许的孤立代理字符也能保真处理。
    if len(raw) > MAX_INPUT_BYTES or len(raw.encode('utf-8', 'surrogatepass')) > MAX_INPUT_BYTES:
        raise ValueError(f'JSON 输入过大，最多允许 {MAX_INPUT_BYTES} 字节（默认 2 MiB）。')

    depth = 0
    for match in _tokens(raw):
        token = match.group()
        if token in ('{', '['):
            depth += 1
            if depth > MAX_DEPTH:
                raise ValueError(f'JSON 嵌套过深，最多允许 {MAX_DEPTH} 层。')
        elif token in ('}', ']'):
            depth -= 1

    duplicate_keys = []
    reported = set()

    def object_pairs(pairs):
        seen = set()
        for key, _ in pairs:
            if key in seen and key not in reported:
                duplicate_keys.append(key)
                reported.add(key)
            seen.add(key)
        # 校验结果不参与输出，避免在对象中保留无用的数据。
        return None

    def reject_constant(value):
        position = next(match.start() for match in _tokens(raw) if match.group() == value)
        raise json.JSONDecodeError(f'JSON 不允许非有限数值 {value}', raw, position)

    json.loads(
        raw,
        parse_int=str,
        parse_float=str,
        parse_constant=reject_constant,
        object_pairs_hook=object_pairs,
    )

    parts = []
    output_bytes = 0

    def emit(text):
        nonlocal output_bytes
        output_bytes += len(text.encode('utf-8', 'surrogatepass'))
        if output_bytes > MAX_OUTPUT_BYTES:
            raise ValueError(f'JSON 输出过大，最多允许 {MAX_OUTPUT_BYTES} 字节（默认 8 MiB）；可尝试压缩模式。')
        parts.append(text)

    depth = 0
    previous = None
    for match in _tokens(raw):
        token = match.group()
        if mode == 'compress':
            emit(token)
            continue
        if token in ('}', ']'):
            depth -= 1
            if previous != ('{' if token == '}' else '['):
                emit('\n' + '  ' * depth)
            emit(token)
        elif token == ',':
            emit(',\n' + '  ' * depth)
        elif token == ':':
            emit(': ')
        else:
            if previous in ('{', '['):
                emit('\n' + '  ' * depth)
            emit(token)
            if token in ('{', '['):
                depth += 1
        previous = token

    return JsonTransformResult(''.join(parts), tuple(duplicate_keys))
