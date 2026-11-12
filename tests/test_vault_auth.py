"""隐域认证、入口守卫与锁屏监控的无交互测试。"""
import ctypes
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from lumina import vault_auth
from lumina.history_panel import HistoryPanel


class _Widget:
    def __init__(self, master=None, **kwargs):
        self.master = master
        self.options = kwargs
        self.value = ""
        self.exists = True
        self.bindings = {}
        self.protocols = {}
        if master is not None and hasattr(master, "children"):
            master.children.append(self)
        self.children = []

    def __getattr__(self, name):
        method = Mock()
        setattr(self, name, method)
        return method

    def pack(self, **kwargs):
        pass

    def grid(self, **kwargs):
        pass

    def configure(self, **kwargs):
        self.options.update(kwargs)

    def get(self):
        return self.value

    def delete(self, *args):
        self.value = ""

    def bind(self, event, callback):
        self.bindings[event] = callback

    def protocol(self, event, callback):
        self.protocols[event] = callback

    def destroy(self):
        self.exists = False

    def winfo_exists(self):
        return self.exists

    def winfo_rootx(self):
        return 0

    winfo_rooty = winfo_rootx

    def winfo_width(self):
        return 600

    winfo_height = winfo_width

    def winfo_reqwidth(self):
        return 300

    winfo_reqheight = winfo_reqwidth


class AuthenticationTests(unittest.TestCase):
    def setUp(self):
        import sys
        from types import ModuleType

        self.owner = _Widget()
        self.dialog = _Widget(self.owner)
        self.entries = []
        self.labels = []
        self.panel = SimpleNamespace(win=self.owner)
        self.key = b"k" * 32
        self.metadata = {"version": 1, "kdf": "argon2id"}
        self.crypto = ModuleType("lumina.money_crypto")

        def create_key_envelope(secret):
            if len(secret) < 10:
                raise ValueError("保护口令至少需要 10 个字符")
            return self.metadata.copy(), self.key

        def unlock_key(metadata, secret):
            if secret != "correct horse battery":
                raise ValueError("保护口令错误")
            return self.key

        def rewrap_key_envelope(metadata, current, new):
            if current != "correct horse battery":
                raise ValueError("保护口令错误")
            if len(new) < 10:
                raise ValueError("保护口令至少需要 10 个字符")
            return {**metadata, "rotated": True}

        self.crypto.create_key_envelope = create_key_envelope
        self.crypto.unlock_key = unlock_key
        self.crypto.rewrap_key_envelope = rewrap_key_envelope
        self.crypto_patch = patch.dict(sys.modules, {"lumina.money_crypto": self.crypto})
        self.crypto_patch.start()
        self.save_patch = patch("lumina.settings.save_config")
        self.save_config = self.save_patch.start()
        self.panel.ui = SimpleNamespace(
            config={}, db=SimpleNamespace(unlock_financial_data=Mock()),
            actions=SimpleNamespace(config_path="test-config.json"))

        def entry(*args, **kwargs):
            widget = _Widget(*args, **kwargs)
            self.entries.append(widget)
            return widget

        def label(*args, **kwargs):
            widget = _Widget(*args, **kwargs)
            self.labels.append(widget)
            return widget

        self.panel.tk = SimpleNamespace(
            Toplevel=Mock(return_value=self.dialog), Frame=_Widget,
            Label=label, Entry=entry, Button=_Widget, Canvas=_Widget)

    def tearDown(self):
        self.save_patch.stop()
        self.crypto_patch.stop()

    def submit(self, password, confirmation=None):
        self.entries[0].value = password
        if len(self.entries) > 1:
            self.entries[1].value = confirmation or ""
        self.assertEqual(self.dialog.bindings["<Return>"](), "break")

    def test_first_use_sets_config_metadata_and_waits_modally(self):
        self.dialog.wait_window.side_effect = lambda: self.submit(
            "correct horse battery", "correct horse battery")
        self.assertTrue(vault_auth.authenticate_vault(self.panel))
        self.assertEqual(self.entries[0].options["show"], "*")
        self.assertEqual([entry.value for entry in self.entries], ["", ""])
        self.assertEqual(self.panel.ui.config["money_crypto"], self.metadata)
        self.save_config.assert_called_once_with(
            self.panel.ui.config, "test-config.json")
        self.panel.ui.db.unlock_financial_data.assert_called_once_with(self.key)
        self.dialog.grab_set.assert_called_once()
        self.dialog.wait_visibility.assert_called_once()
        self.dialog.wait_window.assert_called_once()
        self.assertFalse(self.panel._vault_auth_pending)
        self.assertNotIn("password", vars(self.panel))

    def test_change_passphrase_rewraps_metadata_and_saves_config(self):
        self.panel.ui.config["money_crypto"] = self.metadata

        def interact():
            self.entries[0].value = "correct horse battery"
            self.entries[1].value = "new secure passphrase"
            self.entries[2].value = "new secure passphrase"
            self.assertEqual(self.dialog.bindings["<Return>"](), "break")
            self.assertFalse(self.dialog.exists)

        self.dialog.wait_window.side_effect = interact
        self.assertTrue(vault_auth.change_vault_passphrase(self.panel))
        self.assertTrue(self.panel.ui.config["money_crypto"]["rotated"])
        self.assertEqual([entry.value for entry in self.entries], ["", "", ""])
        self.save_config.assert_called_once_with(
            {"money_crypto": self.panel.ui.config["money_crypto"]}, "test-config.json")
        self.panel.ui.db.unlock_financial_data.assert_not_called()
        self.assertFalse(self.panel._vault_auth_pending)

    def test_change_passphrase_has_independent_eye_toggles(self):
        self.panel.ui.config["money_crypto"] = self.metadata

        def interact():
            for index, entry in enumerate(self.entries):
                entry.value = f"secret value {index}"
            eyes = [next(child for child in entry.master.children if child is not entry)
                    for entry in self.entries]
            self.assertEqual([eye.options["width"] for eye in eyes], [28, 28, 28])
            eyes[0].bindings["<Button-1>"]()
            self.assertEqual([entry.options["show"] for entry in self.entries],
                             ["", "*", "*"])
            eyes[1].bindings["<space>"]()
            self.assertEqual([entry.options["show"] for entry in self.entries],
                             ["", "", "*"])
            eyes[0].bindings["<Button-1>"]()
            eyes[2].bindings["<Return>"]()
            self.assertEqual([entry.options["show"] for entry in self.entries],
                             ["*", "", ""])
            self.assertEqual([entry.value for entry in self.entries],
                             ["secret value 0", "secret value 1", "secret value 2"])
            self.dialog.bindings["<Escape>"]()
            self.assertEqual([entry.options["show"] for entry in self.entries],
                             ["*", "*", "*"])

        self.dialog.wait_window.side_effect = interact
        self.assertFalse(vault_auth.change_vault_passphrase(self.panel))
        self.assertEqual([entry.value for entry in self.entries], ["", "", ""])

    def test_change_passphrase_rejects_wrong_current_and_mismatch(self):
        self.panel.ui.config["money_crypto"] = self.metadata

        def interact():
            self.entries[0].value = "wrong horse battery"
            self.entries[1].value = "new secure passphrase"
            self.entries[2].value = "new secure passphrase"
            self.dialog.bindings["<Return>"]()
            self.assertEqual(self.labels[-1].options["text"], "保护口令错误")
            self.assertEqual(self.panel.ui.config["money_crypto"], self.metadata)
            self.entries[0].value = "correct horse battery"
            self.entries[1].value = "new secure passphrase"
            self.entries[2].value = "different secure phrase"
            self.dialog.bindings["<Return>"]()
            self.assertEqual(self.labels[-1].options["text"], "两次输入的新口令不一致")
            self.dialog.bindings["<Escape>"]()

        self.dialog.wait_window.side_effect = interact
        self.assertFalse(vault_auth.change_vault_passphrase(self.panel))
        self.save_config.assert_not_called()

    def test_first_setup_confirmation_has_its_own_eye_and_copy_has_no_money_word(self):
        def interact():
            self.assertEqual(self.dialog.title.call_args.args[0], "保护口令")
            self.assertEqual(self.labels[0].options["text"], "首次设置保护口令")
            self.assertFalse(any("金额" in str(label.options.get("text", ""))
                                 for label in self.labels))
            password_eye = next(
                child for child in self.entries[0].master.children
                if child is not self.entries[0])
            confirmation_eye = next(
                child for child in self.entries[1].master.children
                if child is not self.entries[1])
            self.entries[0].value = "first secure password"
            self.entries[1].value = "repeat secure password"
            password_eye.bindings["<Button-1>"]()
            self.assertEqual([entry.options["show"] for entry in self.entries], ["", "*"])
            confirmation_eye.bindings["<space>"]()
            self.assertEqual([entry.options["show"] for entry in self.entries], ["", ""])
            password_eye.bindings["<Button-1>"]()
            self.assertEqual([entry.options["show"] for entry in self.entries], ["*", ""])
            self.dialog.bindings["<Escape>"]()
            self.assertEqual([entry.options["show"] for entry in self.entries], ["*", "*"])

        self.dialog.wait_window.side_effect = interact
        self.assertFalse(vault_auth.authenticate_vault(self.panel))
        self.assertEqual([entry.value for entry in self.entries], ["", ""])

    def test_mismatched_setup_passwords_show_error_and_can_cancel(self):
        def interact():
            self.submit("correct horse battery", "different passphrase")
            self.assertTrue(self.dialog.exists)
            self.assertEqual(self.labels[-1].options["text"], "两次输入的口令不一致")
            self.assertEqual([entry.value for entry in self.entries], ["", ""])
            self.dialog.bindings["<Escape>"]()
        self.dialog.wait_window.side_effect = interact
        self.assertFalse(vault_auth.authenticate_vault(self.panel))

    def test_light_theme_eye_toggle_and_wrong_passphrase_rejected(self):
        from lumina.vault_window import VaultWindow
        self.panel.ui.config["money_crypto"] = self.metadata
        def interact():
            self.assertEqual(self.dialog.options["bg"], VaultWindow.LIGHT_PALETTE["surface"])
            password = self.entries[0]
            eye = next(c for c in password.master.children if c is not password)
            password.value = "wrong horse battery"
            eye.bindings["<Button-1>"]()
            self.assertEqual(password.options["show"], "")
            self.assertEqual(password.value, "wrong horse battery")
            eye.bindings["<space>"]()
            self.assertEqual(password.options["show"], "*")
            eye.bindings["<Return>"]()
            self.submit("wrong horse battery")
            self.assertTrue(self.dialog.exists)
            self.assertEqual(self.labels[-1].options["text"], "保护口令错误")
            self.assertEqual(password.value, "")
            self.submit("correct horse battery")
        self.dialog.wait_window.side_effect = interact
        self.assertTrue(vault_auth.authenticate_vault(self.panel))

    def test_window_close_cancels(self):
        self.dialog.wait_window.side_effect = lambda: self.dialog.protocols["WM_DELETE_WINDOW"]()
        self.assertFalse(vault_auth.authenticate_vault(self.panel))

    def test_reentrant_authentication_does_not_create_second_dialog(self):
        def interact():
            self.assertFalse(vault_auth.authenticate_vault(self.panel))
            self.submit("correct horse battery", "correct horse battery")
        self.dialog.wait_window.side_effect = interact
        self.assertTrue(vault_auth.authenticate_vault(self.panel))
        self.panel.tk.Toplevel.assert_called_once()

    def test_explicit_parent_is_used(self):
        parent = _Widget()
        self.dialog.wait_window.side_effect = lambda: self.dialog.bindings["<Escape>"]()
        self.assertFalse(vault_auth.authenticate_vault(self.panel, parent=parent))
        self.panel.tk.Toplevel.assert_called_once_with(parent)
        self.dialog.transient.assert_called_once_with(parent)

    def test_dialog_failure_is_closed_and_not_authenticated(self):
        self.dialog.wait_window.side_effect = RuntimeError("测试异常")
        with self.assertLogs(vault_auth.__name__, level="ERROR"):
            self.assertFalse(vault_auth.authenticate_vault(self.panel))
        self.assertFalse(self.dialog.exists)
        self.assertFalse(self.panel._vault_auth_pending)


class HistoryVaultEntryTests(unittest.TestCase):
    def setUp(self):
        self.panel = HistoryPanel.__new__(HistoryPanel)
        self.panel._popup_open = False
        self.panel._close_popup_menu = Mock()
        self.panel._cancel_hover_open = Mock()

    def test_success_sets_only_session_flag_and_skips_second_login(self):
        parent = object()
        with patch.object(vault_auth, "authenticate_vault", return_value=True) as login:
            self.assertTrue(self.panel._authenticate_vault(parent=parent))
            self.assertTrue(self.panel._authenticate_vault())
        login.assert_called_once_with(self.panel, parent=parent)
        self.assertTrue(self.panel._vault_authenticated)

    def test_failure_does_not_set_authenticated(self):
        with patch.object(vault_auth, "authenticate_vault", return_value=False):
            self.assertFalse(self.panel._authenticate_vault())
        self.assertFalse(getattr(self.panel, "_vault_authenticated", False))

    def test_vault_click_opens_menu_without_authentication(self):
        self.panel._authenticate_vault = Mock(return_value=False)
        self.panel._open_popup_menu = Mock()
        anchor = object()
        self.panel._toggle_popup_menu("vault", anchor)
        self.panel._authenticate_vault.assert_not_called()
        self.panel._open_popup_menu.assert_called_once_with("vault")
        self.assertTrue(self.panel._hover_armed)
        self.assertEqual(self.panel._menu_kind, "vault")
        self.assertIs(self.panel._menu_anchor, anchor)

    def test_vault_click_closes_open_menu_without_authentication(self):
        self.panel._popup_open = True
        self.panel._menu_kind = "vault"
        self.panel._authenticate_vault = Mock()
        self.panel._open_popup_menu = Mock()
        self.panel._toggle_popup_menu("vault", object())
        self.panel._authenticate_vault.assert_not_called()
        self.panel._close_popup_menu.assert_called_once_with()
        self.panel._open_popup_menu.assert_not_called()

    def test_tools_click_does_not_authenticate(self):
        self.panel._authenticate_vault = Mock()
        self.panel._open_popup_menu = Mock()
        self.panel._toggle_popup_menu("tools", object())
        self.panel._authenticate_vault.assert_not_called()

    def test_unauthenticated_direct_menu_and_box_click_do_not_authenticate(self):
        self.panel._authenticate_vault = Mock(return_value=False)
        self.panel.win = _Widget()
        self.panel.win.winfo_screenwidth = Mock(return_value=1920)
        popup = _Widget(self.panel.win)
        self.panel.tk = SimpleNamespace(Toplevel=Mock(return_value=popup),
                                        Frame=_Widget, Button=_Widget)
        self.panel._theme = Mock(return_value=(
            {key: colors[0] for key, colors in HistoryPanel.THEME.items()}, False))
        self.panel._font = Mock(return_value="TkDefaultFont")
        self.panel._sys = Mock(return_value="#244A68")
        self.panel._round_popup_background = Mock()
        self.panel._menu_anchor = _Widget()
        self.panel._popup_hover_job = None
        with patch("PIL.ImageTk.PhotoImage", return_value=object()):
            self.panel._open_popup_menu("vault")
        self.panel.tk.Toplevel.assert_called_once_with(self.panel.win)
        self.panel._authenticate_vault.assert_not_called()
        self.assertTrue(self.panel._popup_open)
        self.assertIs(self.panel._popup_menu, popup)
        button = popup.children[0].children[0]
        ledger_button = popup.children[0].children[1]
        self.assertEqual(button.options["text"], "密匣")
        self.assertEqual(ledger_button.options["text"], "记账")
        self.assertEqual(ledger_button.options["compound"], "right")
        self.assertIsNotNone(ledger_button.options["image"])
        with patch.object(self.panel, "_open_vault_box") as open_box:
            button.options["command"]()
            open_box.assert_called_once_with()
        self.panel._authenticate_vault.assert_not_called()

    def test_unauthenticated_hover_opens_menu_without_prompt(self):
        self.panel._authenticate_vault = Mock()
        self.panel._open_popup_menu = Mock()
        self.panel._hover_armed = True
        self.panel.win = Mock()
        self.panel.win.winfo_pointerxy.return_value = (5, 5)
        button = Mock()
        button.winfo_rootx.return_value = button.winfo_rooty.return_value = 0
        button.winfo_width.return_value = button.winfo_height.return_value = 10
        self.panel._hover_open("vault", button)
        self.panel._open_popup_menu.assert_called_once_with("vault")
        self.panel._authenticate_vault.assert_not_called()

    def test_private_entries_construct_without_authentication_or_panel_reads(self):
        self.panel._authenticate_vault = Mock(return_value=False)
        self.panel.db = Mock()
        self.panel._watch_private_window = Mock()
        for method, module, name in ((self.panel._open_vault_box, "lumina.vault_window", "VaultWindow"),
                                     (self.panel._open_ledger, "lumina.ledger_window", "LedgerWindow")):
            constructor = Mock()
            with patch.dict("sys.modules", {module: SimpleNamespace(**{name: constructor})}):
                self.assertIs(method(), constructor.return_value)
            constructor.assert_called_once_with(self.panel)
        self.panel._authenticate_vault.assert_not_called()
        self.assertEqual(self.panel.db.mock_calls, [])

    def test_authentication_during_lock_cannot_restore_revoked_session(self):
        def login(*args, **kwargs):
            self.panel._vault_write_generation = 1
            return True
        with patch.object(vault_auth, "authenticate_vault", side_effect=login):
            self.assertFalse(self.panel._authenticate_vault())
        self.assertFalse(getattr(self.panel, "_vault_authenticated", False))

    def test_box_success_constructs_once_then_summons(self):
        existing = Mock(_closed=False)
        existing.win.winfo_exists.return_value = True
        constructor = Mock(return_value=existing)
        events = []
        self.panel._watch_private_window = Mock()
        self.panel._authenticate_vault = Mock(side_effect=lambda: events.append("auth") or True)
        constructor.side_effect = lambda panel: events.append("construct") or existing
        existing.summon.side_effect = lambda: events.append("summon")
        with patch.dict("sys.modules", {"lumina.vault_window": SimpleNamespace(VaultWindow=constructor)}):
            self.assertIs(self.panel._open_vault_box(), existing)
            self.assertIs(self.panel._open_vault_box(), existing)
        constructor.assert_called_once_with(self.panel)
        existing.summon.assert_called_once_with()
        self.assertEqual(events, ["construct", "summon"])
        self.panel._authenticate_vault.assert_not_called()


class SessionLockMonitorTests(unittest.TestCase):
    def setUp(self):
        self.window = Mock()
        self.window.after.return_value = "poll-job"
        self.callback = Mock()
        self.wts = SimpleNamespace(
            WTSRegisterSessionNotification=Mock(return_value=1),
            WTSUnRegisterSessionNotification=Mock(return_value=1))
        self.comctl = SimpleNamespace(
            SetWindowSubclass=Mock(return_value=1),
            RemoveWindowSubclass=Mock(return_value=1),
            DefSubclassProc=Mock(return_value=123))
        self.patches = [
            patch.object(vault_auth.os, "name", "nt"),
            patch.object(vault_auth.taskbar, "top_level_hwnd", return_value=456),
            patch.object(ctypes, "WinDLL", create=True,
                         side_effect=lambda name, **kwargs: self.wts if name == "wtsapi32" else self.comctl),
            patch.object(ctypes, "WINFUNCTYPE", getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE), create=True),
            patch.object(ctypes, "WinError", create=True, return_value=OSError("测试 Win32 失败")),
            patch.object(ctypes, "get_last_error", create=True, return_value=1),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def monitor(self):
        monitor = vault_auth.SessionLockMonitor(self.window, self.callback)
        self.addCleanup(monitor.close)
        return monitor

    def test_register_and_release_once_keeps_callback_alive(self):
        monitor = self.monitor()
        self.wts.WTSRegisterSessionNotification.assert_called_once_with(456, 0)
        self.comctl.SetWindowSubclass.assert_called_once_with(456, monitor._proc, id(monitor), 0)
        self.assertIs(vault_auth._LIVE_MONITORS[id(monitor)], monitor)
        monitor.close()
        monitor.close()
        self.wts.WTSUnRegisterSessionNotification.assert_called_once_with(456)
        self.comctl.RemoveWindowSubclass.assert_called_once_with(456, monitor._proc, id(monitor))
        self.assertIsNotNone(monitor._proc)
        self.assertNotIn(id(monitor), vault_auth._LIVE_MONITORS)
        self.window.after_cancel.assert_called_once_with("poll-job")

    def test_lock_from_worker_only_runs_callback_in_tk_poll(self):
        monitor = self.monitor()
        self.window.after.reset_mock()
        worker = threading.Thread(target=lambda: monitor._proc(456, 0x2B1, 7, 0, id(monitor), 0))
        worker.start()
        worker.join()
        self.callback.assert_not_called()
        self.window.after.assert_not_called()
        tk_thread = threading.get_ident()
        observed = []
        self.callback.side_effect = lambda: observed.append(threading.get_ident())
        monitor._poll()
        self.assertEqual(observed, [tk_thread])

    def test_other_notifications_are_forwarded_without_callback(self):
        monitor = self.monitor()
        self.assertEqual(monitor._proc(456, 0x2B1, 8, 0, id(monitor), 0), 123)
        monitor._proc(456, 1, 7, 0, id(monitor), 0)
        monitor._poll()
        self.callback.assert_not_called()

    def test_close_discards_queued_lock(self):
        monitor = self.monitor()
        monitor._proc(456, 0x2B1, 7, 0, id(monitor), 0)
        monitor.close()
        monitor._poll()
        self.callback.assert_not_called()

    def test_register_failure_removes_installed_subclass(self):
        self.wts.WTSRegisterSessionNotification.return_value = 0
        with self.assertLogs(vault_auth.__name__, level="ERROR"):
            monitor = self.monitor()
        self.assertTrue(monitor._closed)
        self.comctl.RemoveWindowSubclass.assert_called_once()
        self.wts.WTSUnRegisterSessionNotification.assert_not_called()

    def test_subclass_failure_never_registers(self):
        self.comctl.SetWindowSubclass.return_value = 0
        with self.assertLogs(vault_auth.__name__, level="ERROR"):
            self.monitor()
        self.wts.WTSRegisterSessionNotification.assert_not_called()

    def test_remove_failure_retains_callback_and_close_can_retry(self):
        monitor = self.monitor()
        self.comctl.RemoveWindowSubclass.return_value = 0
        self.wts.WTSUnRegisterSessionNotification.return_value = 0
        with self.assertLogs(vault_auth.__name__, level="ERROR"):
            monitor.close()
        self.assertIs(vault_auth._LIVE_MONITORS[id(monitor)], monitor)
        self.comctl.RemoveWindowSubclass.return_value = 1
        self.wts.WTSUnRegisterSessionNotification.return_value = 1
        monitor.close()
        self.assertNotIn(id(monitor), vault_auth._LIVE_MONITORS)

    def test_callback_failure_is_logged_without_stopping_poll(self):
        monitor = self.monitor()
        self.callback.side_effect = RuntimeError("测试回调失败")
        monitor._proc(456, 0x2B1, 7, 0, id(monitor), 0)
        with self.assertLogs(vault_auth.__name__, level="ERROR"):
            monitor._poll()
        self.assertFalse(monitor._closed)
        self.assertIsNotNone(monitor._job)

    def test_destroy_message_releases_notifications_and_forwards(self):
        monitor = self.monitor()
        self.assertEqual(monitor._proc(456, 0x82, 0, 0, id(monitor), 0), 123)
        self.assertTrue(monitor._closed)
        self.wts.WTSUnRegisterSessionNotification.assert_called_once_with(456)
        self.comctl.RemoveWindowSubclass.assert_called_once()
        self.assertNotIn(id(monitor), vault_auth._LIVE_MONITORS)

    def test_poll_schedule_failure_releases_native_resources(self):
        self.window.after.side_effect = RuntimeError("测试窗口已销毁")
        with self.assertLogs(vault_auth.__name__, level="ERROR"):
            monitor = self.monitor()
        self.assertTrue(monitor._closed)
        self.wts.WTSUnRegisterSessionNotification.assert_called_once()
        self.comctl.RemoveWindowSubclass.assert_called_once()

    def test_missing_hwnd_is_logged_and_safe(self):
        with patch.object(vault_auth.taskbar, "top_level_hwnd", return_value=None):
            with self.assertLogs(vault_auth.__name__, level="ERROR"):
                monitor = self.monitor()
        self.assertTrue(monitor._closed)
        self.wts.WTSRegisterSessionNotification.assert_not_called()

    def test_non_windows_is_noop(self):
        with patch.object(vault_auth.os, "name", "posix"):
            monitor = self.monitor()
        monitor.close()
        self.comctl.SetWindowSubclass.assert_not_called()
        self.window.after.assert_not_called()


if __name__ == "__main__":
    unittest.main()
