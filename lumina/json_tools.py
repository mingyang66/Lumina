import json
import re
from dataclasses import dataclass

MAX_INPUT_BYTES = 2 * 1024 * 1024
MAX_DEPTH = 128
MAX_OUTPUT_BYTES = 8 * 1024 * 1024

# 字符串整体匹配，绝不将其中的空白、括号或转义当成结构。
_TOKEN_RE = re.compile(r'"(?:[^"\\]|\\[\s\S])*"|[ \t\r\n]+|[{}\[\],:]|[^" \t\r\n{}\[\],:]+|[\s\S]')
_WHITESPACE = ' \t\r\n'

# 展开模式：把字符串值里的「换行类转义」换成真实换行。
# 只处理换行，其余转义（\" \\ \/ \t 等）必须逐字节保留——先解码再重拼会把
# \" 变成裸引号、破坏字符串边界，因此这里在 token 层面原地扫描。
_EXPAND_MODES = ('format', 'compress', 'expand')


def _expand_newline_escapes(body):
    """展开字符串体内的换行转义，返回 (新内容, 是否发生替换)。

    识别 ``\\n``、``\\r``、``\\r\\n``、``\\u000a``（大小写不敏感），统一输出
    **LF**。因此 ``"a\\r\\nb"`` 与 ``"a\\u000ab"`` 都会变成同一个 ``"a<LF>b"``：
    换行形式被规范化，只有本来就是 ``\\n`` 的写法才与原始字节一一对应。
    这属于展示层的投影——``\\r`` 与 ``\\u000a`` 在 JSON 中本就表示同一个换行，
    规范化后更易读；UI 的「恢复原文」直接取库中原始内容，不依赖此处可逆。

    遇到 ``\\\\`` 必须整体跳过两位，否则字面量 ``"a\\\\nb"``（反斜杠+n）会被
    误展开成换行。``\\t``、``\\"``、``\\/`` 等其余转义一律原样保留。
    """
    out = []
    changed = False
    index = 0
    length = len(body)
    while index < length:
        char = body[index]
        if char == '\\' and index + 1 < length:
            following = body[index + 1]
            if following == 'n':
                out.append('\n')
                changed = True
                index += 2
                continue
            if following == 'r':
                # CRLF 视作一次换行，避免展开出多余空行。
                if body[index + 2:index + 4] == '\\n':
                    index += 4
                else:
                    index += 2
                out.append('\n')
                changed = True
                continue
            if following == 'u' and body[index + 2:index + 6].lower() == '000a':
                out.append('\n')
                changed = True
                index += 6
                continue
            out.append(body[index:index + 2])
            index += 2
            continue
        out.append(char)
        index += 1
    return ''.join(out), changed


def _is_object_key(raw, match):
    """判断字符串 token 是否为对象键（其后紧跟 ``:``）。

    键不做换行展开：``"a\\nb"`` 若变成含裸换行的键，会把 ``键: 值`` 拆到两行，
    反而破坏格式化后的行结构。JSON 语法里字符串后紧跟 ``:`` 只可能是键，
    故只需向后跳过空白看一个字符，无需预物化整个 token 流。
    """
    index = match.end()
    length = len(raw)
    while index < length and raw[index] in _WHITESPACE:
        index += 1
    return index < length and raw[index] == ':'


def json_syntax_spans(raw):
    """返回 ``raw`` 中 JSON 语法 token 的 ``(kind, start, end)`` 列表。

    ``kind`` 取值 ``str`` / ``num`` / ``lit``（对象键与字符串值同为 ``str``），
    供 UI 着色复用同一套 token 规则。关键点：字符串体允许出现**真实换行**
    （``expand`` 的输出即如此），因此不能改用「遇换行即断」的代码高亮正则——
    那会把展开后的整段栈文本当作字符串已结束，既失去着色又误判其中的
    数字/单词。调用方须保证 ``raw`` 已通过 ``transform_json`` 校验。
    """
    spans = []
    for match in _tokens(raw):
        token = match.group()
        if len(token) > 1 and token.startswith('"') and token.endswith('"'):
            spans.append(('str', match.start(), match.end()))
        elif token in ('true', 'false', 'null'):
            spans.append(('lit', match.start(), match.end()))
        elif token[0] in '-0123456789':
            spans.append(('num', match.start(), match.end()))
    return spans


@dataclass(frozen=True)
class JsonTransformResult:
    text: str
    duplicate_keys: tuple[str, ...]


def _tokens(raw):
    for match in _TOKEN_RE.finditer(raw):
        if match.group()[0] not in _WHITESPACE:
            yield match


def transform_json(raw: str, mode: str = 'format') -> JsonTransformResult:
    """严格校验并保留 token 原文；重复键按校验发现顺序去重返回。

    模式：
    - ``format``：2 空格缩进展开 JSON 结构，字符串内转义原样保留。
    - ``compress``：去空白压成紧凑形式，字符串内转义原样保留。
    - ``expand``：结构同 ``format``，并把字符串**值**内的换行转义（``\\n`` 等）
      展开为真实换行，便于阅读被挤成一行的异常栈/多行文本。对象键不展开。

    注意 ``expand`` 的输出**不再是合法 JSON**（JSON 不允许字符串内含裸换行），
    只能作为只读展示；还原需基于原始输入，不能反向解析。换行形式会**规范化
    为 LF**（``\\r``、``\\u000a`` 都收敛成 ``\\n``），且展开时**不添加续行
    缩进**，以免与数据里原有的行首空格混淆。
    """
    if not isinstance(raw, str):
        raise TypeError('JSON 输入必须是字符串。')
    if mode not in _EXPAND_MODES:
        raise ValueError('不支持的 JSON 模式，请选择 format、compress 或 expand；restore 由 UI 处理。')
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
            if mode == 'expand' and token[0] == '"' and len(token) > 1 \
                    and not _is_object_key(raw, match):
                # 只展开字符串值里的换行转义；不加续行缩进，避免把作者原有的
                # 行首空格当成缩进而无法区分。
                body, replaced = _expand_newline_escapes(token[1:-1])
                emit('"' + body + '"' if replaced else token)
            else:
                emit(token)
            if token in ('{', '['):
                depth += 1
        previous = token

    return JsonTransformResult(''.join(parts), tuple(duplicate_keys))
