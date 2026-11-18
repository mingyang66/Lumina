"""闹钟设置弹框第一批优化的回归测试。

守四件事：

1. **状态行跟着表单走**：改时间后立刻反映新的「下次提醒」，并标「未保存」；
2. **错误行内提示**：非法输入不弹 messagebox，状态行给中文原因；
3. **预览单例**：连点「预览提醒」不会叠出多张卡片；
4. **控件统一**：下拉是自绘 ``PickField``，窗口里不再有 ttk.Combobox。
"""

import sys
import tkinter as tk
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lumina.alarm_window import (  # noqa: E402
    AlarmSettingsWindow,
    PickField,
    monitor_work_area,
)

try:
    _PROBE = tk.Tk()
    _PROBE.withdraw()
    _PROBE.destroy()
    TK_OK = True
except Exception:  # pragma: no cover - 仅在无图形环境时触发
    TK_OK = False

SKIP_REASON = '本机 Tk 不可用'


def _tomorrow():
    return (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')


def _config(**overrides):
    base = {
        'alarm_enabled': True,
        'alarm_label': '晨会',
        'alarm_date': _tomorrow(),
        'alarm_time': '09:00:00',
        'alarm_lead_minutes': 5,
        'alarm_repeat': 'once',
        'alarm_style': 'neon',
        'alarm_sound': False,
        'alarm_snooze_minutes': 5,
    }
    base.update(overrides)
    return base


@unittest.skipUnless(TK_OK, SKIP_REASON)
class AlarmSettingsFirstBatchTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.errors = []
        self.root.report_callback_exception = (
            lambda *args: self.errors.append(args))
        self.saved = []
        self.config = _config()

    def tearDown(self):
        if getattr(self, 'window', None) is not None:
            self.window.close()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def build(self, config=None):
        self.window = AlarmSettingsWindow(
            self.root, config or self.config,
            on_save=lambda values: self.saved.append(values))
        self.window.window.update()
        return self.window

    # ---------- 状态行 ----------

    def test_status_uses_form_values_not_saved_config(self):
        win = self.build()
        self.assertIn('下次提醒', win.status.cget('text'))
        self.assertNotIn('未保存', win.status.cget('text'))
        win.time_var.set('10:30:00')
        text = win.status.cget('text')
        self.assertIn('10:30:00', text)
        self.assertIn('未保存', text)

    def test_invalid_value_reports_inline_without_dialog(self):
        win = self.build()
        win.lead_var.set('abc')
        self.assertTrue(win.status.cget('text').startswith('设置无效：'))
        self.assertIn('整数', win.status.cget('text'))
        with patch('tkinter.messagebox.showerror') as showerror:
            self.assertFalse(win.save())
            showerror.assert_not_called()
        self.assertEqual(self.saved, [])

    def test_saving_clears_dirty_marker(self):
        win = self.build()
        win.time_var.set('11:00:00')
        self.assertIn('未保存', win.status.cget('text'))
        self.assertTrue(win.save())
        self.assertEqual(len(self.saved), 1)
        self.assertEqual(self.saved[0]['alarm_time'], '11:00:00')
        self.assertNotIn('未保存', win.status.cget('text'))

    def test_save_callback_failure_is_reported_inline(self):
        win = self.build()

        def boom(_values):
            raise OSError('磁盘满了')

        win.on_save = boom
        with patch('tkinter.messagebox.showerror') as showerror:
            self.assertFalse(win.save())
            showerror.assert_not_called()
        self.assertIn('保存失败', win.status.cget('text'))
        self.assertIn('磁盘满了', win.status.cget('text'))

    # ---------- 预览 ----------

    def test_preview_keeps_only_one_card(self):
        win = self.build()
        win.preview()
        first = win._preview
        self.assertIsNotNone(first)
        win.preview()
        second = win._preview
        self.assertIsNot(second, first)
        self.assertFalse(first.alive())

    def test_close_destroys_preview(self):
        win = self.build()
        win.preview()
        preview = win._preview
        win.close()
        self.assertFalse(preview.alive())
        self.window = None

    # ---------- 控件 ----------

    def test_comboboxes_are_replaced_by_pickfield(self):
        win = self.build()
        stack = [win.window]
        combos, picks = 0, 0
        while stack:
            widget = stack.pop()
            stack.extend(widget.winfo_children())
            if widget.winfo_class() == 'TCombobox':
                combos += 1
            if isinstance(widget, PickField):
                picks += 1
        self.assertEqual(combos, 0)
        self.assertEqual(picks, 4)

    def test_pick_field_selection_updates_its_variable(self):
        win = self.build()
        stack = [win.window]
        field = None
        while stack and field is None:
            widget = stack.pop()
            stack.extend(widget.winfo_children())
            if isinstance(widget, PickField) and widget._var is win.lead_var:
                field = widget
        self.assertIsNotNone(field)
        field._choose('10')
        self.assertEqual(win.lead_var.get(), '10')

    def test_enter_binding_exists(self):
        win = self.build()
        self.assertTrue(win.window.bind('<Return>'))

    def test_style_colors_follow_palette(self):
        for style in ('neon', 'soft', 'minimal'):
            with self.subTest(style=style):
                win = self.build(_config(alarm_style=style))
                self.assertNotEqual(win.status.cget('fg'), '#aaa6bd')
                self.assertNotEqual(win.save_button.cget('bg'), '#171521')


# ---------- 第二批：DPI、居中、键盘 ----------

    def test_window_scales_with_dpi(self):
        with patch('lumina.alarm_window.detect_dpi', return_value=1.5):
            win = self.build()
        self.assertAlmostEqual(win._dpi, 1.5, places=3)
        self.assertEqual(win._s(20), 30)
        self.assertAlmostEqual(win.window.winfo_width(), 780, delta=8)

    def test_window_stays_inside_work_area(self):
        win = self.build()
        mx, my, mw, mh = monitor_work_area(win.window)
        x, y, width, height = win._target_geometry
        self.assertGreaterEqual(x, mx)
        self.assertGreaterEqual(y, my)
        self.assertLessEqual(x + width, mx + mw)
        self.assertLessEqual(y + height, my + mh)

    def test_pick_field_is_keyboard_reachable(self):
        win = self.build()
        stack, field = [win.window], None
        while stack and field is None:
            widget = stack.pop()
            stack.extend(widget.winfo_children())
            if isinstance(widget, PickField):
                field = widget
        self.assertIsNotNone(field)
        self.assertTrue(field.cget('takefocus'))
        self.assertTrue(field.bind('<Return>'))
        self.assertTrue(field.bind('<space>'))

    def test_first_entry_gets_initial_focus(self):
        win = self.build()
        win.window.update()
        # 无头环境下可能没有焦点，有的话必须是第一个输入框。
        self.assertIn(win.window.focus_get(), (None, win.label_entry))


if __name__ == '__main__':
    unittest.main()
