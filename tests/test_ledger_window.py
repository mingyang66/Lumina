"""记账窗口的真实 Tk、键鼠事件与内存数据库回归测试。"""
import datetime as dt
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from lumina.database import Database
from lumina.history_panel import HistoryPanel
from lumina.ledger_tools import build_entry
from lumina.ledger_window import LedgerWindow, _LedgerDialog
from lumina.vault_window import VaultWindow

try:
    probe = tk.Tk()
    probe.withdraw()
    probe.destroy()
    TK_OK = True
except tk.TclError:
    TK_OK = False

TODAY = dt.date(2026, 11, 11)


class Panel:
    FONT_FAMILY = "Microsoft YaHei UI"
    _watch_private_window = HistoryPanel._watch_private_window
    _open_ledger = HistoryPanel._open_ledger

    def __init__(self, root, db):
        self.tk = tk
        self.win = tk.Toplevel(root)
        self.win.withdraw()
        self.ui = type("UI", (), {"db": db, "config": {}})()
        self._vault_authenticated = True
        self._tool_windows = {}
        self.status_var = tk.StringVar()

    def _theme(self):
        return {key: value[1] for key, value in HistoryPanel.THEME.items()}, True

    def _sys(self, name):
        return HistoryPanel.SYS_COLORS.get(name, HistoryPanel.SYS_COLORS["gray"])[0]

    def _authenticate_vault(self, parent=None):
        self._vault_authenticated = True
        return True


@unittest.skipUnless(TK_OK, "本机 Tk 不可用")
class LedgerWindowTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.db = Database(":memory:")
        self.db.init_schema()
        self.panel = Panel(self.root, self.db)
        self.window = LedgerWindow(self.panel)
        self.window._today = TODAY
        self.window.year, self.window.month = TODAY.year, TODAY.month
        self.panel._tool_windows["ledger"] = self.window
        self.windows = [self.window]
        self.errors = []
        def report(*args):
            self.errors.append(args)
            dialog = self.window._deposit_dialog
            if dialog is not None and dialog.win.winfo_exists():
                dialog._cancel()
        self.root.report_callback_exception = report
        self.root.update()

    def tearDown(self):
        for window in self.windows:
            window.close()
        self.root.update()
        self.panel.win.destroy()
        self.root.destroy()
        self.db.close()
        self.assertEqual(self.errors, [])

    def add(self, kind="expense", amount="12.34", category="餐饮", day="2026-11-10", **kwargs):
        return self.db.add_ledger_entry(**build_entry(kind, amount, category, "现金", day, **kwargs))

    def reveal(self):
        self.window._toggle_privacy()
        self.root.update()

    def click(self, widget):
        widget.event_generate("<Enter>", x=5, y=5)
        widget.event_generate("<ButtonPress-1>", x=5, y=5)
        if widget.winfo_exists():
            widget.event_generate("<ButtonRelease-1>", x=5, y=5)
        self.root.update()

    def pump(self, milliseconds=80):
        deadline = time.monotonic() + milliseconds / 1000
        while time.monotonic() < deadline:
            self.root.update()

    def dialog(self, item=None):
        return _LedgerDialog(self.window, self.db.list_ledger_accounts(),
                             {k: self.db.list_ledger_categories(k) for k in (
                                 "expense", "income", "transfer", "refund")}, item)

    def fill(self, dialog, **values):
        for key, value in values.items():
            dialog.variables[key].set(value)

    def test_default_privacy_and_no_reads(self):
        with patch.object(self.db, "list_ledger_entries", wraps=self.db.list_ledger_entries) as read:
            self.window._reload()
            self.window._toggle_theme()
            self.window._move_period(-1)
            self.window._set_mode("year")
            self.assertFalse(self.window._privacy_visible)
            self.assertEqual(self.window._rows, [])
            read.assert_not_called()
        self.window.close()
        self.panel._vault_authenticated = True
        new = self.panel._open_ledger()
        self.windows.append(new)
        self.assertFalse(new.dark)
        self.assertEqual(new.win.cget("bg"), VaultWindow.LIGHT_PALETTE["surface"])
        self.assertFalse(new._privacy_visible)
        self.assertIs(self.panel._open_ledger(), new)

    def test_eye_mouse_filters_do_not_change_summary(self):
        self.add(amount="100")
        self.add(kind="income", amount="200", category="工资", note="秘密收入")
        self.click(self.window._privacy_button)
        self.assertTrue(self.window._privacy_visible)
        self.assertEqual(self.window._data["balance_cents"], 10000)
        self.click(self.window.filter_button)
        self.window.query_var.set("秘密")
        self.window.search_entry.focus_force()
        self.root.update()
        self.window.search_entry.event_generate("<Return>")
        self.root.update()
        self.assertEqual(len(self.window._filtered_rows()), 1)
        self.assertEqual(self.window._data["expense_cents"], 10000)
        self.window.category_var.set("餐饮")
        self.window._apply_filters()
        self.assertEqual(self.window._filtered_rows(), [])
        self.click(self.window._privacy_button)
        self.assertEqual(self.window.query_var.get(), "")
        self.assertEqual(self.window.category_option["menu"].index("end"), 0)
        self.assertEqual(self.window._data, {})
        self.assertEqual(self.window._annual, {})

    def test_modal_wait_unique_save_and_edit_delete(self):
        def submit():
            dialog = self.window._deposit_dialog
            self.assertIsNotNone(dialog)
            self.assertEqual(self.db.list_ledger_entries("0001-01-01", "9999-12-31"), [])
            self.fill(dialog, amount="123.45", category="自定义类别", account="自定义账户", note="键盘提交")
            dialog.entries["amount"].focus_force()
            dialog.win.update()
            dialog.entries["amount"].event_generate("<Return>")
        self.window.win.after(40, submit)
        with patch.object(self.db, "add_ledger_entry", wraps=self.db.add_ledger_entry) as write:
            self.window._add_entry()
            self.assertEqual(write.call_count, 1)
        row = self.db.list_ledger_entries("0001-01-01", "9999-12-31")[0]
        self.assertEqual(row["entry_date"], TODAY.isoformat())
        self.assertEqual(row["amount_cents"], 12345)
        self.assertFalse(self.window._privacy_visible)
        self.assertEqual(self.window._rows, [])
        self.reveal()

        def edit():
            dialog = self.window._deposit_dialog
            self.fill(dialog, amount="9.87", note="已编辑")
            dialog._submit()
        self.window.win.after(40, edit)
        self.window._edit_entry(row["id"])
        self.assertEqual(self.db.get_ledger_entry(row["id"])["amount_cents"], 987)
        with patch("lumina.ledger_window.messagebox.askyesno", return_value=True):
            self.window._delete_entry(row["id"])
        self.assertIsNone(self.db.get_ledger_entry(row["id"]))

    def test_save_again_cancellation_and_first_focus(self):
        calls = []

        def first():
            dialog = self.window._deposit_dialog
            self.assertEqual(dialog.win.focus_get(), dialog.entries["amount"])
            self.fill(dialog, amount="5")
            calls.append(1)
            dialog._submit(True)
            self.window.win.after(40, second)

        def second():
            dialog = self.window._deposit_dialog
            self.assertEqual(dialog.variables["amount"].get(), "")
            calls.append(2)
            dialog.entries["amount"].focus_force()
            dialog.win.update()
            dialog.entries["amount"].event_generate("<Escape>")
            self.assertFalse(dialog.win.winfo_exists())

        self.window.win.after(40, first)
        self.window._add_entry()
        self.assertEqual(calls, [1, 2])
        self.assertEqual(len(self.db.list_ledger_entries("0001-01-01", "9999-12-31")), 1)

    def test_lock_destroys_dialog_and_post_wait_permission(self):
        dialog_holder = []

        def lock():
            dialog = self.window._deposit_dialog
            dialog_holder.append(dialog)
            self.fill(dialog, amount="10")
            self.window.lock_privacy()
        self.window.win.after(40, lock)
        self.window._add_entry()
        self.assertIsNone(dialog_holder[0].result)
        self.assertFalse(dialog_holder[0].win.winfo_exists())
        self.assertFalse(self.panel._vault_authenticated)
        self.assertEqual(self.db.list_ledger_entries("0001-01-01", "9999-12-31"), [])
        self.panel._vault_authenticated = True

        def revoke_before_write(dialog, item=None):
            self.panel._vault_authenticated = False
            LedgerWindow._save_dialog(self.window, dialog, item)

        def submit_then_revoke():
            dialog = self.window._deposit_dialog
            self.fill(dialog, amount="10")
            dialog._submit()
        self.window.win.after(40, submit_then_revoke)
        with patch.object(self.window, "_save_dialog", side_effect=revoke_before_write):
            self.window._add_entry()
        self.assertEqual(self.db.list_ledger_entries("0001-01-01", "9999-12-31"), [])

    def test_shared_vault_close_and_ledger_close_hide_other(self):
        vault = VaultWindow(self.panel)
        self.windows.append(vault)
        self.panel._tool_windows["vault"] = vault
        self.panel._watch_private_window(vault)
        self.reveal()
        vault._toggle_privacy()
        vault.close()
        self.assertFalse(self.window._privacy_visible)
        self.pump()
        self.assertFalse(self.window._privacy_visible)
        self.panel._vault_authenticated = True
        vault = VaultWindow(self.panel)
        self.windows.append(vault)
        self.panel._tool_windows["vault"] = vault
        self.panel._watch_private_window(vault)
        vault._toggle_privacy()
        self.window.close()
        self.assertFalse(vault._privacy_visible)
        self.assertEqual(vault._data, {})

    def test_minimize_and_poll_revoke(self):
        self.add()
        self.reveal()
        # 真实 Win32 最小化导致 Python 3.13 GIL 崩溃，单列集成阻塞。
        with patch("lumina.tool_window.taskbar.minimize_window", return_value=True):
            self.window._minimize()
        self.assertFalse(self.panel._vault_authenticated)
        self.assertEqual(self.window._rows, [])
        self.panel._vault_authenticated = True
        self.window.summon()
        self.root.update()
        self.reveal()
        self.panel._vault_authenticated = False
        self.pump()
        self.assertFalse(self.window._privacy_visible)

    def test_month_year_cutoff_limits_and_drill(self):
        self.add(amount="10", day="2026-01-01")
        self.add(amount="20", day="2026-11-10")
        self.add(amount="30", day="2026-11-12")
        self.reveal()
        self.assertEqual(self.window._data["expense_cents"], 2000)
        self.window._set_mode("year")
        self.assertEqual(self.window._data["expense_cents"], 3000)
        self.assertEqual(self.window._data["cutoff"], TODAY.isoformat())
        self.assertEqual(self.window._data["months_elapsed"], 11)
        self.window._move_period(1)
        self.assertEqual(self.window.year, 2026)
        self.window._drill_month(1)
        self.assertEqual(self.window.mode, "month")
        self.assertEqual(self.window._data["expense_cents"], 1000)
        self.window.year, self.window.month = 1, 1
        self.window._move_period(-1)
        self.assertEqual((self.window.year, self.window.month), (1, 1))
        self.assertFalse(self.window._valid_period("year", 10000, 1))

    def test_four_kinds_add_without_login_or_sensitive_reads(self):
        self.panel._vault_authenticated = False
        self.window._auth_seen = False
        for kind, caption in (("expense", "支出"), ("income", "收入"),
                              ("transfer", "转账"), ("refund", "退款")):
            def submit():
                dialog = self.window._deposit_dialog
                self.pump()
                self.assertTrue(dialog.win.winfo_exists())
                self.assertEqual(set(dialog.variables), {"amount", "kind", "category", "account", "entry_date", "note"})
                self.assertEqual(dialog.win.winfo_width(), 500)
                self.fill(dialog, kind=caption, amount="12.34", category="自定义", account="现金")
                dialog._kind_changed()
                dialog._submit()
            self.window.win.after(40, submit)
            with self.subTest(kind=kind), patch.object(self.db, "list_ledger_entries") as read, \
                    patch.object(self.panel, "_authenticate_vault") as auth:
                self.window._add_entry()
                read.assert_not_called()
                auth.assert_not_called()
            rows = self.db.list_ledger_entries("0001-01-01", "9999-12-31")
            self.assertEqual(rows[0]["kind"], kind)
            self.assertFalse(self.window._privacy_visible)
            self.assertEqual(self.window._rows, [])

    def test_stats_negative_axis_no_invalid_share_and_themes_geometry(self):
        self.add(amount="100", day="2026-10-01")
        self.add(kind="refund", amount="90", day="2026-11-01")
        self.add(amount="10", category="交通")
        for _ in range(2):
            if not self.window._privacy_visible:
                self.reveal()
            self.window._select_tab("stats")
            self.window.win.geometry(f"{self.window.min_width}x{self.window.min_height}")
            self.root.update()
            canvas = self.window.category_canvas
            self.assertTrue(canvas.find_withtag("zero"))
            texts = [canvas.itemcget(i, "text") for i in canvas.find_all() if canvas.type(i) == "text"]
            self.assertFalse(any("%" in text for text in texts))
            bars = [canvas.coords(i) for i in canvas.find_withtag("category_bar")]
            zero = canvas.coords(canvas.find_withtag("zero")[0])[0]
            self.assertTrue(any(bar[0] < zero for bar in bars))
            self.assertEqual(canvas.cget("bg"), self.window.P["card"])
            self.assertGreater(self.window._canvas.winfo_height(), 100)
            self.assertLessEqual(self.window._add_entry_button.winfo_rootx() + self.window._add_entry_button.winfo_width(),
                                 self.window.win.winfo_rootx() + self.window.win.winfo_width())
            canvas.event_generate("<MouseWheel>", delta=-120)
            self.root.update()
            self.assertGreater(self.window._canvas.yview()[0], 0)
            self.window.chart_var.set("结余")
            self.window._change_chart()
            self.assertTrue(self.window.trend_canvas.find_withtag("zero"))
            self.window._toggle_theme()

    def test_write_failure_keeps_input_and_save_again_multiple(self):
        real_add = self.db.add_ledger_entry
        attempts = []

        def write(**record):
            attempts.append(record)
            if len(attempts) == 1:
                raise RuntimeError("数据库暂不可写")
            return real_add(**record)

        def exercise():
            dialog = self.window._deposit_dialog
            self.fill(dialog, amount="7.25", category="自定义", account="钱包", note="失败不丢")
            before = {k: v.get() for k, v in dialog.variables.items()}
            dialog._submit(True)
            self.assertTrue(dialog.win.winfo_exists())
            self.assertIn("输入已保留", dialog.error.cget("text"))
            self.assertEqual(before, {k: v.get() for k, v in dialog.variables.items()})
            dialog._submit(True)
            self.assertIs(dialog, self.window._deposit_dialog)
            self.assertTrue(dialog.win.winfo_exists())
            self.assertEqual(dialog.variables["amount"].get(), "")
            self.assertEqual(dialog.variables["note"].get(), "")
            self.assertEqual(dialog.variables["category"].get(), "自定义")
            self.assertEqual(dialog.error.cget("text"), "")
            self.fill(dialog, amount="8.50", note="第二笔")
            dialog._submit(True)
            self.assertEqual(dialog.variables["amount"].get(), "")
            self.fill(dialog, amount="9", note="第三笔")
            dialog._submit()

        self.window.win.after(40, exercise)
        with patch.object(self.db, "add_ledger_entry", side_effect=write):
            self.window._add_entry()
        self.assertEqual(len(attempts), 4)
        rows = self.db.list_ledger_entries("0001-01-01", "9999-12-31")
        self.assertEqual(sorted(r["amount_cents"] for r in rows), [725, 850, 900])
        self.assertEqual(self.window._rows, [])

    def test_edit_write_failure_preserves_fields_and_lock_cancels_retry(self):
        entry_id = self.add(amount="10")
        self.reveal()
        real_update = self.db.update_ledger_entry
        attempts = []

        def update(item_id, **record):
            attempts.append(record)
            if len(attempts) == 1:
                raise ValueError("分类暂不可保存")
            return real_update(item_id, **record)

        def exercise():
            dialog = self.window._deposit_dialog
            self.fill(dialog, amount="12", category="购物", note="编辑保留")
            before = {k: v.get() for k, v in dialog.variables.items()}
            dialog._submit()
            self.assertTrue(dialog.win.winfo_exists())
            self.assertEqual(before, {k: v.get() for k, v in dialog.variables.items()})
            self.assertIn("分类暂不可保存", dialog.error.cget("text"))
            self.fill(dialog, category="餐饮")
            dialog._submit()

        self.window.win.after(40, exercise)
        with patch.object(self.db, "update_ledger_entry", side_effect=update):
            self.window._edit_entry(entry_id)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(self.db.get_ledger_entry(entry_id)["note"], "编辑保留")

        def fail_and_lock():
            dialog = self.window._deposit_dialog
            self.fill(dialog, amount="8", note="锁屏前失败")
            dialog._submit(True)
            self.assertTrue(dialog.win.winfo_exists())
            self.window.lock_privacy()
            self.assertFalse(dialog.win.winfo_exists())
            self.assertIsNone(dialog.result)
            self.assertTrue(all(v.get() == "" for v in dialog.variables.values()))

        self.window.win.after(40, fail_and_lock)
        with patch.object(self.db, "add_ledger_entry", side_effect=RuntimeError("写入失败")) as write:
            self.window._add_entry()
        self.assertEqual(write.call_count, 1)
        self.assertEqual(len(self.db.list_ledger_entries("0001-01-01", "9999-12-31")), 1)

    def test_permission_revoked_during_validation_never_writes(self):
        from lumina.ledger_tools import build_entry as real_build

        def validate(*args, **kwargs):
            record = real_build(*args, **kwargs)
            self.panel._vault_authenticated = False
            return record

        def submit():
            dialog = self.window._deposit_dialog
            self.fill(dialog, amount="10")
            dialog._submit()
            self.assertFalse(dialog.win.winfo_exists())

        self.window.win.after(40, submit)
        with patch("lumina.ledger_window.tools.build_entry", side_effect=validate), \
                patch.object(self.db, "add_ledger_entry", wraps=self.db.add_ledger_entry) as write:
            self.window._add_entry()
            write.assert_not_called()

    def test_real_trend_and_month_table_mouse_drill(self):
        self.add(amount="30", day="2026-01-01")
        self.add(amount="10", day="2026-02-01")
        self.reveal()
        self.window._set_mode("year")
        self.window._select_tab("stats")
        self.root.update()
        canvas = self.window.trend_canvas
        bar = next(i for i in canvas.find_withtag("month1") if canvas.type(i) == "rectangle"
                   and canvas.coords(i)[3] > canvas.coords(i)[1])
        x1, y1, x2, y2 = canvas.coords(bar)
        x, y = int((x1 + x2) / 2), int((y1 + y2) / 2)
        canvas.event_generate("<Motion>", x=x, y=y)
        canvas.event_generate("<ButtonPress-1>", x=x, y=y)
        self.root.update()
        self.assertEqual((self.window.mode, self.window.month, self.window.tab), ("month", 1, "entries"))
        self.assertEqual(self.window._data["expense_cents"], 3000)
        self.window._set_mode("year")
        self.window._select_tab("stats")
        self.root.update()
        self.assertEqual(self.window.month_rows, [])
        self.click(self.window.months_button)
        self.assertEqual(len(self.window.month_rows), 12)
        label = next(child for row in self.window._table.winfo_children()
                     for child in row.winfo_children()
                     if isinstance(child, tk.Label) and child.cget("text") == "02月")
        self.window._canvas.yview_moveto(.7)
        self.root.update()
        self.click(label)
        self.assertEqual((self.window.mode, self.window.month, self.window.tab), ("month", 2, "entries"))
        self.assertEqual(self.window._data["expense_cents"], 1000)

    def test_660px_large_amount_geometry_both_themes(self):
        self.assertEqual((LedgerWindow.width, LedgerWindow.min_width),
                         (VaultWindow.width, VaultWindow.min_width))
        for month in range(1, 12):
            self.add(kind="income", amount="999999999.99", category="长分类名称", day=f"2026-{month:02d}-01")
            self.add(amount="999999999.99", day=f"2026-{month:02d}-01")
        self.reveal()
        self.window._set_mode("year")
        self.window._select_tab("stats")
        for _ in range(2):
            self.window.win.geometry("660x900")
            self.root.update()
            self.assertEqual(self.window.win.winfo_width(), 660)
            right = self.window.win.winfo_rootx() + 660
            for widget in (self.window.period_label, self.window._add_entry_button,
                           *self.window._metric_labels.values(), self.window.trend_canvas,
                           self.window.category_canvas):
                self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(), right)
            canvas = self.window.trend_canvas
            previous_right = 0
            for month in range(1, 13):
                label = next(i for i in canvas.find_withtag(f"month{month}") if canvas.type(i) == "text")
                x1, _, x2, _ = canvas.bbox(label)
                self.assertGreaterEqual(x1, previous_right)
                self.assertLessEqual(x2, canvas.winfo_width())
                previous_right = x2
            for item in canvas.find_withtag("axis"):
                self.assertGreaterEqual(canvas.bbox(item)[0], 0)
            for row in self.window._table.winfo_children():
                for widget in row.winfo_children():
                    self.assertLessEqual(widget.winfo_x() + widget.winfo_width(), row.winfo_width())
            self.window._toggle_theme()

    def test_initial_unverified_session_lock_during_validation_and_retry(self):
        self.panel._vault_authenticated = False
        self.window._auth_seen = False
        real_build = build_entry
        dialog = self.dialog()
        self.fill(dialog, amount="10")
        token = dialog._write_session
        def lock(*args, **kwargs):
            record = real_build(*args, **kwargs)
            self.window.lock_privacy()
            return record
        with patch("lumina.ledger_window.tools.build_entry", side_effect=lock):
            dialog._submit()
        self.assertIsNone(dialog.result)
        self.assertFalse(dialog.win.winfo_exists())
        self.assertFalse(self.window._can_write(token))
        new = self.dialog()
        self.fill(new, amount="5")
        new._submit()
        self.assertIsNotNone(new.result)
        self.assertFalse(self.panel._vault_authenticated)

    def test_two_eyes_reuse_login_but_each_window_starts_hidden(self):
        self.panel._vault_authenticated = False
        self.window._auth_seen = False
        vault = VaultWindow(self.panel)
        self.windows.append(vault)
        self.panel._tool_windows["vault"] = vault
        with patch.object(self.panel, "_authenticate_vault", wraps=self.panel._authenticate_vault) as login:
            self.assertFalse(vault._privacy_visible)
            self.reveal()
            login.assert_called_once_with(parent=self.window.win)
            self.assertFalse(vault._privacy_visible)
            vault._toggle_privacy()
            self.assertTrue(vault._privacy_visible)
            self.assertEqual(login.call_count, 1)
            vault.lock_privacy()
            self.assertFalse(self.window._privacy_visible)
            self.reveal()
            self.assertEqual(login.call_count, 2)

    def test_shared_lock_cancels_initial_unverified_form_immediately(self):
        self.panel._vault_authenticated = False
        self.window._auth_seen = False
        vault = VaultWindow(self.panel)
        self.windows.append(vault)
        self.panel._tool_windows["vault"] = vault
        def lock():
            dialog = self.window._deposit_dialog
            self.fill(dialog, amount="10")
            vault.lock_privacy()
            self.assertFalse(dialog.win.winfo_exists())
            self.assertIsNone(dialog.result)
        self.window.win.after(40, lock)
        with patch.object(self.db, "add_ledger_entry") as write:
            self.window._add_entry()
            write.assert_not_called()

    def test_basic_wcag_text_colors_both_themes(self):
        def luminance(color):
            channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            linear = [c / 12.92 if c <= .04045 else ((c + .055) / 1.055) ** 2.4 for c in channels]
            return sum(c * weight for c, weight in zip(linear, (.2126, .7152, .0722)))

        for _ in range(2):
            p = self.window.P
            for fg, bg in (("title", "card"), ("muted", "card"), ("muted", "surface"),
                           ("muted", "head_bg"), ("link", "head_bg"),
                           ("matured_fg", "surface"), ("button_fg", "button")):
                one, two = sorted((luminance(p[fg]), luminance(p[bg])))
                self.assertGreaterEqual((two + .05) / (one + .05), 4.5, (self.window.dark, fg, bg))
            self.window._toggle_theme()

    def test_initial_height_fits_low_resolution_screen(self):
        for screen_height in (600, 768, 1080):
            with patch.object(tk.Toplevel, "winfo_screenheight", return_value=screen_height):
                window = LedgerWindow(self.panel)
                self.windows.append(window)
                self.root.update()
                self.assertLessEqual(window.win.winfo_height(), screen_height - 120)
                self.assertGreaterEqual(window.win.winfo_rooty(), 0)
                self.assertLessEqual(window.win.winfo_rooty() + window.win.winfo_height(), screen_height - 40)
                window.close()
                self.panel._vault_authenticated = True

    def test_two_kind_buttons_payment_accounts_and_alignment(self):
        dialog = self.dialog()
        self.root.update()
        self.assertEqual(set(dialog.kind_buttons), {"expense", "income"})
        self.assertLess(dialog.entries["kind"].winfo_rooty(), dialog.entries["amount"].winfo_rooty())
        self.assertEqual(dialog.kind_buttons["expense"].cget("relief"), "sunken")
        self.click(dialog.kind_buttons["income"])
        self.assertEqual(dialog.variables["kind"].get(), "收入")
        self.assertEqual(dialog.variables["category"].get(), "工资")
        self.assertEqual(dialog.kind_buttons["income"].cget("relief"), "sunken")
        menu = dialog.account_menu["menu"]
        labels = [menu.entrycget(i, "label") for i in range(menu.index("end") + 1)]
        for account in ("支付宝", "微信", "京东支付", "银联云闪付"):
            self.assertIn(account, labels)
            menu.invoke(labels.index(account))
            self.assertEqual(dialog.variables["account"].get(), account)
        self.assertEqual(dialog.category_menu.winfo_rootx(),
                         dialog.entries["category"].winfo_rootx())
        self.assertLessEqual(dialog.account_menu.winfo_rootx() + dialog.account_menu.winfo_width(),
                             dialog.entries["account"].winfo_rootx() + dialog.entries["account"].winfo_width())
        dialog._cancel()

    def test_note_tooltip_complete_text_and_privacy_cleanup(self):
        note = "多行备注\n" + "超长备注内容" * 100
        item = self.add(note=note)
        self.reveal()
        info = self.window._entry_widgets[item][3]
        self.assertNotIn("\n", info.cget("text"))
        self.assertTrue(info.cget("text").endswith("..."))
        info.event_generate("<Enter>")
        self.pump(450)
        tip = self.window._note_tip
        self.assertIsNotNone(tip)
        text = next(c for c in tip.winfo_children() if isinstance(c, tk.Text))
        self.assertEqual(text.get("1.0", "end-1c"), note)
        self.window._hide_privacy()
        self.root.update()
        self.assertIsNone(self.window._note_tip)
        self.assertIsNone(self.window._note_tip_job)
        self.assertFalse(tip.winfo_exists())

    def test_single_line_date_subtotals_and_empty_states(self):
        self.reveal()
        children = self.window._table.winfo_children()
        self.assertTrue(any(c.winfo_class() == "Label" and "本期还没有流水" in c.cget("text")
                            for c in children))
        self.assertTrue(any(c.winfo_class() == "Button" and c.cget("text") == "记一笔"
                            for c in children))
        self.assertEqual(self.window._entry_widgets, {})
        self.add(amount="20", day="2026-11-10", note="午饭")
        self.add(amount="30", day="2026-11-10", note="地铁")
        self.add(kind="income", amount="50", category="工资", day="2026-11-09")
        self.window._reload()
        self.root.update()
        heads = [c for c in self.window._table.winfo_children()
                 if c.winfo_class() == "Frame" and c.cget("bg") == self.window.P["head_bg"]]
        self.assertEqual(len(heads), 2)
        first = tuple(x.cget("text") for x in heads[0].winfo_children())
        self.assertIn("11月10日", first[0])
        self.assertIn("2 笔", first[0])
        self.assertEqual(first[1], "本日 · 收入 ¥0.00 · 净支出 ¥50.00")
        self.assertEqual(heads[1].winfo_children()[1].cget("text"),
                         "本日 · 收入 ¥50.00 · 净支出 ¥0.00")
        self.assertEqual(len(self.window._entry_widgets), 3)
        self.window.query_var.set("不存在的关键词")
        self.window._apply_filters()
        self.root.update()
        children = self.window._table.winfo_children()
        self.assertTrue(any(c.winfo_class() == "Label" and "没有符合筛选条件" in c.cget("text")
                            for c in children))
        clear = next(c for c in children if c.winfo_class() == "Button")
        self.assertEqual(clear.cget("text"), "清空筛选")
        self.click(clear)
        self.assertEqual(len(self.window._entry_widgets), 3)

    def test_inline_detail_expand_collapse_and_kind_marks(self):
        item = self.add(amount="12.34", day="2026-11-10",
                        note="完整备注用于验证行内展开" * 12)
        for index, kind in enumerate(("income", "transfer", "refund"), start=1):
            self.add(kind=kind, amount="5", day=f"2026-11-{index:02d}")
        self.reveal()
        marks = {w[4].cget("text")[0] for w in self.window._entry_widgets.values()}
        self.assertEqual(marks, {"−", "＋", "⇄", "↩"})
        row, category, account, info, amount, edit, delete, detail = self.window._entry_widgets[item]
        self.assertTrue(info.cget("text").endswith("..."))
        self.assertFalse(detail.winfo_ismapped())
        self.assertTrue(int(edit.cget("takefocus")))
        self.assertTrue(edit.bind("<space>") and delete.bind("<Return>"))
        height = row.winfo_height()
        info.focus_force()
        info.event_generate("<Return>")
        self.root.update()
        self.assertTrue(detail.winfo_ismapped())
        self.assertIn("完整备注用于验证行内展开" * 12, detail.cget("text"))
        self.assertIn("¥12.34", detail.cget("text"))
        self.assertGreater(row.winfo_height(), height)
        self.window._render()
        self.root.update()
        self.assertTrue(self.window._entry_widgets[item][7].winfo_ismapped())
        self.window._entry_widgets[item][3].event_generate("<Button-1>", x=5, y=5)
        self.root.update()
        self.assertFalse(self.window._entry_widgets[item][7].winfo_ismapped())

    def test_amount_column_adapts_and_stays_right_aligned(self):
        self.add(amount="12.34", day="2026-11-10")
        self.reveal()
        small = next(iter(self.window._entry_widgets.values()))[4].winfo_width()
        self.add(amount="999999999.99", day="2026-11-09")
        self.window._reload()
        self.root.update()
        widths = [w[4].winfo_width() for w in self.window._entry_widgets.values()]
        self.assertLess(small, max(widths))
        self.assertEqual(len(set(widths)), 1)
        right = {w[4].winfo_rootx() + w[4].winfo_width()
                 for w in self.window._entry_widgets.values()}
        self.assertEqual(len(right), 1)
        for w in self.window._entry_widgets.values():
            self.assertEqual(w[4].cget("anchor"), "e")
            self.assertLessEqual(w[4].winfo_x() + w[4].winfo_width(), w[0].winfo_width())

    def test_real_action_button_and_resize(self):
        self.add()
        self.reveal()
        self.window.win.geometry("1000x760")
        self.root.update()
        item, widgets = next(iter(self.window._entry_widgets.items()))
        row, category, account, info, amount, edit, delete, detail = widgets
        self.assertEqual([edit.cget("text"), delete.cget("text")], ["编辑", "删除"])
        self.assertFalse(detail.winfo_ismapped())
        self.assertEqual(len([c for c in row.winfo_children() if isinstance(c, tk.Button)]), 0)
        def submit_edit():
            dialog = self.window._deposit_dialog
            self.fill(dialog, amount="15.50", note="按钮编辑")
            dialog._submit()
        self.window.win.after(40, submit_edit)
        self.click(edit)
        self.assertEqual(self.db.get_ledger_entry(item)["amount_cents"], 1550)
        with patch("lumina.ledger_window.messagebox.askyesno", return_value=True), \
                patch.object(self.db, "delete_ledger_entry", wraps=self.db.delete_ledger_entry) as remove:
            self.click(self.window._entry_widgets[item][6])
            remove.assert_called_once_with(item)
        self.assertIsNone(self.db.get_ledger_entry(item))
        self.window._hide_privacy()
        self.window._toggle_theme()
        self.assertFalse(self.window._privacy_visible)
        self.assertEqual(self.window._data, {})

    def test_compact_header_metrics_footer_and_rows_660_880_themes(self):
        from tkinter import font as tkfont
        for day in range(1, 11):
            self.add(amount="999999999.99", day=f"2026-11-{day:02d}", note="很长的备注" * 80)
        self.reveal()
        for _ in range(2):
            for width in (660, 880):
                self.window.win.geometry(f"{width}x660")
                self.root.update()
                window = self.window
                self.assertEqual(len(window._metric_labels), 3)
                self.assertEqual(window.mode_buttons["month"].cget("relief"), "sunken")
                self.assertEqual(window.tab_buttons["entries"].cget("relief"), "sunken")
                self.assertEqual(window.mode_buttons["month"].winfo_rooty(), window.current_button.winfo_rooty())
                for value in window._metric_labels.values():
                    font = tkfont.Font(font=value.cget("font"))
                    self.assertEqual(value.cget("wraplength"), 0)
                    self.assertLessEqual(font.measure(value.cget("text")), value.winfo_width() - 4)
                right_edges = []
                backgrounds = []
                for row, category, account, info, amount, edit, delete, detail in window._entry_widgets.values():
                    backgrounds.append(row.cget("bg"))
                    for widget in (category, account, info, amount, edit, delete):
                        self.assertLessEqual(abs((widget.winfo_y() + widget.winfo_height() / 2)
                                                 - (amount.winfo_y() + amount.winfo_height() / 2)), 1)
                        self.assertLessEqual(widget.winfo_x() + widget.winfo_width(), row.winfo_width())
                    self.assertEqual([edit.cget("text"), delete.cget("text")], ["编辑", "删除"])
                    self.assertTrue(info.cget("text").endswith("..."))
                    self.assertFalse(detail.winfo_ismapped())
                    self.assertEqual(len([c for c in row.winfo_children() if isinstance(c, tk.Button)]), 0)
                    font = tkfont.Font(font=amount.cget("font"))
                    self.assertLessEqual(font.measure(amount.cget("text")), amount.winfo_width() - 4)
                    right_edges.append(amount.winfo_rootx() + amount.winfo_width())
                self.assertEqual(len(set(right_edges)), 1)
                self.assertEqual(backgrounds[:2], [window.P["card"], window.P["row_alt"]])
                footer_y = window.footer.winfo_rooty()
                self.assertLess(window._canvas.winfo_rooty() + window._canvas.winfo_height(), footer_y)
                window._canvas.event_generate("<MouseWheel>", delta=-1200)
                self.root.update()
                self.assertGreater(window._canvas.yview()[0], 0)
                self.assertEqual(window.footer.winfo_rooty(), footer_y)
                self.assertTrue(window._add_entry_button.winfo_ismapped())
            self.window._toggle_theme()

    def test_filter_tags_clear_and_details_permission_events(self):
        item = self.add(note="特定备注" * 60)
        self.add(kind="income", category="工资")
        self.reveal()
        self.assertFalse(self.window.filters.winfo_ismapped())
        self.click(self.window.filter_button)
        self.window.query_var.set("特定")
        self.window.kind_var.set("支出")
        self.window.category_var.set("餐饮")
        self.window.search_entry.focus_force()
        self.root.update()
        self.window.search_entry.event_generate("<Return>")
        self.root.update()
        self.assertEqual(self.window.filter_tags.cget("text"), "特定 · 支出 · 餐饮")
        self.assertEqual(len(self.window._filtered_rows()), 1)
        self.click(self.window.filter_button)
        self.assertFalse(self.window.filters.winfo_ismapped())
        self.assertTrue(self.window.clear_filters_button.winfo_ismapped())
        self.click(self.window.clear_filters_button)
        self.assertEqual(len(self.window._filtered_rows()), 2)
        self.assertEqual(self.window.filter_tags.cget("text"), "")
        self.assertFalse(self.window.clear_filters_button.winfo_ismapped())
        row, category, account, info, amount, edit, delete, detail = self.window._entry_widgets[item]
        with patch("lumina.ledger_window.messagebox.showinfo") as show:
            self.click(info)
            self.assertIn("特定备注" * 60, detail.cget("text"))
            self.assertTrue(detail.winfo_ismapped())
            show.assert_not_called()
            self.click(self.window._metric_labels["expense_cents"])
            self.assertIn("12.34", show.call_args.args[1])
        self.panel._vault_authenticated = False
        with patch.object(self.db, "get_ledger_entry") as read, \
                patch.object(self.db, "delete_ledger_entry") as remove, \
                patch("lumina.ledger_window.messagebox.showinfo") as show, \
                patch("lumina.ledger_window.messagebox.askyesno") as confirm:
            edit.event_generate("<Button-1>", x=5, y=5)
            delete.event_generate("<Button-1>", x=5, y=5)
            info.event_generate("<Button-1>", x=5, y=5)
            self.window._show_metric("expense_cents")
            read.assert_not_called()
            remove.assert_not_called()
            show.assert_not_called()
            confirm.assert_not_called()
        self.window._render()
        self.assertEqual(self.window._entry_widgets, {})
        self.assertTrue(all(label.cget("text") == "******" for label in self.window._metric_labels.values()))

    def test_separate_columns_header_and_narrow_window_growth(self):
        from tkinter import font as tkfont
        category = "超长分类名称" * 8
        account = "超长账户名称" * 8
        for day, amount in (("2026-11-10", "999999999.99"), ("2026-11-09", "1")):
            self.db.add_ledger_entry(**build_entry(
                "expense", amount, category, account, day, "完整备注" * 100))
        self.reveal()
        for _ in range(2):
            note_widths = []
            for width in (660, 1000):
                self.window.win.geometry(f"{width}x760")
                self.root.update()
                header = self.window._entry_header
                labels = header.winfo_children()
                self.assertEqual([w.cget("text") for w in labels],
                                 ["分类", "账户", "备注", "金额", "操作"])
                self.assertEqual(sum(c is header for c in self.window._table.winfo_children()), 1)
                for head in self.window._table.winfo_children():
                    if head is not header and head.cget("bg") == self.window.P["head_bg"]:
                        summary = head.winfo_children()[1]
                        self.assertFalse(summary.cget("text").endswith("..."))
                        font = tkfont.Font(font=summary.cget("font"))
                        self.assertLessEqual(font.measure(summary.cget("text")), summary.winfo_width() - 4)
                for widgets in self.window._entry_widgets.values():
                    row, cat, acc, note, amount, edit, delete, detail = widgets
                    self.assertGreaterEqual(row.winfo_height(), 36)
                    self.assertLessEqual(row.winfo_height(), 40)
                    for column, widget in enumerate((cat, acc, note, amount)):
                        self.assertEqual(widget.winfo_rootx(), labels[column].winfo_rootx())
                    self.assertEqual(cat.cget("text")[-3:], "...")
                    self.assertEqual(acc.cget("text")[-3:], "...")
                    self.assertNotIn(account, cat.cget("text"))
                    for widget in (cat, acc, note, amount, edit, delete):
                        self.assertGreaterEqual(widget.winfo_x(), 0)
                        self.assertLessEqual(widget.winfo_x() + widget.winfo_width(), row.winfo_width())
                        self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(),
                                             self.window.win.winfo_rootx() + width)
                    font = tkfont.Font(font=amount.cget("font"))
                    self.assertLessEqual(font.measure(amount.cget("text")), amount.winfo_width() - 4)
                note_widths.append(next(iter(self.window._entry_widgets.values()))[3].winfo_width())
            self.assertEqual(note_widths[1] - note_widths[0], 340)
            self.window._toggle_theme()

    def test_whole_row_hover_restores_stripes_and_keeps_button_emphasis(self):
        self.add()
        self.add(day="2026-11-09")
        self.reveal()
        for _ in range(2):
            for index, widgets in enumerate(self.window._entry_widgets.values()):
                row, category, account, note, amount, edit, delete, detail = widgets
                base = self.window.P["row_alt" if index % 2 else "card"]
                row.event_generate("<Leave>", x=-10, y=-10)
                self.root.update()
                self.assertTrue(all(w.cget("bg") == base for w in (row, *row.winfo_children())))
                for widget in (row, category, account, note, amount, edit, delete):
                    widget.event_generate("<Enter>", x=2, y=2)
                    self.root.update()
                    self.assertTrue(all(w.cget("bg") == self.window.P["head_bg"]
                                        for w in (row, *row.winfo_children())))
                    widget.event_generate("<Leave>",
                                          rootx=row.winfo_rootx() + 5,
                                          rooty=row.winfo_rooty() + 5)
                    self.root.update()
                    self.assertEqual(row.cget("bg"), self.window.P["head_bg"])
                row.event_generate("<Leave>", x=-10, y=-10)
                self.root.update()
                self.assertTrue(all(w.cget("bg") == base for w in (row, *row.winfo_children())))
            self.window._toggle_theme()

    def test_date_subtotals_filter_scope_refund_and_transfer(self):
        self.add(amount="20", note="匹配")
        self.add(amount="5", note="其他")
        self.add(kind="refund", amount="30", note="匹配")
        self.add(kind="income", amount="50", note="匹配")
        self.add(kind="transfer", amount="999", note="匹配")
        self.reveal()
        def summary():
            head = next(c for c in self.window._table.winfo_children()
                        if c.winfo_class() == "Frame" and c is not self.window._entry_header
                        and c.cget("bg") == self.window.P["head_bg"])
            return [c.cget("text") for c in head.winfo_children()]
        self.assertIn("5 笔", summary()[0])
        self.assertEqual(summary()[1], "本日 · 收入 ¥50.00 · 净支出 ¥-5.00")
        self.window.query_var.set("匹配")
        self.window._apply_filters()
        self.root.update()
        self.assertIn("4 笔", summary()[0])
        self.assertEqual(summary()[1], "筛选后 · 收入 ¥50.00 · 净支出 ¥-10.00")
        self.assertEqual(self.window._data["expense_cents"], -500)
        self.window.kind_var.set("转账")
        self.window._apply_filters()
        self.root.update()
        self.assertIn("1 笔", summary()[0])
        self.assertEqual(summary()[1], "筛选后 · 收入 ¥0.00 · 净支出 ¥0.00")
        self.window._hide_privacy()
        self.assertIsNone(self.window._entry_header)
        self.assertEqual(self.window._entry_widgets, {})

    def test_category_account_tooltips_and_pending_privacy_cleanup(self):
        item = self.db.add_ledger_entry(**build_entry(
            "expense", "1", "长分类" * 12, "长账户" * 12, "2026-11-10", "备注"))
        self.reveal()
        for index, full in ((1, "长分类" * 12), (2, "长账户" * 12)):
            widget = self.window._entry_widgets[item][index]
            widget.event_generate("<Enter>")
            self.pump(450)
            tip = self.window._note_tip
            self.assertIsNotNone(tip)
            text = next(c for c in tip.winfo_children() if isinstance(c, tk.Text))
            self.assertEqual(text.get("1.0", "end-1c"), full)
            self.window._hide_note_tip()
        self.window._entry_widgets[item][1].event_generate("<Enter>")
        self.assertIsNotNone(self.window._note_tip_job)
        self.window._hide_privacy()
        self.pump(450)
        self.assertIsNone(self.window._note_tip)
        self.assertIsNone(self.window._note_tip_job)

    def test_delete_confirmation_cancel_retains_entry(self):
        item = self.add()
        self.reveal()
        with patch("lumina.ledger_window.messagebox.askyesno", return_value=False) as confirm, \
                patch.object(self.db, "delete_ledger_entry") as remove:
            self.click(self.window._entry_widgets[item][6])
            confirm.assert_called_once()
            remove.assert_not_called()
        self.assertIsNotNone(self.db.get_ledger_entry(item))

    def test_daily_aggregation_and_year_switch_real_events(self):
        self.add(amount="20", day="2026-11-01")
        self.add(kind="refund", amount="30", day="2026-11-01")
        self.add(kind="income", amount="50", category="工资", day="2026-11-02")
        self.add(kind="transfer", amount="999", day="2026-11-02")
        self.reveal()
        self.click(self.window.tab_buttons["stats"])
        periods = self.window._trend_periods()
        self.assertEqual(len(periods), 30)
        self.assertEqual(periods[0]["expense_cents"], -1000)
        self.assertEqual(periods[1]["income_cents"], 5000)
        self.assertEqual(periods[1]["expense_cents"], 0)
        self.assertTrue(self.window.trend_canvas.find_withtag("day1"))
        self.assertFalse(self.window.trend_canvas.find_withtag("month1"))
        self.assertLess(self.window.category_canvas.winfo_y(), self.window.trend_canvas.winfo_y())
        self.assertIn("月均", self.window.average_label.cget("text"))
        self.assertEqual(self.window.month_rows, [])
        self.assertFalse(self.window._explanation_open)
        self.click(self.window.explanation_button)
        self.assertTrue(self.window._explanation_open)
        self.click(self.window.mode_buttons["year"])
        self.assertEqual(self.window.current_button.cget("text"), "本年")
        self.assertEqual(len(self.window._trend_periods()), 12)
        self.assertTrue(self.window.trend_canvas.find_withtag("month12"))
        self.assertFalse(self.window.trend_canvas.find_withtag("day1"))
        self.click(self.window.mode_buttons["month"])
        self.assertTrue(self.window.trend_canvas.find_withtag("day30"))


if __name__ == "__main__":
    unittest.main()
