import calendar
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

KINDS = {"expense": "支出", "income": "收入", "transfer": "转账", "refund": "退款"}
DEFAULT_CATEGORIES = {
    "expense": ["餐饮", "交通", "购物", "住房", "娱乐", "医疗", "教育", "其他"],
    "income": ["工资", "奖金", "投资收益", "其他收入"],
    "transfer": ["转账"],
    "refund": ["餐饮", "交通", "购物", "住房", "娱乐", "医疗", "教育", "其他"],
}
MAX_AMOUNT_CENTS = 99_999_999_999  # 单笔上限 999,999,999.99 元。
RECORD_FIELDS = ("kind", "amount_cents", "category", "account", "entry_date", "note")


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("日期须为 YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("日期无效") from exc


def _today(value):
    if value is None:
        return date.today()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return _date(value)


def _validate_record(record):
    unknown = set(record) - set(RECORD_FIELDS)
    if unknown:
        raise ValueError("未知记账字段：" + ", ".join(sorted(unknown)))
    result = dict(record)
    result.setdefault("note", "")
    if set(result) != set(RECORD_FIELDS):
        raise ValueError("缺少必要记账字段")
    if result["kind"] not in KINDS:
        raise ValueError("无效的记账类型")
    cents = result["amount_cents"]
    if type(cents) is not int or not 0 < cents <= MAX_AMOUNT_CENTS:
        raise ValueError("金额分须为正整数且不超过 999,999,999.99 元")
    for field in ("category", "account", "note"):
        if not isinstance(result[field], str):
            raise ValueError("分类、账户及备注须为文本")
        result[field] = result[field].strip()
    if not result["category"] or not result["account"]:
        raise ValueError("分类和账户不能为空")
    _date(result["entry_date"])
    return result


def build_entry(kind, amount, category, account, entry_date, note=''):
    if isinstance(amount, bool) or not isinstance(amount, (str, int, float, Decimal)):
        raise ValueError("金额无效")
    try:
        value = Decimal(str(amount).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("金额无效") from exc
    if not value.is_finite() or value <= 0 or value > Decimal("999999999.99"):
        raise ValueError("金额须为正数且不超过 999,999,999.99 元")
    if value.as_tuple().exponent < -2:
        raise ValueError("金额最多两位小数")
    # 不使用浮点或依赖调用者的 Decimal 精度上下文。
    _sign, digits, exponent = value.as_tuple()
    coefficient = int(''.join(map(str, digits)))
    cents = coefficient * 10 ** (exponent + 2)
    return _validate_record({"kind": kind, "amount_cents": cents, "category": category,
                                 "account": account, "entry_date": entry_date, "note": note})


def format_money(cents):
    if type(cents) is not int:
        raise ValueError("金额分须为整数")
    whole, fraction = divmod(abs(cents), 100)
    return f"¥{'-' if cents < 0 else ''}{whole:,}.{fraction:02d}"


def period_bounds(mode, year, month, today=None):
    """月/年区间两端均包含；当前周期截至今天，未来周期明确拒绝。"""
    current = _today(today)
    if mode not in ("month", "year"):
        raise ValueError("统计模式须为 month 或 year")
    if type(year) is not int or type(month) is not int or not 1 <= month <= 12:
        raise ValueError("年份或月份无效")
    try:
        start = date(year, month if mode == "month" else 1, 1)
        end = (date(year, month, calendar.monthrange(year, month)[1])
               if mode == "month" else date(year, 12, 31))
    except ValueError as exc:
        raise ValueError("年份或月份无效") from exc
    if start > current:
        raise ValueError("不能统计未来周期")
    return start.isoformat(), min(end, current).isoformat()


def summarize(rows, mode='month', year=2026, month=11, today=None):
    """退款按退款发生日抵减分类支出；转账不计收支。"""
    current = _today(today)
    start, end = period_bounds(mode, year, month, current)
    months_elapsed = (current.month if year == current.year else 12) if mode == "year" else 1
    months = [{"month": m, "income_cents": 0, "expense_cents": 0, "balance_cents": 0,
                   "future": date(year, m, 1) > current} for m in range(1, 13)]
    income = expense = 0
    categories = {}
    for row in rows:
        entry_date = row["entry_date"]
        if not start <= entry_date <= end:
            continue
        kind, amount = row["kind"], row["amount_cents"]
        if kind == "transfer":
            continue
        bucket = months[_date(entry_date).month - 1]
        if kind == "income":
            income += amount
            bucket["income_cents"] += amount
        elif kind in ("expense", "refund"):
            net = -amount if kind == "refund" else amount
            expense += net
            bucket["expense_cents"] += net
            category = row["category"]
            categories[category] = categories.get(category, 0) + net
    for bucket in months:
        bucket["balance_cents"] = bucket["income_cents"] - bucket["expense_cents"]
    # 精确整数四舍五入（负数也按绝对值进位）。
    average = (abs(expense) * 2 + months_elapsed) // (2 * months_elapsed)
    if expense < 0:
        average = -average
    category_rows = [{"category": category, "amount_cents": amount,
                          "share": amount / expense if expense > 0 else None}
                     for category, amount in sorted(categories.items(),
                                                     key=lambda item: (-item[1], item[0]))]
    return {"income_cents": income, "expense_cents": expense,
                "balance_cents": income - expense, "average_expense_cents": average,
                "months_elapsed": months_elapsed, "categories": category_rows, "months": months,
                "cutoff": end, "start": start, "end": end,
                "title": f"{year}年{month}月" if mode == "month" else f"{year}年"}
