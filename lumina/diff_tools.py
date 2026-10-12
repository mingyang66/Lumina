"""保真文本比较；超出匹配预算时拒绝比较，不返回近似的行差异。"""

from dataclasses import dataclass
from difflib import SequenceMatcher
import re
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


def compare_text(left, right, ignore_trailing_space=False, ignore_line_endings=False, json_format=False):
    """返回不可变差异结果。

    行号从 1 开始；blocks 是连续非 equal 显示区段的 0-based 起点。
    exact_equal 比较原始输入，rule_equal 比较有效副本的规则化行。
    rows 保留有效副本中的真实差异（忽略的差异仍可查看），EOL 独立保存。
    忽略行尾空白仅移除空格及 tab；忽略 EOL 统一 CR/LF/CRLF，但不移除
    最后一行是否终止的区别。长行字符高亮降为整行，绝不近似行匹配。
    unified_diff 保留原始 EOL；无 EOL 的记录追加标准 No newline 标记。
    JSON/忽略模式的导出始终带副本说明，即使规则化后没有差异。
    """
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
    matcher = _BoundedMatcher(left_keys, right_keys, budget)
    # 完全规则相等时跳过重复行搜索，仍使用准确的完整 equal opcode。
    if rule_equal:
        matcher.opcodes = [('equal', 0, len(left_keys), 0, len(right_keys))] if left_keys else []
    rows, blocks = [], []
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
            if kind != 'equal' and (not rows or rows[-1].kind == 'equal'):
                blocks.append(len(rows))
            rows.append(AlignedRow(lt, rt, li + 1 if li is not None else None,
                                   ri + 1 if ri is not None else None, kind, ls, rs, le, reol))
    unified = _unified(matcher, left_keys, right_keys,
                       json_format or ignore_trailing_space or ignore_line_endings, budget)
    budget.check()
    return DiffResult(tuple(rows), tuple(blocks), left, right, exact_equal, rule_equal,
                      unified, duplicates[0], duplicates[1])
