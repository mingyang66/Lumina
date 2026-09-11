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
    bpp, = struct.unpack_from("<H", dib, 14)
    compression, = struct.unpack_from("<I", dib, 16)
    if header_size < 40 or width <= 0 or height == 0 or compression not in (0, 3):
        return None
    if bpp == 32:
        mode, rawmode = "RGBA", "BGRA"
    elif bpp == 24:
        mode, rawmode = "RGB", "BGR"
    else:
        return None
    offset = header_size
    if compression == 3 and header_size == 40:
        offset += 12
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
