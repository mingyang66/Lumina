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
_MODES = ('format', 'compress', 'expand', 'escape_java')

# 公开的模式清单。名称、顺序、展示文案的**唯一来源**是 ``json_modes``（它从这里
# 取），这里只做「哪些值可以通过校验」的判定。两者不一致会被
# ``test_every_declared_mode_is_transformable`` 拦下。
MODES = _MODES

# 输出为「紧凑单行」的模式。``escape_java`` 先压成紧凑形式再整体转义，因为
# Java 侧要的是**一个字符串**的值，多行缩进只会让常量定义变得难读。
_COMPACT_MODES = ('compress', 'escape_java')


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


_JAVA_ESCAPES = {
    '\\': '\\\\', '"': '\\"',
    '\n': '\\n', '\r': '\\r', '\t': '\\t', '\b': '\\b', '\f': '\\f',
}


def java_string_body(text):
    """把文本转成 Java 字符串字面量的**内容**（不含外层引号）。

    逐字符替换，不做任何「先解码再重拼」：调用方传入的已经是紧凑 JSON 的
    原文字节，转义只叠加一层，Java 侧 ``String`` 的值恰好等于那段紧凑 JSON。
    这样用户把结果贴进 ``setPayload("...")`` 就能拿到同一份 JSON。

    **各类字符为什么要转义，实测口径（不同理由，别一概说成「不转义就编译
    不过」——曾这么写过，实测证明只有前两类成立）：**

    - ``\\`` 与 ``"``：**必需**。不转义直接截断字符串 / 吞掉后一个字符。
    - LF / CR：**必需**。裸写进源码 javac 报「未结束的字符串」，实测编译失败。
    - 孤立代理字符：**必需**。``utf-8`` 根本编不出来，源文件都写不下去。
    - TAB / BS / FF：**惯例**。实测裸写能编译（javac 21），但裸 TAB 会被编辑器
      按自己的缩进设置重排、BS 与 FF 是隐形字符，源码里不该出现。
    - 其余 C0 控制字符与 DEL：**可读性**。实测同样能编译，但源码里留不可打印
      字符会让 diff、代码审查、复制粘贴都变得不可信。

    注意经 ``transform_json`` 进来时，字符串体里**不会有裸 LF/CR/C0**——JSON
    规范本就禁止（``json.loads`` 会先拒掉）。那几个分支是为「将来别处把任意
    文本转成 Java 字面量」留的，也是本函数自称通用的必要条件；别因为「当前
    路径走不到」就删掉。

    **不需要特判 ``u`` 陷阱。** Java 有 unicode escape 预处理阶段（JLS 3.3），
    反斜杠紧跟 ``u`` 会最先被处理；但判定按**前导反斜杠的奇偶**——逐字符转义
    产出的 ``\\\\u0041``（源码里两个反斜杠）是安全的，实测 javac 编译通过且
    值为字面 ``\\u0041``。只有人为写裸 ``\\u005c`` 反而会炸成「非法的 Unicode
    转义」，那条路径本函数不会产生。

    中文等非 ASCII 字符**原样保留**，不转成 ``\\uXXXX``：可读性远重要于
    「纯 ASCII 源码」这种很少被真正要求的约束，且现代 Java 源码统一 UTF-8。
    """
    out = []
    for char in text:
        escape = _JAVA_ESCAPES.get(char)
        if escape is not None:
            out.append(escape)
            continue
        code = ord(char)
        if code < 0x20 or code == 0x7f or 0xd800 <= code <= 0xdfff:
            out.append(f'\\u{code:04x}')
            continue
        out.append(char)
    return ''.join(out)


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
    - ``escape_java``：先压成紧凑形式，再整体转成 Java 字符串字面量**内容**
      （不含外层引号），可直接贴进 ``setXxx("...")``。转义规则见
      ``java_string_body``。

    注意 ``expand`` 的输出**不再是合法 JSON**（JSON 不允许字符串内含裸换行），
    只能作为只读展示；还原需基于原始输入，不能反向解析。换行形式会**规范化
    为 LF**（``\\r``、``\\u000a`` 都收敛成 ``\\n``），且展开时**不添加续行
    缩进**，以免与数据里原有的行首空格混淆。

    ``escape_java`` 的输出同样不是 JSON，而是 Java 源码片段。
    """
    if not isinstance(raw, str):
        raise TypeError('JSON 输入必须是字符串。')
    if mode not in _MODES:
        raise ValueError(
            '不支持的 JSON 模式，请选择 format、compress、expand 或 '
            'escape_java；restore 由 UI 处理。')
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
        # compress 与 escape_java 都要紧凑单行：token 原样输出即可，后者只是
        # 在整个结果外面再叠一层 Java 转义（在循环结束后做，避免逐 token 转义
        # 时把结构字符也当成内容处理）。
        if mode in _COMPACT_MODES:
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

    text = ''.join(parts)
    if mode == 'escape_java':
        # 输出长度会因转义而膨胀（反斜杠与引号翻倍），所以这里单独计一次上限。
        text = java_string_body(text)
        if len(text.encode('utf-8', 'surrogatepass')) > MAX_OUTPUT_BYTES:
            raise ValueError(
                f'JSON 输出过大，最多允许 {MAX_OUTPUT_BYTES} 字节'
                '（默认 8 MiB）；可尝试压缩模式。')
    return JsonTransformResult(text, tuple(duplicate_keys))
