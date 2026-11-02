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

**为什么运行时生成而不是提交一个 .ico 文件**：
① 项目里现在没有任何图标资源，新增二进制文件还要处理 PyInstaller 打包；
② 画法改动时不需要重新导出，托盘与任务栏不会出现两套不一致的图。

## 图形设计（2026-11-02 重做）

**唯一来源**：本模块的 :func:`draw_mark` 是全项目图标画法的唯一实现。
``system_tray.py`` 直接调用它，**不再自行复制一份绘制代码**——之前两处各写
一份坐标完全相同的大段代码，改一处必然导致任务栏与托盘长得不一样。

### 三条硬约束（都有实测支撑）

**① 底色亮度必须落在 0.143 ~ 0.269。** 图标要同时在浅色与深色任务栏上成立，
底色亮度低于 0.143 会在深色栏上糊、高于 0.269 会在浅色栏上糊。这两个边界由
两侧各需 3:1 对比度反解得到。旧方案的深色玻璃底亮度只有 0.02，远在区间之外
——实测深色任务栏轮廓对比仅 1.41:1，几乎看不见。

渐变两端都取在区间内（0.250 / 0.160），因此**无论任务栏是浅是深，轮廓对比
都不低于 3:1**。渲染成 16px 后实测实心填充色 `#7E66F4`：浅色栏 **3.72:1**、
深色栏 **3.95:1**（16px 是最坏情形，边缘占比最大；32px 起余量更足）。

⚠️ **对比度只能拿「实心填充色」去算，不要拿「边界像素的最小对比度」。**
抗锯齿渐隐带最外圈的像素 alpha 趋近 0、合成后已与任务栏同色，其对比度必然
趋近 1:1——用它当判据，任何图标都会被判成不达标。更麻烦的是这个量能被 alpha
阈值随意操纵：实测同一张 16px 图，阈值取 0 时最小值 1.16、取 50 时 2.62、
取 250 时 2.94，没有有原则的分界点。曾据此误判「16px 深色栏仅 2.92:1 不
达标」而白跑一轮调色。守护见 `tests/test_icon_design_constraints.py` 的
`RenderedContrastTests`。

**② 小尺寸下不能写字。** 16px 下「LUMINA」只剩约 4px 高，必然糊成灰噪声，
这是分辨率决定的，调字体救不回来。品牌靠色彩与剪影承载。

**③ 必须超采样抗锯齿。** PIL 的 ``ImageDraw`` 原语**不做抗锯齿**——实测直接
绘制只有 2 种 alpha 层级（0/255 硬台阶），2 倍画布绘制再降采样后有 172 种。
旧实现直接绘制，边缘全是锯齿，这是「图标看着毛糙」的根因。

顺带一提，``rounded_rectangle`` 会把 ``radius`` **静默钳制**在画布边长的 1/4
（本模块设计画布 96 单位，即上限 24）。实测请求 60 与请求 200 得到完全相同的
图形，超标部分不报错。:data:`_CORNER_RADIUS` 因此不能取太大。

所以下面在 ``_SUPERSAMPLE`` 倍画布上绘制，最后 LANCZOS 降采样。`size` 参数
是**成品的逻辑边长**，画布边长是 ``size * _SUPERSAMPLE``。
"""
import ctypes
import os
import tempfile

# 任务栏实际会用到 16（小图标）、32（高 DPI 小图标）、48/256（Alt+Tab 大图标）。
# 全部塞进一个 ico，Windows 自己按场景挑。
ICON_SIZES = ((16, 16), (32, 32), (48, 48), (256, 256))

# 画布基准尺寸。256 起画再 LANCZOS 缩到各尺寸，比直接按 16px 画更平滑。
_ART_SIZE = 256

# 构图坐标是在 **96px 画布** 上量的，缩放比的分母就是 96。
_DESIGN_SIZE = 96

# 超采样倍数。实测 2 倍即可把 alpha 层级从 2 级提到 172 级；取 3 倍留余量，
# 代价只是绘制时多占几十 KB 内存。
_SUPERSAMPLE = 3

# ---- 配色：渐变两端都落在可行域 [0.143, 0.269] 内 ----
# 亮度 0.250 / 0.160。16px 渲染后实心填充 ~#7E66F4 → 浅色栏 3.72:1、深色 3.95:1
_GRAD_TOP = (0x7B, 0x61, 0xFF)
_GRAD_BOTTOM = (0x6F, 0x57, 0xE6)
_WHITE = (255, 255, 255)

# ---- 图形几何（96px 设计画布上的单位）----
_CORNER_RADIUS = 21.5               # 圆角方形底
_BODY = (19.5, 24.0, 76.5, 78.5)    # 剪贴板纸身
_BODY_RADIUS = 14.0
_NOTCH = (34.0, 24.0, 62.0, 42.0)   # 顶部凹槽（挖空，露出底色）
_NOTCH_RADIUS = 8.0
_CLIP = (38.5, 16.0, 57.5, 47.0)    # 夹子（白色，向上探出）
_CLIP_RADIUS = 9.0

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


def draw_mark(size=_ART_SIZE):
    """画出 Lumina 标记，返回 ``size × size`` 的 RGBA ``Image``。

    **全项目唯一的图标画法**，任务栏（:func:`apply_window_icon`）与托盘
    （``system_tray.TrayIcon.start``）都调用这里。

    构图：圆角方形靛紫渐变底 + 白色剪贴板（纸身 + 顶部凹槽 + 夹子）。
    凹槽是「挖空」出来的——置 0 后露出底色，因此不需要描边，也就避免了
    细描边在 16px 下断线的问题。

    实际绘制在 ``size * _SUPERSAMPLE`` 的画布上，最后 LANCZOS 降采样，
    这样边缘才有抗锯齿（PIL 的绘图原语本身不做抗锯齿）。
    """
    from PIL import Image, ImageChops, ImageDraw

    canvas = size * _SUPERSAMPLE
    scale = canvas / float(_DESIGN_SIZE)

    def s(value):
        return int(round(value * scale))

    def box(rect):
        return tuple(s(v) for v in rect)

    # 竖向渐变的圆角方形底
    base = Image.new("RGB", (canvas, canvas))
    painter = ImageDraw.Draw(base)
    for y in range(canvas):
        t = y / max(1, canvas - 1)
        painter.line(
            (0, y, canvas, y),
            fill=tuple(round(_GRAD_TOP[i] + (_GRAD_BOTTOM[i] - _GRAD_TOP[i]) * t)
                       for i in range(3)))

    # 圆角遮罩：既是底色的形状，也裁掉探出画布的图形
    mask = Image.new("L", (canvas, canvas), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, canvas - 1, canvas - 1), radius=s(_CORNER_RADIUS), fill=255)

    # 白色图形。凹槽填 0 即为挖空，凹槽两侧的底色正好把夹子与纸身分开
    shapes = Image.new("L", (canvas, canvas), 0)
    shape_draw = ImageDraw.Draw(shapes)
    shape_draw.rounded_rectangle(box(_BODY), radius=s(_BODY_RADIUS), fill=255)
    shape_draw.rounded_rectangle(box(_NOTCH), radius=s(_NOTCH_RADIUS), fill=0)
    shape_draw.rounded_rectangle(box(_CLIP), radius=s(_CLIP_RADIUS), fill=255)

    art = base.convert("RGBA")
    art.putalpha(mask)
    white = Image.new("RGBA", (canvas, canvas), (*_WHITE, 0))
    white.putalpha(ImageChops.multiply(shapes, mask))
    art.alpha_composite(white)
    return art.resize((size, size), Image.LANCZOS)


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
        art = draw_mark()
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
