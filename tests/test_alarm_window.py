"""Focused checks for the isolated alarm scheduling helpers."""
import re
import unittest
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

try:
    import tkinter as tk
except ImportError:
    tk = None

from lumina.alarm_window import (
    AlarmAlertWindow,
    AlarmScheduler,
    AlarmSettingsWindow,
    format_seconds,
    humanize_delta,
    month_grid,
    next_alarm_occurrence,
    parse_alarm_time,
)


class NextAlarmOccurrenceTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2025, 1, 3, 8, 30)  # Friday
        self.settings = {
            "alarm_enabled": True,
            "alarm_date": "2025-01-03",
            "alarm_time": "09:00",
            "alarm_repeat": "once",
        }

    def test_single_alarm_uses_configured_local_date_and_time(self):
        self.assertEqual(
            next_alarm_occurrence(self.settings, self.now),
            datetime(2025, 1, 3, 9, 0))

    def test_alarm_time_can_carry_seconds(self):
        self.settings["alarm_time"] = "09:00:30"
        self.assertEqual(
            next_alarm_occurrence(self.settings, self.now),
            datetime(2025, 1, 3, 9, 0, 30))

    def test_past_single_alarm_is_not_scheduled(self):
        self.settings["alarm_time"] = "08:00"
        self.assertIsNone(next_alarm_occurrence(self.settings, self.now))

    def test_daily_alarm_rolls_to_tomorrow_after_time_passes(self):
        self.settings.update(alarm_repeat="daily", alarm_time="08:00")
        self.assertEqual(
            next_alarm_occurrence(self.settings, self.now),
            datetime(2025, 1, 4, 8, 0))

    def test_weekday_alarm_skips_weekend(self):
        self.settings.update(alarm_repeat="weekdays", alarm_time="08:00")
        self.assertEqual(
            next_alarm_occurrence(self.settings, self.now),
            datetime(2025, 1, 6, 8, 0))

    def test_disabled_or_invalid_alarm_is_not_scheduled(self):
        self.settings["alarm_enabled"] = False
        self.assertIsNone(next_alarm_occurrence(self.settings, self.now))
        self.settings.update(alarm_enabled=True, alarm_time="not-a-time")
        self.assertIsNone(next_alarm_occurrence(self.settings, self.now))

    def test_parse_alarm_time_accepts_legacy_and_new_formats(self):
        self.assertEqual(parse_alarm_time("08:05"), datetime(2000, 1, 1, 8, 5).time())
        self.assertEqual(parse_alarm_time("08:05:09"), datetime(2000, 1, 1, 8, 5, 9).time())
        with self.assertRaises(ValueError):
            parse_alarm_time("08:05:60")


class MonthGridTests(unittest.TestCase):
    def test_grid_is_monday_first_and_contains_the_month(self):
        cells = month_grid(2026, 11)
        self.assertEqual(len(cells), 42)
        self.assertEqual(cells[0].weekday(), 0)
        self.assertIn(date(2026, 11, 13), cells)
        self.assertEqual(cells[0], date(2026, 10, 26))


class HumanizeDeltaTests(unittest.TestCase):
    def test_short_durations_render_as_readable_phrases(self):
        self.assertEqual(humanize_delta(timedelta(seconds=42)), "42 秒")
        self.assertEqual(humanize_delta(timedelta(minutes=5, seconds=30)), "5 分钟")
        self.assertEqual(
            humanize_delta(timedelta(hours=1, minutes=20)), "1 小时 20 分钟")
        self.assertEqual(
            humanize_delta(timedelta(days=2, hours=3)), "2 天 3 小时")


class CountdownSecondsTests(unittest.TestCase):
    def test_ring_centre_uses_total_seconds(self):
        self.assertEqual(format_seconds(0), "0")
        self.assertEqual(format_seconds(125), "125")
        self.assertEqual(format_seconds(2 * 3600 + 15 * 60), "8100")
        self.assertEqual(format_seconds(-5), "0")


@unittest.skipIf(tk is None, "Tkinter is unavailable")
class AlarmAlertWindowTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except tk.TclError as exc:
            raise unittest.SkipTest(f"Tk display unavailable: {exc}") from exc
        self.window = None

    def tearDown(self):
        if self.window is not None:
            self.window.destroy_without_callback()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _make(self, stage="early", persistent=False):
        self.window = AlarmAlertWindow(
            self.root,
            {"alarm_label": "测试提醒", "alarm_style": "soft", "alarm_sound": False},
            stage, datetime.now() + timedelta(minutes=3),
            lambda _stage: None, lambda _stage, _minutes: None,
            persistent=persistent)
        return self.window

    def test_card_is_borderless_and_shows_live_countdown(self):
        window = self._make()
        self.assertTrue(window.window.overrideredirect())
        self.assertEqual(window.title_label.cget("text"), "即将开始")
        self.assertEqual(window.label_caption.cget("text"), "测试提醒")
        self.assertEqual(window.primary.cget("text"), "知道了")

    def test_card_header_shows_a_live_clock(self):
        window = self._make()
        self.assertRegex(
            window.clock_label.cget("text"),
            r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    def test_card_settings_entry_opens_alarm_settings(self):
        opened = []
        self.window = AlarmAlertWindow(
            self.root,
            {"alarm_label": "测试提醒", "alarm_style": "soft", "alarm_sound": False},
            "scheduled", datetime.now() + timedelta(minutes=3),
            lambda _stage: None, lambda _stage, _minutes: None,
            persistent=True, on_open_settings=lambda: opened.append(True))
        self.assertEqual(self.window.settings_label.winfo_manager(), "pack")
        self.window._open_settings()
        self.assertEqual(opened, [True])
        self.assertTrue(self.window.collapsed)

    def test_alarm_stage_hides_settings_entry(self):
        self.window = AlarmAlertWindow(
            self.root,
            {"alarm_label": "测试提醒", "alarm_style": "soft", "alarm_sound": False},
            "alarm", datetime.now(),
            lambda _stage: None, lambda _stage, _minutes: None,
            persistent=True, on_open_settings=lambda: None)
        self.assertEqual(self.window.settings_label.winfo_manager(), "")

    def test_alarm_uses_alert_accent(self):
        window = self._make("alarm")
        self.assertEqual(window.title_label.cget("fg"), window.ALERT_ACCENT)
        self.assertEqual(window.primary.cget("bg"), window.ALERT_ACCENT)
        self.assertEqual(
            window.header_dot.itemcget(window.header_dot_item, "fill"),
            window.ALERT_ACCENT)
        self.assertIsNotNone(window._alert_job)

    def test_leaving_alarm_restores_theme_style(self):
        window = self._make("alarm")
        window.apply("snoozed", datetime.now() + timedelta(minutes=5))
        self.assertEqual(window.primary.cget("bg"), window.accent)
        self.assertEqual(window.full_frame.cget("bg"), window.border)
        self.assertIsNone(window._alert_job)

    def test_alarm_stage_offers_stop_and_snooze(self):
        window = self._make("alarm")
        self.assertEqual(window.title_label.cget("text"), "时间到了")
        self.assertEqual(window.primary.cget("text"), "停止")
        self.assertEqual(window.secondary.cget("text"), "5 分钟后提醒")

    def test_expanded_card_draws_rings_and_stops_when_collapsed(self):
        window = self._make()
        self.assertIsNotNone(window._animation_job)
        self.assertTrue(window.canvas.find_all())
        window.collapse()
        self.assertIsNone(window._animation_job)
        window.expand()
        self.assertIsNotNone(window._animation_job)

    def test_ring_centre_shows_a_second_countdown(self):
        window = self._make()
        texts = [
            window.canvas.itemcget(item, "text")
            for item in window.canvas.find_all()
            if window.canvas.type(item) == "text"
        ]
        self.assertTrue(any(re.fullmatch(r"\d+", text) for text in texts))

    def test_collapsed_orb_keeps_ripples_moving(self):
        window = self._make("scheduled", persistent=True)
        window.primary.invoke()
        self.assertTrue(window.collapsed)
        self.assertIsNotNone(window._tab_job)
        window.expand()
        self.assertIsNone(window._tab_job)

    def test_idle_orb_shows_standby(self):
        self.window = AlarmAlertWindow(
            self.root,
            {"alarm_label": "闹钟", "alarm_style": "soft", "alarm_sound": False},
            "idle", None, lambda _stage: None,
            lambda _stage, _minutes: None, persistent=True,
            start_collapsed=True)
        self.assertTrue(self.window.collapsed)
        texts = [
            self.window.handle_frame.itemcget(item, "text")
            for item in self.window.handle_frame.find_all()
            if self.window.handle_frame.type(item) == "text"
        ]
        self.assertEqual(texts, ["休息"])
        self.window.expand()
        self.assertFalse(self.window.collapsed)
        self.assertEqual(self.window.title_label.cget("text"), "闹钟未设置")
        self.window.primary.invoke()
        self.assertTrue(self.window.collapsed)

    def test_scheduled_card_collapses_and_reopens(self):
        window = self._make("scheduled", persistent=True)
        self.assertEqual(window.title_label.cget("text"), "闹钟已设置")
        self.assertEqual(window.primary.cget("text"), "知道了")
        self.assertEqual(window.secondary.winfo_manager(), "")
        window.primary.invoke()
        self.assertTrue(window.collapsed)
        self.assertFalse(window._closed)
        self.assertEqual(window.full_frame.winfo_manager(), "")
        self.assertEqual(window.handle_frame.winfo_manager(), "pack")
        self.assertEqual(window._handle_click(), "break")
        self.assertFalse(window.collapsed)
        self.assertEqual(window.full_frame.winfo_manager(), "pack")
        self.assertEqual(window.handle_frame.winfo_manager(), "")

    def test_collapsed_orb_can_be_dragged_without_expanding(self):
        window = self._make("scheduled", persistent=True)
        window.collapse()
        window._cancel_slide()
        window._current_x = 500
        window._target_y = 120
        window.window.geometry("100x100+500+120")
        window._handle_press(SimpleNamespace(x_root=510, y_root=130))
        window._handle_drag(SimpleNamespace(x_root=560, y_root=170))
        window._handle_release(SimpleNamespace(x_root=560, y_root=170))
        self.assertTrue(window.collapsed)
        self.assertTrue(window._custom_position)
        self.assertEqual((window._current_x, window._target_y), (550, 160))

    def test_orb_click_without_moving_expands(self):
        window = self._make("scheduled", persistent=True)
        window.collapse()
        window._cancel_slide()
        window._current_x = 500
        window._target_y = 120
        window.window.geometry("100x100+500+120")
        window._handle_press(SimpleNamespace(x_root=510, y_root=130))
        window._handle_release(SimpleNamespace(x_root=510, y_root=130))
        self.assertFalse(window.collapsed)

    def test_expanded_card_drag_keeps_custom_position(self):
        window = self._make()
        window._current_x = 1000
        window._target_y = 100
        window.window.geometry("420x363+1000+100")
        window._drag_start(SimpleNamespace(x_root=1010, y_root=110))
        window._drag_move(SimpleNamespace(x_root=1060, y_root=150))
        window._drag_end()
        self.assertTrue(window._custom_position)
        self.assertEqual((window._current_x, window._target_y), (1050, 140))

    def test_custom_position_survives_collapse_and_expand(self):
        window = self._make()
        window._custom_position = True
        window._current_x = 500
        window._target_y = 100
        window.collapse()
        window._cancel_slide()
        self.assertEqual((window._current_x, window._target_y), (500, 100))
        window.expand()
        window._cancel_slide()
        self.assertEqual((window._current_x, window._target_y), (500, 100))

    def test_idle_orb_click_opens_clipboard_instead_of_card(self):
        calls = []
        self.window = AlarmAlertWindow(
            self.root,
            {"alarm_label": "闹钟", "alarm_style": "soft", "alarm_sound": False},
            "idle", None, lambda _stage: None,
            lambda _stage, _minutes: None, persistent=True,
            start_collapsed=True, on_show_clipboard=lambda: calls.append(True))
        self.window._handle_press(SimpleNamespace(x_root=100, y_root=100))
        self.window._handle_release(SimpleNamespace(x_root=100, y_root=100))
        self.assertEqual(calls, [True])
        self.assertTrue(self.window.collapsed)

    def test_collapsed_tab_shows_the_second_countdown(self):
        window = self._make("scheduled", persistent=True)
        window.primary.invoke()
        texts = [
            window.handle_frame.itemcget(item, "text")
            for item in window.handle_frame.find_all()
            if window.handle_frame.type(item) == "text"
        ]
        self.assertTrue(any(re.fullmatch(r"\d+", text) for text in texts))

    def test_persistent_alarm_ack_hands_off_without_closing(self):
        window = self._make("alarm", persistent=True)
        window._ack()
        self.assertFalse(window._closed)

    def test_scheduled_card_auto_collapses_after_preview(self):
        window = self._make("scheduled", persistent=True)
        self.assertIsNotNone(window._auto_collapse_job)
        window._auto_collapse()
        self.assertTrue(window.collapsed)
        self.assertFalse(window._closed)

    def test_early_stage_also_auto_collapses(self):
        window = self._make("scheduled", persistent=True)
        window.apply("early", window.target)
        self.assertIsNotNone(window._auto_collapse_job)
        self.assertFalse(window.collapsed)
        window._auto_collapse()
        self.assertTrue(window.collapsed)

    def test_alarm_stage_cancels_auto_collapse(self):
        window = self._make("scheduled", persistent=True)
        window.apply("alarm", window.target)
        self.assertIsNone(window._auto_collapse_job)
        self.assertFalse(window.collapsed)

    def test_preview_acknowledge_closes_the_card(self):
        window = self._make("early")
        window._ack()
        self.assertTrue(window._closed)


@unittest.skipIf(tk is None, "Tkinter is unavailable")
class AlarmSchedulerTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except tk.TclError as exc:
            raise unittest.SkipTest(f"Tk display unavailable: {exc}") from exc
        self.scheduler = None

    def tearDown(self):
        if self.scheduler is not None:
            self.scheduler.stop()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def test_scheduler_keeps_a_card_and_auto_expands_for_the_alarm(self):
        target = (datetime.now() + timedelta(minutes=10)).replace(microsecond=0)
        config = {
            "alarm_enabled": True,
            "alarm_label": "测试闹钟",
            "alarm_date": target.strftime("%Y-%m-%d"),
            "alarm_time": target.strftime("%H:%M:%S"),
            "alarm_lead_minutes": 0,
            "alarm_repeat": "once",
            "alarm_style": "soft",
            "alarm_sound": False,
        }
        self.scheduler = AlarmScheduler(self.root, config, config.update)
        self.scheduler.start()
        card = self.scheduler._overlay
        self.assertIsNotNone(card)
        self.assertEqual(card.stage, "scheduled")
        card._ack()
        self.assertTrue(card.collapsed)
        self.scheduler._show("early", target)
        self.assertEqual(card.stage, "early")
        self.assertFalse(card.collapsed)
        card._ack()
        self.assertTrue(card.collapsed)
        self.scheduler._show("alarm", target)
        self.assertIs(self.scheduler._overlay, card)
        self.assertFalse(card.collapsed)
        self.assertEqual(card.stage, "alarm")

    def test_service_start_shows_an_idle_orb_without_an_alarm(self):
        config = {
            "alarm_enabled": False,
            "alarm_label": "闹钟",
            "alarm_date": datetime.now().strftime("%Y-%m-%d"),
            "alarm_time": "09:00:00",
            "alarm_repeat": "once",
            "alarm_style": "soft",
            "alarm_sound": False,
        }
        self.scheduler = AlarmScheduler(self.root, config, config.update)
        self.scheduler.start()
        card = self.scheduler._overlay
        self.assertIsNotNone(card)
        self.assertEqual(card.stage, "idle")
        self.assertTrue(card.collapsed)

    def test_stop_keeps_the_same_orb_and_returns_to_idle(self):
        target = (datetime.now() + timedelta(minutes=10)).replace(microsecond=0)
        config = {
            "alarm_enabled": True,
            "alarm_label": "测试闹钟",
            "alarm_date": target.strftime("%Y-%m-%d"),
            "alarm_time": target.strftime("%H:%M:%S"),
            "alarm_lead_minutes": 0,
            "alarm_repeat": "once",
            "alarm_style": "soft",
            "alarm_sound": False,
        }
        self.scheduler = AlarmScheduler(self.root, config, config.update)
        self.scheduler.start()
        card = self.scheduler._overlay
        self.scheduler._show("alarm", target)
        card._ack()
        self.assertIs(self.scheduler._overlay, card)
        self.assertFalse(card._closed)
        self.assertTrue(card.collapsed)
        self.assertEqual(card.stage, "idle")
        self.assertFalse(config["alarm_enabled"])

    def test_snooze_keeps_the_same_orb_counting_down(self):
        target = (datetime.now() + timedelta(minutes=10)).replace(microsecond=0)
        config = {
            "alarm_enabled": True,
            "alarm_label": "测试闹钟",
            "alarm_date": target.strftime("%Y-%m-%d"),
            "alarm_time": target.strftime("%H:%M:%S"),
            "alarm_lead_minutes": 0,
            "alarm_repeat": "once",
            "alarm_style": "soft",
            "alarm_sound": False,
        }
        self.scheduler = AlarmScheduler(self.root, config, config.update)
        self.scheduler.start()
        card = self.scheduler._overlay
        self.scheduler._show("alarm", target)
        card._snooze(5)
        self.assertIs(self.scheduler._overlay, card)
        self.assertFalse(card._closed)
        self.assertTrue(card.collapsed)
        self.assertEqual(card.stage, "snoozed")
        self.assertIsNotNone(self.scheduler._snooze_at)


@unittest.skipIf(tk is None, "Tkinter is unavailable")
class AlarmSettingsWindowTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except tk.TclError as exc:
            raise unittest.SkipTest(f"Tk display unavailable: {exc}") from exc
        self.config = {}
        self.saved = {}
        self.window = AlarmSettingsWindow(
            self.root, self.config, self.saved.update)

    def tearDown(self):
        try:
            picker = getattr(self.window, "datetime_picker", None)
            popup = getattr(picker, "_popup", None)
            if popup is not None:
                popup.close()
            self.window.close()
            self.root.destroy()
        except (tk.TclError, AttributeError):
            pass

    def test_settings_page_saves_a_future_one_shot_alarm(self):
        self.window.enabled_var.set(True)
        self.window.date_var.set(
            (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d"))
        self.window.time_var.set("12:34")
        self.window.lead_var.set("5")
        self.assertTrue(self.window.save())
        self.assertEqual(self.saved["alarm_time"], "12:34:00")
        self.assertEqual(self.saved["alarm_repeat"], "once")
        self.assertTrue(self.saved["alarm_enabled"])

    def test_settings_page_keeps_selected_seconds(self):
        self.window.enabled_var.set(True)
        self.window.date_var.set(
            (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d"))
        self.window.time_var.set("12:34:56")
        self.assertTrue(self.window.save())
        self.assertEqual(self.saved["alarm_time"], "12:34:56")

    def test_preview_without_an_enabled_alarm_shows_idle_card(self):
        self.window.enabled_var.set(False)
        with patch("lumina.alarm_window.AlarmAlertWindow") as factory:
            self.window.preview()
        args, _kwargs = factory.call_args
        self.assertEqual(args[2], "idle")
        self.assertIsNone(args[3])

    def test_preview_with_an_enabled_alarm_shows_early_card(self):
        self.window.enabled_var.set(True)
        self.window.date_var.set(
            (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d"))
        self.window.time_var.set("12:34:56")
        with patch("lumina.alarm_window.AlarmAlertWindow") as factory:
            self.window.preview()
        args, _kwargs = factory.call_args
        self.assertEqual(args[2], "early")
        self.assertIsNotNone(args[3])

    def test_picker_selection_writes_back_date_and_time(self):
        moment = (datetime.now() + timedelta(days=2)).replace(microsecond=0)
        self.window.datetime_picker.set_value(moment)
        self.assertEqual(
            self.window.date_var.get(), moment.strftime("%Y-%m-%d"))
        self.assertEqual(
            self.window.time_var.get(), moment.strftime("%H:%M:%S"))
        self.assertEqual(
            self.window.datetime_picker.display_var.get(),
            moment.strftime("%Y-%m-%d %H:%M:%S"))

    def test_calendar_popup_confirm_keeps_hour_minute_second(self):
        base = (datetime.now() + timedelta(days=3)).replace(
            hour=10, minute=20, second=30, microsecond=0)
        self.window.datetime_picker.set_value(base)
        self.window.datetime_picker.open_picker()
        popup = self.window.datetime_picker._popup
        self.assertIsNotNone(popup)
        popup.hour_var.set("23")
        popup.minute_var.set("58")
        popup.second_var.set("57")
        popup._confirm()
        self.assertEqual(self.window.time_var.get(), "23:58:57")
        self.assertEqual(
            self.window.date_var.get(), base.strftime("%Y-%m-%d"))
        self.assertEqual(
            self.window.datetime_picker.display_var.get(),
            f"{base:%Y-%m-%d} 23:58:57")

    def test_date_field_click_or_enter_opens_the_calendar(self):
        self.assertIn("_open_from_entry", self.window.date_entry.bind("<Button-1>"))
        self.assertIn("_open_from_entry", self.window.date_entry.bind("<Return>"))
        self.window.datetime_picker._open_from_entry()
        popup = self.window.datetime_picker._popup
        self.assertIsNotNone(popup)
        popup.close()

    def test_repeating_alarm_does_not_require_a_date(self):
        self.window.repeat_var.set("每天")
        self.assertEqual(self.window.date_entry.cget("state"), "disabled")


if __name__ == "__main__":
    unittest.main()
