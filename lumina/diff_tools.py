"""保真文本比较；超出匹配预算时拒绝比较，不返回近似的行差异。"""

import json
import re
from dataclasses import dataclass
from decimal import MAX_EMAX, Decimal, InvalidOperation
from difflib import SequenceMatcher
from time import monotonic

from . import json_tools

MAX_CHARACTERS = 1024 * 1024
MAX_TOTAL_LINES = 10000
MAX_SECONDS = 2.0
MAX_MATCH_OPERATIONS = 1_000_000
MAX_CHARACTER_LINE = 2000
MAX_CHARACTER_PRODUCT = 250_000
_EOL_RE = re.compile(r'\r\n|\r|\n')


@dataclass(frozen=True)
class AlignedRow:
    left_text: str
    right_text: str
    left_no: int | None
    right_no: int | None
    kind: str
    left_spans: tuple[tuple[int, int], ...] = ()
    right_spans: tuple[tuple[int, int], ...] = ()
    left_eol: str = ''
    right_eol: str = ''


@dataclass(frozen=True)
class DiffResult:
    rows: tuple[AlignedRow, ...]
    blocks: tuple[int, ...]
    left_text: str
    right_text: str
    exact_equal: bool
    rule_equal: bool
    unified_diff: str
    left_duplicate_keys: tuple[str, ...] = ()
    right_duplicate_keys: tuple[str, ...] = ()
    mode: str = 'text'


class _Budget:
    def __init__(self):
        self.deadline = monotonic() + MAX_SECONDS
        self.remaining = MAX_MATCH_OPERATIONS
        self.ticks = 0

    def check(self):
        if monotonic() >= self.deadline:
            raise ValueError('差异比较超过 2 秒时间预算。')

    def spend(self):
        self.remaining -= 1
        self.ticks += 1
        if self.remaining < 0:
            raise ValueError('差异匹配复杂度超过预算（可能包含大量重复行）。')
        if self.ticks % 256 == 0:
            self.check()


class _BoundedMatcher(SequenceMatcher):
    """对最长连续匹配的每次候选访问计费，禁止 autojunk 隐藏重复行。"""

    def __init__(self, a, b, budget):
        self.budget = budget
        budget.check()
        super().__init__(None, a, b, autojunk=False)
        budget.check()

    def find_longest_match(self, alo=0, ahi=None, blo=0, bhi=None):
        from difflib import Match

        self.budget.check()
        a, b = self.a, self.b
        ahi = len(a) if ahi is None else ahi
        bhi = len(b) if bhi is None else bhi
        best_i, best_j, best_size = alo, blo, 0
        previous = {}
        for i in range(alo, ahi):
            self.budget.spend()
            current = {}
            for j in self.b2j.get(a[i], ()):
                self.budget.spend()
                if j < blo:
                    continue
                if j >= bhi:
                    break
                size = current[j] = previous.get(j - 1, 0) + 1
                if size > best_size:
                    best_i, best_j, best_size = i - size + 1, j - size + 1, size
            previous = current
        # 没有 junk/popular 元素，但仍保留标准算法的边界扩展语义。
        while best_i > alo and best_j > blo and a[best_i - 1] == b[best_j - 1]:
            self.budget.spend()
            best_i -= 1
            best_j -= 1
            best_size += 1
        while best_i + best_size < ahi and best_j + best_size < bhi and a[best_i + best_size] == b[best_j + best_size]:
            self.budget.spend()
            best_size += 1
        self.budget.check()
        return Match(best_i, best_j, best_size)


def _lines(text, budget):
    lines = []
    start = 0
    for match in _EOL_RE.finditer(text):
        lines.append((text[start:match.start()], match.group()))
        start = match.end()
        if len(lines) > MAX_TOTAL_LINES:
            raise ValueError('有效文本总行数最多允许 10000 行。')
        if len(lines) % 256 == 0:
            budget.check()
    if start < len(text):
        lines.append((text[start:], ''))
    return lines


def _keys(lines, trailing, endings):
    return [(text.rstrip(' \t') if trailing else text,
             '\n' if endings and eol else eol) for text, eol in lines]


def _whole_span(text):
    return ((0, len(text)),) if text else ()


def _spans(left, right, budget):
    if left == right:
        return (), ()
    # 长行仅整行高亮，不进行昂贵的字符匹配；行差异本身仍精确。
    if max(len(left), len(right)) > MAX_CHARACTER_LINE or len(left) * len(right) > MAX_CHARACTER_PRODUCT:
        return _whole_span(left), _whole_span(right)
    matcher = _BoundedMatcher(left, right, budget)
    left_spans, right_spans = [], []
    for kind, i, j, k, l in matcher.get_opcodes():
        if kind != 'equal':
            if i != j:
                left_spans.append((i, j))
            if k != l:
                right_spans.append((k, l))
    return tuple(left_spans), tuple(right_spans)


def _range(start, stop):
    length = stop - start
    if length == 1:
        return str(start + 1)
    return f'{start if length == 0 else start + 1},{length}'


def _unified(matcher, left, right, normalized, budget):
    parts = []
    if normalized:
        parts.append('# 规范化副本差异（normalized copies）：不可作为原文 patch。\n')

    def emit(prefix, line):
        text, eol = line
        parts.append(prefix + text + (eol or '\n'))
        if not eol:
            parts.append('\\ No newline at end of file\n')

    header = False
    for group in matcher.get_grouped_opcodes(3):
        budget.check()
        if not header:
            parts.extend(('--- left\n', '+++ right\n'))
            header = True
        first, last = group[0], group[-1]
        parts.append(f'@@ -{_range(first[1], last[2])} +{_range(first[3], last[4])} @@\n')
        for kind, i, j, k, l in group:
            if kind == 'equal':
                for line in left[i:j]:
                    emit(' ', line)
            else:
                for line in left[i:j]:
                    emit('-', line)
                for line in right[k:l]:
                    emit('+', line)
    budget.check()
    return ''.join(parts)


MAX_JSON_FIELDS = 10000
MAX_JSON_OUTPUT_CHARACTERS = json_tools.MAX_OUTPUT_BYTES
_SIMPLE_JSON_KEY = re.compile(r'[A-Za-z_][A-Za-z0-9_]*\Z')
# 行对齐身份使用的键名/字符串模式；只读不写，不改变任何原文。
_JSON_MEMBER_RE = re.compile(r'^[ \t]*"((?:[^"\\]|\\[\s\S])*)"[ \t]*:')
_JSON_STRING_RE = re.compile(r'"(?:[^"\\]|\\[\s\S])*"')
_MISSING = object()


def _json_document(raw):
    """两侧都是 JSON 容器时返回 True；任何失败都退化为普通文本比较。"""
    text = raw.strip()
    if not text or text[0] not in '{[':
        return False
    try:
        json_tools.transform_json(raw, 'compress')
    except (ValueError, TypeError):
        return False
    return True


def _json_identities(lines):
    """生成行身份用于跨侧配对：对象成员用"深度 + 键名"，结构行用"角色 + 深度"。

    键名相同才算同一字段，故字段顺序不同的同名字段也能配对；数组元素、
    花括号等结构行按角色配对，值不同则交给差异判定，不会被误当成不同字段。
    """
    identities = []
    depth = 0
    for text, _ in lines:
        member = _JSON_MEMBER_RE.match(text)
        if member:
            identities.append(f'\x00key/{depth}/{member.group(1)}')
        else:
            stripped = text.strip()
            if not stripped:
                identities.append('\x00blank')
            elif stripped in ('{', '['):
                identities.append(f'\x00open/{depth}')
            elif stripped in ('}', ']'):
                identities.append(f'\x00close/{depth}')
            else:
                identities.append(f'\x00item/{depth}')
        cleaned = _JSON_STRING_RE.sub('""', text)
        depth += (cleaned.count('{') + cleaned.count('[')
                  - cleaned.count('}') - cleaned.count(']'))
        depth = max(depth, 0)
    return identities


def _member_key(line):
    match = _JSON_MEMBER_RE.match(line)
    return match.group(1) if match else None


def _member_value(line):
    match = _JSON_MEMBER_RE.match(line)
    return None if match is None else line[match.end():].strip().rstrip(',')


JSON_LOOKAHEAD = 64


def _align_json_lines(left_lines, right_lines, budget):
    """按行身份合并对齐两侧文本行，返回 (左下标, 右下标) 序列（缺侧为 None）。

    只有身份相同才配对成一行；同名字段即使两侧顺序不同也会对齐。
    身份各不相同时各自独占一行，绝不错配成"两个不同字段挤在一行"。
    前视窗口内优先同步到更近的同身份行，避免把配对机会让给远处。
    """
    left_ids = _json_identities(left_lines)
    right_ids = _json_identities(right_lines)
    right_index = {}
    for index, identity in enumerate(right_ids):
        right_index.setdefault(identity, []).append(index)

    def next_right(identity, start, stop):
        for index in right_index.get(identity, ()):
            if start <= index < stop:
                return index
        return None

    def next_left(identity, start, stop):
        for index in range(start, min(stop, len(left_ids))):
            if left_ids[index] == identity:
                return index
        return None

    pairs = []
    i = j = 0
    while i < len(left_ids) and j < len(right_ids):
        budget.spend()
        if left_ids[i] == right_ids[j]:
            pairs.append((i, j))
            i += 1
            j += 1
            continue
        ahead = next_right(left_ids[i], j + 1, j + 1 + JSON_LOOKAHEAD)
        behind = next_left(right_ids[j], i + 1, i + 1 + JSON_LOOKAHEAD)
        if ahead is not None and (behind is None or ahead - j <= behind - i):
            pairs.append((None, j))
            j += 1
        elif behind is not None:
            pairs.append((i, None))
            i += 1
        else:
            # 两侧身份各不相同：各自独占一行，绝不把不同字段挤到同一行。
            pairs.append((i, None))
            pairs.append((None, j))
            i += 1
            j += 1
    while i < len(left_ids):
        pairs.append((i, None))
        i += 1
    while j < len(right_ids):
        pairs.append((None, j))
        j += 1
    return pairs


def _reclassify_shared_keys(rows):
    """两侧同名字段未能配对时，按"两侧是否都有这个字段"重新判定颜色。

    值不同 → 'shared_change'：存在侧标黄，另一侧留空，不再标红。
    值相同 → 'neutral'：仅位置错开，不属于差异，不标任何颜色。
    真正只有一侧存在的字段保持 insert/delete，继续标红。

    绝不增删或重排行，保证两侧行号仍严格单调、原文可逐行重建。
    """
    left_values, right_values = {}, {}
    for row in rows:
        if row.left_no is not None:
            key = _member_key(row.left_text)
            if key is not None:
                left_values.setdefault(key, set()).add(_member_value(row.left_text))
        if row.right_no is not None:
            key = _member_key(row.right_text)
            if key is not None:
                right_values.setdefault(key, set()).add(_member_value(row.right_text))
    shared = set(left_values) & set(right_values)
    if not shared:
        return rows
    result = []
    for row in rows:
        if row.kind == 'delete' and _member_key(row.left_text) in shared:
            key = _member_key(row.left_text)
            kind = 'neutral' if _member_value(row.left_text) in right_values[key] else 'shared_change'
            spans = () if kind == 'neutral' else _whole_span(row.left_text)
            result.append(AlignedRow(row.left_text, '', row.left_no, None,
                                     kind, spans, (), row.left_eol, ''))
            continue
        if row.kind == 'insert' and _member_key(row.right_text) in shared:
            key = _member_key(row.right_text)
            kind = 'neutral' if _member_value(row.right_text) in left_values[key] else 'shared_change'
            spans = () if kind == 'neutral' else _whole_span(row.right_text)
            result.append(AlignedRow('', row.right_text, None, row.right_no,
                                     kind, (), spans, '', row.right_eol))
            continue
        result.append(row)
    return result


@dataclass(frozen=True)
class _JsonNumber:
    text: str
    value: Decimal


def _parse_json_fields(raw, budget, field_count):
    # 复用严格语法、UTF-8 输入、深度及输出预算，数字校验不经过 float/int。
    json_tools.transform_json(raw, 'compress')
    budget.check()
    tokens = []
    for match in json_tools._tokens(raw):
        budget.spend()
        tokens.append(match)
    position = 0

    def parse(root=False):
        nonlocal position
        budget.spend()
        match = tokens[position]
        token = match.group()
        position += 1
        if not root or token not in ('{', '[') or tokens[position].group() in ('}', ']'):
            field_count[0] += 1
            if field_count[0] > MAX_JSON_FIELDS:
                raise ValueError('左右 JSON 字段总数最多允许 10000 项。')
        if token == '{':
            result = {}
            while tokens[position].group() != '}':
                key_match = tokens[position]
                key = json.loads(key_match.group())
                position += 2  # 已校验的键及冒号。
                if key in result:
                    raise json.JSONDecodeError('JSON 不允许重复键 ' + json.dumps(key, ensure_ascii=True),
                                               raw, key_match.start())
                result[key] = parse()
                if tokens[position].group() != ',':
                    break
                position += 1
            position += 1
            return result
        if token == '[':
            result = []
            while tokens[position].group() != ']':
                result.append(parse())
                if tokens[position].group() != ',':
                    break
                position += 1
            position += 1
            return result
        if token.startswith('"') or token in ('null', 'true', 'false'):
            return json.loads(token)
        # 限制指数文本后才转整数；绝不展开指数或用上下文舍入数值。
        exponent = re.split('[eE]', token)[-1] if 'e' in token.lower() else '0'
        digits = exponent.lstrip('+-').lstrip('0') or '0'
        if len(digits) > 7 or int(digits) > min(MAX_CHARACTERS, MAX_EMAX - len(token)):
            raise json.JSONDecodeError('JSON 数字指数超过精确比较预算', raw, match.start())
        try:
            return _JsonNumber(token, Decimal(token))
        except InvalidOperation:
            raise json.JSONDecodeError('JSON 数字超过精确比较预算', raw, match.start()) from None

    return parse(root=True)


def compare_json_fields(left, right):
    """只读结构比较；原文用于复制，字段行不是原文行，blocks 每项独立。

    对象按解码键排序，数组按索引；缺失或类型改变的子树仅显示一项。
    字段模式 unified_diff 为空；非法 JSON、重复键及预算超限抛 ValueError。
    """
    if not isinstance(left, str) or not isinstance(right, str):
        raise TypeError('左右输入必须是字符串。')
    if max(len(left), len(right)) > MAX_CHARACTERS:
        raise ValueError('每侧文本最多允许 1 MiB 字符（1048576 个字符）。')
    budget = _Budget()
    budget.check()
    parsed, errors, field_count = [], [], [0]
    for label, raw in (('左侧', left), ('右侧', right)):
        try:
            parsed.append(_parse_json_fields(raw, budget, field_count))
        except json.JSONDecodeError as error:
            errors.append(f'{label} JSON：{error.msg}（第 {error.lineno} 行，第 {error.colno} 列）。')
        except ValueError as error:
            errors.append(f'{label} JSON：{error}')
        budget.check()
    if errors:
        raise ValueError('\n'.join(errors))
    rows, blocks = [], []
    numbers = [0, 0]
    output_size = 0

    def charge(text):
        nonlocal output_size
        budget.spend()
        output_size += len(text)
        if output_size > MAX_JSON_OUTPUT_CHARACTERS:
            raise ValueError('JSON 字段展示输出超过字符预算。')
        return text

    def render(value):
        if isinstance(value, dict):
            parts = [charge('{')]
            for index, key in enumerate(sorted(value)):
                if index:
                    parts.append(charge(','))
                parts.append(charge(json.dumps(key, ensure_ascii=True) + ':'))
                parts.append(render(value[key]))
            parts.append(charge('}'))
            return ''.join(parts)
        if isinstance(value, list):
            parts = [charge('[')]
            for index, child in enumerate(value):
                if index:
                    parts.append(charge(','))
                parts.append(render(child))
            parts.append(charge(']'))
            return ''.join(parts)
        return charge(value.text if isinstance(value, _JsonNumber)
                      else json.dumps(value, ensure_ascii=True, separators=(',', ':')))

    def walk(path, lv, rv):
        budget.spend()
        if type(lv) is type(rv) and isinstance(lv, (dict, list)) and (lv or rv):
            if isinstance(lv, dict):
                for key in sorted(lv.keys() | rv.keys()):
                    suffix = '.' + key if _SIMPLE_JSON_KEY.fullmatch(key) else '[' + json.dumps(key, ensure_ascii=True) + ']'
                    walk(path + suffix, lv.get(key, _MISSING), rv.get(key, _MISSING))
            else:
                for index in range(max(len(lv), len(rv))):
                    walk(f'{path}[{index}]', lv[index] if index < len(lv) else _MISSING,
                         rv[index] if index < len(rv) else _MISSING)
            return
        if len(rows) >= MAX_JSON_FIELDS:
            raise ValueError('JSON 展示字段总数最多允许 10000 项。')
        if lv is _MISSING:
            kind = 'insert'
        elif rv is _MISSING:
            kind = 'delete'
        else:
            equal = type(lv) is type(rv) and (lv.value == rv.value if isinstance(lv, _JsonNumber) else lv == rv)
            kind = 'equal' if equal else 'replace'
        display, item_numbers = [], []
        for side, value in enumerate((lv, rv)):
            if value is _MISSING:
                display.append('')
                item_numbers.append(None)
            else:
                display.append(charge(path + ' = ') + render(value))
                numbers[side] += 1
                item_numbers.append(numbers[side])
        if kind != 'equal':
            blocks.append(len(rows))
        rows.append(AlignedRow(*display, *item_numbers, kind,
                               _whole_span(display[0]) if kind != 'equal' else (),
                               _whole_span(display[1]) if kind != 'equal' else ()))

    walk('$', *parsed)
    budget.check()
    return DiffResult(tuple(rows), tuple(blocks), left, right, left == right,
                      not blocks, '', mode='json_fields')


def compare_text(left, right, ignore_trailing_space=False, ignore_line_endings=False, json_format=False,
                 json_fields=False):
    """返回不可变差异结果。

    行号从 1 开始；blocks 是连续非 equal 显示区段的 0-based 起点。
    exact_equal 比较原始输入，rule_equal 比较有效副本的规则化行。
    rows 保留有效副本中的真实差异（忽略的差异仍可查看），EOL 独立保存。
    忽略行尾空白仅移除空格及 tab；忽略 EOL 统一 CR/LF/CRLF，但不移除
    最后一行是否终止的区别。长行字符高亮降为整行，绝不近似行匹配。
    unified_diff 保留原始 EOL；无 EOL 的记录追加标准 No newline 标记。
    JSON/忽略模式的导出始终带副本说明，即使规则化后没有差异。
    json_fields 优先于其他副本规则，使用结构值比较并保留原文供复制。
    """
    if json_fields:
        return compare_json_fields(left, right)
    if not isinstance(left, str) or not isinstance(right, str):
        raise TypeError('左右输入必须是字符串。')
    if max(len(left), len(right)) > MAX_CHARACTERS:
        raise ValueError('每侧文本最多允许 1 MiB 字符（1048576 个字符）。')
    budget = _Budget()
    exact_equal = left == right
    left_lines, right_lines = _lines(left, budget), _lines(right, budget)
    if len(left_lines) + len(right_lines) > MAX_TOTAL_LINES:
        raise ValueError('有效文本总行数最多允许 10000 行。')
    duplicates = [(), ()]
    if json_format:
        copies, errors = [], []
        for label, raw in (('左侧', left), ('右侧', right)):
            try:
                transformed = json_tools.transform_json(raw, 'format')
                copies.append(transformed)
            except ValueError as error:
                errors.append(f'{label} JSON：{error}')
            budget.check()
        if errors:
            raise ValueError('\n'.join(errors))
        left, right = (copy.text for copy in copies)
        duplicates = [copy.duplicate_keys for copy in copies]
        if max(len(left), len(right)) > MAX_CHARACTERS:
            raise ValueError('JSON 有效副本超过每侧 1 MiB 字符限制。')
        left_lines, right_lines = _lines(left, budget), _lines(right, budget)
        if len(left_lines) + len(right_lines) > MAX_TOTAL_LINES:
            raise ValueError('有效文本总行数最多允许 10000 行。')
    left_keys = _keys(left_lines, ignore_trailing_space, ignore_line_endings)
    right_keys = _keys(right_lines, ignore_trailing_space, ignore_line_endings)
    rule_equal = left_keys == right_keys
    # 仅 JSON 格式化模式下（两侧都是 JSON）按"深度 + 键名"合并对齐，让同名字段
    # 必然配到同一行。原文比较模式必须保持按行位置对齐的原始语义，不得格式化。
    json_aligned = (json_format and not rule_equal
                    and _json_document(left) and _json_document(right))
    matcher = _BoundedMatcher(left_keys, right_keys, budget)
    # 完全规则相等时跳过重复行搜索，仍使用准确的完整 equal opcode。
    if rule_equal:
        matcher.opcodes = [('equal', 0, len(left_keys), 0, len(right_keys))] if left_keys else []
    rows = []
    if json_aligned:
        left_ids = _json_identities(left_lines)
        right_ids = _json_identities(right_lines)
        for li, ri in _align_json_lines(left_lines, right_lines, budget):
            lt, le = left_lines[li] if li is not None else ('', '')
            rt, reol = right_lines[ri] if ri is not None else ('', '')
            if li is None:
                kind, ls, rs = 'insert', (), _whole_span(rt)
            elif ri is None:
                kind, ls, rs = 'delete', _whole_span(lt), ()
            elif left_ids[li] == right_ids[ri] and (lt, le) == (rt, reol):
                # 同一字段且整行（含 EOL）完全一致才算 equal。
                kind, ls, rs = 'equal', (), ()
            elif left_ids[li] == right_ids[ri] and lt.strip() == rt.strip():
                kind, ls, rs = 'neutral', (), ()
            else:
                kind = 'replace'
                ls, rs = _spans(lt, rt, budget)
            rows.append(AlignedRow(lt, rt, li + 1 if li is not None else None,
                                   ri + 1 if ri is not None else None, kind, ls, rs, le, reol))
    else:
        for _, i, j, k, l in matcher.get_opcodes():
            budget.check()
            for offset in range(max(j - i, l - k)):
                if offset % 256 == 0:
                    budget.check()
                li = i + offset if i + offset < j else None
                ri = k + offset if k + offset < l else None
                lt, le = left_lines[li] if li is not None else ('', '')
                rt, reol = right_lines[ri] if ri is not None else ('', '')
                if li is None:
                    kind, ls, rs = 'insert', (), _whole_span(rt)
                elif ri is None:
                    kind, ls, rs = 'delete', _whole_span(lt), ()
                elif (lt, le) == (rt, reol):
                    kind, ls, rs = 'equal', (), ()
                else:
                    kind = 'replace'
                    ls, rs = _spans(lt, rt, budget)
                rows.append(AlignedRow(lt, rt, li + 1 if li is not None else None,
                                       ri + 1 if ri is not None else None, kind, ls, rs, le, reol))
    # 同名字段跨行改判（shared_change/neutral）只属于 JSON 对齐路径的产物；
    # 原文比较模式不做此改判，单侧字段一律保持 insert/delete 的红色语义。
    if not rule_equal and json_aligned:
        rows = _reclassify_shared_keys(rows)
    # 连续非 equal 显示区段的起点；neutral（同名同值）与 equal 一样不构成差异。
    blocks, previous_equal = [], True
    for index, row in enumerate(rows):
        is_difference = row.kind not in ('equal', 'neutral')
        if is_difference and previous_equal:
            blocks.append(index)
        previous_equal = not is_difference
    unified = _unified(matcher, left_keys, right_keys,
                       json_format or ignore_trailing_space or ignore_line_endings, budget)
    budget.check()
    return DiffResult(tuple(rows), tuple(blocks), left, right, exact_equal, rule_equal,
                      unified, duplicates[0], duplicates[1])
