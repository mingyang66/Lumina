"""闹钟弹框「紧凑化」的补充回归测试。

原有的 ``tests/test_alarm_window.py`` 覆盖了行为契约（倒计时、折叠、拖拽、
调度器），这里只补这次改造新增的三类约束，两者互补、互不覆盖：

1. **卡片是紧凑的**：动画画布压到 96 高之后，整卡请求高度不超过 260。
2. **长名称不撑宽卡片**：闹钟名超过 ``CAPTION_MAX`` 时标题栏截断，宽度恒在 WIDTH 以内。
3. **误点不会永久关闹钟**：``×`` / Esc 在 alarm 阶段走「稍后提醒」，其余阶段收起；
   非常驻弹框仍然是关闭；另外覆盖 Enter/S 快捷键与可配置的稍后提醒时长。
"""

import sys
import tkinter as tk
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lumina.alarm_window import (  # noqa: E402
    PALETTES,
    SNOOZE_MINUTES,
    AlarmAlertWindow,
    PillButton,
)

try:
    _PROBE = tk.Tk()
    _PROBE.withdraw()
    _PROBE.destroy()
    TK_OK = True
except Exception:  # pragma: no cover - 仅在无图形环境时触发
    TK_OK = False

SKIP_REASON = '本机 Tk 不可用'


class AlarmWindowHarness(unittest.TestCase):
    """建窗与回调记录，供下面两个测试类共用（本身不含用例）。"""

    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.errors = []
        self.root.report_callback_exception = (
            lambda *args: self.errors.append(args))
        self.acked = []
        self.snoozed = []
        self.window = None

    def tearDown(self):
        if self.window is not None:
            self.window.destroy_without_callback()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def build(self, stage='alarm', persistent=True, **config):
        base = {
            'alarm_label': '闹钟',
            'alarm_style': 'neon',
            'alarm_sound': False,
            'alarm_snooze_minutes': 5,
        }
        base.update(config)
        target = datetime.now() + timedelta(minutes=1)
        self.window = AlarmAlertWindow(
            self.root, base, stage, target,
            on_acknowledge=lambda s: self.acked.append(s),
            on_snooze=lambda s, m: self.snoozed.append((s, m)),
            persistent=persistent)
        self.window.window.update_idletasks()
        return self.window


@unittest.skipUnless(TK_OK, SKIP_REASON)
class AlarmPopupCompactTests(AlarmWindowHarness):
    """第一批：紧凑化与误点保护。"""

    # ---------- 尺寸 ----------

    def test_canvas_height_within_design_bound(self):
        """画布高度上限 160：既要让涟漪够大（144），也不要退回最早的 200。"""
        self.assertLessEqual(AlarmAlertWindow.CANVAS_HEIGHT, 160)
        self.assertGreaterEqual(AlarmAlertWindow.CANVAS_HEIGHT, 100)

    def test_card_height_stays_compact(self):
        win = self.build()
        # 阈值按当前 DPI 换算：逻辑高度上限 260。
        self.assertLessEqual(win.window.winfo_reqheight(),
                             win._s(320) + 4)

    def test_long_label_does_not_widen_card(self):
        long_name = '这是一个特别特别长的闹钟名称用来验证卡片不会被撑宽的问题'
        win = self.build(alarm_label=long_name)
        self.assertLessEqual(win.window.winfo_reqwidth(),
                             win._s(AlarmAlertWindow.WIDTH) + 2)
        caption = win.label_caption.cget('text')
        self.assertLessEqual(len(caption), AlarmAlertWindow.CAPTION_MAX)
        self.assertTrue(caption.endswith('…'))

    # ---------- 语义 ----------

    def test_dismiss_on_alarm_snoozes_instead_of_acking(self):
        win = self.build(stage='alarm')
        win._dismiss_view()
        self.assertEqual(self.acked, [])
        self.assertEqual(self.snoozed, [('alarm', 5)])

    def test_escape_on_scheduled_only_collapses(self):
        win = self.build(stage='scheduled')
        win._dismiss_view()
        self.assertTrue(win.collapsed)
        self.assertEqual(self.acked, [])

    def test_non_persistent_dismiss_still_acks(self):
        win = self.build(stage='alarm', persistent=False)
        win._dismiss_view()
        self.assertEqual(self.acked, ['alarm'])

    def test_key_bindings_exist(self):
        win = self.build()
        for sequence in ('<Return>', '<KP_Enter>', '<s>', '<S>', '<Escape>'):
            self.assertTrue(win.window.bind(sequence), f'{sequence} 未绑定')

    def test_primary_invoke_acks(self):
        win = self.build(stage='alarm')
        win._invoke_primary()
        self.assertEqual(self.acked, ['alarm'])

    # ---------- 稍后提醒时长 ----------

    def test_snooze_button_follows_config(self):
        win = self.build(stage='alarm', alarm_snooze_minutes=15)
        self.assertEqual(win.secondary.cget('text'), '15 分钟后提醒')
        win.secondary.invoke()
        self.assertEqual(self.snoozed, [('alarm', 15)])

    def test_snooze_minutes_fall_back_and_clamp(self):
        win = self.build(alarm_snooze_minutes='abc')
        self.assertEqual(win.snooze_minutes(), 5)
        win.destroy_without_callback()
        win = self.build(alarm_snooze_minutes=0)
        self.assertEqual(win.snooze_minutes(), 1)
        win.destroy_without_callback()
        win = self.build(alarm_snooze_minutes=999)
        self.assertEqual(win.snooze_minutes(), 60)

    # ---------- 配色 ----------

    def test_button_colors_are_derived_from_palette(self):
        for style in PALETTES:
            with self.subTest(style=style):
                win = self.build(alarm_style=style)
                self.assertNotEqual(win.secondary.cget('bg'), '#37334d')
                self.assertNotEqual(win.primary.cget('fg'), '#171521')
                win.destroy_without_callback()


class SnoozeMinutesConstantTests(unittest.TestCase):
    def test_default_is_offered(self):
        self.assertIn(AlarmAlertWindow.SNOOZE_DEFAULT, SNOOZE_MINUTES)


@unittest.skipUnless(TK_OK, SKIP_REASON)
class AlarmPopupAdaptationTests(AlarmWindowHarness):
    """第二批：DPI 缩放、自绘按钮 API 兼容、多屏工作区、稍后时长菜单。"""

    def test_dpi_is_sane(self):
        win = self.build()
        self.assertGreaterEqual(win._dpi, 0.8)
        self.assertLessEqual(win._dpi, 3.0)

    def test_canvas_dimensions_follow_dpi(self):
        win = self.build()
        self.assertEqual(win._canvas_w, win._s(AlarmAlertWindow.CANVAS_WIDTH))
        self.assertEqual(win._canvas_h, win._s(AlarmAlertWindow.CANVAS_HEIGHT))
        self.assertEqual(win._handle_w, win._s(AlarmAlertWindow.HANDLE_WIDTH))

    def test_buttons_are_self_drawn_and_keep_button_api(self):
        win = self.build(stage='alarm')
        for widget in (win.primary, win.secondary):
            self.assertIsInstance(widget, PillButton)
            self.assertTrue(widget.winfo_manager())
            self.assertTrue(widget.cget('text'))
            self.assertEqual(widget.cget('bg'), widget._bg)
        win.secondary.grid_remove()
        self.assertEqual(win.secondary.winfo_manager(), '')

    def test_primary_invoke_runs_its_command(self):
        win = self.build(stage='alarm')
        self.assertEqual(win.primary.invoke(), None)
        self.assertEqual(self.acked, ['alarm'])

    def test_monitor_work_area_is_usable(self):
        win = self.build()
        x, y, width, height = win._monitor_work_area()
        self.assertGreater(width, 0)
        self.assertGreater(height, 0)
        self.assertIsInstance(x, int)
        self.assertIsInstance(y, int)

    def test_snooze_button_has_right_click_menu(self):
        win = self.build(stage='alarm')
        self.assertTrue(win.secondary.bind('<Button-3>'))

    def test_button_hover_and_click_raise_no_callback_error(self):
        """回归：PillButton 少初始化字段时，按下/松开会在 _draw 里抛异常，
        表现为“按钮能点但没反应”。"""
        win = self.build(stage='alarm')
        button = win.primary
        win.window.update()          # 未映射的控件收到 event_generate 也不会派发
        self.assertTrue(button.winfo_ismapped())
        button.event_generate('<Enter>')
        button.event_generate('<Leave>')
        button.event_generate('<ButtonPress-1>', x=5, y=5)
        button.event_generate('<ButtonRelease-1>', x=5, y=5)
        win.window.update()
        self.assertEqual(self.errors, [])

    def test_real_click_on_snooze_collapses_the_card(self):
        """用真实 press/release 事件点「稍后提醒」，卡片必须收起。"""
        win = self.build(stage='alarm')
        win.on_snooze = lambda stage, minutes: win.apply(
            'snoozed', datetime.now() + timedelta(minutes=minutes),
            collapsed=True)
        button = win.secondary
        win.window.update()
        button.update_idletasks()
        self.assertTrue(button.winfo_ismapped())
        x, y = button.winfo_width() // 2, button.winfo_height() // 2
        button.event_generate('<ButtonPress-1>', x=x, y=y)
        button.event_generate('<ButtonRelease-1>', x=x, y=y)
        win.window.update()
        self.assertTrue(win.collapsed)
        self.assertEqual(self.errors, [])

    def test_ripple_fills_the_canvas(self):
        win = self.build()
        max_radius = win._s(36 + 30)
        self.assertLessEqual(max_radius, win._canvas_h / 2 + 1)
        self.assertGreaterEqual(max_radius, win._canvas_h / 2 - 6)


if __name__ == '__main__':
    unittest.main()
