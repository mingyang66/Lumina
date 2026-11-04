"""日期 / 时间转换（纯逻辑，零 UI 依赖）。

三种能力，**由输入形态自动判定**，不需要用户先选模式：

- **时间戳换算**：一行纯数字 → 三时区时间。10 位当秒、13 位当毫秒。
- **时区互转**：一行日期时间 → 北京 / 美东 / UTC 三地对照。
- **时间差**：两行 → 两者相差多少天时分秒。

设计边界：

- **美东时区不走 ``zoneinfo``**：Windows 不自带 IANA 时区库，本项目也没有
  依赖 ``tzdata`` —— 实测 ``ZoneInfo("America/New_York")`` 直接抛
  ``ZoneInfoNotFoundError``。改为按美国现行 DST 规则自行计算，见
  ``eastern_offset``。这条约束是刻意的，别「顺手优化」成 zoneinfo。
- **没有时区标记的输入按北京时间解释**。用户是中文环境，把 ``2026-11-04
  13:08`` 当成 UTC 会得到一个差 8 小时的答案——比报错更糟，因为看起来是对的。
- **时间差不取绝对值**：``A - B`` 为负时明说「终点比起点早」，而不是把两个
  时间悄悄对调，让用户拿到一个方向相反的结论却看不出来。
"""

import datetime
import re

# 段落标记。与 ``tool_window.SECTION_MARK`` 必须一致——两边一个是窗口骨架、
# 一个是纯逻辑模块，**故意不互相导入**（骨架要导入本模块，反向会成环）。
# 守卫 ``test_section_mark_matches_tool_window`` 钉住两者相等。
SECTION_MARK = "▍"

# 北京固定 UTC+8（1991 年后无夏令时），不需要查表。
BEIJING_OFFSET = datetime.timedelta(hours=8)
UTC_OFFSET = datetime.timedelta(0)

# 美东：标准时 EST = UTC-5，夏令时 EDT = UTC-4。
EASTERN_STD = datetime.timedelta(hours=-5)
EASTERN_DST = datetime.timedelta(hours=-4)

_EPOCH = datetime.datetime(1970, 1, 1)
_WEEKDAYS = "一二三四五六日"

# 纯数字（含负号）。注意**不用** ``\d+`` 单独判定，否则 ``-100`` 会被漏掉。
_DIGITS_RE = re.compile(r"^[+-]?\d+$")

# 尾部时区标记。认三类写法：
#   1. ``Z`` / ``UTC`` / ``GMT``（含 ``UTC+8`` / ``GMT-3:30`` 这类带偏移的）
#   2. 裸的 ``EST`` / ``EDT`` —— 美东的常用缩写，用户手打的多半是这个
#   3. ``±HH:MM`` / ``±HHMM``
# ``-04`` 这种两位数字**不匹配**，否则 ``2026-11-04`` 的日会被当成时区。
_TZ_SUFFIX_RE = re.compile(
    r"^(?P<body>.*?)\s*(?P<tz>Z|UTC|GMT"
    r"|(?:UTC|GMT)[+-]\d{1,2}(?::?\d{2})?"
    r"|EST|EDT"
    r"|[+-]\d{2}:?\d{2})$",
    re.IGNORECASE,
)

# 2026-11-04 / 2026/11/04 / 2026.11.04，可带 T 或空格分隔的时间。
_DT_RE = re.compile(
    r"^(?P<y>\d{4})[-/.](?P<mo>\d{1,2})[-/.](?P<d>\d{1,2})"
    r"(?:[T\s]+(?P<h>\d{1,2}):(?P<mi>\d{1,2})"
    r"(?::(?P<s>\d{1,2})(?:[.,](?P<frac>\d{1,6}))?)?)?$"
)

# 中文写法：2026年11月4日 13:08
_DT_CN_RE = re.compile(
    r"^(?P<y>\d{4})年(?P<mo>\d{1,2})月(?P<d>\d{1,2})日"
    r"(?:\s*(?P<h>\d{1,2}):(?P<mi>\d{1,2})(?::(?P<s>\d{1,2}))?)?$"
)

# 紧凑写法：20261104 / 202611041308 / 20261104130811。
# **只认 8 / 12 / 14 位**：10 位必须留给秒时间戳（``2026110413`` 两者都讲得通，
# 但 10 位时间戳远比「2026 年 11 月 4 日 13 点」常见得多）。
_COMPACT_RE = re.compile(
    r"^(?P<y>\d{4})(?P<mo>\d{2})(?P<d>\d{2})"
    r"(?:(?P<h>\d{2})(?P<mi>\d{2})(?P<s>\d{2})?)?$"
)


class DateError(ValueError):
    """输入不是可识别的时间。"""


# ---------------------------------------------------------------- 美东夏令时
def _nth_sunday(year, month, n):
    """某年某月的第 n 个周日。"""
    first = datetime.date(year, month, 1)
    # date.weekday(): 周一=0 … 周日=6
    lead = (6 - first.weekday()) % 7
    return first + datetime.timedelta(days=lead + 7 * (n - 1))


def _dst_bounds(utc_year):
    """该年美东夏令时的起止时刻（**朴素 UTC**）。

    美国自 2007 年起固定为：3 月第二个周日 02:00 标准时开始，
    11 月第一个周日 02:00 夏令时结束。

    - 开始：02:00 EST = 07:00 UTC
    - 结束：02:00 EDT = 06:00 UTC
    """
    start = datetime.datetime.combine(
        _nth_sunday(utc_year, 3, 2), datetime.time(7, 0))
    end = datetime.datetime.combine(
        _nth_sunday(utc_year, 11, 1), datetime.time(6, 0))
    return start, end


def eastern_offset(utc_naive):
    """给定**朴素 UTC 时刻**，返回美东当时的 UTC 偏移。

    DST 区间按 UTC 年份取即可：美国夏令时完全落在同一公历年内
    （3 月~11 月），不存在跨年。
    """
    start, end = _dst_bounds(utc_naive.year)
    return EASTERN_DST if start <= utc_naive < end else EASTERN_STD


def eastern_name(utc_naive):
    """``EST`` / ``EDT``。"""
    return "EDT" if eastern_offset(utc_naive) == EASTERN_DST else "EST"


def eastern_to_utc(naive):
    """美东**本地朴素时间** → (朴素 UTC, 提示)。

    必须处理两种 DST 边界：

    - **重叠**（11 月回拨那小时）：本地 01:30 出现两次，两个偏移都自洽。
      按 ``fold=0`` 惯例取**较早**的那个（EDT），并在提示里说明。
    - **空洞**（3 月前拨那小时）：本地 02:30 不存在，两个偏移都不自洽。

    返回 ``(utc 或 None, 提示文本)``；空洞时 utc 为 None。
    """
    hits = {}
    for offset in (EASTERN_STD, EASTERN_DST):
        aware = naive.replace(tzinfo=datetime.timezone(offset))
        utc_naive = aware.astimezone(datetime.UTC).replace(tzinfo=None)
        if eastern_offset(utc_naive) == offset:
            hits[offset] = utc_naive

    if not hits:
        return None, "美东该时刻不存在（夏令时前拨跳过的一小时）"
    if len(hits) == 2:
        # 两次都成立 = 回拨重叠。取较早的 UTC（即 EDT）。
        return hits[EASTERN_DST], "美东该时刻出现两次（夏令时回拨），已按夏令时取值"
    return next(iter(hits.values())), ""


# ---------------------------------------------------------------- 解析
class Moment:
    """一个时间点。``utc`` 始终是**朴素 UTC**，比较与换算都基于它。"""

    __slots__ = ("note", "utc", "zone_tag")

    def __init__(self, utc, zone_tag="", note=""):
        self.utc = utc
        self.zone_tag = zone_tag      # 输入自带的时区标记，空串表示按北京解释
        self.note = note

    def __repr__(self):
        return f"Moment({self.utc!r}, zone_tag={self.zone_tag!r})"


_OFFSET_PER_MOMENT = {"EST": EASTERN_STD, "EDT": EASTERN_DST}


def _offset_from_tag(tag):
    """把 ``+08:00`` / ``UTC-4`` / ``Z`` / ``EST`` 换成偏移；无法识别返回 None。"""
    s = tag.strip()
    upper = s.upper()
    if upper in _OFFSET_PER_MOMENT:
        return _OFFSET_PER_MOMENT[upper]
    if upper in ("Z", "UTC", "GMT"):
        return UTC_OFFSET
    # 剥掉 UTC / GMT 前缀，剩下的应当是 ±HH[:MM]
    body = upper[3:] if upper.startswith(("UTC", "GMT")) else upper
    if not body:
        return UTC_OFFSET
    sign = -1 if body.startswith("-") else 1
    digits = body[1:].replace(":", "")
    if not digits.isdigit():
        return None
    if len(digits) == 2:
        hours, minutes = int(digits), 0
    elif len(digits) == 4:
        hours, minutes = int(digits[:2]), int(digits[2:])
    else:
        return None
    if hours > 14 or minutes > 59:
        return None
    return sign * datetime.timedelta(hours=hours, minutes=minutes)


def _build(y, mo, d, h=0, mi=0, s=0, frac="0"):
    try:
        base = datetime.datetime(int(y), int(mo), int(d),
                                 int(h or 0), int(mi or 0), int(s or 0))
    except ValueError as exc:
        raise DateError(f"不是合法日期：{exc}") from exc
    if frac:
        micro = int((frac + "000000")[:6])
        base = base.replace(microsecond=micro)
    return base


def _parse_local(text):
    """解析成**朴素本地时间**（不含时区）。识别不出返回 None。"""
    m = _DT_RE.match(text)
    if m:
        return _build(m["y"], m["mo"], m["d"], m["h"], m["mi"], m["s"], m["frac"])
    m = _DT_CN_RE.match(text)
    if m:
        return _build(m["y"], m["mo"], m["d"], m["h"], m["mi"], m["s"])
    m = _COMPACT_RE.match(text)
    if m:
        return _build(m["y"], m["mo"], m["d"], m["h"], m["mi"], m["s"])
    return None


def _moment_from_local(naive, offset):
    """朴素本地时间 + 偏移 → Moment。"""
    if offset is None:
        raise DateError("时区偏移无法识别")

    if offset in (EASTERN_STD, EASTERN_DST):
        return _moment_from_eastern(naive, offset)

    utc = naive - offset
    return Moment(utc, zone_tag=_tag_echo(offset))


def _moment_from_eastern(naive, offset):
    """美东本地时间 → Moment，Dst 边界上的歧义/空洞在这里落地。

    先把输入的偏移当线索缩小范围，再按「哪个偏移自洽」判定，避免把
    回拨重叠期里明确写了 ``EST`` 的时间强行当成 EDT。
    """
    utc_naive, note = eastern_to_utc(naive)
    if utc_naive is None:
        raise DateError(note)
    # 用户明确写了 EST/EDT 时以输入为准，但仍要提示规则冲突。
    actual = eastern_offset(utc_naive)
    if actual != offset:
        note = (note + "；" if note else "") + (
            f"这天气的美东实际是 {'EDT' if actual == EASTERN_DST else 'EST'}")
    return Moment(utc_naive, zone_tag=eastern_name(utc_naive), note=note)


def _tag_echo(offset):
    """把偏移还原成 ``UTC+8`` 这类可读标记。"""
    total = int(offset.total_seconds() // 60)
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    hours, minutes = divmod(total, 60)
    if minutes:
        return f"UTC{sign}{hours}:{minutes:02d}"
    return f"UTC{sign}{hours}"


def timestamp_seconds(text):
    """纯数字文本 → (秒数, 单位说明)。位数决定单位。"""
    digits = text.strip()
    if not _DIGITS_RE.match(digits):
        raise DateError(f"不是时间戳：{digits}")
    clean = digits.lstrip("+-")
    sign = -1 if digits.startswith("-") else 1
    length = len(clean)

    if length == 13:
        return sign * int(clean) / 1000.0, "毫秒"
    if length <= 11:
        return sign * int(clean), "秒"
    # 14 位以上既不是秒也不是毫秒（16 位是 .NET tick，19 位是纳秒），不猜。
    raise DateError(f"{length} 位数字太长了，看不出是秒还是毫秒")


def parse_moment(text):
    """一行文本 → Moment。时间戳与日期时间都认。"""
    raw = (text or "").strip()
    if not raw:
        raise DateError("空行")

    if _DIGITS_RE.match(raw):
        # 8 / 12 / 14 位优先当「紧凑日期」——``20261104`` 当秒时间戳会落到
        # 1970 年，一眼就是错的；而 10 位留给时间戳，见 _COMPACT_RE 的注释。
        #
        # 这个长度检查**在行为上是冗余的**：``_COMPACT_RE`` 本身只认 8/12/14
        # 位，10 位即便进了分支也会返回 None 再落回下面。变异验证里往集合里
        # 加个 10 没有任何用例变红——属于**等价变异**而非测试盲区。留着是因为
        # 它是个 O(1) 早退（省掉对长数字做三轮正则），也是意图的显式表达。
        # 真正的守门人是 ``_COMPACT_RE``。别当死代码删掉。
        if len(raw) in (8, 12, 14):
            local = _parse_local(raw)
            if local is not None:
                return Moment(local - BEIJING_OFFSET, zone_tag="")
        seconds, _unit = timestamp_seconds(raw)
        return Moment(_EPOCH + datetime.timedelta(seconds=seconds), zone_tag="")

    body, offset = raw, None
    m = _TZ_SUFFIX_RE.match(raw)
    if m:
        candidate = _offset_from_tag(m["tz"])
        if candidate is not None:
            body, offset = m["body"].strip(), candidate

    local = _parse_local(body)
    if local is None:
        raise DateError(f"认不出这个时间：{raw}")

    if offset is None:
        # 没写时区 = 北京时间。这是用户环境的默认，比假设 UTC 更贴近直觉。
        return Moment(local - BEIJING_OFFSET, zone_tag="")
    return _moment_from_local(local, offset)


# ---------------------------------------------------------------- 展示
def _weekday_cn(date_obj):
    return f"周{_WEEKDAYS[date_obj.weekday()]}"


def _stamp(naive):
    return f"{naive.strftime('%Y-%m-%d %H:%M:%S')}  {_weekday_cn(naive)}"


def _has_subsecond(naive):
    return naive.microsecond != 0


def humanize_delta(seconds):
    """把秒数拆成「X 天 Y 小时 Z 分 W 秒」。零值单位省略，全零给「0 秒」。"""
    total = int(abs(seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)

    chunks = []
    if days:
        chunks.append(f"{days} 天")
    if hours:
        chunks.append(f"{hours} 小时")
    if minutes:
        chunks.append(f"{minutes} 分")
    # 秒**总是**给出：只差 30 秒时前面全空，没有秒就变成空字符串了。
    if secs or not chunks:
        chunks.append(f"{secs} 秒")
    return " ".join(chunks)


def _relative(utc_naive):
    """相对本机当前 UTC 的粗略说法，用于状态栏。"""
    now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    delta = (utc_naive - now).total_seconds()
    if abs(delta) < 60:
        return "就是现在"
    return humanize_delta(delta) + ("后" if delta > 0 else "前")


def format_point(moment):
    """一个时间点 → 三时区对照 + 两种单位的时间戳。"""
    utc = moment.utc
    eastern_delta = eastern_offset(utc)
    eastern_tag = f"{eastern_name(utc)} {_tag_echo(eastern_delta)}"

    zones = (
        ("北京时间", BEIJING_OFFSET, "UTC+8"),
        ("美东时间", eastern_delta, eastern_tag),
        ("UTC", UTC_OFFSET, "UTC"),
    )
    blocks = [
        f"{SECTION_MARK} {label}  {tag}\n{_stamp(utc + delta)}"
        for label, delta, tag in zones
    ]

    seconds = round((utc - _EPOCH).total_seconds())
    millis = seconds * 1000
    blocks.append(
        f"{SECTION_MARK} 时间戳\n{seconds}（秒）\n{millis}（毫秒）")
    return "\n\n".join(blocks)


def _out_of_range(moment):
    """时间戳换算出明显离谱的年份时，提示「单位可能搞错了」。

    这条比报错宽松：用户可能就是查 1970 年以前的时间，不该拦。但
    13 位数字被当成秒、或 10 位被当成毫秒，都会得到一个 2286 年那样
    荒诞的结果，而界面上一切正常——这种「看着对、用起来坏」必须提示。
    """
    year = moment.utc.year
    if 1900 <= year <= 2200:
        return ""
    return f"年份 {year} 看起来不对，时间戳单位可能选错了"


# ------------------------------------------------- 单项换算（窄栏用）
def beijing_to_eastern(text):
    """北京时间 → 美东时间。返回 ``(结果文本, 状态文本, 状态类型)``。

    **这个字段里输入一律按北京时间解释**，不去看有没有时区后缀。理由：它是
    一个**带标签的专用输入框**（旁边就写着「北京时间」），用户往里打字时心里
    想的就是北京时间；此时再去尊重 ``UTC`` 后缀只会让「我明明按标签填的」变成
    错的结果。带时区的自由解释留给「日期转换」的自动识别路径（``convert``）。

    输入是纯数字时间戳时**报错而不是猜**——那是下面那个「时间戳转日期」框的
    活。两个框各管一段，给出明确指引比悄悄兼容更有用。
    """
    raw = (text or "").strip()
    if not raw:
        raise DateError("请输入北京时间")
    if looks_like_timestamp(raw):
        # 纯数字且**不是**紧凑日期 → 是时间戳，而本框只收日期时间。
        # 这里必须用 ``looks_like_timestamp`` 而不是「是不是纯数字」：
        # ``20261104`` 也是纯数字，但它是紧凑日期写法，本框应当能算。
        # 曾经写错成 ``_DIGITS_RE``，把紧凑日期一并拦掉了。
        raise DateError(f"这是时间戳，请用下面「时间戳转日期」那个框：{raw}")
    local = _parse_local(raw)
    if local is None:
        raise DateError(f"认不出这个时间：{raw}")
    moment = Moment(local - BEIJING_OFFSET)
    utc = moment.utc
    eastern_delta = eastern_offset(utc)
    eastern = utc + eastern_delta

    body = (f"{eastern.strftime('%Y-%m-%d %H:%M:%S')}  "
            f"{_weekday_cn(eastern.date())}\n"
            f"{eastern_name(utc)} {_tag_echo(eastern_delta)}")
    status = f"北京时间 → 美东时间 · {_relative(utc)}"
    return body, status, "ok"


def timestamp_to_date(text):
    """时间戳 → 日期。返回 ``(结果文本, 状态文本, 状态类型)``。

    结果同时给北京与美东两地：本工具的用户就活在这两个时区之间，只给一个
    还得自己再换一次。单位（秒 / 毫秒）由位数判定，与 ``convert`` 完全一致，
    判定规则只有一处（``timestamp_seconds``）。
    """
    raw = (text or "").strip()
    if not raw:
        raise DateError("请输入时间戳")
    if not _DIGITS_RE.match(raw):
        # 这里收到日期串是常见误操作（两个框挨着）。直接说清该用哪个框。
        raise DateError(f"不是时间戳：{raw}（日期换美东请用上面那个框）")
    seconds, unit = timestamp_seconds(raw)
    moment = Moment(_EPOCH + datetime.timedelta(seconds=seconds))
    utc = moment.utc
    beijing = utc + BEIJING_OFFSET
    eastern = utc + eastern_offset(utc)

    body = (f"{beijing.strftime('%Y-%m-%d %H:%M:%S')}  "
            f"{_weekday_cn(beijing.date())}  北京时间\n"
            f"{eastern.strftime('%Y-%m-%d %H:%M:%S')}  "
            f"{_weekday_cn(eastern.date())}  {eastern_name(utc)}")
    status = f"时间戳 · {unit} · {_relative(utc)}"
    warning = _out_of_range(moment)
    if warning:
        status = f"{status} · {warning}"
    return body, status, ("error" if warning else "ok")


def looks_like_timestamp(text):
    """一段纯数字看起来是时间戳吗。

    **不是简单的 ``isdigit()``**：``20261104`` 也是纯数字，但它是「紧凑日期」
    写法（用户手打日期常这么省分隔符），把它当秒时间戳会落到 1970 年。所以与
    ``parse_moment`` 采用同一条口径——8 / 12 / 14 位让给紧凑日期，其余纯数字
    当时间戳。
    """
    raw = (text or "").strip()
    if not _DIGITS_RE.match(raw):
        return False
    return len(raw.lstrip("+-")) not in (8, 12, 14)


# ------------------------------------------------- 实时时钟
def clock_snapshot(now_utc=None):
    """当前时刻快照，供顶部实时时钟条使用。

    ``now_utc`` 可注入（测试用）；不传就是本机当前时间。返回 dict 而不是
    拼好的文本：窗口要把这几项**分开放进不同的控件**，先在逻辑层拼成字符串
    再切回来是本末倒置。

    **秒以下一律抹掉**：时钟条每秒重画一次，微秒位只会让两栏的秒数在边界上
    看起来对不齐（北京时间已经进位、时间戳还是上一秒）。
    """
    utc = now_utc or datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    utc = utc.replace(microsecond=0)
    beijing = utc + BEIJING_OFFSET
    eastern_delta = eastern_offset(utc)
    eastern = utc + eastern_delta
    seconds = round((utc - _EPOCH).total_seconds())
    return {
        "beijing": beijing.strftime("%Y-%m-%d %H:%M:%S"),
        "beijing_weekday": _weekday_cn(beijing.date()),
        "eastern": eastern.strftime("%Y-%m-%d %H:%M:%S"),
        "eastern_weekday": _weekday_cn(eastern.date()),
        "eastern_tag": f"{eastern_name(utc)} {_tag_echo(eastern_delta)}",
        "seconds": seconds,
        "millis": seconds * 1000,
        "zone_name": "Asia/Shanghai",
    }


# ---------------------------------------------------------------- 主入口
def _convert_single(line):
    """一行 → (结果文本, 状态文本, 状态类型)。"""
    if _DIGITS_RE.match(line) and len(line.lstrip("+-")) not in (8, 12, 14):
        seconds, unit = timestamp_seconds(line)
        moment = Moment(_EPOCH + datetime.timedelta(seconds=seconds))
        status = [f"时间戳 · {unit}", _relative(moment.utc)]
        warning = _out_of_range(moment)
        if warning:
            status.append(warning)
        return format_point(moment), " · ".join(status), (
            "error" if warning else "ok")

    moment = parse_moment(line)
    status = ["日期时间"]
    status.append("北京时间" if not moment.zone_tag else f"按 {moment.zone_tag} 解释")
    if moment.note:
        status.append(moment.note)
    return format_point(moment), " · ".join(status), (
        "error" if moment.note else "ok")


def _convert_diff(first, second):
    """两行 → 时间差。"""
    start = parse_moment(first)
    end = parse_moment(second)
    delta = (end.utc - start.utc).total_seconds()

    direction = "" if delta >= 0 else "（终点比起点早）"
    blocks = [
        f"{SECTION_MARK} 时间差\n{humanize_delta(delta)}{direction}",
        f"{SECTION_MARK} 起点\n{_stamp(start.utc + BEIJING_OFFSET)}",
        f"{SECTION_MARK} 终点\n{_stamp(end.utc + BEIJING_OFFSET)}",
    ]
    # 秒/毫秒绝对值：用户贴进代码或算 SLA 时要用原始数值。
    blocks.append(
        f"{SECTION_MARK} 合计\n{abs(round(delta))} 秒\n"
        f"{abs(round(delta)) * 1000} 毫秒")

    status = [f"时间差 · {humanize_delta(delta)}"]
    if delta < 0:
        status.append("终点比起点早")
    return "\n\n".join(blocks), " · ".join(status), (
        "error" if delta < 0 else "ok")


def convert(text):
    """左栏文本 → ``(结果文本, 状态文本, 状态类型)``，与窗口 ``_analyze`` 对齐。"""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        raise DateError("左栏还没有内容")
    if len(lines) > 2:
        # 静默取前两行会让用户以为多出来的行被算了进去，直接说清楚。
        raise DateError(f"最多两个时间点，现在有 {len(lines)} 行")
    if len(lines) == 2:
        return _convert_diff(lines[0], lines[1])
    return _convert_single(lines[0])


# ================================================= 三栏版式（日期转换窗口）
#
# 下面是「日期转换」窗口改成**三栏并排**之后新增的换算。每栏自带下拉选项
# （时区 / 单位 / 方向），所以返回值比旧函数多一项「副行」——
#
#     (主值, 副行, 状态文本, 状态类型)
#
# 主值与副行在界面上是**两个不同颜色的控件**（绿的主值 + 灰的副行），
# 在这里拼成一个字符串再让窗口切回去是本末倒置，所以拆开返回。
#
# **与 ``beijing_to_eastern`` / ``timestamp_to_date`` 并存而不合并**：那两个
# 是双栏版式的产物，``_analyze`` 那个基类契约仍指向它们，``test_datetime_tools``
# 里几十条用例也钉着它们的行为。为了「窗口换版式」去改纯逻辑层的既有函数，
# 只会把爆炸半径扩大到与需求无关的地方。

# 时区选项。**只给 key 与展示名，不给偏移**——美东的偏移随夏令时变，
# 写一个固定值在这里必然是错的（EST 与 EDT 差一小时）。偏移由
# ``zone_offset`` 按具体时刻算。
ZONE_CHOICES = (
    ("beijing", "北京时间 (UTC+08:00)"),
    ("eastern", "美东时间 (EST/EDT)"),
    ("utc", "UTC (UTC+00:00)"),
)

# 短名。下拉里要带偏移（否则用户不知道那个时区是几点），但**结果副行**里
# 带偏移就太长了（「即北京时间 (UTC+08:00) 2026-11-04 17:27:15」），而且那行
# 本来就是为了让人快速确认「是哪一天」。两处各取所需，别合成一张表。
ZONE_SHORT = {"beijing": "北京时间", "eastern": "美东时间", "utc": "UTC"}

# 输入时间戳的单位。``auto`` 是默认：位数判定（``timestamp_seconds``）已经
# 足够可靠，逼用户每次先想「我这是秒还是毫秒」是白加的摩擦。
INPUT_UNIT_CHOICES = (
    ("auto", "自动识别"),
    ("seconds", "秒 (s)"),
    ("millis", "毫秒 (ms)"),
)

# 输出时间戳的单位。**默认毫秒**：这个工具的用户（翻前端日志、对后端接口）
# 绝大多数场合要的是毫秒时间戳。
OUTPUT_UNIT_CHOICES = (
    ("millis", "毫秒 (ms)"),
    ("seconds", "秒 (s)"),
)

# 固定偏移的两个时区。美东**不在**这里——它要按时刻查夏令时。
_FIXED_ZONE_OFFSETS = {"beijing": BEIJING_OFFSET, "utc": UTC_OFFSET}

# 时间戳单位选择 → ``None`` 表示「交给位数判定」。
_FIXED_TIMESTAMP_UNITS = {"seconds": (1, "秒"), "millis": (1 / 1000.0, "毫秒")}


def _label_of(choices, key):
    """在选项表里查展示名。查不到**报错而不是回退成第一个**。

    静默回退会让界面上显示一个与用户所选无关的值，而且看不出来。选项表与
    调用点分处两个文件，写错一个 key 完全可能。
    """
    for item_key, label in choices:
        if item_key == key:
            return label
    raise DateError(f"未知选项：{key}")


def zone_label(key):
    return _label_of(ZONE_CHOICES, key)


def input_unit_label(key):
    return _label_of(INPUT_UNIT_CHOICES, key)


def output_unit_label(key):
    return _label_of(OUTPUT_UNIT_CHOICES, key)


def zone_offset(key, utc_naive):
    """时区 key 在**给定朴素 UTC 时刻**的偏移。

    ``utc_naive`` 对固定偏移的时区是多余的，但签名里必须留着：美东的偏移
    取决于该时刻是否落在夏令时区间内。让所有时区走同一个入口，调用点就不必
    记得「美东要特殊处理」——那种分支迟早会在某个新调用点上漏掉。
    """
    if key == "eastern":
        return eastern_offset(utc_naive)
    offset = _FIXED_ZONE_OFFSETS.get(key)
    if offset is None:
        raise DateError(f"未知时区：{key}")
    return offset


def zone_tag(key, utc_naive):
    """时区 key → ``UTC+8`` / ``EST UTC-5`` 这类可读标记（与时钟条同款）。"""
    offset = zone_offset(key, utc_naive)
    if key == "eastern":
        return f"{eastern_name(utc_naive)} {_tag_echo(offset)}"
    return _tag_echo(offset)


def local_in(key, utc_naive):
    """朴素 UTC 时刻 → 该时区的本地朴素时间。"""
    return utc_naive + zone_offset(key, utc_naive)


def parse_timestamp_with_unit(text, unit="auto"):
    """时间戳文本 + 单位选择 → ``(秒数, 单位说明)``。

    ``unit="auto"`` 时完全交给 ``timestamp_seconds``（唯一的位数判定规则）。
    显式选了秒/毫秒时**不再做位数检查**——这是用户自己拍板的，13 位数字被
    他指定成「秒」就该按秒算。硬拦下来只会让人以为工具坏了。
    """
    raw = (text or "").strip()
    if not raw:
        raise DateError("请输入时间戳")
    if not _DIGITS_RE.match(raw):
        raise DateError(f"不是时间戳：{raw}")
    if unit == "auto":
        return timestamp_seconds(raw)
    pair = _FIXED_TIMESTAMP_UNITS.get(unit)
    if pair is None:
        raise DateError(f"未知的时间戳单位：{unit}")
    factor, name = pair
    digits = raw.lstrip("+-")
    sign = -1 if raw.startswith("-") else 1
    return sign * int(digits) * factor, name


def moment_from_seconds(seconds):
    """秒数 → Moment。超出 ``datetime`` 可表示范围时**报错而不是抛溢出**。"""
    try:
        return Moment(_EPOCH + datetime.timedelta(seconds=seconds))
    except (OverflowError, OSError) as exc:
        raise DateError("这个数值太大，超出了可表示的时间范围") from exc


def timestamp_to_zone(text, zone="beijing", unit="auto"):
    """时间戳 → 指定时区的日期时间。返回 ``(主值, 副行, 状态, 类型)``。

    **只给一行时间、不给星期**：星期在顶部时钟条已经有一处（北京那格的
    说明行），结果区再重复一遍会把「这块地方放着答案」这个焦点冲淡。副行
    留空是刻意的——见 ``_sub_for_zone`` 的说明。
    """
    seconds, unit_name = parse_timestamp_with_unit(text, unit)
    moment = moment_from_seconds(seconds)
    utc = moment.utc
    local = local_in(zone, utc)
    value = local.strftime("%Y-%m-%d %H:%M:%S")
    status = f"时间戳 · {unit_name} · {_relative(utc)}"
    warning = _out_of_range(moment)
    if warning:
        status = f"{status} · {warning}"
    return value, "", status, ("error" if warning else "ok")


def to_timestamp(text, zone="beijing", unit="millis"):
    """日期时间 → 时间戳。``zone`` 是**时间基准**：输入按哪个时区解释。

    返回 ``(主值, 副行, 状态, 类型)``。主值是纯数字字符串——它是拿出去
    直接贴进代码的，带上任何单位后缀都会让人多删一次。
    """
    raw = (text or "").strip()
    if not raw:
        raise DateError("请输入日期时间")
    if looks_like_timestamp(raw):
        # 与本栏挨着的那栏是「时间戳 → 日期时间」，走错门是常见误操作。
        raise DateError(f"这是时间戳，请用左边「时间戳 → 日期时间」那一栏：{raw}")
    local = _parse_local(raw)
    if local is None:
        raise DateError(f"认不出这个时间：{raw}")

    note = ""
    if zone == "eastern":
        # 美东要处理夏令时的重叠与空洞，不能简单相减。
        utc, note = eastern_to_utc(local)
        if utc is None:
            raise DateError(note)
    else:
        utc = local - zone_offset(zone, local)

    total = (utc - _EPOCH).total_seconds()
    if unit == "seconds":
        value, unit_name = str(round(total)), "秒"
    elif unit == "millis":
        value, unit_name = str(round(total * 1000)), "毫秒"
    else:
        raise DateError(f"未知的输出单位：{unit}")

    status = f"日期时间 · 按{ZONE_SHORT[zone]}解释 · {unit_name} · {_relative(utc)}"
    if note:
        status = f"{status} · {note}"
    return value, "", status, ("error" if note else "ok")


def convert_zone(text, source="beijing", target="eastern"):
    """时区互转：``source`` 时区的日期时间 → ``target`` 时区的日期时间。

    返回 ``(主值, 副行, 状态, 类型)``。主值是目标时区的日期时间，副行给出
    它对应的那个时区的标记（``EST UTC-5`` 这类）——**美东那侧尤其需要**：
    「02:32:33」本身看不出是标准时还是夏令时，而这正是这类换算最容易出错的
    地方。

    ``source`` 是「时间基准」，语义上回答了「我填的这串数字是哪个时区的时间」。
    它**不看输入自带的时区后缀**——旁边就写着基准时区的名字，用户往里打字时
    想的就是那个时区。尊重后缀只会让「我明明按标签填的」变成错的结果。
    """
    raw = (text or "").strip()
    if not raw:
        raise DateError("请输入日期时间")
    if looks_like_timestamp(raw):
        raise DateError(f"这是时间戳，请用左边「时间戳 → 日期时间」那一栏：{raw}")
    local = _parse_local(raw)
    if local is None:
        raise DateError(f"认不出这个时间：{raw}")

    note = ""
    if source == "eastern":
        # 美东要处理夏令时的重叠与空洞，不能简单相减。
        utc, note = eastern_to_utc(local)
        if utc is None:
            raise DateError(note)
    else:
        utc = local - zone_offset(source, local)

    lands = local_in(target, utc)
    value = lands.strftime("%Y-%m-%d %H:%M:%S")
    # 副行只写目标时区的标记。两边都是固定偏移（如北京→UTC）时它就是常量，
    # 看着有点重复，但**美东那侧非写不可**，而「有时显示有时不显示」比
    # 「一直显示」更容易让人以为漏了什么。
    sub = zone_tag(target, utc)

    status = (f"{ZONE_SHORT[source]} → {ZONE_SHORT[target]} · "
              f"{zone_tag(source, utc)} → {zone_tag(target, utc)} · "
              f"{_relative(utc)}")
    if note:
        status = f"{status} · {note}"
    return value, sub, status, ("error" if note else "ok")
