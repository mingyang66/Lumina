"""分栏比例与尺寸记忆的纯逻辑测试（不需要 Tk）。

这些函数是面板拖拽的“算术层”：比例如何夹紧、两栏宽度怎么算、像素尺寸
怎么换算回 config 的逻辑单位、多大的变化才值得写盘。UI 只负责接线，
所以这里可以完全脱离窗口把边界测干净。
"""
import types
import unittest

from lumina.history_panel import (
    DEFAULT_SPLIT_RATIO,
    SPLIT_CHROME,
    SPLIT_MAX_RATIO,
    SPLIT_MIN_RATIO,
    HistoryPanel,
    logical_size,
    resolve_split_ratio,
    size_worth_saving,
    split_widths,
)


class ResolveSplitRatioTests(unittest.TestCase):
    def test_missing_key_uses_default(self):
        self.assertEqual(resolve_split_ratio({}), DEFAULT_SPLIT_RATIO)
        self.assertEqual(resolve_split_ratio(None), DEFAULT_SPLIT_RATIO)

    def test_blank_value_uses_default(self):
        for raw in ("", None, "   "):
            with self.subTest(raw=raw):
                self.assertEqual(resolve_split_ratio({"panel_split": raw}),
                                 DEFAULT_SPLIT_RATIO)

    def test_invalid_values_fall_back(self):
        for raw in ("abc", [], {}, object()):
            with self.subTest(raw=raw):
                self.assertEqual(resolve_split_ratio({"panel_split": raw}),
                                 DEFAULT_SPLIT_RATIO)

    def test_out_of_range_is_clamped(self):
        self.assertEqual(resolve_split_ratio({"panel_split": 0.9}), SPLIT_MAX_RATIO)
        self.assertEqual(resolve_split_ratio({"panel_split": 0.01}), SPLIT_MIN_RATIO)
        self.assertEqual(resolve_split_ratio({"panel_split": -5}), SPLIT_MIN_RATIO)
        self.assertEqual(resolve_split_ratio({"panel_split": 99}), SPLIT_MAX_RATIO)

    def test_valid_value_is_kept(self):
        self.assertAlmostEqual(resolve_split_ratio({"panel_split": 0.5}), 0.5, places=6)
        self.assertAlmostEqual(resolve_split_ratio({"panel_split": "0.45"}), 0.45, places=6)

    def test_broken_config_does_not_raise(self):
        class Broken:
            def get(self, *_args):
                raise RuntimeError("boom")

        self.assertEqual(resolve_split_ratio(Broken()), DEFAULT_SPLIT_RATIO)


class SplitWidthsTests(unittest.TestCase):
    TOTAL = 1304          # 1360 - 56，与 _build_compact 的 body_w 同口径
    DPI = 1.0

    def widths(self, total=TOTAL, dpi=DPI, ratio=DEFAULT_SPLIT_RATIO):
        return split_widths(total, dpi, ratio, HistoryPanel.LEFT_MIN,
                            HistoryPanel.LEFT_MAX, HistoryPanel.DETAIL_MIN)

    def test_left_and_detail_follow_the_ratio(self):
        left, detail = self.widths()
        avail = self.TOTAL - SPLIT_CHROME
        self.assertAlmostEqual(left / avail, DEFAULT_SPLIT_RATIO, delta=0.01)
        self.assertEqual(left + detail, avail)

    def test_left_is_clamped_by_min_and_max(self):
        left, _detail = self.widths(ratio=0.01)
        self.assertEqual(left, HistoryPanel.LEFT_MIN)
        left, _detail = self.widths(total=4000)
        self.assertEqual(left, HistoryPanel.LEFT_MAX)

    def test_detail_never_below_its_minimum(self):
        for total in (900, 1000, 1304, 2400):
            with self.subTest(total=total):
                _left, detail = self.widths(total=total)
                self.assertGreaterEqual(detail, HistoryPanel.DETAIL_MIN)

    def test_dpi_scales_the_constraints(self):
        left, _detail = self.widths(dpi=2.0, ratio=0.01)
        self.assertEqual(left, HistoryPanel.LEFT_MIN * 2)

    def test_degenerate_total_stays_positive(self):
        left, detail = self.widths(total=100)
        self.assertGreater(left, 0)
        self.assertGreater(detail, 0)

    def test_default_ratio_alias_matches_class(self):
        # 既有代码与测试按 LEFT_RATIO 取默认值，别名不能丢。
        self.assertEqual(HistoryPanel.LEFT_RATIO, DEFAULT_SPLIT_RATIO)


class SizeMemoryTests(unittest.TestCase):
    def test_logical_size_round_trip(self):
        self.assertEqual(logical_size(2040, 1260, 1.5), (1360, 840))
        self.assertEqual(logical_size(1360, 840, 1.0), (1360, 840))

    def test_logical_size_guards_bad_dpi(self):
        self.assertEqual(logical_size(1360, 840, 0), (1360, 840))
        self.assertEqual(logical_size(1360, 840, None), (1360, 840))

    def test_threshold_ignores_small_changes(self):
        self.assertFalse(size_worth_saving((1360, 840), (1356, 837)))
        self.assertTrue(size_worth_saving((1370, 840), (1360, 840)))
        self.assertTrue(size_worth_saving((1360, 830), (1360, 840)))

    def test_missing_baseline_always_saves(self):
        self.assertTrue(size_worth_saving((1360, 840), None))
        self.assertTrue(size_worth_saving((1360, 840), ()))


class _FakeBody:
    def __init__(self, width):
        self.width = width

    def winfo_width(self):
        return self.width


class _FakeWidget:
    def __init__(self):
        self.options = {}
        self.grabbed = False

    def configure(self, **kwargs):
        self.options.update(kwargs)

    def grab_set(self):
        self.grabbed = True

    def grab_release(self):
        self.grabbed = False


class SplitDragHandlerTests(unittest.TestCase):
    """拖拽处理器只依赖少数控件/属性，用替身把算术路径跑一遍。"""

    WIDTH = 1304

    def make_panel(self):
        panel = HistoryPanel.__new__(HistoryPanel)
        panel._dpi = 1.0
        panel._split_ratio = DEFAULT_SPLIT_RATIO
        panel._body_split = _FakeBody(self.WIDTH)
        panel._left_frame = _FakeWidget()
        panel._detail = types.SimpleNamespace(frame=_FakeWidget(), row=None)
        panel._split_handle = _FakeWidget()
        panel._split_dragging = False
        panel.ui = types.SimpleNamespace()          # 无 actions → _persist 静默
        return panel

    @staticmethod
    def event(x_root):
        return types.SimpleNamespace(x_root=x_root)

    def test_press_starts_drag_and_grabs_pointer(self):
        panel = self.make_panel()
        panel._split_handle_press(self.event(500))
        self.assertTrue(panel._split_dragging)
        self.assertEqual(panel._split_drag_origin, (500, DEFAULT_SPLIT_RATIO))
        self.assertTrue(panel._split_handle.grabbed)

    def test_motion_moves_the_ratio_and_both_columns(self):
        panel = self.make_panel()
        before = panel._left_width(self.WIDTH)
        panel._split_handle_press(self.event(500))
        panel._split_handle_motion(self.event(600))
        self.assertGreater(panel._split_ratio, DEFAULT_SPLIT_RATIO)
        self.assertGreater(panel._left_width(self.WIDTH), before)
        self.assertEqual(panel._left_frame.options.get("width"),
                         panel._left_width(self.WIDTH))
        self.assertEqual(panel._detail.frame.options.get("width"),
                         panel._detail_width(self.WIDTH))

    def test_motion_is_clamped_to_the_allowed_band(self):
        panel = self.make_panel()
        panel._split_handle_press(self.event(500))
        panel._split_handle_motion(self.event(-5000))
        self.assertEqual(panel._left_width(self.WIDTH), HistoryPanel.LEFT_MIN)
        panel._split_handle_motion(self.event(5000))
        avail = self.WIDTH - SPLIT_CHROME
        expected_max = min(HistoryPanel.LEFT_MAX, avail - HistoryPanel.DETAIL_MIN)
        self.assertEqual(panel._left_width(self.WIDTH), expected_max)

    def test_draggable_band_is_wide_enough(self):
        """可拖范围不能只剩一点点：用户明确抱怨过「只能微调」。"""
        panel = self.make_panel()
        panel._split_handle_press(self.event(500))
        panel._split_handle_motion(self.event(-5000))
        lowest = panel._split_ratio
        panel._split_handle_motion(self.event(5000))
        highest = panel._split_ratio
        self.assertLess(lowest, 0.30)
        self.assertGreater(highest, 0.60)
        self.assertGreater(highest - lowest, 0.30)

    def test_release_ends_drag_and_releases_pointer(self):
        panel = self.make_panel()
        panel._split_handle_press(self.event(500))
        panel._split_handle_motion(self.event(560))
        panel._split_handle_release()
        self.assertFalse(panel._split_dragging)
        self.assertFalse(panel._split_handle.grabbed)

    def test_motion_without_press_does_nothing(self):
        panel = self.make_panel()
        panel._split_handle_motion(self.event(900))
        self.assertEqual(panel._split_ratio, DEFAULT_SPLIT_RATIO)

    def test_double_click_resets_to_default(self):
        panel = self.make_panel()
        panel._split_handle_press(self.event(500))
        panel._split_handle_motion(self.event(650))
        self.assertNotEqual(panel._split_ratio, DEFAULT_SPLIT_RATIO)
        panel._split_handle_reset()
        self.assertEqual(panel._split_ratio, DEFAULT_SPLIT_RATIO)
        self.assertEqual(panel._left_frame.options.get("width"),
                         panel._left_width(self.WIDTH))

    def test_degenerate_body_width_does_not_move(self):
        panel = self.make_panel()
        panel._body_split = _FakeBody(0)
        panel._split_handle_press(self.event(500))
        panel._split_handle_motion(self.event(700))
        self.assertEqual(panel._split_ratio, DEFAULT_SPLIT_RATIO)


class SplitHitBandTests(unittest.TestCase):
    """分隔线只有 1px，但按在附近也要能抓到；其余区域仍拖窗口。"""

    def make_panel(self):
        panel = HistoryPanel.__new__(HistoryPanel)
        panel._dpi = 1.0
        panel._split_ratio = DEFAULT_SPLIT_RATIO
        panel._body_split = types.SimpleNamespace(
            winfo_width=lambda: 1304, winfo_rootx=lambda: 100)
        panel._split_handle = types.SimpleNamespace(
            winfo_rootx=lambda: 110, winfo_width=lambda: 1,
            grab_set=lambda: None, grab_release=lambda: None)
        panel._left_frame = _FakeWidget()
        panel._detail = types.SimpleNamespace(frame=_FakeWidget(), row=None)
        panel._split_dragging = False
        panel.ui = types.SimpleNamespace()
        panel.calls = []
        panel._hdr_press = lambda event: panel.calls.append(("press", event.x)) or "break"
        panel._hdr_move = lambda event: panel.calls.append(("move", event.x)) or "break"
        return panel

    @staticmethod
    def event(x):
        return types.SimpleNamespace(x=x, x_root=100 + x)

    def test_line_position_uses_widget_geometry(self):
        self.assertEqual(self.make_panel()._split_line_x(), 10)

    def test_press_near_line_starts_split_drag(self):
        panel = self.make_panel()
        self.assertTrue(panel._near_split(12))
        panel._split_body_press(self.event(12))
        self.assertTrue(panel._split_dragging)
        self.assertEqual(panel.calls, [])

    def test_press_away_from_line_drags_window(self):
        panel = self.make_panel()
        self.assertFalse(panel._near_split(200))
        panel._split_body_press(self.event(200))
        self.assertFalse(panel._split_dragging)
        self.assertEqual(panel.calls, [("press", 200)])

    def test_motion_routes_to_the_active_gesture(self):
        panel = self.make_panel()
        panel._split_body_press(self.event(11))
        panel._split_body_motion(self.event(60))
        self.assertEqual(panel.calls, [])          # 没走窗口拖动
        self.assertGreater(panel._split_ratio, DEFAULT_SPLIT_RATIO)

    def test_motion_without_split_drag_drags_window(self):
        panel = self.make_panel()
        panel._split_body_motion(self.event(60))
        self.assertEqual(panel.calls, [("move", 60)])

    def test_missing_geometry_is_not_near(self):
        panel = self.make_panel()
        panel._body_split = None
        self.assertIsNone(panel._split_line_x())
        self.assertFalse(panel._near_split(10))


if __name__ == "__main__":
    unittest.main()
