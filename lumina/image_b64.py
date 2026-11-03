"""图片 ↔ Base64 的纯逻辑（**零 UI 依赖**，可脱离 Tk 单测）。

设计边界：

- **文件来源的图片不重新编码**。从磁盘读到的字节原样编码，不做「解码 → 再编码」。
  这样零质量损失、最快，base64 里就是用户文件本身。只有剪贴板来的 DIB 没有
  字节形态，才用 PIL 编成 PNG。
- **保留原格式**。一张 2 MB 的 JPEG 转成 PNG 会变成 8 MB，base64 再膨胀 4/3
  ——对「把图塞进 base64」这个用途是灾难。只有浏览器/常见客户端不认的格式
  （BMP / TIFF / ICO）才降级成 PNG。
- **大小是硬约束，先拒绝再报错**。base64 会膨胀 4/3，且结果要整段塞进 Tk Text；
  超大输入不设限的话不是「慢」，是直接把 UI 线程卡死。这里的上限取得比较保守
  （原图 6 MB），覆盖截图与常见照片，超出时给可读原因而不是让程序假死。
- **宽容解析**。base64 从浏览器 DevTools、CSS、JSON、日志里复制出来常带
  ``data:`` 前缀、换行空白、JSON 的 ``\\/`` 转义、成对引号、缺失的 ``=`` 填充、
  URL-safe 的 ``-_``。这些都吃掉再解码，与 ``jwt_tools.strip_wrapping`` 同一套思路。
"""

import base64
import binascii
import io
import re
from dataclasses import dataclass

from PIL import Image

# 原图字节上限。6 MB 的 PNG 约等于 2K 分辨率截图，足够覆盖日常用途；
# base64 之后约 8 MB，仍在 Tk Text 能流畅插入的范围内。
MAX_SOURCE_BYTES = 6 * 1024 * 1024

# Base64 文本上限。按 4/3 膨胀反推，留一点余量。
MAX_B64_CHARS = 10 * 1024 * 1024

# 解码后的图片字节上限，与 ``media.MAX_IMAGE_PIXELS`` 配套。
MAX_IMAGE_BYTES = 8 * 1024 * 1024

# 像素上限，与 ``media.MAX_IMAGE_PIXELS`` 取同一个值：单张图 6400 万像素
# （约 8000x8000）已远超屏幕截图与手机照片，再大多半是恶意构造的压缩炸弹。
MAX_IMAGE_PIXELS = 64_000_000

# 图片格式 → MIME。``MPO`` 是相机产出的多图 JPEG，按 JPEG 处理。
FORMAT_MIME = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "MPO": "image/jpeg",
    "GIF": "image/gif",
    "WEBP": "image/webp",
    "BMP": "image/bmp",
    "TIFF": "image/tiff",
    "ICO": "image/x-icon",
}

# 这几种可以直接交给前端；其余格式先转 PNG 再编码。
WEB_SAFE_FORMATS = frozenset({"PNG", "JPEG", "MPO", "GIF", "WEBP"})

# 界面显示用的折行宽度。76 是 MIME / PEM / GNU coreutils ``base64`` 的惯例值，
# 不是随便挑的：这个宽度下导出的 .txt 可以直接喂给 ``base64 -d`` 或 PEM 解析器。
WRAP_WIDTH = 76

# ``data:image/png;base64,`` 前缀。分号后的参数（如 ``;base64``）不参与判断，
# 因为 ``data:image/svg+xml;utf8,...`` 这类非 base64 的形态解不出来、会落到
# 后面的解码错误分支里，报错更直接。
_DATA_URI = re.compile(r"^\s*data:(?P<mime>[^;,]*)(?:;[^,]*)?,", re.IGNORECASE)


class ImageB64Error(ValueError):
    """输入不是可转换的图片，或不是可解码的 Base64。"""


@dataclass(frozen=True)
class Encoded:
    """编码结果。``text`` 是最终产物（纯 Base64 或 Data URI）。"""

    text: str
    mime: str
    format: str
    width: int
    height: int
    raw_bytes: int
    data_uri: bool = False


@dataclass(frozen=True)
class Decoded:
    """解码结果。``image`` 已完成 ``load()``，可以脱离源字节独立使用。"""

    image: Image.Image
    mime: str
    format: str
    width: int
    height: int
    raw_bytes: int


def human_size(count):
    """字节数 → ``342.1 KB`` 这种可读字符串。"""
    value = float(max(0, int(count)))
    unit = "B"
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            break
        value /= 1024
    return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"


def inspect_image(raw):
    """校验字节确实是图片，返回 ``(格式, 宽, 高)``。

    只读文件头，不解码像素，所以对大图也很快。真正的「能不能解出来」由
    :func:`decode_base64` 的 ``load()`` 兜底——这两件事失败原因不同，报错文案
    也不同，不要合并。
    """
    if not raw:
        raise ImageB64Error("图片内容为空。")
    try:
        with Image.open(io.BytesIO(raw)) as probe:
            fmt = (probe.format or "").upper()
            width, height = probe.size
    except Exception as exc:  # PIL 抛的异常类型很杂，统一转成语义错误
        raise ImageB64Error(f"不是可识别的图片：{exc}") from exc
    if width * height > MAX_IMAGE_PIXELS:
        raise ImageB64Error(
            f"图片像素过多（{width} × {height}），上限 {MAX_IMAGE_PIXELS:,} 像素。")
    return fmt, width, height


def _to_png(raw):
    """把不受前端支持的格式转成 PNG 字节。"""
    try:
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
            out = io.BytesIO()
            # 带透明通道的保留 alpha；其余交给 PNG 自己决定。
            image.save(out, "PNG")
    except Exception as exc:
        raise ImageB64Error(f"图片转换失败：{exc}") from exc
    return out.getvalue()


def encode_bytes(raw, *, data_uri=False):
    """图片字节 → :class:`Encoded`。

    ``raw`` 来自文件时是**原样字节**；来自剪贴板时应先经 PIL 编成 PNG
    （DIB 没有可移植的字节形态）。
    """
    if not raw:
        raise ImageB64Error("图片内容为空。")
    if len(raw) > MAX_SOURCE_BYTES:
        raise ImageB64Error(
            f"图片过大（{human_size(len(raw))}），上限 {human_size(MAX_SOURCE_BYTES)}。")
    fmt, width, height = inspect_image(raw)
    if fmt not in WEB_SAFE_FORMATS:
        raw = _to_png(raw)
        fmt, width, height = inspect_image(raw)
    payload = base64.b64encode(raw).decode("ascii")
    mime = FORMAT_MIME.get(fmt, "image/png")
    text = f"data:{mime};base64,{payload}" if data_uri else payload
    return Encoded(text=text, mime=mime, format=fmt, width=width, height=height,
                   raw_bytes=len(raw), data_uri=bool(data_uri))


def encode_image(image, *, data_uri=False):
    """PIL 图像 → :class:`Encoded`（统一编成 PNG）。剪贴板取图走这条。"""
    buffer = io.BytesIO()
    try:
        image.save(buffer, "PNG")
    except Exception as exc:
        raise ImageB64Error(f"图片编码失败：{exc}") from exc
    return encode_bytes(buffer.getvalue(), data_uri=data_uri)


def wrap_text(payload, width=WRAP_WIDTH):
    """把长 base64 按固定宽度折行，**只用于显示与落盘**。

    **这是性能修复，不是排版偏好。** base64 是一整行几十万字符且不含空格，
    直接塞进 ``wrap="char"`` 的 ``tk.Text`` 会让 Tk 逐个字符找折行点：实测
    1.79 M 字符要 **310 秒**（另一次测得 48.7 秒），而先折成 76 字符的行再插入
    只要 **18.4 ms**。``insert()`` 调用本身始终是 2.4 ms——时间全在 Tk 的换行
    计算上。折行等于是替 Tk 把这件事做完了。

    76 字符还顺带对齐了 MIME / GNU ``base64`` 的惯例，导出的 .txt 可以直接
    喂给 ``base64 -d`` 或 PEM 解析器。

    **解码一侧不受影响**：``strip_payload`` 会吃掉所有空白，换行、``\\r\\n``、
    甚至折在 ``data:`` 前缀中间都能还原。往返有
    ``test_wrap_round_trips_through_decode`` 钉住。
    """
    if not payload:
        return ""
    step = int(width)
    # 非正宽度视为「不折行」原样返回，**不是**夹到 1。夹到 1 会得到逐字符
    # 折行的荒谬结果（``'ABC'`` → ``'A\\nB\\nC'``），而且看起来还「成功」了。
    # 调用方从不传这个值，这里纯粹是防御，那就该选语义最不意外的那个。
    if step < 1:
        return payload
    return "\n".join(payload[i:i + step] for i in range(0, len(payload), step))


def unwrap_text(text):
    """去掉折行产生的空白，得回单行 base64。:func:`strip_payload` 的别名。

    存在的意义是让调用方读起来明确：「我要的是紧凑形式」而不是「我在清理
    脏输入」。两者行为必须完全一致，有测试钉住。

    **注意它连 ``data:`` 前缀一起去掉**（因为就是 ``strip_payload``）。要把
    「显示用的折行文本」还原成「原本该复制出去的那串」而**保留前缀**时，
    用 :func:`compact`。
    """
    return strip_payload(text)[0]


def compact(text):
    """只吃掉所有空白，**不动** ``data:`` 前缀与引号。

    :func:`unwrap_text` 会把前缀也剥掉，于是「有缓冲」与「没缓冲」两条复制
    路径拿到的结果不一致——前者带前缀、后者不带。这个方法用于后者：折行是
    唯一的差别，前缀必须留着。
    """
    return "".join((text or "").split())


def strip_payload(text):
    """去掉 Data URI 前缀、成对引号、JSON 的 ``\\/`` 转义与所有空白。

    返回 ``(payload, mime)``；``mime`` 只在输入带 ``data:`` 前缀时非空。
    空白必须全部去掉：从编辑器或终端里复制出来的 base64 常被折成多行，
    逐行拼接又容易在接缝处夹进 ``\\r``。
    """
    s = (text or "").strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        s = s[1:-1].strip()
    mime = ""
    match = _DATA_URI.match(s)
    if match:
        mime = (match.group("mime") or "").strip().lower()
        s = s[match.end():]
    # JSON 字符串里的斜杠转义：接口返回的 base64 常长这样
    s = s.replace("\\/", "/")
    return "".join(s.split()), mime


def decode_base64(text):
    """Base64 / Data URI 文本 → :class:`Decoded`。"""
    payload, uri_mime = strip_payload(text)
    if not payload:
        raise ImageB64Error("Base64 内容为空。")
    if len(payload) > MAX_B64_CHARS:
        raise ImageB64Error(
            f"Base64 过长（{len(payload):,} 字符），上限 {MAX_B64_CHARS:,} 字符。")

    # URL-safe 的 -_ 与标准 base64 的 +/ 混用在 JWT、URL 参数里很常见，两边都收。
    s = payload.replace("-", "+").replace("_", "/")
    padding = (-len(s)) % 4
    # 长度 4n+1 在 base64 里不合法，补三个 = 也无解，说明输入被截断了。
    if padding == 3:
        raise ImageB64Error("Base64 长度非法，内容可能被截断。")
    s += "=" * padding
    try:
        raw = base64.b64decode(s, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ImageB64Error(f"不是合法的 Base64：{exc}") from exc

    if len(raw) > MAX_IMAGE_BYTES:
        raise ImageB64Error(
            f"解码后图片过大（{human_size(len(raw))}），"
            f"上限 {human_size(MAX_IMAGE_BYTES)}。")
    fmt, width, height = inspect_image(raw)
    try:
        with Image.open(io.BytesIO(raw)) as opened:
            # ``copy()`` 内部就会完整解码（等价于 ``load()`` + 深拷贝），
            # 于是「文件头合法但像素数据被截断」在这一步抛 OSError。
            # 顺带把结果与 BytesIO 解耦：出函数后字节流已关闭，图仍可用。
            #
            # **别再单独加一行 ``opened.load()``**：实测它与 ``copy()`` 重复，
            # 删掉后行为完全一致（这是变异验证查出来的「等价变异」，不是
            # 测试盲区）。留两行只会让人以为其中一个有额外作用。
            image = opened.copy()
    except Exception as exc:
        raise ImageB64Error(f"图片数据不完整或已损坏：{exc}") from exc

    mime = uri_mime if uri_mime.startswith("image/") else FORMAT_MIME.get(fmt, "")
    return Decoded(image=image, mime=mime, format=fmt, width=width, height=height,
                   raw_bytes=len(raw))


# 落盘扩展名。**跟实际格式走，不一律写 .png**：用户要的是「把这段 base64
# 还原成原文件」，强制转 PNG 属于额外动作。``MPO`` 是相机多图 JPEG，PIL 能读
# 不能写，落盘时按 JPEG 处理。
SAVE_SUFFIX = {
    "PNG": ".png",
    "JPEG": ".jpg",
    "MPO": ".jpg",
    "GIF": ".gif",
    "WEBP": ".webp",
    "BMP": ".bmp",
    "TIFF": ".tif",
    "ICO": ".ico",
}


def save_suffix(fmt):
    """格式名 → 落盘扩展名。未知格式按 PNG 处理（PIL 一定写得出）。"""
    return SAVE_SUFFIX.get((fmt or "").upper(), ".png")


def writable_format(fmt):
    """格式名 → PIL **能写**的格式名。``MPO`` 只有读能力，落盘改 JPEG。"""
    name = (fmt or "").upper()
    return "JPEG" if name == "MPO" else (name or "PNG")


__all__ = [
    "FORMAT_MIME",
    "MAX_B64_CHARS",
    "MAX_IMAGE_BYTES",
    "MAX_IMAGE_PIXELS",
    "MAX_SOURCE_BYTES",
    "SAVE_SUFFIX",
    "WEB_SAFE_FORMATS",
    "WRAP_WIDTH",
    "Decoded",
    "Encoded",
    "ImageB64Error",
    "compact",
    "decode_base64",
    "encode_bytes",
    "encode_image",
    "human_size",
    "inspect_image",
    "save_suffix",
    "strip_payload",
    "unwrap_text",
    "wrap_text",
    "writable_format",
]
