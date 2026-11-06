"""密匣的纯计算层测试。

**这是最该有测试的一块**：金额与天数算错是「用户不会立刻发现、但会照着
做决定」的那类错误。全部是纯函数，``today`` 由参数注入，所以每一条断言都
不依赖「跑测试那天是几号」——依赖当天日期的断言会在某个日期悄悄变红。
"""
import datetime as dt
import sys
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lumina import vault_tools as vt  # noqa: E402

TODAY = dt.date(2026, 11, 5)


def deposit(principal, rate, months, end, bank_id=1, name="存单"):
    return {"id": 1, "bank_id": bank_id, "name": name,
            "principal": principal, "rate": rate, "months": months,
            "end_date": end}


def _is_docstring(tree, node):
    """``node`` 是不是某个模块/类/函数体的第一个表达式（即文档字符串）。

    ``#`` 注释不产生 AST 节点，所以扫常量就天然跳过了注释；需要显式排除的
    只有文档字符串——它是真的字符串常量，会被 ``ast.walk`` 收到。
    """
    import ast
    for parent in ast.walk(tree):
        body = getattr(parent, "body", None)
        # **必须判 ``list``**：``ast`` 里不是所有节点的 ``body`` 都是语句列表——
        # ``IfExp.body`` 是单个表达式（``Attribute`` / ``Subscript`` / …），
        # 直接 ``body[0]`` 会抛 ``TypeError: object is not subscriptable``。
        if not isinstance(body, list) or not body:
            continue
        if not isinstance(body[0], ast.Expr):
            continue
        first = body[0].value
        if isinstance(first, ast.Constant) and first is node:
            return True
    return False


class AmountParseTests(unittest.TestCase):
    """本金输入的解析。中文单位是刚需：用户记存款时脑子里就是「40 万」。"""

    def test_plain_number(self):
        self.assertEqual(vt.parse_amount("400000"), 400000.0)

    def test_chinese_wan(self):
        self.assertEqual(vt.parse_amount("40万"), 400000.0)

    def test_chinese_wan_with_yuan_suffix(self):
        self.assertEqual(vt.parse_amount("40万元"), 400000.0)

    def test_wan_and_w_and_k(self):
        self.assertEqual(vt.parse_amount("1.5万"), 15000.0)
        self.assertEqual(vt.parse_amount("40w"), 400000.0)
        self.assertEqual(vt.parse_amount("5k"), 5000.0)

    def test_thousand_separators_and_symbols(self):
        self.assertEqual(vt.parse_amount("400,000"), 400000.0)
        self.assertEqual(vt.parse_amount("¥ 40万"), 400000.0)
        self.assertEqual(vt.parse_amount("￥1,234.5"), 1234.5)

    def test_empty_is_none(self):
        self.assertIsNone(vt.parse_amount(""))
        self.assertIsNone(vt.parse_amount("   "))
        self.assertIsNone(vt.parse_amount(None))

    def test_rejects_garbage(self):
        for bad in ("abc", "40万x", "--3", "4 0 0"):
            with self.subTest(bad=bad):
                with self.assertRaises(vt.VaultError):
                    vt.parse_amount(bad)

    def test_rejects_zero_and_negative(self):
        for bad in ("0", "-5", "0万"):
            with self.subTest(bad=bad):
                with self.assertRaises(vt.VaultError):
                    vt.parse_amount(bad)

    def test_error_message_names_the_input(self):
        """报错消息里必须带上用户填的那个值。只说「金额无效」等于让用户
        自己去猜六个字段里到底哪个错了。"""
        with self.assertRaises(vt.VaultError) as ctx:
            vt.parse_amount("四万")
        self.assertIn("四万", str(ctx.exception))


class RateParseTests(unittest.TestCase):

    def test_with_and_without_percent(self):
        self.assertEqual(vt.parse_rate("3.1"), 3.1)
        self.assertEqual(vt.parse_rate("3.1%"), 3.1)
        self.assertEqual(vt.parse_rate("3.10％"), 3.1)

    def test_rejects_zero_and_full_percent(self):
        for bad in ("0", "100", "120", "-1"):
            with self.subTest(bad=bad):
                with self.assertRaises(vt.VaultError):
                    vt.parse_rate(bad)

    def test_rejects_garbage(self):
        with self.assertRaises(vt.VaultError):
            vt.parse_rate("三点一")


class TermParseTests(unittest.TestCase):
    """存期统一归一到**月数**——产品的期限本来就是按月定义的。"""

    def test_forms(self):
        self.assertEqual(vt.parse_term("36"), 36)
        self.assertEqual(vt.parse_term("36个月"), 36)
        self.assertEqual(vt.parse_term("3年"), 36)
        self.assertEqual(vt.parse_term("3 年"), 36)
        self.assertEqual(vt.parse_term("1年6个月"), 18)
        self.assertEqual(vt.parse_term("一年" if False else "12个月"), 12)
        self.assertEqual(vt.parse_term("半年"), 6)

    def test_rejects_zero(self):
        for bad in ("0", "0年", "0个月"):
            with self.subTest(bad=bad):
                with self.assertRaises(vt.VaultError):
                    vt.parse_term(bad)

    def test_rejects_garbage(self):
        with self.assertRaises(vt.VaultError):
            vt.parse_term("三个月")

    def test_format_round_trip(self):
        self.assertEqual(vt.format_term(36), "3 年")
        self.assertEqual(vt.format_term(12), "1 年")
        self.assertEqual(vt.format_term(6), "6 个月")
        self.assertEqual(vt.format_term(18), "1 年 6 个月")


class DateParseTests(unittest.TestCase):

    def test_separators(self):
        expect = dt.date(2027, 3, 15)
        for text in ("2027-03-15", "2027/3/15", "2027.3.15", "2027年3月15日"):
            with self.subTest(text=text):
                self.assertEqual(vt.parse_date(text), expect)

    def test_nonexistent_day_says_so(self):
        """「这天不存在」与「格式看不懂」是两回事，要分开报——
        前者用户改数字就行，后者他得先想清楚格式。"""
        with self.assertRaises(vt.VaultError) as ctx:
            vt.parse_date("2027-02-30")
        self.assertIn("没有这一天", str(ctx.exception))

    def test_empty_is_none(self):
        self.assertIsNone(vt.parse_date(""))
        self.assertIsNone(vt.parse_date(None))


class EndDateTests(unittest.TestCase):
    def test_month_end_and_leap_year(self):
        for start, term, expected in (
                ("2023-01-31", "1", "2023-02-28"),
                ("2024-01-31", "1", "2024-02-29"),
                ("2024-02-29", "12", "2025-02-28"),
                ("2026-11-30", "3", "2027-02-28"),
                ("2026-12-31", "6", "2027-06-30"),
                ("2024-02-29", "36", "2027-02-28"),
                ("2026-01-15", "24", "2028-01-15")):
            with self.subTest(start=start, term=term):
                self.assertEqual(vt.calculate_end_date(start, term).isoformat(), expected)

    def test_missing_invalid_and_out_of_range(self):
        for start, term in (("", "3"), ("2024-02-30", "3"),
                            ("2024-01-01", ""), ("2024-01-01", "abc"),
                            ("9999-12-01", "1")):
            with self.subTest(start=start, term=term), self.assertRaises(vt.VaultError):
                vt.calculate_end_date(start, term)

    def test_nonfinite_amount_and_rate_rejected(self):
        with self.assertRaises(vt.VaultError):
            vt.parse_amount("9" * 400)
        for value in ("nan", "inf", "-inf"):
            with self.subTest(value=value), self.assertRaises(vt.VaultError):
                vt.parse_rate(value)


class InterestTests(unittest.TestCase):
    """利息口径：单利按月份折算 ``本金 × 年利率 × 月数 ÷ 12``。"""

    def test_three_year_certificate(self):
        # 40 万、3.10%、3 年 → 37200
        self.assertAlmostEqual(vt.interest(400000, 3.10, 36), 37200.0)

    def test_half_year(self):
        # 5 万、1.55%、6 个月 → 387.5
        self.assertAlmostEqual(vt.interest(50000, 1.55, 6), 387.5)

    def test_one_year_is_rate_times_principal(self):
        """满 12 个月的利息必须**恰好**等于本金×年利率——这是口径正确性
        最直接的自证：用户拿存单上的利率乘一下就能核对。"""
        for principal, rate in ((100000, 2.10), (300000, 2.75),
                                (123456, 1.85)):
            with self.subTest(principal=principal, rate=rate):
                self.assertAlmostEqual(
                    vt.interest(principal, rate, 12),
                    principal * rate / 100, places=6)

    def test_is_linear_in_each_argument(self):
        """单利：翻倍本金 / 翻倍月数，利息都翻倍。利率翻倍同理。

        这条在「按复利/按天折算」的写法下会失败，所以它是口径的守卫。
        """
        base = vt.interest(100000, 2.0, 12)
        self.assertAlmostEqual(vt.interest(200000, 2.0, 12), base * 2)
        self.assertAlmostEqual(vt.interest(100000, 2.0, 24), base * 2)
        self.assertAlmostEqual(vt.interest(100000, 4.0, 12), base * 2)

    def test_missing_fields_are_zero_not_crash(self):
        self.assertEqual(vt.interest(None, 3.0, 12), 0.0)
        self.assertEqual(vt.interest(100000, None, 12), 0.0)
        self.assertEqual(vt.interest(100000, 3.0, None), 0.0)


class DaysLeftTests(unittest.TestCase):

    def test_signs(self):
        self.assertEqual(vt.days_left("2026-11-05", TODAY), 0)
        self.assertEqual(vt.days_left("2026-11-08", TODAY), 3)
        self.assertEqual(vt.days_left("2026-10-28", TODAY), -8)

    def test_accepts_date_object(self):
        self.assertEqual(vt.days_left(dt.date(2027, 3, 15), TODAY), 130)

    def test_accepts_sqlite_row_end_date(self):
        """数据库里 ``end_date`` 是文本，所以字符串这条路径必须通。"""
        self.assertEqual(vt.days_left("2027-01-01", TODAY), 57)


class DueStateTests(unittest.TestCase):
    """倒计时文案。用户不该自己做减法，所以文案里直接写天数。"""

    def test_matured(self):
        self.assertEqual(vt.due_state(-8), ("matured", "已到期 8 天"))

    def test_today_belongs_to_matured(self):
        """**当天到期归「已到期」**（用户明确要求口径含当天）。

        文案仍然单独写「今天到期」而不是「已到期 0 天」：后者读起来像
        「可操作时间已经归零」，而今天其实还有一整天。

        档位与文案分开断言是有意的：有一条缺陷正是「文案对了但档位没跟上」，
        只查文案的话那种错位（当天被算进「90 天内到期」）会逃掉。
        """
        state, text = vt.due_state(0)
        self.assertEqual(state, "matured")
        self.assertEqual(text, "今天到期")

    def test_urgent_window_boundary(self):
        self.assertEqual(vt.due_state(1)[0], "urgent")
        self.assertEqual(vt.due_state(vt.URGENT_DAYS)[0], "urgent")
        self.assertEqual(vt.due_state(vt.URGENT_DAYS + 1)[0], "normal")

    def test_text(self):
        self.assertEqual(vt.due_state(3)[1], "剩 3 天")
        self.assertEqual(vt.due_state(130)[1], "剩 130 天")


class WeightedRateTests(unittest.TestCase):
    """资金加权年化：``总利息 ÷ Σ(本金 × 月数 ÷ 12)``。

    **分母是「本金×年限」而不是本金合计**，所以下面这条用本金平均会发生
    偏差的数据集必须给出加权值。
    """

    def test_weights_by_time_not_by_principal(self):
        # 10 万存 6 个月 @1.0%，10 万存 36 个月 @3.0%
        # 利息 = 500 + 9000 = 9500；分母 = 50000 + 300000 = 350000
        items = [deposit(100000, 1.0, 6, "2027-05-01"),
                 deposit(100000, 3.0, 36, "2029-11-01")]
        self.assertAlmostEqual(vt.weighted_rate(items), 9500 / 350000 * 100,
                               places=6)
        # 按本金简单平均会得到 2.0%，与上面的结果明显不同——这正是
        # 「必须按资金时间加权」的体现，也是这条断言的守卫价值。
        naive = (1.0 + 3.0) / 2
        self.assertNotAlmostEqual(vt.weighted_rate(items), naive, places=3)

    def test_single_deposit_equals_its_rate(self):
        """只有一笔存款时加权年化必须**恰好**等于它的票面利率。"""
        self.assertAlmostEqual(vt.weighted_rate([deposit(100000, 2.75, 24, "2028-11-01")]),
                               2.75, places=9)

    def test_uniform_rate_stays_uniform(self):
        """所有存款利率相同时，无论期限怎么混，加权年化都该等于那个利率。"""
        items = [deposit(100000, 2.5, 6, "2027-05-01"),
                 deposit(300000, 2.5, 36, "2029-11-01"),
                 deposit(50000, 2.5, 12, "2027-11-01")]
        self.assertAlmostEqual(vt.weighted_rate(items), 2.5, places=9)

    def test_empty_is_zero(self):
        self.assertEqual(vt.weighted_rate([]), 0.0)

    def test_zero_months_does_not_divide_by_zero(self):
        """月数为 0 时（脏数据）不能抛 ``ZeroDivisionError``——
        那会让整个窗口渲染失败、用户看不到任何东西。"""
        self.assertEqual(vt.weighted_rate([deposit(100000, 3.0, 0, "2027-01-01")]),
                         0.0)


class SummarizeTests(unittest.TestCase):
    """顶部四张指标卡。"""

    def setUp(self):
        self.items = [
            deposit(400000, 3.10, 36, "2027-03-15", bank_id=1),
            deposit(50000, 1.55, 6, "2026-11-08", bank_id=1),
            deposit(300000, 2.75, 24, "2027-01-10", bank_id=2),
            deposit(100000, 2.10, 12, "2026-10-28", bank_id=3),
        ]

    def test_totals(self):
        data = vt.summarize(self.items, TODAY)
        self.assertAlmostEqual(data["principal"], 850000.0)
        self.assertAlmostEqual(data["interest"], 56187.5)
        # **本息合计退到副值**（主值改成本金了），但这个字段本身必须仍然
        # 等于「本金 + 利息」——它现在是「本金」卡副行那句「本息 ¥…」。
        self.assertAlmostEqual(data["total"], 906187.5)
        self.assertEqual(data["count"], 4)

    def test_soon_window_is_inclusive_and_excludes_matured(self):
        """90 天内到期：**含边界第 90 天，且不含已到期**。

        已到期单独一张卡，混进「即将到期」会让用户以为钱还能按原样拿回来。
        天数为 0（当天到期）也归「已到期」——见下面那条边界测试。
        """
        data = vt.summarize(self.items, TODAY)
        # 2026-11-08（3 天）与 2027-01-10（66 天）→ 2 笔
        self.assertEqual(data["soon_count"], 2)
        self.assertAlmostEqual(data["soon_principal"], 350000.0)

    def test_soon_boundary_exactly_ninety_days(self):
        edge = [deposit(10000, 2.0, 12, "2027-02-03")]   # 恰好 90 天
        self.assertEqual(vt.summarize(edge, TODAY)["soon_count"], 1)
        over = [deposit(10000, 2.0, 12, "2027-02-04")]   # 91 天
        self.assertEqual(vt.summarize(over, TODAY)["soon_count"], 0)

    def test_today_matured_goes_to_matured_not_soon(self):
        """**当天到期只能进「已到期」**（用户要求口径含当天）。

        这条守的是两张卡的**互斥性**：``soon`` 与 ``matured`` 如果都收当天，
        「90 天内到期 2 笔 + 已到期 1 笔」会大于总笔数 2，用户一眼看出对不上。
        """
        items = [deposit(10000, 2.0, 12, "2026-11-05")]   # 今天到期
        data = vt.summarize(items, TODAY)
        self.assertEqual(data["soon_count"], 0, "当天到期不该进 soon")
        self.assertEqual(data["matured_count"], 1)

    def test_matured_total_is_principal_plus_interest(self):
        data = vt.summarize(self.items, TODAY)
        self.assertEqual(data["matured_count"], 1)
        # 10 万 + 2100 = 102100
        self.assertAlmostEqual(data["matured_total"], 102100.0)

    def test_empty(self):
        data = vt.summarize([], TODAY)
        for key in ("principal", "interest", "total", "rate",
                    "soon_principal", "matured_total"):
            self.assertEqual(data[key], 0.0, key)
        self.assertEqual(data["count"], 0)


class PickUrgentTests(unittest.TestCase):
    """主行「到期」列该显示哪一笔。"""

    def test_prefers_nearest_future(self):
        items = [deposit(1, 1, 1, "2027-03-15"),
                 deposit(1, 1, 1, "2026-11-08"),
                 deposit(1, 1, 1, "2027-01-10")]
        self.assertEqual(vt.pick_urgent(items, TODAY)["end_date"], "2026-11-08")

    def test_falls_back_to_most_recent_matured(self):
        """**未来的全都没有时要退回显示最近那笔已到期**。主行显示空白会让
        用户以为这家银行没有到期信息，实际是他有笔钱已经到期了。"""
        items = [deposit(1, 1, 1, "2026-10-01"),
                 deposit(1, 1, 1, "2026-10-28"),
                 deposit(1, 1, 1, "2026-09-15")]
        self.assertEqual(vt.pick_urgent(items, TODAY)["end_date"], "2026-10-28")

    def test_today_is_not_treated_as_future(self):
        """**当天到期不算「未来」**，主行应显示后面那笔真正未到期的。

        这条与 ``summarize`` 的划界是同一条：两边都用 ``days_left <= 0``
        判「已到期」。如果这里把当天算作未来，主行会显示「今天到期」而
        指标卡把它计入「已到期 N 笔」——同一笔钱在两处落在不同档位。
        """
        items = [deposit(1, 1, 1, "2026-11-05"),
                 deposit(1, 1, 1, "2026-11-08")]
        self.assertEqual(vt.pick_urgent(items, TODAY)["end_date"], "2026-11-08")

    def test_only_today_matured_still_shows_it(self):
        """只有一笔当天到期的存单时，主行仍要显示它（退回分支）。"""
        items = [deposit(1, 1, 1, "2026-11-05")]
        self.assertEqual(vt.pick_urgent(items, TODAY)["end_date"], "2026-11-05")

    def test_empty_is_none(self):
        self.assertIsNone(vt.pick_urgent([], TODAY))


class GroupByBankTests(unittest.TestCase):
    """两级结构的聚合。"""

    def setUp(self):
        self.banks = [{"id": 1, "name": "招商银行"},
                      {"id": 2, "name": "工商银行"}]
        self.items = [deposit(400000, 3.10, 36, "2027-03-15", bank_id=1),
                      deposit(50000, 1.55, 6, "2026-11-08", bank_id=1),
                      deposit(300000, 2.75, 24, "2027-01-10", bank_id=2)]

    def test_groups_in_bank_order(self):
        groups = vt.group_by_bank(self.banks, self.items, TODAY)
        self.assertEqual([g["bank"]["name"] for g in groups],
                         ["招商银行", "工商银行"])
        self.assertEqual([g["count"] for g in groups], [2, 1])

    def test_group_totals(self):
        group = vt.group_by_bank(self.banks, self.items, TODAY)[0]
        self.assertAlmostEqual(group["principal"], 450000.0)
        self.assertAlmostEqual(group["interest"], 37587.5)
        self.assertEqual(group["urgent"]["end_date"], "2026-11-08")

    def test_bank_without_deposits_is_kept(self):
        """**没有存款的银行也要出现在结果里**。用户刚建完银行就会看到它；
        如果因为「没有子行」而不显示，他会以为建失败了，接着再建一次，
        得到重复记录。"""
        groups = vt.group_by_bank(self.banks, self.items[:2], TODAY)
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[1]["count"], 0)
        self.assertIsNone(groups[1]["urgent"])
        self.assertEqual(groups[1]["principal"], 0)

    def test_orphan_deposits_are_not_dropped(self):
        """``bank_id`` 指向不存在的银行时，存单必须仍然渲染出来。
        数据在而看不见是最难排查的一类问题。"""
        orphan = [deposit(1000, 1.0, 12, "2027-01-01", bank_id=999)]
        groups = vt.group_by_bank(self.banks, orphan, TODAY)
        rendered = [d for g in groups for d in g["deposits"]]
        self.assertEqual(len(rendered), 1)
        self.assertEqual(rendered[0]["name"], "存单")
        self.assertEqual(groups[-1]["bank"]["id"], None)

    def test_no_orphan_group_when_all_match(self):
        groups = vt.group_by_bank(self.banks, self.items, TODAY)
        self.assertEqual(len(groups), 2)

    def test_empty_inputs(self):
        self.assertEqual(vt.group_by_bank([], [], TODAY), [])


class FieldAccessTests(unittest.TestCase):
    """``sqlite3.Row`` 兼容性。

    **这是本项目已经踩过两次的坑**（见 ``MEMORY.md``）：``sqlite3.Row``
    没有 ``.get()``，而 ``"列名" in row`` 判断的是**值**不是列名、恒为假。
    密匣的数据全部来自 ``Row``（库里读出来就是），所以取值必须走 ``keys()``。
    """

    def _row(self):
        """一行记录，``row_factory`` 设成 ``Row``（与库里读出来的一致）。

        **连接必须显式关**：裸 ``sqlite3.connect`` 不关会在解释器退出时
        报 ``ResourceWarning: unclosed database``，而且那条警告会冒在**别的
        模块**的栈里（实测是 ``ast`` / ``tokenize``），看起来像是那边的毛病。
        用 ``addCleanup`` 而不是 ``tearDown``：``setUp`` 里建的东西由
        ``setUp`` 自己登记，不依赖 ``tearDown`` 是否被子类覆写。
        """
        import sqlite3
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE t (a INTEGER, b TEXT)")
        conn.execute("INSERT INTO t VALUES (7, 'x')")
        return conn.execute("SELECT * FROM t").fetchone()

    def setUp(self):
        self.row = self._row()

    def test_reads_from_row(self):
        self.assertEqual(vt.field(self.row, "a"), 7)
        self.assertEqual(vt.field(self.row, "b"), "x")

    def test_missing_column_returns_default(self):
        self.assertEqual(vt.field(self.row, "nope", "fallback"), "fallback")
        self.assertIsNone(vt.field(self.row, "nope"))

    def test_row_has_no_get(self):
        """守卫：如果哪天 ``Row`` 加上了 ``.get()``，这条会失败，
        提醒我们上面的实现理由已经过期、可以简化。"""
        self.assertFalse(hasattr(self.row, "get"))

    def test_membership_on_row_is_the_wrong_idiom(self):
        """``"a" in row`` 恒为 False——正是因此不能用成员判断来探测列存在。
        这条不是「测试实现细节」，它钉住的是本条注释所解释的那个事实。"""
        self.assertNotIn("a", self.row)
        self.assertIn(7, self.row)

    def test_functions_accept_rows_end_to_end(self):
        """把真实的 ``Row`` 一路喂进聚合函数：这条覆盖的是「库里的数据
        直接进渲染」这条路径，``dict`` 版本测不到 ``.get()`` 那类崩溃。"""
        import sqlite3
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE d (id INTEGER, bank_id INTEGER, name TEXT,"
                     " principal REAL, rate REAL, months INTEGER,"
                     " end_date TEXT)")
        conn.execute("INSERT INTO d VALUES (1, 1, 's', 100000, 2.0, 12,"
                     " '2027-11-01')")
        rows = conn.execute("SELECT * FROM d").fetchall()
        self.assertAlmostEqual(vt.summarize(list(rows), TODAY)["interest"],
                               2000.0)
        groups = vt.group_by_bank([{"id": 1, "name": "B"}], list(rows), TODAY)
        self.assertEqual(groups[0]["count"], 1)


class BuildDepositTests(unittest.TestCase):
    """界面输入 → 入库记录。**这条路径与对话框走的是同一个函数**，
    所以这里的断言同时是界面的守卫，不存在「测试过了界面不过」。"""

    def test_normalizes(self):
        record = vt.build_deposit("大额存单 3 年", 1, "40万", "3.1%",
                                 "3年", "2027年3月15日")
        self.assertEqual(record, {"name": "大额存单 3 年", "bank_id": 1,
                                  "principal": 400000.0, "rate": 3.1,
                                  "months": 36, "end_date": "2027-03-15"})

    def test_strips_name(self):
        self.assertEqual(vt.build_deposit("  存单  ", 1, "1万", "1", "1年",
                                          "2027-01-01")["name"], "存单")

    def test_each_missing_field_names_itself(self):
        """**每个必填项都要有自己的提示**。只报一句「输入不完整」会让
        用户在六个字段之间反复试。"""
        cases = [
            (("", "1", "1万", "1", "1年", "2027-01-01"), "存单名称"),
            (("n", "1", "", "1", "1年", "2027-01-01"), "本金"),
            (("n", "1", "1万", "", "1年", "2027-01-01"), "年利率"),
            (("n", "1", "1万", "1", "", "2027-01-01"), "存期"),
            (("n", "1", "1万", "1", "1年", ""), "到期日"),
        ]
        for args, expected in cases:
            with self.subTest(expected=expected):
                with self.assertRaises(vt.VaultError) as ctx:
                    vt.build_deposit(*args)
                self.assertIn(expected, str(ctx.exception))


class NoInsuranceLimitTests(unittest.TestCase):
    """**「不做 50 万额度」这条需求必须钉在源码上。**

    第一版设计稿做了存款保险额度对照，被用户明确否掉（「不用设置50万上限」）。
    几何断言与渲染断言都拦不住「有人后来又把额度线加回来」——那种改动加的
    是新的控件，会让既有断言全部照常通过。所以这里直接扫源码。

    **扫的是「代码里的字面量」，必须先剥掉文档字符串。** 第一版没剥，于是
    模块文档里那句「本模块刻意不提供任何额度相关计算」把测试自己打红了——
    解释「为什么不做」的说明文字与「真的做了」的代码是两回事。用 ``ast``
    取常量节点，``#`` 注释本来就不在语法树里，文档字符串则要显式排除。
    """

    def _string_literals(self, name):
        """源码里所有**非文档字符串**的字符串字面量。"""
        import ast
        tree = ast.parse((ROOT / "lumina" / name).read_text(encoding="utf-8"))
        return [n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
                and not _is_docstring(tree, n)]

    def test_no_insurance_ceiling_in_compute_layer(self):
        literals = self._string_literals("vault_tools.py")
        for banned in ("500000", "500_000", "50万", "存款保险", "额度上限"):
            with self.subTest(banned=banned):
                hits = [s for s in literals if banned in s]
                self.assertEqual(
                    hits, [],
                    f"纯计算层不该有 {banned} 这样的字面量——"
                    "用户明确要求不做 50 万额度")

    def test_no_insurance_ceiling_in_window(self):
        literals = self._string_literals("vault_window.py")
        for banned in ("500000", "500_000", "存款保险", "额度上限"):
            with self.subTest(banned=banned):
                hits = [s for s in literals if banned in s]
                self.assertEqual(
                    hits, [],
                    f"密匣窗口不该有 {banned} 这样的字面量——"
                    "用户明确要求不做 50 万额度")

    def test_no_numeric_insurance_constant(self):
        """额度也可能是**纯数字常量**（不是字符串），上面那两条扫不到。
        这里查数值常量 500000，以及任何以它为分母的除法。"""
        import ast
        for name in ("vault_tools.py", "vault_window.py"):
            tree = ast.parse(
                (ROOT / "lumina" / name).read_text(encoding="utf-8"))
            with self.subTest(name=name):
                numbers = [n.value for n in ast.walk(tree)
                           if isinstance(n, ast.Constant)
                           and isinstance(n.value, (int, float))]
                self.assertNotIn(500000, numbers)
                self.assertNotIn(500_000, numbers)
                for node in ast.walk(tree):
                    if isinstance(node, ast.BinOp) and isinstance(
                            node.op, ast.Div):
                        self.assertNotEqual(
                            getattr(node.right, "value", None), 500000,
                            "出现了以 500000 为分母的比例——这是隐式额度线")

    def test_the_guard_itself_would_catch_a_regression(self):
        """**反向验证守卫本身**：凭空造一段带额度线的源码，断言这套扫描能
        抓到它。没有这一条的话，上面几个 ``assertEqual(hits, [])`` 在
        「扫描函数永远返回空列表」时也是绿的——那就是恒真断言。"""
        import ast
        sample = (
            '"""额度说明：本模块不涉及任何上限。"""\n'
            "def f(deposits):\n"
            "    name = \"存款保险额度\"\n"
            "    return sum(d['principal'] for d in deposits) / 500000\n"
        )
        tree = ast.parse(sample)
        literals = [n.value for n in ast.walk(tree)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and not _is_docstring(tree, n)]
        self.assertIn("存款保险额度", literals)
        self.assertNotIn("额度说明：本模块不涉及任何上限。", literals)
        numbers = [n.value for n in ast.walk(tree)
                   if isinstance(n, ast.Constant)
                   and isinstance(n.value, (int, float))]
        self.assertIn(500000, numbers)


class TodayInjectionTests(unittest.TestCase):
    """基准日必须能从外部注入。

    「今天」散落在多处取时，跨零点的那次刷新会算出两个不同的「今天」，
    逾期天数因此差一天。这里顺带验证注入真的生效。
    """

    def test_summarize_uses_injected_today(self):
        items = [deposit(100000, 2.0, 12, "2026-11-06")]
        before = vt.summarize(items, dt.date(2026, 11, 5))
        after = vt.summarize(items, dt.date(2026, 11, 7))
        self.assertEqual(before["matured_count"], 0)
        self.assertEqual(after["matured_count"], 1)
        self.assertEqual(after["soon_count"], 0)

    def test_module_does_not_call_date_today(self):
        """守卫：计算层里**不允许**自己调 ``dt.date.today()``——
        那会让上面那种注入失效，且不可测。"""
        source = (ROOT / "lumina" / "vault_tools.py").read_text(
            encoding="utf-8")
        self.assertNotIn("date.today()", source)

    def test_module_does_not_import_tkinter(self):
        """计算层不碰界面。混进来的第一个后果就是纯函数测试要建 Tk 根窗口。"""
        source = (ROOT / "lumina" / "vault_tools.py").read_text(
            encoding="utf-8")
        self.assertNotIn("import tkinter", source)


if __name__ == "__main__":
    unittest.main()
