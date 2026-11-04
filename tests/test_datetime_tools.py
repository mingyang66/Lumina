"""日期转换工具（`datetime_tools`）的纯逻辑测试。

不依赖 Tk：`datetime_tools` 刻意与 UI 解耦，便于这里直接断言。

**重点在美东夏令时**：Windows 上没有 tzdata，本模块自己按规则算偏移，
所以边界（前拨跳过的空洞、回拨重叠的两次）必须逐点钉死——算错一小时
在界面上完全看不出来，只有对照真实时区规则才抓得住。
"""
import datetime
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lumina import datetime_tools as dt  # noqa: E402
from lumina.datetime_tools import (  # noqa: E402
    SECTION_MARK,
    DateError,
    beijing_to_eastern,
    clock_snapshot,
    convert,
    eastern_name,
    eastern_offset,
    eastern_to_utc,
    humanize_delta,
    looks_like_timestamp,
    parse_moment,
    timestamp_seconds,
    timestamp_to_date,
)

UTC = datetime.UTC


def utc(*args):
    return datetime.datetime(*args)


class EasternDstTests(unittest.TestCase):
    """美东 DST：3 月第二个周日 07:00 UTC 起，11 月第一个周日 06:00 UTC 止。"""

    def test_2026_dst_bounds(self):
        """2026 年：3/8 开始、11/1 结束（两个都必须算对）。"""
        self.assertEqual(dt._nth_sunday(2026, 3, 2), datetime.date(2026, 3, 8))
        self.assertEqual(dt._nth_sunday(2026, 11, 1), datetime.date(2026, 11, 1))

    def test_offset_flips_exactly_at_the_boundary_second(self):
        """边界**前一秒与后一秒**必须给出不同的偏移。

        只测「7 月是 EDT、1 月是 EST」抓不到差一小时的错误——那种测试在
        把起止时刻写成 08:00 或 06:00 时照样全绿。
        """
        cases = (
            (utc(2026, 3, 8, 6, 59, 59), dt.EASTERN_STD),
            (utc(2026, 3, 8, 7, 0, 0), dt.EASTERN_DST),
            (utc(2026, 11, 1, 5, 59, 59), dt.EASTERN_DST),
            (utc(2026, 11, 1, 6, 0, 0), dt.EASTERN_STD),
        )
        for moment, expected in cases:
            with self.subTest(moment=moment):
                self.assertEqual(eastern_offset(moment), expected)
                self.assertEqual(eastern_name(moment),
                                 'EDT' if expected == dt.EASTERN_DST else 'EST')

    def test_winter_is_est_and_summer_is_edt(self):
        self.assertEqual(eastern_offset(utc(2026, 1, 15, 12)), dt.EASTERN_STD)
        self.assertEqual(eastern_offset(utc(2026, 7, 15, 12)), dt.EASTERN_DST)

    def test_dst_rule_holds_across_many_years(self):
        """逐年扫一遍：每年都必须出现约 238 天的夏令时。

        单点断言挡不住「规则只对某一年成立」的错误（比如把日期算成固定
        几号）。这里连续扫 2015~2035，长度落在合理区间才算过。
        """
        for year in range(2015, 2036):
            with self.subTest(year=year):
                start, end = dt._dst_bounds(year)
                self.assertEqual(start.weekday(), 6, f'{year} 开始日不是周日')
                self.assertEqual(end.weekday(), 6, f'{year} 结束日不是周日')
                self.assertEqual(start.month, 3)
                self.assertEqual(end.month, 11)
                days = (end - start).days
                # 3 月第 2 个周日 ~ 11 月第 1 个周日：实测 231~238 天
                self.assertGreaterEqual(days, 224, f'{year} 夏令时过短：{days}')
                self.assertLessEqual(days, 245, f'{year} 夏令时过长：{days}')

    def test_nth_sunday_handles_month_starting_on_sunday(self):
        """2026-03-01 本身就是周日 → 第 2 个周日是 3/8，不是 3/1。

        这是 ``_nth_sunday`` 最容易写错的分支：``lead`` 算出来是 0，
        若忘了 ``+7*(n-1)`` 就会把 1 号当成第一个周日。
        """
        self.assertEqual(datetime.date(2026, 3, 1).weekday(), 6)
        self.assertEqual(dt._nth_sunday(2026, 3, 1), datetime.date(2026, 3, 1))
        self.assertEqual(dt._nth_sunday(2026, 3, 2), datetime.date(2026, 3, 8))


class EasternAmbiguityTests(unittest.TestCase):
    """前拨造成的「不存在时刻」与回拨造成的「出现两次」。"""

    def test_spring_forward_gap_is_rejected(self):
        """3 月前拨：美东 02:00~02:59 **不存在**，必须报错而不是猜一个。

        猜过去的后果是差一小时，而界面上时间看起来完全正常。
        """
        utc_naive, note = eastern_to_utc(datetime.datetime(2026, 3, 8, 2, 30))
        self.assertIsNone(utc_naive)
        self.assertIn('不存在', note)

    def test_gap_boundaries_are_exclusive(self):
        """01:59 存在、02:00 不存在、03:00 存在——闭开区间必须精确。"""
        self.assertIsNotNone(
            eastern_to_utc(datetime.datetime(2026, 3, 8, 1, 59))[0])
        self.assertIsNone(
            eastern_to_utc(datetime.datetime(2026, 3, 8, 2, 0))[0])
        self.assertIsNone(
            eastern_to_utc(datetime.datetime(2026, 3, 8, 2, 59))[0])
        self.assertIsNotNone(
            eastern_to_utc(datetime.datetime(2026, 3, 8, 3, 0))[0])

    def test_fall_back_overlap_picks_dst_and_warns(self):
        """11 月回拨：01:30 出现两次，取较早的那个（EDT），并明确告知。"""
        utc_naive, note = eastern_to_utc(datetime.datetime(2026, 11, 1, 1, 30))
        self.assertIsNotNone(utc_naive)
        # 01:30 EDT = 05:30 UTC；01:30 EST 是 06:30 UTC。取较早的。
        self.assertEqual(utc_naive, utc(2026, 11, 1, 5, 30))
        self.assertIn('两次', note)

    def test_unambiguous_local_times_have_no_note(self):
        for moment in (datetime.datetime(2026, 7, 1, 12, 0),
                       datetime.datetime(2026, 1, 15, 3, 0)):
            with self.subTest(moment=moment):
                utc_naive, note = eastern_to_utc(moment)
                self.assertIsNotNone(utc_naive)
                self.assertEqual(note, '')


class TimestampTests(unittest.TestCase):
    def test_ten_digits_are_seconds(self):
        self.assertEqual(timestamp_seconds('1730786280'), (1730786280, '秒'))

    def test_thirteen_digits_are_milliseconds(self):
        self.assertEqual(timestamp_seconds('1730786280000'),
                         (1730786280.0, '毫秒'))

    def test_millisecond_precision_is_kept(self):
        """毫秒时间戳里的毫秒不能被丢掉——用户拿它算耗时。"""
        seconds, _unit = timestamp_seconds('1730786280123')
        self.assertAlmostEqual(seconds, 1730786280.123, places=3)

    def test_negative_timestamps_are_not_dropped(self):
        """1970 年以前的时间戳是负数，不能因为正则没写 ``-`` 就当成非法。"""
        self.assertEqual(timestamp_seconds('-100'), (-100, '秒'))

    def test_too_long_digits_raise(self):
        """19 位（纳秒）不猜，直接报错。"""
        with self.assertRaises(DateError):
            timestamp_seconds('1730786280123456789')

    def test_seconds_and_millis_agree(self):
        """同一时刻的两种写法必须落到同一个结果。"""
        a = parse_moment('1730786280')
        b = parse_moment('1730786280000')
        self.assertEqual(a.utc, b.utc)


class ParseMomentTests(unittest.TestCase):
    def test_naive_input_is_beijing_time(self):
        """没写时区的输入按北京时间解释——不是 UTC。

        假设 UTC 会让用户看到的时间**整整差 8 小时**，且界面上一切正常。
        """
        moment = parse_moment('2026-11-04 13:08:00')
        self.assertEqual(moment.utc, utc(2026, 11, 4, 5, 8, 0))

    def test_explicit_offset_is_honoured(self):
        moment = parse_moment('2026-11-04 13:08:00+08:00')
        self.assertEqual(moment.utc, utc(2026, 11, 4, 5, 8, 0))

    def test_zulu_suffix_is_utc(self):
        moment = parse_moment('2026-11-04T05:08:00Z')
        self.assertEqual(moment.utc, utc(2026, 11, 4, 5, 8, 0))

    def test_date_only_defaults_to_midnight(self):
        moment = parse_moment('2026-11-04')
        self.assertEqual(moment.utc, utc(2026, 11, 3, 16, 0, 0))

    def test_chinese_date_format(self):
        moment = parse_moment('2026年11月4日 13:08')
        self.assertEqual(moment.utc, utc(2026, 11, 4, 5, 8, 0))

    def test_slash_and_dot_separators(self):
        for text in ('2026/11/04 13:08', '2026.11.04 13:08'):
            with self.subTest(text=text):
                self.assertEqual(parse_moment(text).utc, utc(2026, 11, 4, 5, 8))

    def test_compact_8_and_14_digit_forms(self):
        """``20261104`` 当日期、不当 1970 年附近的秒时间戳。"""
        self.assertEqual(parse_moment('20261104').utc, utc(2026, 11, 3, 16, 0))
        self.assertEqual(parse_moment('20261104130811').utc,
                         utc(2026, 11, 4, 5, 8, 11))

    def test_ten_digit_stays_a_timestamp(self):
        """10 位纯数字**必须**留给时间戳，不能当紧凑日期。

        ``2026110413`` 两种解释都讲得通，但 10 位时间戳远比
        「2026 年 11 月 4 日 13 点」常见。这条钉住那个取舍。
        """
        moment = parse_moment('2026110413')
        self.assertNotEqual(moment.utc, utc(2026, 11, 4, 5, 0))
        self.assertGreater(moment.utc.year, 2030)

    def test_eastern_input_uses_dst_rules(self):
        """明确写 EST/EDT 的美东时间要按当日实际规则换算。"""
        summer = parse_moment('2026-07-01 12:00 EDT')
        self.assertEqual(summer.utc, utc(2026, 7, 1, 16, 0))
        winter = parse_moment('2026-01-15 12:00 EST')
        self.assertEqual(winter.utc, utc(2026, 1, 15, 17, 0))

    def test_wrong_eastern_label_is_flagged(self):
        """写了 EST 但那天实际是 EDT → 要提示规则冲突，不能闷头算。"""
        moment = parse_moment('2026-07-01 12:00 EST')
        self.assertIn('EDT', moment.note)

    def test_invalid_date_raises_with_message(self):
        with self.assertRaises(DateError) as ctx:
            parse_moment('2026-02-30 10:00')
        self.assertIn('合法日期', str(ctx.exception))

    def test_garbage_raises(self):
        with self.assertRaises(DateError):
            parse_moment('不是时间')


class HumanizeDeltaTests(unittest.TestCase):
    def test_combined_units(self):
        self.assertEqual(humanize_delta(181845), '2 天 2 小时 30 分 45 秒')

    def test_zero_units_are_omitted(self):
        self.assertEqual(humanize_delta(3600), '1 小时')

    def test_seconds_always_shown_when_nothing_else(self):
        """只差 30 秒时前面全空，秒必须仍在——否则得到空字符串。"""
        self.assertEqual(humanize_delta(30), '30 秒')

    def test_exact_zero(self):
        self.assertEqual(humanize_delta(0), '0 秒')

    def test_negative_uses_absolute_value(self):
        """``humanize_delta`` 只负责量级，方向由调用方表达。"""
        self.assertEqual(humanize_delta(-90), '1 分 30 秒')


class ConvertTests(unittest.TestCase):
    def test_single_line_shows_three_zones(self):
        text, _status, kind = convert('2026-11-04 13:08:00')
        self.assertEqual(kind, 'ok')
        for label in ('北京时间', '美东时间', 'UTC'):
            self.assertIn(label, text)
        self.assertIn('2026-11-04 13:08:00', text)      # 北京
        self.assertIn('2026-11-04 00:08:00', text)      # 美东（EST）
        self.assertIn('2026-11-04 05:08:00', text)      # UTC

    def test_single_line_also_gives_both_timestamp_units(self):
        text, _status, _kind = convert('2026-11-04 13:08:00')
        self.assertIn('1793768880（秒）', text)
        self.assertIn('1793768880000（毫秒）', text)

    def test_two_lines_compute_difference(self):
        text, status, kind = convert('2026-11-04 13:00:00\n2026-11-06 15:30:45')
        self.assertEqual(kind, 'ok')
        self.assertIn('2 天 2 小时 30 分 45 秒', text)
        self.assertIn('2 天 2 小时 30 分 45 秒', status)
        self.assertIn('181845 秒', text)

    def test_reverse_order_is_flagged_not_silently_swapped(self):
        """终点早于起点必须明说——悄悄对调会给出方向相反的结论。"""
        text, status, kind = convert('2026-11-06 15:30:45\n2026-11-04 13:00:00')
        self.assertEqual(kind, 'error')
        self.assertIn('终点比起点早', text)
        self.assertIn('终点比起点早', status)

    def test_blank_lines_are_ignored(self):
        text, _status, _kind = convert('\n2026-11-04 13:00:00\n\n2026-11-05 13:00:00\n')
        self.assertIn('1 天', text)

    def test_three_lines_raise_instead_of_silently_dropping(self):
        with self.assertRaises(DateError) as ctx:
            convert('2026-11-04\n2026-11-05\n2026-11-06')
        self.assertIn('3 行', str(ctx.exception))

    def test_absurd_year_is_flagged(self):
        """13 位被当成秒会落到 50000 年，提示单位可能选错。"""
        _text, status, kind = convert('9999999999999')
        self.assertEqual(kind, 'error')
        self.assertIn('单位', status)

    def test_mixed_timestamp_and_date_difference(self):
        """一行时间戳、一行日期，也要能算差。"""
        text, _status, _kind = convert('1730786280\n2024-11-05 13:58:00')
        self.assertIn('0 秒', text)

    def test_dst_transition_difference_is_23_or_25_hours(self):
        """美东跨 DST 的一个自然日不是 24 小时。

        用**美东本地时间**输入 11/1 前后两天：回拨那天有 25 小时。
        这条同时验证了「差值基于 UTC」这个设计——若拿本地时间去减，
        结果永远是 24 小时，看起来对，实际错。
        """
        text, _status, _kind = convert(
            '2026-10-31 12:00 EDT\n2026-11-01 12:00 EST')
        self.assertIn('1 天 1 小时', text)


class SectionMarkTests(unittest.TestCase):
    def test_section_mark_matches_tool_window(self):
        """本模块的段落标记必须与窗口骨架一致。

        两处各写了一份（骨架→模块会成环，不能互相导入），所以只能靠测试钉。
        """
        from lumina.tool_window import SECTION_MARK as WINDOW_MARK
        self.assertEqual(SECTION_MARK, WINDOW_MARK)


class NoZoneinfoDependencyTests(unittest.TestCase):
    """守卫：不许改回 ``zoneinfo`` / ``pytz``。

    本机（以及任何没装 tzdata 的 Windows）上 ``ZoneInfo("America/New_York")``
    直接抛 ``ZoneInfoNotFoundError``。改回去会在装了 tzdata 的开发机上「看起来
    正常」，而在用户机器上整个工具崩掉。

    **只能扫「导入语句」而不是全文**：模块 docstring 里正大光明地写着「不走
    zoneinfo」这个决定和原因，扫全文会把那段说明本身当成违规——第一版就是这么
    假失败的。判据要盯机制（有没有真的 import），不是盯词。
    """

    def test_module_does_not_import_timezone_libraries(self):
        source = (ROOT / 'lumina' / 'datetime_tools.py').read_text(encoding='utf-8')
        import_lines = [
            line.strip() for line in source.splitlines()
            if line.strip().startswith(('import ', 'from '))
        ]
        joined = '\n'.join(import_lines).lower()
        for banned in ('zoneinfo', 'pytz', 'dateutil', 'tzdata'):
            with self.subTest(module=banned):
                self.assertNotIn(
                    banned, joined,
                    f'datetime_tools 导入了 {banned}，在没装 tzdata 的 Windows 上会崩')


class BeijingToEasternTests(unittest.TestCase):
    """单项换算：北京时间 → 美东时间（窗口第 3 段的逻辑）。"""

    def test_winter_uses_est(self):
        text, _status, kind = beijing_to_eastern('2026-11-04 15:34:20')
        self.assertIn('2026-11-04 02:34:20', text)
        self.assertIn('EST', text)
        self.assertEqual(kind, 'ok')

    def test_summer_uses_edt(self):
        """夏天必须走 EDT。只测冬天会让整个夏令时分支无人覆盖。"""
        text, _status, _kind = beijing_to_eastern('2026-07-01 20:00:00')
        self.assertIn('2026-07-01 08:00:00', text)
        self.assertIn('EDT', text)

    def test_date_only_input_is_accepted(self):
        """只给日期不给时间：按当天 00:00 算，而不是报错。"""
        text, _status, _kind = beijing_to_eastern('2026-11-04')
        self.assertIn('2026-11-03 11:00:00', text)   # 00:00 - 13h(夏令时前一日)

    def test_compact_date_is_accepted(self):
        text, _status, _kind = beijing_to_eastern('20261104')
        self.assertIn('2026-11-03 11:00:00', text)

    def test_timestamp_input_points_at_the_other_field(self):
        """填错框时要指到隔壁那个框，而不是干巴巴一句「认不出」。"""
        with self.assertRaises(DateError) as ctx:
            beijing_to_eastern('1793777660684')
        self.assertIn('时间戳转日期', str(ctx.exception))

    def test_empty_input_is_rejected(self):
        with self.assertRaises(DateError):
            beijing_to_eastern('')
        with self.assertRaises(DateError):
            beijing_to_eastern('   ')

    def test_garbage_is_rejected(self):
        with self.assertRaises(DateError):
            beijing_to_eastern('随便写的')

    def test_does_not_respect_a_timezone_suffix(self):
        """**这个框的输入一律按北京时间解释**，后缀不参与判定。

        它是一个带标题的专用输入框（标题就是「北京时间 → 美东时间」），用户
        心里想的就是北京时间。此时再去尊重 ``UTC`` 后缀，会让「我明明按标题
        填的」变成错的结果。

        带后缀的写法在本框里**直接报错**（而不是被悄悄按 UTC 解释）——报错
        比给一个「看着合理但差 8 小时」的结果安全。真正需要按后缀解释的场景
        走 ``convert`` 的自动识别路径。
        """
        with self.assertRaises(DateError):
            beijing_to_eastern('2026-11-04 15:34:20 UTC')
        # 不带后缀的同一时刻则是正常结果，证明前者不是「解析不了日期」。
        text, _status, _kind = beijing_to_eastern('2026-11-04 15:34:20')
        self.assertIn('02:34:20', text)


class TimestampToDateTests(unittest.TestCase):
    """单项换算：时间戳 → 日期（窗口第 4 段的逻辑）。"""

    def test_milliseconds_gives_both_zones(self):
        text, status, kind = timestamp_to_date('1793777660684')
        self.assertIn('2026-11-04 15:34:20', text)   # 北京
        self.assertIn('2026-11-04 02:34:20', text)   # 美东
        self.assertIn('北京时间', text)
        self.assertIn('毫秒', status)
        self.assertEqual(kind, 'ok')

    def test_seconds_are_recognized(self):
        text, status, _kind = timestamp_to_date('1793777660')
        self.assertIn('2026-11-04 15:34:20', text)
        self.assertIn('秒', status)

    def test_date_input_points_at_the_other_field(self):
        with self.assertRaises(DateError) as ctx:
            timestamp_to_date('2026-11-04')
        self.assertIn('上面', str(ctx.exception))

    def test_empty_is_rejected(self):
        with self.assertRaises(DateError):
            timestamp_to_date('')

    def test_out_of_range_year_warns(self):
        """13 位当秒会得到 2286 年——「看着对、用起来坏」必须提示。

        这类结果比报错更危险：界面上一切正常，用户却拿到了一个差了几百年的
        日期，看不出哪里不对。
        """
        _text, status, kind = timestamp_to_date('9999999999999')
        self.assertEqual(kind, 'error')
        self.assertIn('单位', status)

    def test_absurdly_long_digits_are_rejected(self):
        """16 位既不是秒也不是毫秒（是 .NET tick），不猜。"""
        with self.assertRaises(DateError):
            timestamp_to_date('1793777660684000')


class LooksLikeTimestampTests(unittest.TestCase):
    """分流判据：哪些纯数字该走时间戳路径。

    **与 ``parse_moment`` 必须同一条口径**：8/12/14 位让给紧凑日期。两处若
    不一致，同一个输入在「自动识别」与「单项换算」下会得到不同结果。
    """

    def test_ordinary_timestamps(self):
        for text in ('1793777660', '1793777660684', '0', '1'):
            with self.subTest(text=text):
                self.assertTrue(looks_like_timestamp(text))

    def test_compact_dates_are_not_timestamps(self):
        for text in ('20261104', '202611041308', '20261104130811'):
            with self.subTest(text=text):
                self.assertFalse(looks_like_timestamp(text))

    def test_dates_and_text_are_not_timestamps(self):
        for text in ('2026-11-04', '2026-11-04 15:34:20', '随便', '', 'abc'):
            with self.subTest(text=text):
                self.assertFalse(looks_like_timestamp(text))

    def test_agrees_with_parse_moment(self):
        """两处判据必须给出同一个答案——这是本条唯一真正要守的东西。

        若 ``looks_like_timestamp`` 与 ``parse_moment`` 的口径不一致，同一个
        输入在「顶部时钟的分类」与「单项换算框」下会得到不同结果。
        """
        for text in ('1793777660', '20261104', '1793777660684'):
            with self.subTest(text=text):
                moment = parse_moment(text)
                if looks_like_timestamp(text):
                    # 时间戳路径：1970 + N 秒（毫秒值会带出微秒）
                    expected = (datetime.datetime(1970, 1, 1)
                                + datetime.timedelta(
                                    seconds=timestamp_seconds(text)[0]))
                    self.assertEqual(moment.utc, expected)
                else:
                    # 紧凑日期路径：落在 2026 年，而不是 1970 年
                    self.assertEqual(moment.utc.year, 2026)


class ClockSnapshotTests(unittest.TestCase):
    """实时时钟条的数据源。"""

    def test_returns_both_zones_for_the_same_moment(self):
        snap = clock_snapshot(datetime.datetime(2026, 11, 4, 7, 34, 20))
        self.assertEqual(snap['beijing'], '2026-11-04 15:34:20')
        self.assertEqual(snap['eastern'], '2026-11-04 02:34:20')
        self.assertEqual(snap['eastern_tag'], 'EST UTC-5')

    def test_timestamp_matches_the_beijing_string(self):
        """时间戳与北京时间必须是同一时刻——它们并排显示，不一致就是 bug。"""
        snap = clock_snapshot(datetime.datetime(2026, 11, 4, 7, 34, 20))
        self.assertEqual(snap['millis'], 1793777660000)
        as_beijing = (datetime.datetime(1970, 1, 1)
                      + datetime.timedelta(milliseconds=snap['millis'])
                      + datetime.timedelta(hours=8))
        self.assertEqual(as_beijing.strftime('%Y-%m-%d %H:%M:%S'),
                         snap['beijing'])

    def test_weekdays_are_provided_for_both_zones(self):
        snap = clock_snapshot(datetime.datetime(2026, 11, 4, 7, 34, 20))
        self.assertEqual(snap['beijing_weekday'], '周三')
        self.assertEqual(snap['eastern_weekday'], '周三')

    def test_microseconds_are_dropped(self):
        """秒以下抹掉：两栏每秒重画，微秒会让它们在边界上看起来对不齐。"""
        snap = clock_snapshot(
            datetime.datetime(2026, 11, 4, 7, 34, 20, 999999))
        self.assertEqual(snap['beijing'], '2026-11-04 15:34:20')
        self.assertEqual(snap['millis'], 1793777660000)

    def test_eastern_tag_follows_dst(self):
        summer = clock_snapshot(datetime.datetime(2026, 7, 1, 12, 0, 0))
        winter = clock_snapshot(datetime.datetime(2026, 11, 4, 12, 0, 0))
        self.assertEqual(summer['eastern_tag'], 'EDT UTC-4')
        self.assertEqual(winter['eastern_tag'], 'EST UTC-5')

    def test_zone_name_is_the_local_one(self):
        snap = clock_snapshot(datetime.datetime(2026, 11, 4, 7, 34, 20))
        self.assertEqual(snap['zone_name'], 'Asia/Shanghai')

    def test_defaults_to_now(self):
        """不传参数时取本机当前时间——两次调用不该差出一分钟。"""
        before = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
        snap = clock_snapshot()
        after = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
        stamped = datetime.datetime(1970, 1, 1) + datetime.timedelta(
            seconds=snap['seconds'])
        self.assertLessEqual(before.replace(microsecond=0)
                             - datetime.timedelta(seconds=1), stamped)
        self.assertLessEqual(stamped, after.replace(microsecond=0)
                             + datetime.timedelta(seconds=1))


if __name__ == '__main__':
    unittest.main()
