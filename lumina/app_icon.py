"""应用图标：运行时绘制，不依赖任何二进制资源。

**为什么不用 `iconphoto`**：实测对 ``overrideredirect(True)`` 的窗口完全无效，
``WM_GETICON`` 三个槽位（small / big / small2）全都返回 0——任务栏只能退化成
Windows 默认图标。

**为什么也不用 `iconbitmap`**：它能设上图标，但**不区分尺寸**——实测把 ico 里
32x32 那张塞进了本该放 16x16 的 ``ICON_SMALL`` 槽位（三个槽位拿到同一个 HICON，
``GetIconInfo`` 读出来是 ``(32, 32)``）。任务栏按钮取 ``ICON_SMALL`` 绘制，尺寸
对不上时就画不出图标。正确做法是 ``LoadImageW`` 按尺寸各加载一次，再用
``WM_SETICON`` 分别写进 ``ICON_SMALL`` / ``ICON_BIG``（实测分别得到 16x16 与
32x32，``ICON_SMALL2`` 由系统自动跟随 SMALL）。

画法与 ``system_tray.py`` 的托盘图标保持一致（深色玻璃底 + 蓝紫光晕 +
剪贴板剪影 + LUMINA 字样），让托盘和任务栏看起来是同一个应用。

**为什么运行时生成而不是提交一个 .ico 文件**：
① 项目里现在没有任何图标资源，新增二进制文件还要处理 PyInstaller 打包；
② 画法改动时不需要重新导出，托盘与任务栏不会出现两套不一致的图。
"""
import ctypes
import os
import tempfile

# 任务栏实际会用到 16（小图标）、32（高 DPI 小图标）、48/256（Alt+Tab 大图标）。
# 全部塞进一个 ico，Windows 自己按场景挑。
ICON_SIZES = ((16, 16), (32, 32), (48, 48), (256, 256))

# 画布基准尺寸。256 起画再 LANCZOS 缩到各尺寸，比直接按 16px 画更平滑。
_ART_SIZE = 256

# 构图坐标是在 **96px 画布** 上量的（托盘那版先画 96 再缩到 48），
# 所以缩放比的分母必须是 96，不是 48——按 48 算会让坐标放大一倍直接溢出画布。
_DESIGN_SIZE = 96

# ---------- Win32 常量 ----------
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
# WM_SETICON / WM_GETICON 的槽位索引
ICON_SMALL = 0
ICON_BIG = 1
WM_SETICON = 0x0080
WM_GETICON = 0x007F

# 尺寸 → HICON。进程内共享，避免反复 LoadImageW 泄漏句柄。
_ICON_CACHE = {}


def _draw_mark(size=_ART_SIZE):
    """画出 Lumina 标记，返回 RGBA 的 PIL Image。

    构图与托盘图标同源：竖向渐变底 → 模糊光晕 → 圆角描边 → 剪贴板剪影 →
    品牌字样。深色底 + 冷色高光，缩到 16px 仍能看出「白色纸片」主体。
    """
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    scale = size / float(_DESIGN_SIZE)
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    def s(value):
        return round(value * scale)

    # 竖向渐变底：上暗下亮的靛蓝，给玻璃质感
    for y in range(size):
        t = y / max(1, size - 1)
        draw.line((0, y, size, y),
                  fill=(round(12 + 28 * t), round(20 + 14 * t),
                        round(50 + 80 * t), 255))

    # 蓝紫光晕：先画实心椭圆再高斯模糊，得到柔和外发光
    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((s(11), 0, s(85), s(66)),
                                 fill=(76, 142, 255, 110))
    image.alpha_composite(glow.filter(ImageFilter.GaussianBlur(s(8))))
    draw = ImageDraw.Draw(image)

    # 外圈描边。线宽按比例给，太小会在大尺寸下断线
    draw.rounded_rectangle((s(5), s(5), s(91), s(91)),
                           radius=s(18), outline=(117, 179, 255, 220),
                           width=max(1, s(2)))

    # 剪贴板：纸片主体 + 顶部夹子 + 三行内容
    draw.rounded_rectangle((s(28), s(13), s(68), s(59)),
                           radius=s(7), fill=(246, 249, 255, 255))
    draw.rounded_rectangle((s(37), s(7), s(59), s(19)),
                           radius=s(5), fill=(246, 249, 255, 255))
    ink = (68, 92, 169, 255)
    draw.line((s(39), s(31), s(57), s(31)), fill=ink, width=max(1, s(4)))
    draw.line((s(39), s(42), s(57), s(42)), fill=ink, width=max(1, s(4)))
    draw.ellipse((s(39), s(49), s(43), s(53)), fill=(103, 81, 220, 255))
    draw.line((s(49), s(51), s(58), s(51)), fill=ink, width=max(1, s(3)))

    # 品牌字样。缺字体时回退默认字体，宁可字丑也不能抛异常
    font_path = os.path.join(
        os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "segoeuib.ttf")
    try:
        font = ImageFont.truetype(font_path, max(6, s(12)))
    except OSError:
        font = ImageFont.load_default()
    label = "LUMINA"
    bbox = draw.textbbox((0, 0), label, font=font)
    draw.text(((size - (bbox[2] - bbox[0])) // 2 - bbox[0], s(70)), label,
              font=font, fill=(246, 249, 255, 255))
    return image


def ensure_icon_file(path=None):
    """确保多尺寸 ico 存在于磁盘，返回其路径；失败返回 ``None``。

    成功生成过一次就复用，不再重画。写入用临时文件 + ``os.replace``，
    避免两个实例同时启动时读到写了一半的文件。
    """
    if path is None:
        path = os.path.join(tempfile.gettempdir(), "lumina-app.ico")
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    try:
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        art = _draw_mark()
        temporary = f"{path}.{os.getpid()}.tmp"
        art.save(temporary, format="ICO", sizes=ICON_SIZES)
        os.replace(temporary, path)
        return path
    except Exception:
        try:
            if 'temporary' in locals() and os.path.exists(temporary):
                os.remove(temporary)
        except OSError:
            pass
        return None


def _top_level_hwnd(window):
    """沿父链找到最外层窗口句柄。

    ``winfo_id()`` 返回的是内层 ``TkChild``，真正代表窗口的是取不到父窗口的
    那层 ``TkTopLevel``。**不要缓存**：``overrideredirect`` 翻转会重建 HWND。
    """
    if os.name != "nt":
        return None
    try:
        user32 = ctypes.windll.user32
        user32.GetParent.restype = ctypes.c_void_p
        user32.GetParent.argtypes = [ctypes.c_void_p]
        hwnd = window.winfo_id()
        for _ in range(6):
            parent = user32.GetParent(ctypes.c_void_p(hwnd))
            if not parent:
                break
            hwnd = parent
        return hwnd
    except Exception:
        return None


def _load_icon(size):
    """按边长加载 HICON，**进程内只加载一次**。

    必须缓存：``claim_taskbar`` 在每次显示/还原时都会重设图标，若不缓存就是
    每次 ``LoadImageW`` 产生一个新句柄、旧句柄再没人释放——长时间使用会持续
    泄漏 GDI/USER 资源。

    句柄由进程持有到退出，**不要 ``DestroyIcon``**：窗口可能仍在引用它。
    """
    cached = _ICON_CACHE.get(size)
    if cached:
        return cached
    path = ensure_icon_file()
    if not path:
        return None
    try:
        load = ctypes.windll.user32.LoadImageW
        load.restype = ctypes.c_void_p
        load.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint,
                         ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        hicon = load(None, path, IMAGE_ICON, size, size, LR_LOADFROMFILE)
        if hicon:
            _ICON_CACHE[size] = hicon
        return hicon
    except Exception:
        return None


def apply_window_icon(window):
    """给任意窗口设置任务栏图标（分尺寸），返回是否成功。

    每个尺寸各 ``LoadImageW`` 一次（结果缓存），再 ``WM_SETICON`` 写进对应
    槽位。这样任务栏取 ``ICON_SMALL`` 时拿到的是真正的 16x16，而不是被强行
    缩放的 32x32——**这正是「窗口一显示图标就没了」的成因**（Tk 的
    ``iconbitmap`` 三个槽位塞同一个 32x32 HICON）。
    """
    if os.name != "nt":
        return False
    hwnd = _top_level_hwnd(window)
    if not hwnd:
        return False
    try:
        send = ctypes.windll.user32.SendMessageW
        send.restype = ctypes.c_ssize_t
        send.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t,
                         ctypes.c_ssize_t]
        changed = False
        for index, size in ((ICON_SMALL, 16), (ICON_BIG, 32)):
            hicon = _load_icon(size)
            if not hicon:
                continue
            send(ctypes.c_void_p(hwnd), WM_SETICON, index, hicon)
            changed = True
        return changed
    except Exception:
        return False
