"""金额加密、口令封装与财务数据重置的回归测试。"""
import importlib.util
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from lumina.database import Database
from lumina.ledger_tools import build_entry

CRYPTO_AVAILABLE = (importlib.util.find_spec("argon2") is not None
                    and importlib.util.find_spec("cryptography") is not None)


class FinanceResetTests(unittest.TestCase):
    def test_reset_entry_removes_only_finance_envelope_and_preserves_clipboard(self):
        from lumina.vault_auth import reset_encrypted_financial_data

        db = Database(":memory:")
        try:
            db.init_schema()
            with db.conn:
                db.conn.execute(
                    "INSERT INTO clipboard(kind, content, hash) VALUES('text','保留剪贴板','hash')")
                bank_id = db.add_vault_bank("旧银行")
                db.add_vault_deposit(bank_id, "旧存单", 100.0, 1.0, 12, "2027-01-01")
                db.add_ledger_entry(**build_entry(
                    "expense", "1.23", "旧分类", "现金", "2026-01-01"))
            config = {
                "money_crypto": {"version": 1, "salt": "old"},
                "theme": "dark", "db_path": "finance.db",
            }
            panel = SimpleNamespace(
                ui=SimpleNamespace(
                    config=config, db=db,
                    actions=SimpleNamespace(config_path="config.json")),
                _vault_authenticated=True, _vault_write_generation=4,
                _vault_auth_pending=False,
            )
            revoked = Mock()
            with patch("lumina.settings.save_config") as save_config:
                self.assertTrue(reset_encrypted_financial_data(
                    panel, revoke_callback=revoked))

            save_config.assert_called_once_with(
                {"theme": "dark", "db_path": "finance.db"}, "config.json")
            revoked.assert_called_once_with()
            self.assertNotIn("money_crypto", config)
            self.assertEqual(config["theme"], "dark")
            self.assertFalse(panel._vault_authenticated)
            self.assertEqual(panel._vault_write_generation, 5)
            self.assertFalse(panel._vault_auth_pending)
            self.assertEqual(db.conn.execute(
                "SELECT content FROM clipboard").fetchone()[0], "保留剪贴板")
            self.assertEqual(db.conn.execute(
                "SELECT COUNT(*) FROM ledger_entry").fetchone()[0], 0)
            self.assertEqual(db.conn.execute(
                "SELECT COUNT(*) FROM vault_deposit").fetchone()[0], 0)
            self.assertEqual(db.conn.execute(
                "SELECT COUNT(*) FROM vault_bank").fetchone()[0], 0)
            self.assertTrue(db._financial_encryption_enabled)
            self.assertIsNone(db._financial_key)
        finally:
            db.close()

    def test_reset_entry_does_not_remove_envelope_if_config_save_fails(self):
        from lumina.vault_auth import reset_encrypted_financial_data

        db = Database(":memory:")
        try:
            db.init_schema()
            with db.conn:
                db.conn.execute(
                    "INSERT INTO clipboard(kind, content, hash) VALUES('text','保留剪贴板','hash')")
                bank_id = db.add_vault_bank("旧银行")
                db.add_vault_deposit(bank_id, "旧存单", 100.0, 1.0, 12, "2027-01-01")
            config = {"money_crypto": {"version": 1}, "theme": "dark"}
            panel = SimpleNamespace(
                ui=SimpleNamespace(
                    config=config, db=db,
                    actions=SimpleNamespace(config_path="config.json")),
                _vault_authenticated=True, _vault_write_generation=0,
                _vault_auth_pending=False,
            )
            with patch("lumina.settings.save_config", side_effect=OSError("read-only")):
                with self.assertRaises(OSError):
                    reset_encrypted_financial_data(panel)
            self.assertIn("money_crypto", config)
            self.assertEqual(config["theme"], "dark")
            self.assertEqual(db.conn.execute(
                "SELECT content FROM clipboard").fetchone()[0], "保留剪贴板")
            self.assertEqual(db.conn.execute(
                "SELECT COUNT(*) FROM vault_deposit").fetchone()[0], 0)
            self.assertFalse(panel._vault_auth_pending)
        finally:
            db.close()

    def test_reset_drops_only_finance_data(self):
        db = Database(":memory:")
        try:
            db.init_schema()
            with db.conn:
                db.conn.execute(
                    "INSERT INTO clipboard(kind, content, hash) VALUES('text','保留剪贴板','hash')")
                bank_id = db.add_vault_bank("旧银行")
                db.add_vault_deposit(bank_id, "旧存单", 100.0, 1.0, 12, "2027-01-01")
                db.add_ledger_entry(**build_entry(
                    "expense", "1.23", "旧分类", "现金", "2026-01-01"))
            db.init_schema(encrypted_finances=True, reset_financial_data=True)
            self.assertEqual(db.conn.execute("SELECT content FROM clipboard").fetchone()[0],
                             "保留剪贴板")
            self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM ledger_entry").fetchone()[0], 0)
            self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM vault_deposit").fetchone()[0], 0)
            self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM vault_bank").fetchone()[0], 0)
            with self.assertRaises(PermissionError):
                db.list_vault_deposits()
            with self.assertRaises(PermissionError):
                db.list_ledger_entries("2026-01-01", "2026-12-31")
        finally:
            db.close()


class EncryptedDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.payloads = {}
        self.counter = 0
        crypto = ModuleType("lumina.money_crypto")

        def encrypt_amount(key, table, row_id, field, cents):
            self.counter += 1
            nonce = self.counter.to_bytes(12, "big")
            payload = nonce + b"\0" * 17
            self.payloads[payload] = (key, table, row_id, field, cents)
            return payload

        def decrypt_amount(key, table, row_id, field, payload):
            stored = self.payloads[bytes(payload)]
            if stored[:4] != (key, table, row_id, field):
                raise ValueError("invalid authentication data")
            return stored[4]

        crypto.encrypt_amount = encrypt_amount
        crypto.decrypt_amount = decrypt_amount
        self.crypto_patch = patch.dict("sys.modules", {"lumina.money_crypto": crypto})
        self.crypto_patch.start()
        self.db = Database(":memory:")
        self.db.init_schema(encrypted_finances=True, reset_financial_data=True)
        self.key = b"k" * 32
        self.db.unlock_financial_data(self.key)

    def tearDown(self):
        self.db.close()
        self.crypto_patch.stop()

    def test_ciphertext_storage_ledger_and_vault_crud(self):
        entry_id = self.db.add_ledger_entry(**build_entry(
            "expense", "123.45", "餐饮", "现金", "2026-11-12"))
        raw = self.db.conn.execute(
            "SELECT amount_cents FROM ledger_entry WHERE id=?", (entry_id,)).fetchone()
        self.assertIsInstance(raw["amount_cents"], bytes)
        self.assertEqual(len(raw["amount_cents"]), 29)
        self.assertEqual(self.db.get_ledger_entry(entry_id)["amount_cents"], 12345)
        self.db.update_ledger_entry(entry_id, amount_cents=23456)
        self.assertEqual(self.db.get_ledger_entry(entry_id)["amount_cents"], 23456)

        bank_id = self.db.add_vault_bank("示例银行")
        deposit_id = self.db.add_vault_deposit(
            bank_id, "定期存单", 9876.54, 2.1, 12, "2027-11-12")
        raw_principal = self.db.conn.execute(
            "SELECT principal FROM vault_deposit WHERE id=?", (deposit_id,)).fetchone()
        self.assertIsInstance(raw_principal["principal"], bytes)
        self.assertEqual(len(raw_principal["principal"]), 29)
        self.assertEqual(self.db.list_vault_deposits()[0]["principal"], 9876.54)
        self.db.update_vault_deposit(deposit_id, principal=111.11)
        self.assertEqual(self.db.list_vault_deposits()[0]["principal"], 111.11)

        self.db.lock_financial_data()
        with self.assertRaises(PermissionError):
            self.db.list_ledger_entries("2026-11-01", "2026-11-30")
        with self.assertRaises(PermissionError):
            self.db.list_vault_deposits()


@unittest.skipUnless(CRYPTO_AVAILABLE, "请安装 requirements.txt 中的金额加密依赖")
class MoneyCryptoTests(unittest.TestCase):
    def test_passphrase_wraps_random_key_and_rejects_wrong_password(self):
        from lumina.money_crypto import InvalidPassphrase, create_key_envelope, unlock_key

        metadata, key = create_key_envelope("correct horse battery")
        self.assertEqual(len(key), 32)
        self.assertNotIn(key.hex(), str(metadata))
        self.assertEqual(unlock_key(metadata, "correct horse battery"), key)
        with self.assertRaises(InvalidPassphrase):
            unlock_key(metadata, "incorrect horse battery")

    def test_change_passphrase_rewraps_same_key_without_touching_amounts(self):
        from lumina.money_crypto import (
            InvalidPassphrase, create_key_envelope, decrypt_amount,
            encrypt_amount, rewrap_key_envelope, unlock_key,
        )

        old_metadata, data_key = create_key_envelope("correct horse battery")
        payload = encrypt_amount(data_key, "ledger_entry", 7, "amount_cents", 12345)
        new_metadata = rewrap_key_envelope(
            old_metadata, "correct horse battery", "new secure passphrase")

        self.assertNotEqual(new_metadata["salt"], old_metadata["salt"])
        self.assertNotEqual(new_metadata["wrap_nonce"], old_metadata["wrap_nonce"])
        self.assertEqual(unlock_key(new_metadata, "new secure passphrase"), data_key)
        self.assertEqual(
            decrypt_amount(data_key, "ledger_entry", 7, "amount_cents", payload), 12345)
        with self.assertRaises(InvalidPassphrase):
            unlock_key(new_metadata, "correct horse battery")
        with self.assertRaises(InvalidPassphrase):
            rewrap_key_envelope(old_metadata, "wrong old passphrase", "new secure passphrase")
        self.assertEqual(unlock_key(old_metadata, "correct horse battery"), data_key)

    def test_money_fields_are_ciphertext_and_api_returns_cents(self):
        from lumina.money_crypto import create_key_envelope

        db = Database(":memory:")
        try:
            db.init_schema(encrypted_finances=True, reset_financial_data=True)
            _, key = create_key_envelope("correct horse battery")
            db.unlock_financial_data(key)
            entry_id = db.add_ledger_entry(**build_entry(
                "expense", "123.45", "餐饮", "现金", "2026-11-12"))
            raw = db.conn.execute(
                "SELECT amount_cents FROM ledger_entry WHERE id=?", (entry_id,)).fetchone()
            self.assertIsInstance(raw["amount_cents"], bytes)
            self.assertNotIn(b"12345", raw["amount_cents"])
            row = db.get_ledger_entry(entry_id)
            self.assertEqual(row["amount_cents"], 12345)

            bank_id = db.add_vault_bank("示例银行")
            deposit_id = db.add_vault_deposit(
                bank_id, "定期存单", 9876.54, 2.1, 12, "2027-11-12")
            raw_principal = db.conn.execute(
                "SELECT principal FROM vault_deposit WHERE id=?", (deposit_id,)).fetchone()
            self.assertIsInstance(raw_principal["principal"], bytes)
            self.assertNotIn(b"987654", raw_principal["principal"])
            self.assertEqual(db.list_vault_deposits()[0]["principal"], 9876.54)

            db.lock_financial_data()
            with self.assertRaises(PermissionError):
                db.list_ledger_entries("2026-11-01", "2026-11-30")
        finally:
            db.close()
