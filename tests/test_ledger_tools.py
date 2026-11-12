import unittest
from datetime import date, datetime
from decimal import Decimal, localcontext

from lumina.ledger_tools import (
    DEFAULT_CATEGORIES,
    KINDS,
    MAX_AMOUNT_CENTS,
    RECORD_FIELDS,
    build_entry,
    format_money,
    period_bounds,
    summarize,
)

TODAY = date(2026, 11, 11)


def entry(kind='expense', amount='10.00', day='2026-11-11', category='餐饮', **kwargs):
    return build_entry(kind, amount, category, '现金', day, **kwargs)


class LedgerAmountTests(unittest.TestCase):
    def test_exact_amount_and_record_contract(self):
        for amount, expected in [('0.01', 1), ('1.1', 110), (12, 1200),
                                 (0.29, 29), (Decimal('123.45'), 12345),
                                 ('999999999.99', MAX_AMOUNT_CENTS)]:
            with self.subTest(amount=amount):
                result = entry(amount=amount)
                self.assertEqual(result['amount_cents'], expected)
                self.assertEqual(set(result), set(RECORD_FIELDS))
                self.assertEqual(tuple(result), RECORD_FIELDS)
                self.assertNotIn('to_account', result)
                self.assertNotIn('refund_of', result)
        with localcontext() as ctx:
            ctx.prec = 2
            self.assertEqual(entry(amount='123456789.12')['amount_cents'], 12345678912)

    def test_reject_invalid_amount(self):
        for amount in ('', 'abc', '0', '-1', '0.001', '1.230', '1.999',
                       'NaN', 'sNaN', 'Infinity', '-Infinity', float('nan'),
                       float('inf'), '1000000000', '1e1000', True, None, object()):
            with self.subTest(amount=amount):
                with self.assertRaises(ValueError):
                    entry(amount=amount)

    def test_money_format_exact_integer(self):
        for cents, text in [(0, '¥0.00'), (1, '¥0.01'), (123456, '¥1,234.56'),
                            (-123456, '¥-1,234.56'), (10**25 + 1, '¥100,000,000,000,000,000,000,000.01')]:
            self.assertEqual(format_money(cents), text)
        for bad in (True, 1.0, '1'):
            with self.assertRaises(ValueError):
                format_money(bad)

    def test_validation_and_normalization(self):
        row = build_entry('transfer', '1', ' 转账 ', ' 现金 ', '2024-02-29', note=' 备注 ')
        self.assertEqual(row['account'], '现金')
        self.assertEqual(row['category'], '转账')
        self.assertEqual(row['note'], '备注')
        for changes in [{'kind': 'bad'}, {'category': ' '}, {'account': ''},
                        {'entry_date': '2023-02-29'}, {'entry_date': '2026-1-01'},
                        {'entry_date': '2026-11-31'}, {'note': None}, {'category': 1}]:
            base = {'kind': 'expense', 'amount': '1', 'category': '餐饮', 'account': '现金',
                        'entry_date': '2026-11-11'}
            base.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                build_entry(**base)
        for field, value in [('to_account', ''), ('to_account', '现金'),
                             ('refund_of', None), ('refund_of', 1)]:
            with self.subTest(field=field, value=value), self.assertRaises(TypeError):
                entry(**{field: value})
        for kind in KINDS:
            row = entry(kind=kind, category='独立分类')
            self.assertEqual(row['kind'], kind)
            self.assertEqual(row['category'], '独立分类')
        self.assertEqual(set(KINDS), {'expense', 'income', 'transfer', 'refund'})
        self.assertEqual(set(DEFAULT_CATEGORIES), set(KINDS))


class LedgerPeriodTests(unittest.TestCase):
    def test_current_and_historical_periods(self):
        for mode, year, month, expected in [
            ('month', 2026, 11, ('2026-11-01', '2026-11-11')),
            ('month', 2026, 10, ('2026-10-01', '2026-10-31')),
            ('month', 2024, 2, ('2024-02-01', '2024-02-29')),
            ('month', 2025, 2, ('2025-02-01', '2025-02-28')),
            ('year', 2026, 1, ('2026-01-01', '2026-11-11')),
            ('year', 2024, 11, ('2024-01-01', '2024-12-31')),
        ]:
            with self.subTest(mode=mode, year=year, month=month):
                self.assertEqual(period_bounds(mode, year, month, TODAY), expected)
        self.assertEqual(period_bounds('month', 2026, 11, '2026-11-11')[1], '2026-11-11')
        self.assertEqual(period_bounds('month', 2026, 11, datetime(2026, 11, 11))[1], '2026-11-11')

    def test_invalid_and_future_periods(self):
        for mode, year, month in [('month', 2026, 12), ('year', 2027, 1),
                                  ('bad', 2026, 1), ('month', 0, 1),
                                  ('year', 10000, 1), ('month', 2026, 13),
                                  ('month', 2026, 0), ('month', 2026, True)]:
            with self.subTest(mode=mode, year=year, month=month), self.assertRaises(ValueError):
                period_bounds(mode, year, month, TODAY)
        self.assertEqual(period_bounds('month', 2026, 11, date(2026, 11, 1)),
                         ('2026-11-01', '2026-11-01'))

    def test_month_filter_transfer_refund_and_categories(self):
        rows = [entry(amount='10', day='2026-11-01'), entry(amount='5'),
                entry(kind='income', amount='20', category='工资'),
                entry(kind='transfer', amount='500'),
                entry(kind='refund', amount='2'),
                entry(amount='999', day='2026-11-12'),
                entry(amount='999', day='2026-10-31'),
                entry(amount='999', day='2025-11-11')]
        summary = summarize(iter(rows), today=TODAY)
        self.assertEqual((summary['income_cents'], summary['expense_cents'],
                          summary['balance_cents'], summary['average_expense_cents']),
                         (2000, 1300, 700, 1300))
        self.assertEqual(summary['categories'],
                         [{'category': '餐饮', 'amount_cents': 1300, 'share': 1.0}])
        self.assertEqual(len(summary['months']), 12)
        self.assertEqual(summary['months'][10]['expense_cents'], 1300)
        self.assertEqual(summary['months'][11], {'month': 12, 'income_cents': 0,
                         'expense_cents': 0, 'balance_cents': 0, 'future': True})
        self.assertEqual((summary['start'], summary['end'], summary['cutoff'], summary['title']),
                         ('2026-11-01', '2026-11-11', '2026-11-11', '2026年11月'))

    def test_refund_uses_own_category_without_matching_expense(self):
        rows = [entry(amount='10', category='餐饮'),
                entry(kind='refund', amount='25', category='独立退款'),
                entry(kind='transfer', amount='10000', category='不计收支')]
        summary = summarize(rows, today=TODAY)
        self.assertEqual((summary['income_cents'], summary['expense_cents'],
                          summary['balance_cents']), (0, -1500, 1500))
        self.assertEqual({row['category']: row['amount_cents'] for row in summary['categories']},
                         {'餐饮': 1000, '独立退款': -2500})
        self.assertEqual(summary['months'][10]['expense_cents'], -1500)
        self.assertTrue(all(row['share'] is None for row in summary['categories']))

    def test_year_elapsed_months_rounding_and_cutoff(self):
        rows = [entry(amount='0.06', day='2026-01-01'),
                entry(kind='income', amount='10', day='2026-03-01'),
                entry(amount='100', day='2026-11-12'), entry(amount='100', day='2026-12-01')]
        summary = summarize(rows, mode='year', today=TODAY)
        self.assertEqual(summary['months_elapsed'], 11)
        self.assertEqual(summary['average_expense_cents'], 1)
        self.assertEqual(summary['expense_cents'], 6)
        self.assertEqual(summary['months'][2]['income_cents'], 1000)
        self.assertEqual(summary['months'][11]['expense_cents'], 0)
        historical = summarize([entry(amount='0.06', day='2024-12-31')],
                               mode='year', year=2024, today=TODAY)
        self.assertEqual(historical['months_elapsed'], 12)
        self.assertEqual(historical['average_expense_cents'], 1)
        self.assertFalse(any(row['future'] for row in historical['months']))

    def test_cross_year_refund_negative_expense_and_zero_share(self):
        rows = [entry(amount='100', day='2025-12-31'),
                entry(kind='refund', amount='20', day='2026-01-01')]
        original_year = summarize(rows, 'year', 2025, 1, TODAY)
        refund_year = summarize(rows, 'year', 2026, 1, TODAY)
        self.assertEqual(original_year['expense_cents'], 10000)
        self.assertEqual(refund_year['expense_cents'], -2000)
        self.assertEqual(refund_year['balance_cents'], 2000)
        self.assertEqual(refund_year['average_expense_cents'], -182)
        self.assertIsNone(refund_year['categories'][0]['share'])
        zero = summarize([entry(), entry(kind='refund')], today=TODAY)
        self.assertIsNone(zero['categories'][0]['share'])
        empty = summarize([], today=TODAY)
        self.assertEqual(empty['categories'], [])
        self.assertEqual(empty['average_expense_cents'], 0)


if __name__ == '__main__':
    unittest.main()
