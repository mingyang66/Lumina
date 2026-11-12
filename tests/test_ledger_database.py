import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from lumina.database import LEDGER_TABLE_SQL, Database
from lumina.ledger_tools import MAX_AMOUNT_CENTS, RECORD_FIELDS, build_entry, summarize

OLD_LEDGER_SQL = """
CREATE TABLE ledger_entry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK(kind IN ('expense','income','transfer','refund')),
    amount_cents INTEGER NOT NULL CHECK(typeof(amount_cents)='integer'
        AND amount_cents > 0 AND amount_cents <= 99999999999),
    category TEXT NOT NULL CHECK(length(trim(category)) > 0),
    account TEXT NOT NULL CHECK(length(trim(account)) > 0),
    to_account TEXT NOT NULL DEFAULT '',
    entry_date TEXT NOT NULL CHECK(entry_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    note TEXT NOT NULL DEFAULT '',
    refund_of INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    CHECK((kind='refund' AND typeof(refund_of)='integer' AND refund_of > 0)
        OR (kind!='refund' AND refund_of IS NULL)),
    CHECK((kind='transfer' AND length(trim(to_account)) > 0 AND to_account!=account)
        OR (kind!='transfer' AND to_account=''))
);
CREATE INDEX IF NOT EXISTS idx_ledger_entry_date ON ledger_entry(entry_date, id);
CREATE INDEX IF NOT EXISTS idx_ledger_entry_kind_category ON ledger_entry(kind, category, entry_date);
CREATE INDEX IF NOT EXISTS idx_ledger_entry_refund ON ledger_entry(refund_of);
CREATE INDEX IF NOT EXISTS idx_ledger_destination ON ledger_entry(to_account);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_custom ON ledger_entry(note, account) WHERE amount_cents > 0;
"""


def record(kind='expense', amount='100', category='餐饮', account='现金',
           day='2026-11-01', **kwargs):
    return build_entry(kind, amount, category, account, day, **kwargs)


class LedgerDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(':memory:')
        self.db.init_schema()

    def tearDown(self):
        self.db.close()

    def add(self, **kwargs):
        return self.db.add_ledger_entry(**record(**kwargs))

    def test_crud_defaults_and_rows(self):
        self.assertEqual(self.db.list_ledger_accounts(), ['现金', '银行卡'])
        original = self.add()
        row = self.db.get_ledger_entry(original)
        self.assertIsInstance(row, sqlite3.Row)
        self.assertEqual(set(row.keys()), {'id', 'created_at', *RECORD_FIELDS})
        self.assertEqual(row['amount_cents'], 10000)
        self.assertTrue(row['created_at'])
        self.assertTrue(self.db.update_ledger_entry(original, note='更新', amount_cents=99))
        updated = self.db.get_ledger_entry(original)
        self.assertEqual(updated['note'], '更新')
        self.assertEqual(updated['amount_cents'], 99)
        self.assertEqual(updated['created_at'], row['created_at'])
        self.assertTrue(self.db.delete_ledger_entry(original))
        self.assertFalse(self.db.delete_ledger_entry(original))
        self.assertIsNone(self.db.get_ledger_entry(original))
        with self.assertRaisesRegex(ValueError, '不存在'):
            self.db.update_ledger_entry(original, note='无效')

    def test_refunds_are_independent_and_do_not_protect_expense(self):
        refund = self.add(kind='refund', amount='200', category='独立退款',
                          account='退款账户', day='2025-01-01')
        original = self.add(amount='1', day='2026-11-02')
        self.add(kind='refund', amount='300', category='其他退款')
        self.assertTrue(self.db.update_ledger_entry(refund, amount_cents=40000,
                                                    category='自选分类', account='自选账户',
                                                    entry_date='2024-02-29'))
        row = self.db.get_ledger_entry(refund)
        self.assertEqual((row['kind'], row['amount_cents'], row['category'],
                          row['account'], row['entry_date']),
                         ('refund', 40000, '自选分类', '自选账户', '2024-02-29'))
        self.db.update_ledger_entry(original, kind='income', category='工资', amount_cents=1,
                                    entry_date='2026-12-01')
        self.assertTrue(self.db.delete_ledger_entry(original))
        self.assertIsNotNone(self.db.get_ledger_entry(refund))
        self.assertTrue(self.db.delete_ledger_entry(refund))

    def test_transfer_single_account_and_type_changes(self):
        transfer = self.add(kind='transfer', category='内部调拨', account='备用钱包')
        self.assertEqual(self.db.get_ledger_entry(transfer)['account'], '备用钱包')
        for kind in ('refund', 'expense', 'income', 'transfer'):
            self.db.update_ledger_entry(transfer, kind=kind)
            self.assertEqual(self.db.get_ledger_entry(transfer)['kind'], kind)

    def test_sql_filter_inclusive_order_and_literal_search(self):
        start = self.add(day='2026-11-01', note="100%_ ' OR 1=1 --")
        end = self.add(day='2026-11-30', category='购物', account='电子钱包')
        same_day = self.add(day='2026-11-30', kind='income', category='工资')
        self.add(day='2026-10-31')
        self.add(day='2026-12-01')
        self.add(kind='transfer', account='储蓄账户')
        statements = []
        self.db.conn.set_trace_callback(statements.append)
        rows = self.db.list_ledger_entries('2026-11-01', '2026-11-30',
                                           kind='expense', category='购物', query='电子钱包')
        self.db.conn.set_trace_callback(None)
        self.assertEqual([row['id'] for row in rows], [end])
        sql = ' '.join(statements)
        for expected in ('entry_date>=', 'entry_date<=', 'kind=', 'category=', 'instr('):
            self.assertIn(expected, sql)
        self.assertNotIn('to_account', sql)
        rows = self.db.list_ledger_entries('2026-11-01', '2026-11-30')
        self.assertEqual([row['id'] for row in rows[:2]], [same_day, end])
        self.assertIn(start, [row['id'] for row in rows])
        for query in ('%', '_', "' OR 1=1 --"):
            self.assertEqual([row['id'] for row in self.db.list_ledger_entries(
                '2026-11-01', '2026-11-30', query=query)], [start])
        self.assertEqual(len(self.db.list_ledger_entries(
            '2026-11-01', '2026-11-30', query='储蓄账户')), 1)
        self.assertEqual(self.db.list_ledger_entries('2026-11-01', '2026-11-30',
                                                    kind="expense' OR 1=1 --"), [])
        self.assertEqual(self.db.list_ledger_entries('2026-11-01', '2026-11-30',
                                                    category="购物' OR 1=1 --"), [])
        for start_date, end_date in [('2026-11-02', '2026-11-01'),
                                      ("2026-11-01' OR 1=1", '2026-11-30'),
                                      ('2026-02-30', '2026-11-30')]:
            with self.assertRaises(ValueError):
                self.db.list_ledger_entries(start_date, end_date)

    def test_custom_accounts_categories_and_independence(self):
        bank = self.db.add_vault_bank('不应成为记账账户')
        clip = self.db.add_text('不应成为记账分类', category='剪贴分类')
        self.add(account='电子钱包', category='宠物')
        self.add(kind='transfer', category='转账', account='备用钱包')
        self.add(kind='income', category='稿费')
        self.add(kind='refund', category='退款自定义', account='退款账户')
        accounts = self.db.list_ledger_accounts()
        self.assertEqual(accounts[:2], ['现金', '银行卡'])
        self.assertIn('备用钱包', accounts)
        self.assertIn('电子钱包', accounts)
        self.assertIn('退款账户', accounts)
        self.assertNotIn('不应成为记账账户', accounts)
        self.assertEqual(len(accounts), len(set(accounts)))
        self.assertIn('宠物', self.db.list_ledger_categories('expense'))
        self.assertNotIn('稿费', self.db.list_ledger_categories('expense'))
        self.assertIn('稿费', self.db.list_ledger_categories())
        self.assertIn('退款自定义', self.db.list_ledger_categories('refund'))
        self.assertNotIn('剪贴分类', self.db.list_ledger_categories())
        with self.assertRaises(ValueError):
            self.db.list_ledger_categories('bad')
        self.db.cleanup(0, 0)
        self.db.delete(clip)
        self.db.delete_vault_bank(bank)
        self.assertEqual(len(self.db.list_ledger_entries('2026-01-01', '2026-12-31')), 4)

    def test_database_boundary_validation_and_injection(self):
        original = self.add()
        for fields in [{'amount_cents': 0}, {'amount_cents': -1}, {'amount_cents': True},
                       {'amount_cents': 1.0}, {'amount_cents': '100'},
                       {'amount_cents': MAX_AMOUNT_CENTS + 1}, {'category': ''},
                       {'entry_date': '2026-02-30'}, {'account': ''},
                       {'note=? WHERE 1=1 --': '坏字段'}, {'id': 55}, {'created_at': 'bad'},
                       {'to_account': ''}, {'to_account': '银行卡'},
                       {'refund_of': None}, {'refund_of': 1}]:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.db.update_ledger_entry(original, **fields)
            self.assertFalse(self.db.conn.in_transaction)
            bad = record()
            bad.update(fields)
            with self.subTest(add_fields=fields), self.assertRaises(ValueError):
                self.db.add_ledger_entry(**bad)
        self.assertIsNone(self.db.get_ledger_entry('1 OR 1=1'))
        self.assertFalse(self.db.delete_ledger_entry('1 OR 1=1'))
        self.assertEqual(self.db.get_ledger_entry(original)['amount_cents'], 10000)
        malicious = self.add(category="x'); DROP TABLE clipboard; --", note="';DELETE FROM ledger_entry;--")
        self.assertIsNotNone(self.db.get_ledger_entry(malicious))
        self.assertEqual(self.db.count(), 0)

    def test_schema_amount_checks_for_all_kinds(self):
        for kind in ('expense', 'income', 'transfer', 'refund'):
            for amount in (0, -1, 1.5, MAX_AMOUNT_CENTS + 1):
                with self.subTest(kind=kind, amount=amount), self.assertRaises(sqlite3.IntegrityError):
                    with self.db.conn:
                        self.db.conn.execute(
                            'INSERT INTO ledger_entry(kind,amount_cents,category,account,entry_date) '
                            "VALUES (?,?,'餐饮','现金','2026-11-01')", (kind, amount))
            with self.db.conn:
                self.db.conn.execute(
                    'INSERT INTO ledger_entry(kind,amount_cents,category,account,entry_date) '
                    "VALUES (?,1,'独立分类','现金','2026-11-01')", (kind,))

    def test_memory_database_does_not_touch_disk(self):
        with patch('lumina.database.os.makedirs', side_effect=AssertionError('不能创建目录')):
            db = Database(':memory:')
            try:
                db.init_schema()
                db.add_ledger_entry(**record())
            finally:
                db.close()

    def test_cross_year_independent_refund_sql_and_statistics(self):
        self.add(day='2025-12-31')
        self.add(kind='refund', amount='20', category='独立退款', day='2026-01-01')
        self.add(kind='transfer', amount='10000', day='2026-01-01')
        self.add(kind='income', amount='30', day='2026-02-01')
        rows = self.db.list_ledger_entries('2026-01-01', '2026-12-31')
        summary = summarize(rows, 'year', 2026, 1, '2026-11-11')
        self.assertEqual((summary['income_cents'], summary['expense_cents'],
                          summary['balance_cents']), (3000, -2000, 5000))
        self.assertEqual(summary['categories'][0]['category'], '独立退款')
        self.assertEqual(summary['months'][0]['expense_cents'], -2000)


class LedgerPersistenceTests(unittest.TestCase):
    def schemas(self):
        schema = Path(Database._schema_path()).read_text(encoding='utf-8')
        base = schema.split('-- 记账独立于剪贴板')[0]
        return schema, base + OLD_LEDGER_SQL, base

    def seed_old(self, path, empty=False):
        _, old_schema, _ = self.schemas()
        with closing(sqlite3.connect(path)) as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.executescript(old_schema)
            conn.execute("INSERT INTO clipboard(kind,content,hash,category) VALUES ('text','旧记录','hash','text')")
            conn.execute("INSERT INTO vault_bank(name) VALUES ('旧银行')")
            conn.execute("INSERT INTO vault_deposit(bank_id,name,principal,rate,months,end_date) "
                         "VALUES (1,'旧存单',1000,1.5,12,'2027-01-01')")
            rows = [
                (7, 'expense', 10000, '餐饮', '现金', '', '2025-12-31', '支出', None, '2025-12-31 12:34:56'),
                (12, 'income', 30000, '工资', '银行卡', '', '2026-01-01', '收入', None, '2026-01-01 00:00:01'),
                (30, 'transfer', 98765, '历史调拨', '转出钱包', '仅旧转入', '2026-01-02', '转账', None, '2026-01-02 03:04:05'),
                (41, 'refund', 22222, '独立旧分类', '退款账户', '', '2024-02-29', '孤立退款', 9999, '2026-01-03 06:07:08'),
                (42, 'refund', 8000, '餐饮', '现金', '', '2026-01-01', '关联退款', 7, '2026-01-03 06:07:09'),
            ]
            conn.executemany('INSERT INTO ledger_entry VALUES (?,?,?,?,?,?,?,?,?,?)', rows)
            conn.execute("UPDATE sqlite_sequence SET seq=900 WHERE name='ledger_entry'")
            if empty:
                conn.execute('DELETE FROM ledger_entry')
            conn.commit()

    def snapshot(self, conn):
        tables = ('clipboard', 'vault_bank', 'vault_deposit', 'ledger_entry', 'sqlite_sequence')
        return {table: [tuple(row) for row in conn.execute(f'SELECT * FROM {table} ORDER BY 1')]
                for table in tables}

    def test_old_rows_backup_indexes_sequence_and_idempotence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'old.db'
            self.seed_old(path)
            db = Database(path)
            try:
                # 保持连接打开，新增记录留在 WAL 中，验证 backup 不是主文件复制。
                with db.conn:
                    db.conn.execute("UPDATE ledger_entry SET note='WAL 中的转账' WHERE id=30")
                before = self.snapshot(db.conn)
                other_schema = [tuple(row) for row in db.conn.execute(
                    "SELECT type,name,sql FROM sqlite_master WHERE type='table' AND tbl_name IN "
                    "('clipboard','vault_bank','vault_deposit') ORDER BY name")]
                indexes = {row['name']: row['sql'] for row in db.conn.execute(
                    "SELECT name,sql FROM sqlite_master WHERE type='index' AND tbl_name='ledger_entry'")}
                start = datetime.now()
                db.init_schema()
                finish = datetime.now()
                backups = list(Path(directory).glob('old.db.ledger_schema_backup_*.sqlite3'))
                self.assertEqual(len(backups), 1)
                stamp = backups[0].name.split('ledger_schema_backup_')[1].removesuffix('.sqlite3')
                self.assertLessEqual(start, datetime.strptime(stamp, '%Y%m%d_%H%M%S_%f'))
                self.assertLessEqual(datetime.strptime(stamp, '%Y%m%d_%H%M%S_%f'), finish)
                with closing(sqlite3.connect(backups[0])) as backup:
                    self.assertEqual(backup.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                    self.assertEqual(self.snapshot(backup), before)
                after = self.snapshot(db.conn)
                self.assertEqual([tuple(row) for row in db.conn.execute(
                    "SELECT type,name,sql FROM sqlite_master WHERE type='table' AND tbl_name IN "
                    "('clipboard','vault_bank','vault_deposit') ORDER BY name")], other_schema)
                summary = summarize(db.list_ledger_entries('2026-01-01', '2026-12-31'),
                                    'year', 2026, 1, '2026-11-11')
                self.assertEqual((summary['income_cents'], summary['expense_cents'],
                                  summary['balance_cents']), (30000, -8000, 38000))
                self.assertEqual(summary['categories'][0]['category'], '餐饮')
                for table in ('clipboard', 'vault_bank', 'vault_deposit', 'sqlite_sequence'):
                    self.assertEqual(after[table], before[table])
                expected = [tuple(value for i, value in enumerate(row) if i not in (5, 8))
                            for row in before['ledger_entry']]
                self.assertEqual(after['ledger_entry'], expected)
                self.assertEqual({row['name'] for row in db.conn.execute('PRAGMA table_info(ledger_entry)')},
                                 {'id', 'created_at', *RECORD_FIELDS})
                new_indexes = {row['name']: row['sql'] for row in db.conn.execute(
                    "SELECT name,sql FROM sqlite_master WHERE type='index' AND tbl_name='ledger_entry'")}
                self.assertEqual(new_indexes, {key: value for key, value in indexes.items()
                                               if key not in ('idx_ledger_entry_refund', 'idx_ledger_destination')})
                self.assertNotIn('仅旧转入', db.list_ledger_accounts())
                self.assertEqual(db.list_ledger_entries('2024-01-01', '2026-12-31', query='仅旧转入'), [])
                self.assertEqual(db.get_ledger_entry(30)['category'], '历史调拨')
                self.assertEqual(db.get_ledger_entry(41)['amount_cents'], 22222)
                self.assertTrue(db.delete_ledger_entry(7))
                self.assertIsNotNone(db.get_ledger_entry(42))
                db.init_schema()
                db.close()
                db.init_schema()
                self.assertEqual(len(list(Path(directory).glob('*ledger_schema_backup*'))), 1)
                new_id = db.add_ledger_entry(**record(note='新流水'))
                self.assertEqual(new_id, 901)
                self.assertEqual(db.conn.execute(
                    "SELECT seq FROM sqlite_sequence WHERE name='vault_bank'").fetchone()[0], 1)
            finally:
                db.close()

    def test_empty_old_table_keeps_sequence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'empty.db'
            self.seed_old(path, empty=True)
            db = Database(path)
            try:
                db.init_schema()
                self.assertEqual(db.add_ledger_entry(**record()), 901)
            finally:
                db.close()

    def test_migration_failure_rolls_back_and_keeps_readable_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'failure.db'
            self.seed_old(path)
            db = Database(path)
            try:
                before = self.snapshot(db.conn)
                sql_before = [tuple(row) for row in db.conn.execute(
                    'SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY name')]
                denied = []

                def deny_rename(action, arg1, arg2, database, source):
                    if action == sqlite3.SQLITE_ALTER_TABLE:
                        denied.append(arg2)
                        return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK

                db.conn.set_authorizer(deny_rename)
                with self.assertRaises(sqlite3.DatabaseError):
                    db.init_schema()
                db.conn.set_authorizer(None)
                self.assertTrue(denied)
                self.assertFalse(db.conn.in_transaction)
                self.assertEqual(self.snapshot(db.conn), before)
                self.assertEqual([tuple(row) for row in db.conn.execute(
                    'SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY name')], sql_before)
                backups = list(Path(directory).glob('*ledger_schema_backup*'))
                self.assertEqual(len(backups), 1)
                with closing(sqlite3.connect(backups[0])) as backup:
                    self.assertEqual(self.snapshot(backup), before)
                    self.assertEqual(backup.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                db.init_schema()
                self.assertEqual(db.get_ledger_entry(30)['amount_cents'], 98765)
                self.assertEqual(len(list(Path(directory).glob('*ledger_schema_backup*'))), 2)
            finally:
                db.conn.set_authorizer(None)
                db.close()

    def test_backup_failure_does_not_start_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'backup-failure.db'
            self.seed_old(path)
            db = Database(path)
            try:
                before = self.snapshot(db.conn)
                with patch('lumina.database.open', side_effect=OSError('备份不可写')):
                    with self.assertRaisesRegex(OSError, '备份不可写'):
                        db._migrate_ledger_schema()
                self.assertEqual(self.snapshot(db.conn), before)
                self.assertFalse(db.conn.in_transaction)
            finally:
                db.close()

    def test_schema_versions_and_initialization_order(self):
        new_schema, old_schema, base = self.schemas()
        for existing in (None, 'old', 'new'):
            for schema in (old_schema, new_schema):
                with self.subTest(existing=existing, old_schema=schema == old_schema):
                    db = Database(':memory:')
                    try:
                        if existing == 'old':
                            db.conn.executescript(old_schema)
                        elif existing == 'new':
                            db.conn.executescript(base + LEDGER_TABLE_SQL)
                        _ = db.conn  # 补齐连接，后续禁止建立磁盘备份连接。
                        with patch.object(db, '_load_schema', return_value=schema), \
                             patch('lumina.database.sqlite3.connect', side_effect=AssertionError('内存库不能备份到磁盘')):
                            db.init_schema()
                            db.init_schema()
                        self.assertEqual({row['name'] for row in db.conn.execute('PRAGMA table_info(ledger_entry)')},
                                         {'id', 'created_at', *RECORD_FIELDS})
                        db.add_ledger_entry(**record(kind='transfer', note='转账'))
                        db.add_ledger_entry(**record(kind='refund', note='退款'))
                    finally:
                        db.close()

    def test_database_without_ledger_and_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'pre-ledger.db'
            _, _, base = self.schemas()
            with closing(sqlite3.connect(path)) as conn:
                conn.executescript(base)
                conn.execute("INSERT INTO vault_bank(name) VALUES ('旧银行')")
                conn.commit()
            db = Database(path)
            try:
                db.init_schema()
                first = db.add_ledger_entry(**record(day='2024-02-29'))
                refund = db.add_ledger_entry(**record(kind='refund', amount='10', day='2024-03-01'))
                db.close()
                db.init_schema()
                self.assertEqual(db.get_ledger_entry(first)['amount_cents'], 10000)
                self.assertEqual(db.get_ledger_entry(refund)['kind'], 'refund')
                self.assertEqual(db.list_vault_banks()[0]['name'], '旧银行')
                self.assertEqual(list(Path(directory).glob('*ledger_schema_backup*')), [])
            finally:
                db.close()

    def test_concurrent_independent_refunds_both_succeed(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / 'concurrent.db')
            try:
                db.init_schema()
                barrier = threading.Barrier(2)

                def refund():
                    try:
                        barrier.wait(timeout=10)
                        return db.add_ledger_entry(**record(kind='refund', amount='60'))
                    finally:
                        db.close()

                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(refund) for _ in range(2)]
                    results = [future.result(timeout=40) for future in futures]
                self.assertEqual(len(set(results)), 2)
                rows = db.list_ledger_entries('2026-11-01', '2026-11-01', kind='refund')
                self.assertEqual(len(rows), 2)
                self.assertEqual(sum(row['amount_cents'] for row in rows), 12000)
            finally:
                db.close()


if __name__ == '__main__':
    unittest.main()
