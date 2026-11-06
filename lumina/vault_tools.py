"""密匣（隐秘数据记录）的纯计算层：存款利息、到期倒计时、按银行聚合。

**这个模块不碰 Tk、不碰 sqlite**，全部是纯函数，``today`` 一律由调用方注入。
理由：金额与天数的算错是「用户不会立刻发现、但会照着做决定」的那类错误，
必须能用普通单元测试逐条钉住；一旦混进控件或数据库就没法这样测。
界面（``vault_window``）只负责取值、展示与报错。

## 利息口径：单利按月份折算

   利息 = 本金 × 年利率 × 存期月数 ÷ 12

**不用银行的「逐笔计息法」（年利率 ÷ 360 × 实际天数）**：那个口径要求录入
起存日，而结果会比存单上印的到期利息略高一点——用户拿着存单核对会对不上，
反而怀疑这里算错了。整存整取类产品用上面的折算与银行实际给付基本一致，
且只依赖「本金 / 利率 / 期限 / 到期日」四个用户一定拿得到的字段。

**存期用「月数」而不是「天数」**：产品的期限本来就是按月的（3 个月、6 个月、
1 年、3 年…），按天折算会引入闰年、大小月这些与产品定义无关的噪声。

## 关于 50 万存款保险额度

**这个模块刻意不提供任何额度相关计算。** 用户明确要求「不用设置50万上限」，
聚合只按银行分组、只出「本金 / 利息 / 加权年化 / 最近到期」这几个事实值。
不要去加「距上限还有多少」这类判断——那正是被否掉的那版设计。
"""

import calendar
import datetime as dt
import math
import re

# 到期日的三档紧急度。三档是 ``matured``（已到期，含当天）/ ``urgent`` /
# ``normal``。``URGENT_DAYS`` 内的倒计时用警示色，
# ``SOON_DAYS`` 内的算进「90 天内到期」那张指标卡（**不含当天**，
# 当天归「已到期」）。
URGENT_DAYS = 7
SOON_DAYS = 90

# 金额单位后缀。``w`` / ``k`` 是中文语境里常见的简写，一并接受。
_AMOUNT_UNITS = {
    "亿": 100_000_000,
    "万": 10_000,
    "w": 10_000,
    "W": 10_000,
    "k": 1_000,
    "K": 1_000,
}

_AMOUNT_RE = re.compile(r"^([0-9]*\.?[0-9]+)\s*(亿|万|[wWkK])?$")
_YEAR_MONTH_RE = re.compile(r"^\s*(\d+)\s*年\s*(?:(\d+)\s*个?\s*月)?\s*$")
_MONTH_RE = re.compile(r"^\s*(\d+)\s*个?\s*月\s*$")
_DATE_RE = re.compile(
    r"^\s*(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?\s*$")


class VaultError(ValueError):
    """密匣的输入错误。``str(exc)`` 直接显示给用户，所以措辞要说人话。"""


# ---------- 输入解析 ----------

def parse_amount(text):
    """把本金/利息输入解析成 ``float``。空值返回 ``None``。

    接受 ``400000`` / ``40万`` / ``40w`` / ``400,000`` / ``¥40万`` / ``40万元``。
    中文单位是刚需：用户记存款时脑子里就是「40 万」，不该逼他数零。
    """
    if text is None:
        return None
    cleaned = str(text).strip().replace(",", "").replace("，", "")
    cleaned = cleaned.replace("¥", "").replace("￥", "").replace("元", "").strip()
    if not cleaned:
        return None
    match = _AMOUNT_RE.match(cleaned)
    if not match:
        raise VaultError(f"金额看不懂：{text}")
    value = float(match.group(1)) * _AMOUNT_UNITS.get(match.group(2), 1)
    if not math.isfinite(value) or value <= 0:
        raise VaultError("金额要大于 0 且为有限数值")
    return value


def format_amount(value):
    """金额显示：千分位、四舍五入到元。``None`` 显示为 ``—``。

    一律取整到元。存款利息算到分没有意义（实际到账也常按元计），而带小数的
    一串数字在表格里会破坏右对齐的可读性。
    """
    if value is None:
        return "—"
    return f"{round(value):,}"


def parse_rate(text):
    """把年利率解析成百分数数值。``3.1`` / ``3.1%`` / ``3.10％`` 都接受。"""
    if text is None:
        return None
    cleaned = str(text).strip().replace("%", "").replace("％", "").strip()
    if not cleaned:
        return None
    try:
        value = float(cleaned)
    except ValueError:
        raise VaultError(f"年利率看不懂：{text}") from None
    if not math.isfinite(value) or value <= 0 or value >= 100:
        raise VaultError("年利率请填 0 到 100 之间的百分数")
    return value


def format_rate(value):
    """年利率显示：两位小数 + 百分号。``None`` 显示为 ``—``。"""
    if value is None:
        return "—"
    return f"{value:.2f}%"


def parse_term(text):
    """把存期解析成**月数**（整数）。``36`` / ``3年`` / ``半年`` / ``6个月``。

    统一归一到月：产品期限本来就是按月定义的（3 个月、1 年、3 年），
    存成月数之后「按月份折算利息」这条口径就是直接用，不需要再换算。
    """
    if text is None:
        return None
    cleaned = str(text).strip()
    if not cleaned:
        return None
    if cleaned in ("半年", "半 年"):
        return 6
    match = _YEAR_MONTH_RE.match(cleaned)
    if match:
        months = int(match.group(1)) * 12 + int(match.group(2) or 0)
        if months <= 0:
            raise VaultError("存期要大于 0")
        return months
    match = _MONTH_RE.match(cleaned)
    if match:
        months = int(match.group(1))
        if months <= 0:
            raise VaultError("存期要大于 0")
        return months
    if cleaned.isdigit():
        months = int(cleaned)
        if months <= 0:
            raise VaultError("存期要大于 0")
        return months
    raise VaultError(f"存期看不懂：{text}")


def format_term(months):
    """存期显示：整年的写成「3 年」，否则「6 个月」。``None`` 显示为 ``—``。"""
    if months is None:
        return "—"
    months = int(months)
    if months % 12 == 0:
        return f"{months // 12} 年"
    if months > 12:
        return f"{months // 12} 年 {months % 12} 个月"
    return f"{months} 个月"


def parse_date(text, label="日期"):
    """把日期解析成 ``datetime.date``。``2027-03-15`` / ``2027/3/15`` / ``2027年3月15日``。

    ``label`` 是**报错时用哪个字段名**。默认「日期」，对话框传「到期日」——
    密匣里只有一个日期字段，但把字段名写进提示仍然值得：用户看到
    「到期日看不懂」比「日期看不懂」更快知道该改哪一格。
    """
    if text is None:
        return None
    cleaned = str(text).strip()
    if not cleaned:
        return None
    match = _DATE_RE.match(cleaned)
    if not match:
        raise VaultError(f"{label}看不懂：{text}")
    try:
        return dt.date(int(match.group(1)), int(match.group(2)),
                       int(match.group(3)))
    except ValueError:
        # 例如 2027-02-30。构造失败说明「这个日期不存在」，
        # 和「格式看不懂」是两回事，分开报更利于用户定位。
        raise VaultError(f"{label}没有这一天：{text}") from None


def calculate_end_date(start_text, term_text):
    """按完整月数计算到期日，目标月没有对应日时夹到月末。"""
    start = parse_date(start_text, label="起存日")
    if start is None:
        raise VaultError("请填起存日")
    months = parse_term(term_text)
    if months is None:
        raise VaultError("请填存期")
    year, month_index = divmod(start.year * 12 + start.month - 1 + months, 12)
    if not 1 <= year <= 9999:
        raise VaultError("存期超出日期范围")
    month = month_index + 1
    return dt.date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


def format_date(value):
    """日期显示：``YYYY-MM-DD``。接受 ``date`` / ``str`` / ``None``。"""
    if value is None:
        return "—"
    if isinstance(value, str):
        value = parse_date(value)
    return value.isoformat()


def field(item, name, default=None):
    """从 ``dict`` 或 ``sqlite3.Row`` 里取一个字段。

    **不能用 ``item.get(name)``**：``sqlite3.Row`` 没有 ``.get()``，一调就
    ``AttributeError``。也不能用 ``name in item`` 判断——对 ``Row`` 那判断的是
    **值**不是列名，恒为假（这个坑在本项目已经踩过两次，见 ``MEMORY.md``）。
    所以这里按 ``keys()`` 走，它对两种类型都返回正确的名字集合。
    """
    try:
        keys = item.keys()
    except AttributeError:
        return default
    return item[name] if name in keys else default


def deposit_interest(item):
    """一条存单的预计利息。字段名在这里集中一次，别处不再重复拼。"""
    return interest(field(item, "principal"), field(item, "rate"),
                    field(item, "months"))


# ---------- 单笔计算 ----------

def interest(principal, rate, months):
    """单利按月份折算：``本金 × 年利率 × 月数 ÷ 12``。

    ``rate`` 是百分数（3.1 表示 3.1%），不是小数。
    """
    if principal is None or rate is None or months is None:
        return 0.0
    return float(principal) * float(rate) / 100.0 * float(months) / 12.0


def days_left(end_date, today):
    """距到期还有多少天。已过期为负数，当天为 0。

    ``end_date`` 接受 ``date`` 或 ``"YYYY-MM-DD"`` 字符串（数据库里就是文本）。
    """
    end = parse_date(end_date) if isinstance(end_date, str) else end_date
    return (end - today).days


def due_state(days):
    """到期状态：``(档位, 文案)``。档位是 ``matured`` / ``urgent`` / ``normal``。

    这是全表**唯一需要一眼看到**的信息，所以文案直接写成「已到期 8 天」
    「剩 3 天」而不是一个孤零零的日期——用户不必自己做减法。

    **``days <= 0`` 一律归 ``matured``**。用户明确要求口径为「含今天到期」，
    所以当天不再算 ``urgent``。当天单独给一句「今天到期」而不是
    「已到期 0 天」：后者读起来像「可操作时间已经归零」，而今天其实还有
    一整天，那会让人误判成错过了。

    档位名是 ``matured``（已到期）而不是 ``overdue``（逾期）：这两个词的
    外延不同，``matured`` 含当天、``overdue`` 不含。留着旧名会让
    「当天到底算哪一档」这件事在代码里永远说不清。
    """
    if days <= 0:
        if days == 0:
            return "matured", "今天到期"
        return "matured", f"已到期 {-days} 天"
    if days <= URGENT_DAYS:
        return "urgent", f"剩 {days} 天"
    return "normal", f"剩 {days} 天"


# ---------- 聚合 ----------

def weighted_rate(deposits):
    """资金加权年化（百分数）：``总利息 ÷ Σ(本金 × 月数 ÷ 12)``。

    **分母是「本金×年限」而不是本金合计。** 加权年化的意义是「这些钱平均
    每年挣多少」，用本金直接平均会低估长期存款、高估短期存款——一笔 6 个月
    的存款和一笔 3 年的存款，占用的资金时间差 6 倍，不该等价看待。

    没有存款（或分母为 0）时返回 ``0.0``。
    """
    total_interest = 0.0
    total_year_amount = 0.0
    for item in deposits:
        total_interest += deposit_interest(item)
        months = field(item, "months") or 0
        total_year_amount += float(field(item, "principal") or 0) * months / 12.0
    if total_year_amount <= 0:
        return 0.0
    return total_interest / total_year_amount * 100.0


def summarize(deposits, today):
    """顶部四张指标卡的数据。

    返回 ``principal`` / ``interest`` / ``total`` / ``rate`` / ``soon_*`` /
    ``matured_*``。``total`` 是**本息合计**。

    **``matured`` 是「已到期」，含当天**（``days_left <= 0``）。用户明确
    要求按这个口径，所以当天到期的存单进这张卡、不进 ``soon``：两张卡
    必须互斥且穷尽，否则「90 天内到期 N 笔 + 已到期 M 笔」相加会大于
    总笔数，用户一眼就发现对不上。

    ``soon`` 因此收紧成 ``1 <= days <= SOON_DAYS``——第 0 天归 ``matured``。
    """
    principal = sum(float(field(i, "principal") or 0) for i in deposits)
    total_interest = sum(deposit_interest(i) for i in deposits)
    soon, matured = [], []
    for item in deposits:
        left = days_left(field(item, "end_date"), today)
        if left > 0 and left <= SOON_DAYS:
            soon.append(item)
        elif left <= 0:
            matured.append(item)
    return {
        "principal": principal,
        "interest": total_interest,
        "total": principal + total_interest,
        "rate": weighted_rate(deposits),
        "count": len(deposits),
        "soon_count": len(soon),
        "soon_principal": sum(float(field(i, "principal") or 0) for i in soon),
        "matured_count": len(matured),
        "matured_total": sum(
            float(field(i, "principal") or 0) + deposit_interest(i)
            for i in matured),
    }


def pick_urgent(deposits, today):
    """主行「到期」列该显示哪一笔：**最近的未来到期日优先，没有未来就看最近的过去**。

    主行不放「全部到期日」——一个银行可能有五笔存款，日期全列会把一行撑爆。
    只看未来的话，全部存单都已到期的银行会显示成空白，那比显示一个到期日
    更让人误解。所以未来的全都没有时，退回显示最近的那笔已到期。

    **「未来」的判据是 ``> 0``（不含当天）**：当天到期的存单归「已到期」那一档，
    如果这里把它算作「未来」，主行会显示一个「今天到期」而指标卡把它计入
    「已到期 N 笔」——同一笔钱在两处落在不同档位，用户会以为其中一个算错了。
    两者都按 ``days_left <= 0`` 划界，口径只有一个。
    """
    upcoming = [i for i in deposits
                if days_left(field(i, "end_date"), today) > 0]
    if upcoming:
        return min(upcoming,
                   key=lambda i: days_left(field(i, "end_date"), today))
    if deposits:
        return max(deposits,
                   key=lambda i: days_left(field(i, "end_date"), today))
    return None


def group_by_bank(banks, deposits, today):
    """按银行聚合，用于渲染「银行主行 + 存单子行」的两级结构。

    ``banks`` 是 ``[{'id', 'name'}, ...]``，``deposits`` 的每项带 ``bank_id``。
    **没有存款的银行也会返回**（``deposits`` 为空列表、``urgent`` 为 ``None``）：
    用户建了这家银行就说明他在那儿有业务，凭空消失会让人以为记录丢了。
    真正的孤儿存单（``bank_id`` 指向不存在的银行）兜底挂到返回列表末尾，
    不静默丢弃——数据在而看不见是最难排查的一类问题。
    """
    known = {bank["id"] for bank in banks}
    buckets = {bank["id"]: [] for bank in banks}
    orphans = []
    for item in deposits:
        bank_id = field(item, "bank_id")
        if bank_id in known:
            buckets[bank_id].append(item)
        else:
            orphans.append(item)
    groups = [_group(bank, buckets[bank["id"]], today) for bank in banks]
    if orphans:
        groups.append(_group(
            {"id": None, "name": "未归银行（数据异常）"}, orphans, today))
    return groups


def _group(bank, items, today):
    """把一家银行下的存单聚合成主行需要的那几个值。"""
    principal = sum(float(field(i, "principal") or 0) for i in items)
    total_interest = sum(deposit_interest(i) for i in items)
    return {
        "bank": bank,
        "deposits": items,
        "count": len(items),
        "principal": principal,
        "interest": total_interest,
        "rate": weighted_rate(items),
        "urgent": pick_urgent(items, today),
    }


def build_deposit(name, bank_id, amount_text, rate_text, term_text, date_text):
    """把六个输入框的原始文本校验并归一成一条存单记录。

    单独抽出来是为了让「用户填了什么 → 存进库的什么」这条路径可以被
    单元测试直接覆盖，不必建窗口。抛 ``VaultError`` 时消息已经可以直接显示。
    """
    label = (name or "").strip()
    if not label:
        raise VaultError("请先填存单名称")
    principal = parse_amount(amount_text)
    if principal is None:
        raise VaultError("请填本金")
    rate = parse_rate(rate_text)
    if rate is None:
        raise VaultError("请填年利率")
    months = parse_term(term_text)
    if months is None:
        raise VaultError("请填存期")
    end_date = parse_date(date_text, label="到期日")
    if end_date is None:
        raise VaultError("请填到期日")
    return {
        "name": label,
        "bank_id": bank_id,
        "principal": principal,
        "rate": rate,
        "months": months,
        "end_date": end_date.isoformat(),
    }
