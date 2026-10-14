"""隔离测试：不创建真实 Tk 窗口，也不访问系统剪贴板或鼠标。"""

import importlib.util
import types
import unittest
from pathlib import Path
from tkinter import TclError
from unittest.mock import Mock, patch

import lumina

# 测试位于 tests/ 子目录，上跳一层才是项目根（lumina/ 与 main.py 所在处）。
SOURCE = Path(__file__).resolve().parent.parent / 'lumina' / 'json_popup.py'
MOUSE = types.SimpleNamespace(mouse_button_down=Mock(return_value=False))
spec = importlib.util.spec_from_file_location('lumina._test_json_popup', SOURCE)
MODULE = importlib.util.module_from_spec(spec)
with patch.object(lumina, 'clipboard_api', MOUSE, create=True):
    spec.loader.exec_module(MODULE)
JsonPopup = MODULE.JsonPopup


class FakeWidget:
    def __init__(self, parent=None, **options):
        self.parent = parent
        self.options = options
        self.bindings = {}
        self.registry = parent.registry if parent else types.SimpleNamespace(
            classes={}, commands={}, next_id=0, deleted=[])
        self._tclCommands = []
        self._widget_tag = f'widget_{id(self)}'
        self._tags = (self._widget_tag, 'Widget', 'all')
        self.next_id = 0
        self.jobs = {}
        self.cancelled = []
        self.unbound = []
        self.exists = True
        self.rect = (400, 400, 40, 24)
        self.screen = (1000, 800)
        self.pointer = (0, 0)
        self.focused = None
        self.selection = ('1.2', '1.8')
        self.drawings = []
        self.visible = False
        self.fail_transparency = bool(parent and parent.fail_transparency)

    def withdraw(self):
        self.visible = False

    def overrideredirect(self, value):
        self.override = value

    def configure(self, **options):
        self.options.update(options)

    def attributes(self, *args):
        if self.fail_transparency:
            raise TclError('不支持透明色')
        self.attribute_args = args

    def pack(self, **options):
        pass

    def bind(self, sequence, callback, add=None):
        assert add == '+', '绑定必须追加，不能覆盖调用方'
        self.next_id += 1
        binding = f'bind{self.next_id}'
        self.bindings.setdefault(sequence, {})[binding] = callback
        return binding

    def unbind(self, sequence, binding):
        assert binding is not None, '必须按 ID 清理'
        self.unbound.append((sequence, binding))
        del self.bindings[sequence][binding]

    def bindtags(self, tags=None):
        if tags is not None:
            if not self.exists:
                raise TclError('控件已销毁')
            self._tags = tuple(tags)
        return self._tags

    def _root(self):
        return self.parent._root() if self.parent else self

    def bind_class(self, tag, sequence, callback, add=None):
        self.registry.next_id += 1
        binding = f'command{self.registry.next_id}'
        self.registry.commands[binding] = callback
        self._root()._tclCommands.append(binding)
        bindings = self.registry.classes.setdefault(tag, {})
        if add != '+':
            bindings[sequence] = []
        bindings.setdefault(sequence, []).append(binding)
        return binding

    def unbind_class(self, tag, sequence):
        # 与 Tk 一致：清脚本不会自动删除命令。
        bindings = self.registry.classes[tag]
        del bindings[sequence]
        if not bindings:
            del self.registry.classes[tag]

    def deletecommand(self, binding):
        del self.registry.commands[binding]
        self.registry.deleted.append(binding)
        if binding in self._tclCommands:
            self._tclCommands.remove(binding)

    def fire(self, sequence, event=None):
        for tag in self.bindtags():
            if tag == self._widget_tag:
                callbacks = list(self.bindings.get(sequence, {}).values())
            else:
                ids = self.registry.classes.get(tag, {}).get(sequence, [])
                callbacks = [self.registry.commands[binding] for binding in ids]
            for callback in callbacks:
                result = callback(event)
                if result == 'break':
                    return result

    def focus_get(self):
        return self.focused

    def focus_force(self):
        raise AssertionError('不得抢焦点')

    def focus_set(self):
        raise AssertionError('不得修改焦点')

    def update_idletasks(self):
        pass

    def winfo_rootx(self):
        return self.rect[0]

    def winfo_rooty(self):
        return self.rect[1]

    def winfo_width(self):
        return self.rect[2]

    def winfo_height(self):
        return self.rect[3]

    def winfo_screenwidth(self):
        return self.screen[0]

    def winfo_screenheight(self):
        return self.screen[1]

    def winfo_pointerxy(self):
        return self.pointer

    def winfo_exists(self):
        return self.exists

    def geometry(self, value):
        self.geometry_value = value

    def deiconify(self):
        self.visible = True

    def lift(self):
        pass

    def after(self, delay, callback):
        self.next_id += 1
        self.jobs[self.next_id] = (delay, callback)
        return self.next_id

    def after_cancel(self, binding):
        self.cancelled.append(binding)
        del self.jobs[binding]

    def run_next(self):
        binding = next(iter(self.jobs))
        _, callback = self.jobs.pop(binding)
        callback()

    def destroy(self):
        self.exists = False
        self.jobs.clear()

    def delete(self, tag):
        self.drawings.clear()

    def create_image(self, *args, **options):
        self.drawings.append(('image', args, options))

    def create_line(self, *args, **options):
        self.drawings.append(('line', args, options))

    def create_text(self, *args, **options):
        self.drawings.append(('text', args, options))


FakeTk = types.SimpleNamespace(Toplevel=FakeWidget, Canvas=FakeWidget)
THEME = {'card_bg': '#ffffff', 'hairline': '#cccccc', 'label': '#222222',
         'label2': '#666666', 'label3': '#999999', 'accent': '#007aff',
         'accent_soft': '#e5f0ff', 'separator': '#dddddd'}


class JsonPopupTests(unittest.TestCase):
    def setUp(self):
        self.photos = []

        def photo(image, **kwargs):
            self.photos.append(image.copy())
            return object()

        patcher = patch.object(MODULE.ImageTk, 'PhotoImage', side_effect=photo)
        patcher.start()
        self.addCleanup(patcher.stop)
        MOUSE.mouse_button_down.reset_mock()
        MOUSE.mouse_button_down.return_value = False
        self.parent = FakeWidget()
        self.text = FakeWidget(self.parent)
        self.parent.focused = self.text
        self.anchor = FakeWidget(self.parent)
        self.actions = []
        self.items = [
            {'label': '格式化', 'hint': '缩进', 'kind': 'format', 'enabled': True,
             'selected': True, 'command': lambda: self.actions.append('format')},
            {'label': '压缩', 'hint': '单行', 'kind': 'compress', 'enabled': False,
             'command': lambda: self.actions.append('compress')},
            {'label': '恢复', 'hint': '原始', 'kind': 'restore', 'enabled': True,
             'command': lambda: self.actions.append('restore')},
        ]
        self.menus = []
        self.addCleanup(lambda: [menu.close() for menu in self.menus])

    def make(self, dpi=1, show=True, on_close=None, items=None):
        menu = JsonPopup(FakeTk, self.parent,
                         self.items if items is None else items, THEME, dpi)
        self.menus.append(menu)
        if show:
            menu.popup(self.anchor, on_close)
        return menu

    def event(self, menu, index, **changes):
        top, end = menu._rows[index]
        event = {'x': 20, 'y': (top + end) / 2 +
                 (0 if menu._above else menu._tip_height)}
        event.update(changes)
        return types.SimpleNamespace(**event)

    def test_above_geometry_and_anchor_center_tip(self):
        menu = self.make()
        self.assertTrue(menu._above)
        self.assertEqual(menu._bounds, (320, 291, 200, 109))
        self.assertEqual(menu._tip_x, 100)
        self.assertEqual(menu._rows, [(4, 34), (34, 64), (69, 99)])
        self.assertEqual(menu._separators, [66])
        self.assertEqual(menu.win.geometry_value, '200x109+320+291')
        self.assertTrue(menu.win.visible)

    def test_below_fallback_and_edge_avoidance(self):
        self.anchor.rect = (0, 4, 30, 24)
        menu = self.make()
        self.assertFalse(menu._above)
        self.assertEqual(menu._bounds, (0, 28, 200, 109))
        self.assertEqual(menu._tip_x, 17)
        self.assertEqual(menu._row_at(20, 5), None)
        self.assertEqual(menu._row_at(20, 20), 0)
        self.anchor.rect = (985, 700, 15, 24)
        other = self.make()
        self.assertEqual(other._bounds[0], 800)
        self.assertEqual(other._tip_x, 183)

    def test_clamps_when_neither_side_fits(self):
        menu = self.make(show=False)
        menu.win.screen = (180, 100)
        self.anchor.rect = (90, 60, 20, 20)
        menu.popup(self.anchor)
        self.assertEqual(menu._bounds, (0, 0, 180, 100))

    def test_dpi_validation_and_scaling(self):
        for value, expected in [(None, 1), ('bad', 1), (float('nan'), 1),
                                (float('inf'), 1), (-1, .5), (100, 3), (1.5, 1.5)]:
            with self.subTest(value=value):
                menu = self.make(value, show=False)
                self.assertEqual(menu.dpi, expected)
                self.assertEqual(menu._width, round(200 * expected))
                self.assertEqual(menu._padding, max(1, round(4 * expected)))
                self.assertEqual(menu._row_height, round(30 * expected))
                self.assertEqual(menu._tip_height, max(1, round(6 * expected)))
                padding = max(1, round(4 * expected))
                row_height = round(30 * expected)
                restore_gap = max(1, round(5 * expected))
                self.assertEqual(menu._rows, [
                    (padding, padding + row_height),
                    (padding + row_height, padding + 2 * row_height),
                    (padding + 2 * row_height + restore_gap,
                     padding + 3 * row_height + restore_gap),
                ])
                self.assertEqual(menu._separators,
                                 [padding + 2 * row_height + max(1, round(2 * expected))])
                self.assertEqual(menu._height,
                                 2 * padding + 3 * row_height + restore_gap + menu._tip_height)

    def test_contains_half_open_bounds_and_closed(self):
        menu = self.make()
        x, y, w, h = menu._bounds
        self.assertTrue(menu.contains_point(x, y))
        self.assertTrue(menu.contains_point(x + w - 1, y + h - 1))
        self.assertFalse(menu.contains_point(x + w, y))
        self.assertFalse(menu.contains_point(x, y + h))
        self.assertFalse(menu.contains_point(420, 410))
        menu.close()
        self.assertFalse(menu.contains_point(x, y))

    def test_render_background_aa_icons_hints_and_selected_check(self):
        original_resize = MODULE.Image.Image.resize
        original_rounded_rectangle = MODULE.ImageDraw.ImageDraw.rounded_rectangle
        calls = []
        rounded_rectangles = []

        def resize(image, size, resample=None, *args, **kwargs):
            calls.append((image.size, size, resample))
            return original_resize(image, size, resample, *args, **kwargs)

        def rounded_rectangle(draw, xy, radius=0, *args, **kwargs):
            rounded_rectangles.append((xy, radius))
            return original_rounded_rectangle(draw, xy, radius, *args, **kwargs)

        with (patch.object(MODULE.Image.Image, 'resize', resize),
              patch.object(MODULE.ImageDraw.ImageDraw, 'rounded_rectangle', rounded_rectangle)):
            menu = self.make()
        self.assertIn(((800, 436), (200, 109), MODULE.Image.Resampling.LANCZOS), calls)
        self.assertEqual(rounded_rectangles[0], ((0, 0, 799, 411), 32))
        image = self.photos[-1]
        self.assertEqual(image.size, (200, 109))
        self.assertEqual(image.getpixel((0, 0)), (1, 2, 3))
        self.assertEqual(image.getpixel((100, 19)), (229, 240, 255))
        self.assertEqual(image.getpixel((100, 49)), (255, 255, 255))
        texts = [o for kind, _, o in menu.canvas.drawings if kind == 'text']
        self.assertEqual([o['text'] for o in texts], ['格式化', '缩进', '压缩', '单行', '恢复', '原始'])
        self.assertEqual(texts[2]['fill'], THEME['label3'])
        self.assertEqual(texts[3]['fill'], THEME['label3'])
        lines = [o for kind, _, o in menu.canvas.drawings if kind == 'line']
        self.assertEqual(len(lines), 9)
        self.assertEqual(sum(o['fill'] == THEME['accent'] for o in lines), 4)

    def test_flatten_background_transparent_preserves_colors_without_key_blending(self):
        alphas = (0, 1, 127, 128, 200, 255)
        color = (40, 100, 180)
        image = MODULE.Image.new('RGBA', (len(alphas), 1))
        pixels = [(*color, alpha) for alpha in alphas]
        pixels[-1] = (17, 83, 149, 255)
        image.putdata(pixels)
        for key, key_rgb in [('#010203', (1, 2, 3)), ('#030201', (3, 2, 1))]:
            with self.subTest(key=key):
                background = JsonPopup._flatten_background(image, key, THEME['card_bg'])
                self.assertEqual(background.mode, 'RGB')
                self.assertEqual(background.size, image.size)
                self.assertEqual([background.getpixel((x, 0)) for x in range(len(alphas))],
                                 [key_rgb, key_rgb, key_rgb, color, color, (17, 83, 149)])
        self.assertEqual([image.getpixel((x, 0)) for x in range(len(alphas))], pixels)

    def test_flatten_background_fallback_composites_partial_alpha(self):
        alphas = (0, 1, 127, 128, 200, 255)
        color = (40, 100, 180)
        card_bg = (230, 210, 190)
        image = MODULE.Image.new('RGBA', (len(alphas), 1))
        pixels = [(*color, alpha) for alpha in alphas]
        image.putdata(pixels)
        background = JsonPopup._flatten_background(image, None, '#e6d2be')
        expected = [tuple((channel * alpha + base * (255 - alpha) + 127) // 255
                          for channel, base in zip(color, card_bg)) for alpha in alphas]
        self.assertEqual(background.mode, 'RGB')
        self.assertEqual(background.size, image.size)
        self.assertEqual([background.getpixel((x, 0)) for x in range(len(alphas))], expected)
        self.assertEqual(background.getpixel((0, 0)), card_bg)
        self.assertEqual(background.getpixel((5, 0)), color)
        self.assertNotEqual(background.getpixel((3, 0)), color)
        self.assertEqual([image.getpixel((x, 0)) for x in range(len(alphas))], pixels)

    def test_transparency_fallback(self):
        self.parent.fail_transparency = True
        menu = self.make()
        self.assertIsNone(menu._transparent)
        self.assertEqual(menu.canvas.options['bg'], THEME['card_bg'])
        self.assertEqual(self.photos[-1].getpixel((0, 0)), (255, 255, 255))

    def test_hover_disabled_and_leave(self):
        menu = self.make()
        menu._motion(self.event(menu, 2))
        self.assertEqual(menu._active, 2)
        self.assertEqual(menu.canvas.options['cursor'], 'hand2')
        menu._motion(self.event(menu, 1))
        self.assertIsNone(menu._active)
        self.assertEqual(menu.canvas.options['cursor'], '')
        menu._motion(self.event(menu, 0))
        menu._leave(None)
        self.assertIsNone(menu._active)
        self.assertTrue(menu.items[0]['selected'])
        self.assertIsNone(menu._row_at(20, 66))
        self.assertIsNone(menu._row_at(0, 20))

    def test_keyboard_wrap_skip_disabled_and_preserve_selection(self):
        menu = self.make()
        for key, expected in [('Down', 2), ('Down', 0), ('Up', 2)]:
            self.assertEqual(self.text.fire(f'<{key}>'), 'break')
            self.assertEqual(menu._active, expected)
        self.assertEqual(self.text.selection, ('1.2', '1.8'))
        self.assertIs(self.parent.focused, self.text)
        self.assertEqual(self.parent.fire('<Return>'), 'break')
        self.assertTrue(menu._closed)
        self.assertEqual(self.actions, ['restore'])

    def test_first_bindtag_blocks_existing_break_paste_and_hide_handlers(self):
        for target in (self.parent, self.text):
            for key in ('Up', 'Down', 'Return', 'Escape'):
                with self.subTest(target=target, key=key):
                    original = Mock(return_value='break')
                    binding = target.bind(f'<{key}>', original, add='+')
                    tags = target.bindtags()
                    menu = self.make()
                    self.assertEqual(target.bindtags(), (menu._key_tag, *tags))
                    self.assertEqual(target.fire(f'<{key}>'), 'break')
                    original.assert_not_called()
                    if key in ('Up', 'Down'):
                        self.assertEqual(menu._active, 2)
                    elif key == 'Return':
                        self.assertTrue(menu._closed)
                        self.assertEqual(self.actions[-1], 'format')
                    else:
                        self.assertTrue(menu._closed)
                    menu.close()
                    self.assertEqual(target.bindtags(), tags)
                    self.assertEqual(target.fire(f'<{key}>'), 'break')
                    original.assert_called_once_with(None)
                    target.unbind(f'<{key}>', binding)
                    self.assertEqual(self.parent.registry.classes, {})
                    self.assertEqual(self.parent.registry.commands, {})

    def test_canvas_keyboard_uses_same_priority_tag(self):
        menu = self.make()
        canvas = menu.canvas
        original = Mock(return_value='break')
        canvas.bind('<Escape>', original, add='+')
        self.assertEqual(canvas.bindtags()[0], menu._key_tag)
        canvas.fire('<Down>')
        self.assertEqual(menu._active, 2)
        self.assertEqual(canvas.fire('<Escape>'), 'break')
        original.assert_not_called()
        self.assertTrue(menu._closed)
        self.assertEqual(self.parent.registry.commands, {})

    def test_close_removes_only_own_tag_and_commands(self):
        foreign = Mock(return_value='break')
        foreign_id = self.parent.bind_class('foreign', '<Return>', foreign)
        self.text.bindtags(('foreign', *self.text.bindtags()))
        menu = self.make()
        tag = menu._key_tag
        commands = [binding for _, binding in menu._key_bindings]
        canvas = menu.canvas
        targets = (self.parent, self.text, canvas)
        for target in targets:
            target.bindtags(('new_tag', *target.bindtags(), 'tail_tag'))
        before = {target: target.bindtags() for target in targets}
        menu.close()
        for target in targets:
            self.assertEqual(target.bindtags(), tuple(t for t in before[target] if t != tag))
        self.assertNotIn(tag, self.parent.registry.classes)
        self.assertEqual(self.parent.registry.commands, {foreign_id: foreign})
        self.assertEqual(self.parent.registry.deleted, commands)
        self.assertEqual(menu._key_targets, [])
        self.assertEqual(menu._key_bindings, [])
        self.text.fire('<Return>')
        foreign.assert_called_once_with(None)

    def test_unique_tags_and_closing_one_popup_preserves_other(self):
        first = self.make()
        second = self.make()
        first_tag, second_tag = first._key_tag, second._key_tag
        self.assertNotEqual(first_tag, second_tag)
        self.assertEqual(self.text.bindtags()[:2], (second_tag, first_tag))
        first.close()
        self.assertEqual(self.text.bindtags()[0], second_tag)
        self.assertNotIn(first_tag, self.text.bindtags())
        self.assertEqual(len(self.parent.registry.commands), 4)
        self.text.fire('<Down>')
        self.assertEqual(second._active, 2)
        second.close()
        self.assertEqual(self.parent.registry.classes, {})
        self.assertEqual(self.parent.registry.commands, {})

    def test_class_commands_removed_from_root_not_parent_toplevel(self):
        root = FakeWidget()
        self.parent = FakeWidget(root)
        self.text = FakeWidget(self.parent)
        self.parent.focused = self.text
        self.anchor = FakeWidget(self.parent)
        menu = self.make()
        commands = [binding for _, binding in menu._key_bindings]
        self.assertIs(menu._key_owner, root)
        self.assertEqual(root._tclCommands, commands)
        self.assertEqual(self.parent._tclCommands, [])
        menu.close()
        self.assertEqual(root._tclCommands, [])
        self.assertEqual(root.registry.commands, {})
        self.assertEqual(root.registry.classes, {})
        self.assertIsNone(menu._key_owner)

    def test_partial_tag_install_failure_cleans_commands_and_tags(self):
        menu = self.make(show=False)
        before = self.parent.bindtags()
        with (patch.object(self.text, 'bindtags', side_effect=TclError('失效')),
              self.assertRaises(TclError)):
            menu.popup(self.anchor)
        self.assertTrue(menu._closed)
        self.assertEqual(self.parent.bindtags(), before)
        self.assertEqual(self.parent.registry.classes, {})
        self.assertEqual(self.parent.registry.commands, {})
        self.assertEqual(menu._key_targets, [])

    def test_keyboard_no_initial_selection_and_all_disabled(self):
        for first_key, expected in [('Down', 0), ('Up', 2)]:
            items = [dict(item, selected=False) for item in self.items]
            menu = self.make(items=items)
            self.assertIsNone(menu._active)
            menu._key(first_key)
            self.assertEqual(menu._active, expected)
            menu.close()
        menu = self.make(items=[dict(item, enabled=False) for item in self.items])
        for key in ('Down', 'Up', 'Return'):
            self.assertEqual(menu._key(key), 'break')
            self.assertIsNone(menu._active)
        self.assertFalse(menu._closed)
        self.assertEqual(self.actions, [])
        self.assertEqual(menu._key('Escape'), 'break')
        self.assertTrue(menu._closed)

    def test_disabled_and_separator_click_do_not_invoke(self):
        menu = self.make()
        menu._click(self.event(menu, 1))
        menu._click(self.event(menu, 0, y=66))
        menu._invoke(1)
        self.assertFalse(menu._closed)
        self.assertEqual(self.actions, [])
        menu._click(self.event(menu, 0))
        self.assertEqual(self.actions, ['format'])

    def test_poll_anchor_menu_and_outside(self):
        menu = self.make()
        win = menu.win
        MOUSE.mouse_button_down.return_value = True
        for pointer in [(420, 410), (330, 300)]:
            win.pointer = pointer
            win.run_next()
            self.assertFalse(menu._closed)
            self.assertEqual(len(win.jobs), 1)
        self.anchor.rect = (500, 400, 40, 24)
        win.pointer = (510, 410)
        win.run_next()
        self.assertFalse(menu._closed)
        win.pointer = (800, 700)
        win.run_next()
        self.assertTrue(menu._closed)
        self.assertEqual(win.jobs, {})
        self.assertEqual(MOUSE.mouse_button_down.call_count, 4)

    def test_poll_no_mouse_press_does_not_close(self):
        menu = self.make()
        menu.win.pointer = (900, 700)
        menu.win.run_next()
        self.assertFalse(menu._closed)
        self.assertEqual(len(menu.win.jobs), 1)

    def test_destroyed_anchor_and_parent_close(self):
        menu = self.make()
        self.anchor.exists = False
        menu.win.run_next()
        self.assertTrue(menu._closed)
        self.anchor.exists = True
        menu = self.make()
        self.parent.fire('<Destroy>', types.SimpleNamespace(widget=self.text))
        self.assertFalse(menu._closed)
        self.parent.fire('<Destroy>', types.SimpleNamespace(widget=self.parent))
        self.assertTrue(menu._closed)

    def test_cleanup_preserves_existing_bindings_and_reentrant_close(self):
        def old(event):
            return None
        for target in (self.parent, self.text):
            target.bind('<Up>', old, add='+')
        closed = []
        menu = self.make(show=False)

        def on_close():
            closed.append(menu._closed)
            menu.close()

        menu.popup(self.anchor, on_close)
        win, canvas = menu.win, menu.canvas
        timer = menu._outside_check
        menu.close()
        menu.close()
        self.assertEqual(closed, [True])
        self.assertEqual(win.cancelled, [timer])
        self.assertFalse(win.exists)
        self.assertEqual(menu._bindings, [])
        for target in (self.parent, self.text):
            self.assertEqual(list(target.bindings['<Up>'].values()), [old])
            for key in ('Down', 'Return', 'Escape'):
                self.assertEqual(target.bindings.get(f'<{key}>', {}), {})
        self.assertTrue(all(not bindings for bindings in canvas.bindings.values()))
        for name in ('win', 'canvas', '_photo', '_anchor', 'parent_win', 'tk',
                     '_on_close', '_outside_check', '_bounds', '_active', '_key_tag', '_key_owner'):
            self.assertIsNone(getattr(menu, name), name)
        self.assertEqual(menu.items, [])
        self.assertEqual(menu.theme, {})
        self.assertEqual(menu._key('Down'), None)
        menu._motion(types.SimpleNamespace(x=0, y=0))
        menu._leave(None)
        menu._check_outside_click()

    def test_close_before_command_and_command_exception(self):
        order = []
        menu = self.make(show=False)
        win = menu.win

        def command():
            self.assertTrue(menu._closed)
            self.assertFalse(win.exists)
            self.assertEqual(menu._bindings, [])
            order.append('command')
            raise ValueError('动作失败')

        menu.items[0]['command'] = command
        menu.popup(self.anchor, lambda: order.append('close'))
        with self.assertRaisesRegex(ValueError, '动作失败'):
            menu._invoke(0)
        menu.close()
        self.assertEqual(order, ['close', 'command'])

    def test_callback_failure_does_not_prevent_action(self):
        menu = self.make(show=False)
        menu.popup(self.anchor, Mock(side_effect=ValueError('通知失败')))
        with self.assertLogs(MODULE.__name__, level='ERROR'):
            menu._invoke(0)
        self.assertEqual(self.actions, ['format'])

    def test_no_duplicate_popup_bindings_or_callbacks(self):
        first, second = Mock(), Mock()
        menu = self.make(on_close=first)
        binding_count = len(menu._bindings)
        menu.popup(self.anchor, second)
        self.assertEqual(len(menu._bindings), binding_count)
        menu.close()
        menu.popup(self.anchor, second)
        first.assert_called_once_with()
        second.assert_not_called()

    def test_empty_items_and_same_focus_target(self):
        self.parent.focused = self.parent
        menu = self.make(items=[])
        for key in ('Down', 'Up', 'Return'):
            menu._key(key)
        self.assertEqual(self.parent.bindtags().count(menu._key_tag), 1)
        self.assertEqual(len(menu._key_bindings), 4)
        self.assertEqual(len(menu._key_targets), 2)
        self.assertFalse(menu._closed)
        menu.close()

    def test_poll_failure_closes_and_cleans_bindings(self):
        for error in (TclError('控件已销毁'), OSError('鼠标查询失败')):
            menu = self.make()
            with patch.object(MOUSE, 'mouse_button_down', side_effect=error):
                menu.win.run_next()
            self.assertTrue(menu._closed)
            self.assertEqual(menu._bindings, [])

    def test_cleanup_survives_already_destroyed_tk_objects(self):
        callback = Mock()
        menu = self.make(on_close=callback)
        win, canvas = menu.win, menu.canvas
        with (patch.object(win, 'after_cancel', side_effect=TclError('计时器失效')),
              patch.object(canvas, 'unbind', side_effect=TclError('画布已销毁')),
              patch.object(win, 'destroy', side_effect=TclError('窗口已销毁'))):
            menu.close()
        self.assertIsNone(menu.win)
        self.assertEqual(menu._bindings, [])
        callback.assert_called_once_with()
        self.assertEqual(self.text.bindings.get('<Up>', {}), {})

    def test_popup_failure_cleans_up(self):
        menu = self.make(show=False)
        callback = Mock()
        with (patch.object(self.anchor, 'winfo_rootx', side_effect=RuntimeError('失效')),
              self.assertRaisesRegex(RuntimeError, '失效')):
            menu.popup(self.anchor, callback)
        self.assertTrue(menu._closed)
        self.assertEqual(menu._bindings, [])
        callback.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
