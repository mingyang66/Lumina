import ast
import io
import math
import os
from pathlib import Path
import shutil
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image, ImageFont


# 只加载实际源码中的类，隔离包初始化和剪贴板模块的导入副作用。
SOURCE_PATH = Path(__file__).parent / "lumina" / "region_selector.py"
SOURCE = SOURCE_PATH.read_text(encoding="utf-8")
CLASS_NODE = next(
    node for node in ast.parse(SOURCE).body
    if isinstance(node, ast.ClassDef) and node.name == "RegionSelector"
)
NAMESPACE = dict(
    Image=Image, ImageFont=ImageFont, io=io, math=math,
    os=os, time=time, shutil=shutil,
)
exec(compile(ast.Module(body=[CLASS_NODE], type_ignores=[]),
             str(SOURCE_PATH), "exec"), NAMESPACE)
RegionSelector = NAMESPACE["RegionSelector"]


class FakeTkWidget:
    def __init__(self, *args, **kwargs):
        self.items = {}
        self.bindings = {}
        self.jobs = {}
        self.next_id = 0
        self.var = kwargs.get("textvariable")
        self.destroyed = False

    def bind(self, sequence, callback, add=None):
        self.next_id += 1
        binding_id = str(self.next_id)
        self.bindings[binding_id] = (sequence, callback)
        return binding_id

    def unbind(self, sequence, binding_id):
        assert self.bindings[binding_id][0] == sequence
        del self.bindings[binding_id]

    def create(self, kind, *coords, **options):
        self.next_id += 1
        self.items[self.next_id] = [kind, coords, options]
        return self.next_id

    def create_image(self, *coords, **options):
        return self.create("image", *coords, **options)

    def create_line(self, *coords, **options):
        return self.create("line", *coords, **options)

    def create_rectangle(self, *coords, **options):
        return self.create("rectangle", *coords, **options)

    def create_oval(self, *coords, **options):
        return self.create("oval", *coords, **options)

    def itemconfigure(self, item_id, **options):
        self.items[item_id][2].update(options)

    def coords(self, item_id, *coords):
        self.items[item_id][1] = coords

    def bbox(self, item_id):
        _, coords, options = self.items[item_id]
        image = options.get("image")
        width = image.width if image is not None else 10
        height = image.height if image is not None else 20
        return coords[0], coords[1], coords[0] + width, coords[1] + height

    def gettags(self, item_id):
        return self.items[item_id][2].get("tags", ())

    def type(self, item_id):
        return self.items[item_id][0]

    def find_overlapping(self, *coords):
        # 当前用例只有一个文字标注；返回项供生产逻辑按标签过滤。
        return tuple(self.items)

    def delete(self, item_id):
        self.items.pop(item_id, None)

    def tag_raise(self, *args):
        pass

    def winfo_exists(self):
        return not self.destroyed

    def focus_set(self):
        pass

    def focus_force(self):
        pass

    def update_idletasks(self):
        pass

    def place(self, **kwargs):
        pass

    def configure(self, **kwargs):
        pass

    def after(self, delay, callback):
        self.next_id += 1
        self.jobs[self.next_id] = callback
        return self.next_id

    def after_cancel(self, job_id):
        del self.jobs[job_id]

    def destroy(self):
        self.destroyed = True


class FakeStringVar:
    def __init__(self, *args, value=""):
        self.value = value
        self.traces = {}

    def get(self):
        return self.value

    def set(self, value):
        self.value = value
        for callback in list(self.traces.values()):
            callback()

    def trace_add(self, mode, callback):
        self.traces["trace"] = callback
        return "trace"

    def trace_remove(self, mode, trace_id):
        del self.traces[trace_id]


def make_selector():
    selector = RegionSelector.__new__(RegionSelector)
    selector.canvas = FakeTkWidget()
    selector.win = FakeTkWidget()
    selector.ui = SimpleNamespace(_tk=SimpleNamespace(
        StringVar=FakeStringVar, Entry=FakeTkWidget, TclError=RuntimeError,
    ))
    selector._ImageTk = SimpleNamespace(PhotoImage=lambda image: image)
    state = dict(
        done=False, _annotations=[], _undo_stack=[], _redo=[], _edit_items=[],
        _oval_photos={}, _text_editor=None, _text_live_item=None,
        _text_commit=None, _text_cancel=None, _text_refresh=None,
        _text_edit_index=None, _text_click_bindings=[], _text_click_binding=None,
        _text_caret_job=None, _text_tool_anchor=None, _text_font_size=18,
        _text_color="#ff3b30", _draw_start=None, _draw_points=[],
        _draw_preview=None, _draw_preview_items=[], _preview_point_index=0,
        _annotation_draw_time=0.0, _tool="text", _mosaic_radius=12,
        _draw_mosaic_radius=12, _draw_style=(6, "#ff3b30"),
        _drawing_styles={tool: [6, "#ff3b30"]
                         for tool in ("rect", "oval", "arrow", "brush")},
        _sel_box=(0, 0, 100, 100), sx=1, sy=1, sw=100, sh=100,
    )
    for name, value in state.items():
        setattr(selector, name, value)
    selector.src = Image.new("RGB", (100, 100), "white")
    selector.disp = selector.src
    return selector


class RegionSelectorRegressionTests(unittest.TestCase):
    def test_rectangle_four_directions(self):
        selector = make_selector()
        results = []
        for start, end in (
            ((10, 20), (80, 90)), ((80, 90), (10, 20)),
            ((10, 90), (80, 20)), ((80, 20), (10, 90)),
        ):
            selector._annotations = [("rect", start, end, 3, "#ff3b30")]
            results.append(selector._crop_box())
        self.assertEqual(len(set(results)), 1)

    def test_replace_delete_escape_undo_redo(self):
        selector = make_selector()
        original = ("text", (20, 30), "old", 18, "#ff3b30")
        rectangle = ("rect", (1, 1), (4, 4), 3, "#ff3b30")
        selector._record_annotation(original)
        selector._record_annotation(rectangle)

        def edit(value):
            selector._begin_annotation(SimpleNamespace(x=20, y=30))
            selector._text_input_proxy.var.set(value)

        edit("new")
        self.assertEqual(selector._annotations, [original, rectangle])
        self.assertEqual(selector.canvas.items[selector._text_live_item][1], (20, 30))
        selector._text_commit()
        replacement = selector._annotations[0]
        self.assertEqual(replacement[1:3], ((20, 30), "new"))
        self.assertEqual(selector._annotations[1], rectangle)
        selector._undo()
        self.assertEqual(selector._annotations, [original, rectangle])
        selector._redo_action()
        self.assertEqual(selector._annotations, [replacement, rectangle])

        edit("")
        selector._text_commit()
        self.assertEqual(selector._annotations, [rectangle])
        selector._undo()
        self.assertEqual(selector._annotations, [replacement, rectangle])
        selector._redo_action()
        self.assertEqual(selector._annotations, [rectangle])
        selector._undo()

        depth = len(selector._undo_stack)
        edit("cancelled")
        variable = selector._text_input_proxy.var
        selector._set_text_size(26)
        selector._escape()
        self.assertEqual(selector._annotations, [replacement, rectangle])
        self.assertEqual(len(selector._undo_stack), depth)
        self.assertFalse(selector.win.bindings)
        self.assertFalse(selector.win.jobs)
        self.assertFalse(variable.traces)

    def test_noop_preserves_redo_and_whitespace(self):
        selector = make_selector()
        original = ("text", (10, 10), " padded ", 18, "#ff3b30")
        selector._record_annotation(original)
        selector._record_annotation(("rect", (0, 0), (5, 5), 6, "#ff3b30"))
        selector._undo()
        redo = list(selector._redo)
        depth = len(selector._undo_stack)
        selector._begin_annotation(SimpleNamespace(x=10, y=10))
        selector._text_commit()
        self.assertEqual(selector._annotations, [original])
        self.assertEqual(selector._redo, redo)
        self.assertEqual(len(selector._undo_stack), depth)

    def test_reset_clears_state_callbacks_and_photos(self):
        selector = make_selector()
        selector._record_annotation(("text", (10, 10), "old", 18, "#ff3b30"))
        selector._begin_annotation(SimpleNamespace(x=10, y=10))
        selector._text_input_proxy.var.set("pending")
        selector._reset_annotations()
        for name in (
            "_annotations", "_undo_stack", "_redo", "_edit_items",
            "_oval_photos", "_draw_points", "_text_click_bindings",
        ):
            self.assertFalse(getattr(selector, name), name)
        self.assertIsNone(selector._text_commit)
        self.assertIsNone(selector._text_cancel)
        self.assertFalse(selector.canvas.items)
        self.assertFalse(selector.win.jobs)

    def test_brush_5000_events_throttle_and_endpoint(self):
        selector = make_selector()
        selector._tool = "brush"
        selector._begin_annotation(SimpleNamespace(x=0, y=0))
        # 本例验证拖动增量与释放数据；最终图像渲染由独立用例验证。
        selector._redraw_annotations = lambda: None
        with patch.object(time, "perf_counter", return_value=100):
            for index in range(1, 5001):
                selector._update_annotation(SimpleNamespace(x=index, y=index % 100))
            self.assertLessEqual(len(selector._draw_preview_items), 1)
            selector._finish_annotation(SimpleNamespace(x=5002, y=42))
        self.assertEqual(selector._annotations[0][1][-1], (5002, 42))
        self.assertEqual(len(selector._annotations[0][1]), 5002)
        self.assertFalse(selector.canvas.items)

    def test_supersample_budget_and_seams(self):
        allocations = []
        original_new = Image.new

        def track(mode, size, *args, **kwargs):
            allocations.append(size)
            return original_new(mode, size, *args, **kwargs)

        with patch.object(Image, "new", side_effect=track):
            image, _ = RegionSelector._smooth_stroke_image(
                [(0, 0), (1200, 0)], 3, "#ff3b30",
            )
        self.assertTrue(all(width <= 2080 and height <= 2080
                            for width, height in allocations))
        alpha = image.getchannel("A")
        self.assertGreater(alpha.getpixel((512, 5)), 240)
        self.assertGreater(alpha.getpixel((1024, 5)), 240)
        oval, _ = RegionSelector._smooth_oval_image((700, 500), (0, 0), 3)
        self.assertEqual(oval.size, (711, 511))
        self.assertIsNotNone(oval.getbbox())

    def test_text_preview_export_identical_and_anisotropic(self):
        selector = make_selector()
        selector._annotations = [("text", (20, 30), "Bold", 18, "#ff3b30")]
        preview = RegionSelector._text_image("Bold", 18, "#ff3b30")
        expected = selector.src.convert("RGBA")
        expected.alpha_composite(preview, (20, 30))
        actual = Image.open(io.BytesIO(selector._crop_box()))
        self.assertEqual(actual.tobytes(), expected.convert("RGB").tobytes())
        stretched = RegionSelector._text_image("Bold", 18, "#ff3b30", 2, 1)
        self.assertEqual(stretched.width, preview.width * 2)

    def test_current_annotation_styles_preview_and_export(self):
        selector = make_selector()
        selector._annotations = [
            ("rect", (2, 2), (25, 25), 2, "#808080"),
            ("oval", (30, 2), (55, 25), 6, "#1677ff"),
            ("arrow", (2, 35), (45, 50), 12, "#07c160"),
            ("brush", [(5, 65), (45, 70)], 6, "#ff9500"),
            ("mosaic", [(80, 80)], 12),
            ("text", (60, 30), "A", 18, "#111111"),
        ]
        selector._redraw_annotations()
        self.assertEqual(len(selector._edit_items), 6)
        image = Image.open(io.BytesIO(selector._crop_box()))
        self.assertEqual(image.size, (100, 100))
        self.assertEqual(image.getpixel((2, 2)), (128, 128, 128))
        self.assertNotEqual(image.tobytes(), selector.src.tobytes())

    def test_incomplete_and_extra_annotation_fields_are_rejected(self):
        for item in (
            ("rect", (0, 0), (5, 5)),
            ("oval", (0, 0), (5, 5), 6),
            ("arrow", (0, 0), (5, 5)),
            ("brush", [(0, 0), (5, 5)]),
            ("mosaic", [(0, 0)]),
            ("text", (0, 0), "old"),
            ("rect", (0, 0), (5, 5), 6, "#ff3b30", "extra"),
        ):
            for method in ("_crop_box", "_redraw_annotations"):
                with self.subTest(item=item, method=method):
                    selector = make_selector()
                    selector._annotations = [item]
                    with self.assertRaises(ValueError):
                        getattr(selector, method)()
        self.assertNotIn("len(item)", SOURCE)

    def test_no_global_bindings_shutil_and_palette(self):
        self.assertNotIn("bind_all(", SOURCE)
        self.assertNotIn("unbind_all(", SOURCE)
        self.assertIn("import shutil", SOURCE)
        self.assertIn('shutil.which("tesseract")', SOURCE)
        self.assertIn(
            '"#ff3b30", "#111111", "#1677ff", "#07c160", '
            '"#ff9500", "#808080", "#ffffff"', SOURCE,
        )


if __name__ == "__main__":
    unittest.main()
