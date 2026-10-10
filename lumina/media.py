import io
import struct

import mss
from PIL import Image

MAX_IMAGE_PIXELS = 64_000_000


def grab_screen(monitor=1):
    sct_cls = getattr(mss, "MSS", mss.mss)
    with sct_cls() as sct:
        if not (0 <= monitor < len(sct.monitors)):
            monitor = 0
        shot = sct.grab(sct.monitors[monitor])
    return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def to_png(img):
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def dib_to_image(dib):
    if not dib or len(dib) < 40:
        return None
    header_size, width, height = struct.unpack_from("<Iii", dib, 0)
    planes, bpp = struct.unpack_from("<HH", dib, 12)
    compression, = struct.unpack_from("<I", dib, 16)
    if (header_size not in (40, 52, 56, 108, 124) or header_size > len(dib)
            or planes != 1 or width <= 0 or height == 0
            or compression not in (0, 3) or bpp not in (24, 32)):
        return None
    mode, rawmode = "RGB", "BGRX" if bpp == 32 else "BGR"
    offset = header_size
    if compression == 3:
        if bpp != 32:
            return None
        if header_size == 40:
            offset += 12
        if len(dib) < offset:
            return None
        masks = struct.unpack_from("<III", dib, 40)
        # 只支持标准 8 位 BGR 掩码；其他位宽、重叠或通道排列明确拒绝。
        if masks != (0x00FF0000, 0x0000FF00, 0x000000FF):
            return None
        alpha = struct.unpack_from("<I", dib, 52)[0] if header_size >= 56 else 0
        if alpha not in (0, 0xFF000000):
            return None
        if alpha:
            mode, rawmode = "RGBA", "BGRA"
    colors_used, = struct.unpack_from("<I", dib, 32)
    offset += colors_used * 4
    # 带嵌入/链接色彩配置的 V5 DIB 尚不支持，避免误判像素偏移。
    if header_size == 124 and any(struct.unpack_from("<II", dib, 112)):
        return None
    top_down = height < 0
    height = abs(height)
    if width * height > MAX_IMAGE_PIXELS:
        return None
    stride = ((width * bpp + 31) // 32) * 4
    size = stride * height
    data = dib[offset:offset + size]
    if len(data) < size:
        return None
    return Image.frombuffer(mode, (width, height), data, "raw", rawmode, stride,
                            1 if top_down else -1)


def dib_to_png(dib):
    img = dib_to_image(dib)
    return to_png(img) if img else None


def image_to_dib(img):
    img = img.convert("RGB")
    w, h = img.size
    raw = img.tobytes("raw", "BGRX")
    stride = w * 4
    rows = b"".join(raw[(h - 1 - i) * stride:(h - i) * stride] for i in range(h))
    header = struct.pack("<IiiHHIIiiII", 40, w, h, 1, 32, 0, len(rows), 0, 0, 0, 0)
    return header + rows
