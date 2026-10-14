import ast
import ctypes
import importlib.util
import io
import struct
import sys
import threading
import traceback
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image

# 测试位于 tests/ 子目录，上跳一层才是项目根（lumina/ 与 main.py 所在处）。
ROOT = Path(__file__).resolve().parent.parent


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'lumina' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# 导入时也隔离 DLL；测试中不允许任何真实 Win32 调用。
with patch.object(ctypes, 'windll', types.SimpleNamespace(
        user32=MagicMock(), kernel32=MagicMock(), shell32=MagicMock()), create=True):
    clipboard = load_module('clipboard_under_test', 'clipboard_api.py')
media = load_module('media_under_test', 'media.py')

# 只替换用到的 4 个键。切勿改用 patch.dict(sys.modules, ...)：它在退出时按
# 入口快照 clear+update，会把块内新导入的 lumina.diff_window / diff_tools /
# json_tools 一并逐出，导致同名模块出现两份对象，后续 patch.object 打在陈旧
# 模块上失效、dataclass 相等性判定失败（曾使 discover 出现 13 项假失败）。
_FAKE_SUBMODULES = {
    'lumina.clipboard_api': clipboard,
    'lumina.region_selector': types.SimpleNamespace(RegionSelector=object),
    'lumina.history_panel': types.SimpleNamespace(HistoryPanel=object),
    'lumina.settings': types.SimpleNamespace(save_config=MagicMock()),
}
_MISSING = object()
_saved_submodules = {name: sys.modules.get(name, _MISSING) for name in _FAKE_SUBMODULES}
sys.modules.update(_FAKE_SUBMODULES)
try:
    ui = load_module('lumina.ui_under_test', 'ui.py')
finally:
    for _name, _previous in _saved_submodules.items():
        if _previous is _MISSING:
            sys.modules.pop(_name, None)
        else:
            sys.modules[_name] = _previous


class FakeWin32:
    def __init__(self):
        self.user = MagicMock()
        self.kernel = MagicMock()
        self.blocks = {}
        self.windows = {}
        self.events = []
        self.next_handle = 1 << 40
        self.lock = threading.Lock()
        self.clipboard_handle = None
        self.owner = None
        self.user.IsClipboardFormatAvailable.return_value = True
        self.user.GetClipboardData.side_effect = lambda fmt: self.clipboard_handle
        self.user.CreateWindowExW.side_effect = self.create_window
        self.user.DestroyWindow.side_effect = self.destroy_window
        self.user.OpenClipboard.side_effect = self.open_clipboard
        self.user.CloseClipboard.side_effect = self.close_clipboard
        self.user.EmptyClipboard.return_value = True
        self.user.SetClipboardData.side_effect = self.set_data
        self.kernel.GlobalAlloc.side_effect = lambda flags, size: self.allocate(size)
        self.kernel.GlobalSize.side_effect = lambda handle: ctypes.sizeof(self.blocks[handle])
        self.kernel.GlobalLock.side_effect = lambda handle: ctypes.addressof(self.blocks[handle])
        self.kernel.GlobalUnlock.return_value = True
        self.kernel.GlobalFree.side_effect = self.free

    def allocate(self, size, data=None):
        with self.lock:
            self.next_handle += 1
            handle = self.next_handle
            self.blocks[handle] = ctypes.create_string_buffer(size)
        if data is not None:
            ctypes.memmove(ctypes.addressof(self.blocks[handle]), data, len(data))
        return handle

    def free(self, handle):
        del self.blocks[handle]

    def create_window(self, *args):
        with self.lock:
            self.next_handle += 1
            hwnd = self.next_handle
            self.windows[hwnd] = threading.get_ident()
            self.events.append(('create', hwnd, threading.get_ident()))
        return hwnd

    def destroy_window(self, hwnd):
        assert self.windows[hwnd] == threading.get_ident()
        assert self.owner != hwnd
        self.events.append(('destroy', hwnd, threading.get_ident()))
        del self.windows[hwnd]
        return True

    def open_clipboard(self, hwnd):
        with self.lock:
            if self.owner is not None:
                return False
            if hwnd is not None:
                assert self.windows[hwnd] == threading.get_ident()
            self.owner = hwnd if hwnd is not None else 'reader'
            self.events.append(('open', hwnd, threading.get_ident()))
        return True

    def close_clipboard(self):
        self.events.append(('close', self.owner, threading.get_ident()))
        self.owner = None
        return True

    def set_data(self, fmt, handle):
        assert self.owner in self.windows
        assert self.windows[self.owner] == threading.get_ident()
        assert handle in self.blocks
        self.events.append(('set', self.owner, threading.get_ident()))
        return handle


class ClipboardTests(unittest.TestCase):
    def setUp(self):
        self.win = FakeWin32()
        self.patches = [patch.object(clipboard, 'user32', self.win.user),
                        patch.object(clipboard, 'kernel32', self.win.kernel)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def read(self, data):
        self.win.clipboard_handle = self.win.allocate(len(data), data)
        return clipboard.get_clipboard_text()

    def test_unicode_bounded_and_aligned(self):
        data = 'A\u0100😀'.encode('utf-16-le') + b'\0\0' + b'tail'
        original = ctypes.string_at
        with patch.object(ctypes, 'wstring_at', side_effect=AssertionError('禁止无界读取')), \
                patch.object(ctypes, 'string_at', wraps=original) as read:
            self.assertEqual(self.read(data), 'A\u0100😀')
            self.assertEqual(read.call_args.args[1], len(data))
        self.win.kernel.GlobalUnlock.assert_called_once()
        self.win.user.CloseClipboard.assert_called_once()

    def test_unicode_empty_and_padding(self):
        self.assertEqual(self.read(b'\0\0'), '')
        self.assertEqual(self.read(b'A\0\0\0x'), 'A')

    def test_unicode_missing_or_misaligned_terminator(self):
        for data in (b'A\0', b'A\0\0', b'A\0\0\x01'):
            with self.subTest(data=data):
                self.assertIsNone(self.read(data))

    def test_unicode_invalid_surrogates(self):
        for data in (b'\x00\xd8\0\0', b'\x00\xdc\0\0'):
            with self.subTest(data=data):
                self.assertIsNone(self.read(data))

    def test_unicode_rejects_sizes_before_lock(self):
        self.win.clipboard_handle = 42
        for size in (0, 1, clipboard.MAX_TEXT_BYTES + 1):
            with self.subTest(size=size):
                self.win.kernel.GlobalSize.side_effect = None
                self.win.kernel.GlobalSize.return_value = size
                self.assertIsNone(clipboard.get_clipboard_text())
        self.win.kernel.GlobalLock.assert_not_called()
        self.assertEqual(self.win.user.CloseClipboard.call_count, 3)

    def test_unicode_exact_size_limit(self):
        with patch.object(clipboard, 'MAX_TEXT_BYTES', 4):
            self.assertEqual(self.read(b'A\0\0\0'), 'A')
            self.win.kernel.GlobalLock.reset_mock()
            self.assertIsNone(self.read(b'A\0\0\0x'))
            self.win.kernel.GlobalLock.assert_not_called()

    def test_unicode_read_failure_unlocks_and_closes(self):
        self.win.clipboard_handle = self.win.allocate(4, b'A\0\0\0')
        with patch.object(ctypes, 'string_at', side_effect=RuntimeError('模拟读取失败')):
            with self.assertRaises(RuntimeError):
                clipboard.get_clipboard_text()
        self.win.kernel.GlobalUnlock.assert_called_once()
        self.win.user.CloseClipboard.assert_called_once()

    def test_unicode_unavailable_handle_and_lock_failures(self):
        self.win.user.IsClipboardFormatAvailable.return_value = False
        self.assertIsNone(clipboard.get_clipboard_text())
        self.win.user.OpenClipboard.assert_not_called()
        self.win.user.IsClipboardFormatAvailable.return_value = True
        self.assertIsNone(clipboard.get_clipboard_text())
        self.win.clipboard_handle = self.win.allocate(4)
        self.win.kernel.GlobalLock.side_effect = None
        self.win.kernel.GlobalLock.return_value = None
        self.assertIsNone(clipboard.get_clipboard_text())
        self.win.kernel.GlobalUnlock.assert_not_called()

    def test_write_owner_order_and_transferred_memory(self):
        self.assertTrue(clipboard.set_clipboard_text('中文😀'))
        self.assertEqual([e[0] for e in self.win.events],
                         ['create', 'open', 'set', 'close', 'destroy'])
        owner = self.win.user.OpenClipboard.call_args.args[0]
        self.assertGreater(owner, 2 ** 32)
        self.assertFalse(self.win.windows)
        self.win.kernel.GlobalFree.assert_not_called()
        handle = self.win.user.SetClipboardData.call_args.args[1]
        self.assertEqual(self.win.blocks[handle].raw,
                         '中文😀'.encode('utf-16-le') + b'\0\0')

    def test_write_failure_paths_release_memory(self):
        for failure in ('alloc', 'lock', 'owner', 'open', 'empty', 'set'):
            with self.subTest(failure=failure):
                win = FakeWin32()
                function = {'alloc': win.kernel.GlobalAlloc,
                            'lock': win.kernel.GlobalLock,
                            'owner': win.user.CreateWindowExW,
                            'open': win.user.OpenClipboard,
                            'empty': win.user.EmptyClipboard,
                            'set': win.user.SetClipboardData}[failure]
                function.side_effect = None
                function.return_value = 0
                with patch.object(clipboard, 'user32', win.user), \
                        patch.object(clipboard, 'kernel32', win.kernel), \
                        patch.object(clipboard.time, 'sleep'):
                    self.assertFalse(clipboard.set_clipboard_dib(b'dib'))
                self.assertFalse(win.blocks)
                self.assertFalse(win.windows)
                if failure == 'empty':
                    win.user.SetClipboardData.assert_not_called()
                if failure in ('empty', 'set'):
                    win.user.CloseClipboard.assert_called_once()

    def test_write_exception_paths_release_memory(self):
        for name in ('GlobalLock', 'GlobalUnlock', 'CreateWindowExW',
                     'OpenClipboard', 'EmptyClipboard', 'SetClipboardData',
                     'CloseClipboard', 'DestroyWindow', 'memmove'):
            with self.subTest(name=name):
                win = FakeWin32()
                if name == 'memmove':
                    target = ctypes
                elif name.startswith('Global'):
                    target = win.kernel
                else:
                    target = win.user
                original = getattr(target, name)

                def fail(*args, _name=name, _original=original):
                    if _name in ('CloseClipboard', 'DestroyWindow'):
                        _original(*args)
                    raise RuntimeError('模拟异常')

                with patch.object(clipboard, 'user32', win.user), \
                        patch.object(clipboard, 'kernel32', win.kernel), \
                        patch.object(target, name, side_effect=fail):
                    with self.assertRaises(RuntimeError):
                        clipboard.set_clipboard_dib(b'dib')
                transferred = name in ('CloseClipboard', 'DestroyWindow')
                self.assertEqual(bool(win.blocks), transferred)
                self.assertFalse(win.windows)
                self.assertIsNone(win.owner)

    def test_write_from_concurrent_threads_owns_window_lifetime(self):
        barrier = threading.Barrier(4)
        results = []
        errors = []

        def write():
            try:
                barrier.wait()
                results.append(clipboard.set_clipboard_text('thread'))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=write) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(results, [True] * 4)
        self.assertFalse(self.win.windows)
        creates = [e for e in self.win.events if e[0] == 'create']
        self.assertEqual(len({e[1] for e in creates}), 4)
        self.assertEqual(len({e[2] for e in creates}), 4)
        for _, hwnd, tid in creates:
            events = [e for e in self.win.events if e[1] == hwnd]
            self.assertEqual([e[0] for e in events],
                             ['create', 'open', 'set', 'close', 'destroy'])
            self.assertTrue(all(e[2] == tid for e in events))

    def test_open_retries_keep_owner(self):
        self.win.user.OpenClipboard.side_effect = [False, False, True]
        with patch.object(clipboard.time, 'sleep') as sleep:
            self.assertTrue(clipboard.open_clipboard(owner=123))
        self.assertEqual(self.win.user.OpenClipboard.call_count, 3)
        self.assertTrue(all(c.args == (123,) for c in self.win.user.OpenClipboard.call_args_list))
        self.assertEqual(sleep.call_count, 2)


STANDARD_MASKS = (0x00FF0000, 0x0000FF00, 0x000000FF)


def dib(pixels=b'\x03\x02\x01\0', width=1, height=-1, bpp=32,
        compression=0, header_size=40, masks=STANDARD_MASKS, alpha=0,
        planes=1, colors=0):
    header = struct.pack('<IiiHHIIiiII', header_size, width, height,
                         planes, bpp, compression, len(pixels), 0, 0, colors, 0)
    if header_size > 40:
        header += bytes(header_size - 40)
        if header_size >= 52:
            header = header[:40] + struct.pack('<III', *masks) + header[52:]
        if header_size >= 56:
            header = header[:52] + struct.pack('<I', alpha) + header[56:]
    elif compression == 3:
        header += struct.pack('<III', *masks)
    return header + bytes(colors * 4) + pixels


class MediaTests(unittest.TestCase):
    def test_bi_rgb_reserved_byte_is_not_alpha(self):
        for reserved in (0, 1, 127, 255):
            with self.subTest(reserved=reserved):
                image = media.dib_to_image(dib(bytes((3, 2, 1, reserved))))
                self.assertEqual(image.mode, 'RGB')
                self.assertEqual(image.getpixel((0, 0)), (1, 2, 3))

    def test_bitfields_standard_and_explicit_alpha(self):
        for size in (40, 52, 56, 108, 124):
            with self.subTest(size=size):
                image = media.dib_to_image(dib(compression=3, header_size=size))
                self.assertEqual(image.getpixel((0, 0)), (1, 2, 3))
        image = media.dib_to_image(dib(b'\x03\x02\x01\x40', compression=3,
                                      header_size=56, alpha=0xFF000000))
        self.assertEqual(image.mode, 'RGBA')
        self.assertEqual(image.getpixel((0, 0)), (1, 2, 3, 64))

    def test_bitfields_rejects_unsupported_masks(self):
        for masks in ((0, 0, 0), (255, 65280, 16711680),
                      (0xF800, 0x7E0, 0x1F), (255, 255, 255)):
            with self.subTest(masks=masks):
                self.assertIsNone(media.dib_to_image(dib(compression=3, masks=masks)))
        self.assertIsNone(media.dib_to_image(dib(compression=3, header_size=56, alpha=255)))
        self.assertIsNone(media.dib_to_image(dib(compression=3, bpp=24)))
        self.assertIsNone(media.dib_to_image(dib(compression=3)[:48]))

    def test_orientation_padding_and_color_table(self):
        rows = b'\0\0\xff\0' + b'\xff\0\0\0'
        for height, expected in ((-2, [(255, 0, 0), (0, 0, 255)]),
                                 (2, [(0, 0, 255), (255, 0, 0)])):
            for bpp in (24, 32):
                with self.subTest(height=height, bpp=bpp):
                    image = media.dib_to_image(dib(rows, height=height, bpp=bpp, colors=2))
                    self.assertEqual([image.getpixel((0, y)) for y in range(2)], expected)

    def test_invalid_headers_sizes_and_truncated_pixels(self):
        for kwargs in ({'planes': 0}, {'width': 0}, {'width': -1},
                       {'height': 0}, {'bpp': 16}, {'compression': 6},
                       {'header_size': 41}, {'width': media.MAX_IMAGE_PIXELS + 1}):
            with self.subTest(kwargs=kwargs):
                self.assertIsNone(media.dib_to_image(dib(**kwargs)))
        for data in (None, b'', dib()[:39], dib()[:-1], dib(header_size=108)[:60]):
            self.assertIsNone(media.dib_to_image(data))
        data = bytearray(dib(header_size=124))
        struct.pack_into('<II', data, 112, 124, 4)
        self.assertIsNone(media.dib_to_image(data))

    def test_image_dib_png_roundtrip(self):
        original = Image.new('RGB', (2, 2))
        original.putdata([(255, 0, 0), (0, 255, 0), (0, 0, 255), (10, 20, 30)])
        result = Image.open(io.BytesIO(media.dib_to_png(media.image_to_dib(original))))
        self.assertEqual(result.mode, 'RGB')
        self.assertEqual(result.size, original.size)
        self.assertEqual(result.tobytes(), original.tobytes())


class FakeWidget:
    def __init__(self, *args, **kwargs):
        self.bindings = {}

    def bind(self, event, callback):
        self.bindings[event] = callback

    def __getattr__(self, name):
        if name.startswith('winfo_screen'):
            return lambda: 1000
        return lambda *args, **kwargs: None


class PinWheelTests(unittest.TestCase):
    def setUp(self):
        from PIL import ImageTk
        stream = io.BytesIO()
        Image.new('RGB', (10, 10)).save(stream, 'PNG')
        server = types.SimpleNamespace(
            _tk=types.SimpleNamespace(Toplevel=FakeWidget, Label=FakeWidget),
            _root=None, _pins=[])
        with patch.object(ImageTk, 'PhotoImage', return_value=object()):
            self.pin = ui.PinWindow(server, stream.getvalue())
        self.pin._apply_scale = MagicMock()

    def dispatch(self, target, delta):
        event = types.SimpleNamespace(delta=delta)
        # 模拟 Tk bindtags：Label 的事件随后传播至 Toplevel，除非返回 break。
        widgets = [target] if target is self.pin.win else [target, self.pin.win]
        for widget in widgets:
            callback = widget.bindings.get('<MouseWheel>')
            if callback and callback(event) == 'break':
                break

    def test_label_event_scales_once(self):
        self.dispatch(self.pin._label, 120)
        self.assertAlmostEqual(self.pin._scale, 1.1)
        self.pin._apply_scale.assert_called_once()

    def test_toplevel_event_and_negative_delta(self):
        self.dispatch(self.pin.win, -120)
        self.assertAlmostEqual(self.pin._scale, 1 / 1.1)
        self.pin._apply_scale.assert_called_once()

    def test_scale_limits_stop_propagation(self):
        for scale, delta in ((self.pin.MIN_SCALE, -120), (self.pin.MAX_SCALE, 120)):
            self.pin._scale = scale
            self.assertEqual(self.pin._on_wheel(types.SimpleNamespace(delta=delta)), 'break')
            self.assertEqual(self.pin._scale, scale)
        self.pin._apply_scale.assert_not_called()

    def test_separate_events_each_scale_once(self):
        self.dispatch(self.pin._label, 120)
        self.dispatch(self.pin._label, 120)
        self.assertAlmostEqual(self.pin._scale, 1.21)
        self.assertEqual(self.pin._apply_scale.call_count, 2)


class PreviewAndPanelTests(unittest.TestCase):
    def setUp(self):
        self.server = ui.UiServer(None, {}, types.SimpleNamespace())
        self.server._root = MagicMock()
        self.server._panel = MagicMock()

    def test_repeated_image_click_reuses_window(self):
        preview = MagicMock()
        preview.win.winfo_exists.return_value = True
        with patch.object(ui, 'PinWindow', return_value=preview) as factory:
            for _ in range(10):
                self.server.show_image_preview(b'png', ('image', 1))
            factory.assert_called_once_with(self.server, b'png', preview=True)
        preview.replace_image.assert_not_called()
        self.assertEqual(preview.win.lift.call_count, 10)
        self.assertFalse(self.server._pins)

    def test_switch_image_updates_same_window(self):
        preview = MagicMock()
        preview.win.winfo_exists.return_value = True
        with patch.object(ui, 'PinWindow', return_value=preview) as factory:
            self.server.show_image_preview(b'a', 1)
            self.server.show_image_preview(b'b', 2)
            self.server.show_image_preview(b'a', 1)
            self.assertEqual(factory.call_count, 1)
        self.assertEqual(preview.replace_image.call_count, 2)

    def test_destroyed_preview_rebuilds(self):
        old = MagicMock()
        old.win.winfo_exists.return_value = False
        self.server._image_preview = old
        with patch.object(ui, 'PinWindow') as factory:
            self.server.show_image_preview(b'png', 1)
            factory.assert_called_once()
            self.assertIs(self.server._image_preview, factory.return_value)

    def test_preview_close_clears_reference_not_other_pins(self):
        preview = ui.PinWindow.__new__(ui.PinWindow)
        preview.ui = self.server
        preview.win = MagicMock()
        other_pin = object()
        self.server._pins = [other_pin]
        self.server._image_preview = preview
        self.server._image_preview_key = 1
        preview.close()
        self.assertIsNone(self.server._image_preview)
        self.assertIsNone(self.server._image_preview_key)
        self.assertEqual(self.server._pins, [other_pin])
        preview.win.destroy.assert_called_once()

    def test_locked_panel_is_not_topmost_hover_still_is(self):
        with patch.object(ui.clipboard_api, 'get_foreground_hwnd', return_value=12):
            for locked in (True, False):
                self.server._show_panel_from_floating(locked=locked)
                self.server._panel.show.assert_called_with(
                    12, position=None, topmost=not locked)

    def test_hover_to_locked_demotes_and_cancels_hide(self):
        self.server._panel.is_visible.return_value = True
        self.server._floating_hide_job = 42
        self.server._floating_click()
        self.assertTrue(self.server._panel_locked)
        self.server._panel.win.attributes.assert_called_with('-topmost', False)
        self.server._root.after_cancel.assert_called_once_with(42)

    def test_panel_events_bind_without_floating_ball(self):
        with patch.object(ui, 'HistoryPanel', return_value=self.server._panel):
            self.server._build_panel()
        events = [call.args[0] for call in self.server._panel.win.bind.call_args_list]
        self.assertEqual(events, ['<Enter>', '<Leave>', '<FocusOut>'])

    def test_detail_image_click_routes_to_single_preview(self):
        source = (ROOT / 'lumina' / 'history_panel.py').read_text(encoding='utf-8')
        cls = next(node for node in ast.parse(source).body
                   if isinstance(node, ast.ClassDef) and node.name == 'DetailPane')
        method = next(node for node in cls.body
                      if isinstance(node, ast.FunctionDef) and node.name == '_open_image_preview')
        namespace = {'traceback': traceback}
        exec(compile(ast.Module(body=[method], type_ignores=[]), '<preview>', 'exec'), namespace)
        panel = types.SimpleNamespace(ui=self.server, db=MagicMock(), status_var=MagicMock())
        panel.db.get_file_data.return_value = {'data': b'file-png'}
        detail = types.SimpleNamespace(row={'id': 3, 'data': b'png'}, cat='image', panel=panel)
        with patch.object(self.server, 'show_image_preview') as show:
            self.assertEqual(namespace['_open_image_preview'](detail), 'break')
            show.assert_called_with(b'png', ('image', 3))
            detail.cat = 'file'
            namespace['_open_image_preview'](detail)
            show.assert_called_with(b'file-png', ('file', 3))

    def test_no_floating_ball_does_not_auto_hide_panel(self):
        self.server._schedule_floating_hide()
        self.server._root.after.assert_not_called()

    def test_preview_not_topmost_pin_still_topmost_and_update(self):
        from PIL import ImageTk
        stream = io.BytesIO()
        Image.new('RGB', (2000, 1000)).save(stream, 'PNG')
        window = MagicMock()
        window.winfo_screenwidth.return_value = 1000
        window.winfo_screenheight.return_value = 1000
        self.server._tk = types.SimpleNamespace(
            Toplevel=lambda root: window, Label=lambda *args, **kwargs: MagicMock())
        with patch.object(ImageTk, 'PhotoImage', return_value=object()):
            preview = ui.PinWindow(self.server, stream.getvalue(), preview=True)
            preview.win.attributes.assert_called_with('-topmost', False)
            preview.win.overrideredirect.assert_called_with(False)
            preview.win.protocol.assert_called_once_with('WM_DELETE_WINDOW', preview.close)
            preview.replace_image(stream.getvalue())
            pin = ui.PinWindow(self.server, stream.getvalue())
            pin.win.attributes.assert_called_with('-topmost', True)
            pin.win.overrideredirect.assert_called_with(True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
