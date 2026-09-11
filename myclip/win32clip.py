import ctypes
import os
import time
from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

CF_DIB = 8
CF_UNICODETEXT = 13
CF_HDROP = 15
GMEM_MOVEABLE = 0x0002

shell32 = ctypes.windll.shell32
shell32.DragQueryFileW.restype = wintypes.UINT
shell32.DragQueryFileW.argtypes = [ctypes.c_void_p, wintypes.UINT,
                                   wintypes.LPWSTR, wintypes.UINT]
MAX_DIB_BYTES = 64 * 1024 * 1024

kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
kernel32.GlobalSize.restype = ctypes.c_size_t
kernel32.GlobalSize.argtypes = [ctypes.c_void_p]
kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
user32.GetClipboardData.restype = ctypes.c_void_p
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.SetClipboardData.restype = ctypes.c_void_p
user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
user32.GetClipboardSequenceNumber.restype = wintypes.DWORD


def open_clipboard(retries=10, delay=0.03):
    """OpenClipboard 可能因其他程序正持有剪贴板而瞬时失败，重试提高可靠性。"""
    for _ in range(retries):
        if user32.OpenClipboard(None):
            return True
        time.sleep(delay)
    return False


def get_clipboard_sequence():
    return int(user32.GetClipboardSequenceNumber())


def get_clipboard_text():
    if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
        return None
    if not open_clipboard():
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def get_clipboard_dib(max_bytes=MAX_DIB_BYTES):
    if not user32.IsClipboardFormatAvailable(CF_DIB):
        return None
    if not open_clipboard():
        return None
    try:
        handle = user32.GetClipboardData(CF_DIB)
        if not handle:
            return None
        size = kernel32.GlobalSize(handle)
        if not size or size > max_bytes:
            return None
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            buf = ctypes.create_string_buffer(size)
            ctypes.memmove(buf, ptr, size)
            return buf.raw
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def get_clipboard_files(max_files=500):
    """读取 CF_HDROP（资源管理器复制的文件），返回路径列表；无则 None。"""
    if not user32.IsClipboardFormatAvailable(CF_HDROP):
        return None
    if not open_clipboard():
        return None
    try:
        handle = user32.GetClipboardData(CF_HDROP)
        if not handle:
            return None
        # 注意：HDROP 属于剪贴板，不能 DragFinish
        count = shell32.DragQueryFileW(handle, 0xFFFFFFFF, None, 0)
        files = []
        for i in range(min(count, max_files)):
            n = shell32.DragQueryFileW(handle, i, None, 0)
            if not n:
                continue
            buf = ctypes.create_unicode_buffer(n + 1)
            shell32.DragQueryFileW(handle, i, buf, n + 1)
            if buf.value:
                files.append(buf.value)
        return files or None
    finally:
        user32.CloseClipboard()


def _set_clipboard_data(fmt, payload):
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(payload))
    if not handle:
        return False
    ptr = kernel32.GlobalLock(handle)
    if not ptr:
        kernel32.GlobalFree(handle)
        return False
    try:
        ctypes.memmove(ptr, payload, len(payload))
    finally:
        kernel32.GlobalUnlock(handle)
    if not open_clipboard():
        kernel32.GlobalFree(handle)
        return False
    try:
        user32.EmptyClipboard()
        ok = bool(user32.SetClipboardData(fmt, handle))
    finally:
        user32.CloseClipboard()
    if not ok:
        kernel32.GlobalFree(handle)
    return ok


def set_clipboard_dib(dib):
    return _set_clipboard_data(CF_DIB, dib)


def set_clipboard_text(text):
    return _set_clipboard_data(CF_UNICODETEXT,
                               text.encode("utf-16-le") + b"\x00\x00")


def get_foreground_hwnd():
    user32.GetForegroundWindow.restype = wintypes.HWND
    return user32.GetForegroundWindow()


def set_foreground_hwnd(hwnd):
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    return bool(user32.SetForegroundWindow(hwnd))


def send_ctrl_v():
    VK_CONTROL, VK_V = 0x11, 0x56
    KEYEVENTF_KEYUP = 0x0002
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    user32.keybd_event(VK_V, 0, 0, 0)
    time.sleep(0.03)
    user32.keybd_event(VK_V, 0, KEYEVENTF_KEYUP, 0)
    user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)


def clipboard_has_content():
    return bool(user32.IsClipboardFormatAvailable(CF_UNICODETEXT) or
                user32.IsClipboardFormatAvailable(CF_DIB) or
                user32.IsClipboardFormatAvailable(CF_HDROP))


def mouse_button_down():
    """当前是否有鼠标左/右键处于按下状态（菜单外部点击检测用）。"""
    return bool((user32.GetAsyncKeyState(0x01) & 0x8000) or
                (user32.GetAsyncKeyState(0x02) & 0x8000))


def get_foreground_process():
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        hproc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not hproc:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            kernel32.QueryFullProcessImageNameW.argtypes = [
                wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
            ]
            if kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value)
        finally:
            kernel32.CloseHandle(hproc)
    except OSError:
        pass
    return ""
