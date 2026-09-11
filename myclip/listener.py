import ctypes
import queue
import threading
import time
import traceback
from ctypes import wintypes

from . import win32clip
from .imaging import dib_to_png

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WM_CLIPBOARDUPDATE = 0x031D
WM_HOTKEY = 0x0312
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002

HOTKEY_BASE = 1
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
ERROR_CLASS_ALREADY_EXISTS = 1410

LRESULT = ctypes.c_ssize_t
HWND_MESSAGE = wintypes.HWND(-3)
WNDPROC = ctypes.WINFUNCTYPE(
    LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)

user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
]
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p,
]
user32.PostMessageW.argtypes = [
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", wintypes.POINT),
    ]


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]


def parse_hotkey(spec):
    parts = [p.strip().lower() for p in str(spec).split("+") if p.strip()]
    if not parts:
        return None, None
    key = parts.pop()
    mods = MOD_NOREPEAT
    for p in parts:
        if p in ("ctrl", "control"):
            mods |= MOD_CONTROL
        elif p == "alt":
            mods |= MOD_ALT
        elif p == "shift":
            mods |= MOD_SHIFT
        elif p in ("win", "super"):
            mods |= MOD_WIN
        else:
            return None, None
    if len(key) == 1:
        vk = ord(key.upper())
    elif key.startswith("f") and key[1:].isdigit():
        number = int(key[1:])
        if not 1 <= number <= 24:
            return None, None
        vk = 0x70 + number - 1  # VK_F1
    else:
        return None, None
    return mods, vk


class ClipboardListener(threading.Thread):
    """消息窗口线程：监听剪贴板变化 + 响应全局热键。"""

    CLASS_NAME = "MyClipListenerWindow"

    def __init__(self, on_text, on_image, hotkeys=None, on_worker_stop=None,
                 on_files=None):
        super().__init__(name="myclip-listener", daemon=True)
        self._on_text = on_text
        self._on_image = on_image
        self._on_files = on_files
        self._hotkey_specs = [(spec, cb) for spec, cb in (hotkeys or []) if spec and cb]
        self._on_worker_stop = on_worker_stop
        self._hotkey_ids = {}  # hotkey id -> (spec, callback)
        self._ignore_range = None      # 自身写入的序列号区间 (before, after]
        self._ignored_until = 0.0
        self._ignore_lock = threading.Lock()
        self._hwnd = None
        self._work_queue = queue.Queue(maxsize=3)
        self._worker = None
        self._ready = threading.Event()
        self._startup_error = None
        self._wndproc_ref = WNDPROC(self._wnd_proc)

    def write_clipboard(self, writer, payload):
        """串行写入剪贴板，并按序列号区间抑制自身产生的通知。

        EmptyClipboard 与 SetClipboardData 都会递增序列号并触发通知（本机
        实测单次写入 delta 可达 5），因此必须忽略整个 (before, after] 区间
        而不是只忽略终点，否则靠前的通知漏网，自身写入会被重复记录
        （截图重复入库、原记录来源被覆盖）。
        """
        with self._ignore_lock:
            before = win32clip.get_clipboard_sequence()
            ok = writer(payload)
            after = win32clip.get_clipboard_sequence()
            if ok and after != before:
                self._ignore_range = (before, after)
                self._ignored_until = time.monotonic() + 2.0
            else:
                self._ignore_range = None
                self._ignored_until = 0.0
            return ok

    def _consume_ignore(self, sequence):
        with self._ignore_lock:
            rng = self._ignore_range
            if (rng and sequence and rng[0] < sequence <= rng[1]
                    and time.monotonic() <= self._ignored_until):
                return True
            # 不匹配时不清除区间：同一次写入会产生多条通知，需逐条比对；
            # 过期(2s)或下次写入自然覆盖。区间外的序列号只可能来自外部
            # 写入（写入期间剪贴板被我方持有，外部无法插入区间内）。
            return False

    def stop(self):
        if self._hwnd:
            user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)

    def wait_ready(self, timeout=3):
        return self._ready.wait(timeout) and self._startup_error is None

    def run(self):
        self._worker = threading.Thread(target=self._worker_loop,
                                        name="myclip-processor", daemon=True)
        self._worker.start()
        hwnd = None
        listener_added = False
        try:
            hinst = kernel32.GetModuleHandleW(None)
            wc = WNDCLASSEXW()
            wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
            wc.lpfnWndProc = self._wndproc_ref
            wc.hInstance = hinst
            wc.lpszClassName = self.CLASS_NAME
            if not user32.RegisterClassExW(ctypes.byref(wc)):
                if ctypes.GetLastError() != ERROR_CLASS_ALREADY_EXISTS:
                    raise ctypes.WinError()
            hwnd = user32.CreateWindowExW(
                0, self.CLASS_NAME, "MyClip", 0, 0, 0, 0, 0,
                HWND_MESSAGE, None, hinst, None,
            )
            if not hwnd:
                raise ctypes.WinError()
            self._hwnd = hwnd
            if not user32.AddClipboardFormatListener(hwnd):
                raise ctypes.WinError()
            listener_added = True
            for i, (spec, cb) in enumerate(self._hotkey_specs):
                hid = HOTKEY_BASE + i
                mods, vk = parse_hotkey(spec)
                if mods is None:
                    print(f"[myclip] invalid hotkey spec: '{spec}'", flush=True)
                    continue
                if user32.RegisterHotKey(hwnd, hid, mods, vk):
                    self._hotkey_ids[hid] = (spec, cb)
                else:
                    print(f"[myclip] hotkey '{spec}' register failed: "
                          f"{ctypes.WinError()}", flush=True)
            self._ready.set()
            msg = MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as e:
            self._startup_error = e
            self._ready.set()
            traceback.print_exc()
        finally:
            if listener_added:
                user32.RemoveClipboardFormatListener(hwnd)
            for hid in self._hotkey_ids:
                if hwnd:
                    user32.UnregisterHotKey(hwnd, hid)
            if hwnd:
                user32.DestroyWindow(hwnd)
            self._hwnd = None
            while True:
                try:
                    self._work_queue.put_nowait(None)
                    break
                except queue.Full:
                    try:
                        self._work_queue.get_nowait()
                    except queue.Empty:
                        pass
            self._worker.join(timeout=3)
            self._worker = None

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_CLIPBOARDUPDATE:
            self._handle_clipboard()
            return 0
        if msg == WM_HOTKEY:
            item = self._hotkey_ids.get(wparam)
            if item:
                threading.Thread(target=self._safe_hotkey, args=(item[1],),
                                 daemon=True).start()
            return 0
        if msg == WM_CLOSE:
            user32.PostMessageW(hwnd, WM_DESTROY, 0, 0)
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _safe_hotkey(self, callback):
        try:
            callback()
        except Exception:
            traceback.print_exc()

    def _handle_clipboard(self):
        if self._consume_ignore(win32clip.get_clipboard_sequence()):
            return
        try:
            # 文件复制（CF_HDROP）优先：资源管理器复制文件时剪贴板可能同时带文本
            files = win32clip.get_clipboard_files()
            if files:
                self._enqueue(("files", files,
                               win32clip.get_foreground_process()))
                return
            text = win32clip.get_clipboard_text()
            dib = None if text is not None else win32clip.get_clipboard_dib()
            if text is None and dib is None and win32clip.clipboard_has_content():
                # 多程序争用剪贴板时可能瞬时读取失败，稍候重试一次
                time.sleep(0.08)
                text = win32clip.get_clipboard_text()
                if text is None:
                    dib = win32clip.get_clipboard_dib()
                    if dib is None:
                        files = win32clip.get_clipboard_files()
                        if files:
                            self._enqueue(("files", files,
                                           win32clip.get_foreground_process()))
                            return
            source = win32clip.get_foreground_process()
            if text is not None:
                self._enqueue(("text", text, source))
            elif dib:
                self._enqueue(("dib", dib, source))
        except Exception:
            traceback.print_exc()

    def _enqueue(self, item):
        try:
            self._work_queue.put_nowait(item)
        except queue.Full:
            # 极端突发时优先保留最新内容，避免大量图片耗尽内存。
            try:
                self._work_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._work_queue.put_nowait(item)
            except queue.Full:
                print("[myclip] processor queue saturated; newest item dropped",
                      flush=True)
                return
            print("[myclip] processor queue full; oldest item dropped", flush=True)

    def _worker_loop(self):
        try:
            while True:
                item = self._work_queue.get()
                if item is None:
                    return
                kind, data, source = item
                try:
                    if kind == "text":
                        self._on_text(data, source)
                    elif kind == "files":
                        if self._on_files:
                            self._on_files(data, source)
                    else:
                        png = dib_to_png(data)
                        if png:
                            self._on_image(png, source)
                except Exception:
                    traceback.print_exc()
        finally:
            if self._on_worker_stop:
                self._on_worker_stop()
