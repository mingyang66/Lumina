"""``image_b64`` 的纯逻辑测试：不解 Tk，直接断言字节与异常。

这个模块刻意做到零 UI 依赖（与 ``jwt_tools`` 同一条纪律），所以这里能全跑
在内存里——图片用 PIL 现场生成，不留任何临时文件。
"""
import base64
import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from lumina import image_b64  # noqa: E402
from lumina.image_b64 import (  # noqa: E402
    ImageB64Error,
    compact,
    decode_base64,
    encode_bytes,
    encode_image,
    human_size,
    strip_payload,
    unwrap_text,
    wrap_text,
)


def make_png(size=(8, 6), color=(200, 40, 40)):
    buffer = io.BytesIO()
    Image.new('RGB', size, color).save(buffer, 'PNG')
    return buffer.getvalue()


def make_jpeg(size=(16, 12)):
    buffer = io.BytesIO()
    Image.new('RGB', size, (30, 90, 200)).save(buffer, 'JPEG')
    return buffer.getvalue()


def make_bmp(size=(10, 10)):
    buffer = io.BytesIO()
    Image.new('RGB', size, (10, 200, 90)).save(buffer, 'BMP')
    return buffer.getvalue()


class EncodeTests(unittest.TestCase):
    def test_png_roundtrip(self):
        raw = make_png()
        result = encode_bytes(raw)
        self.assertEqual(result.format, 'PNG')
        self.assertEqual(result.mime, 'image/png')
        self.assertEqual((result.width, result.height), (8, 6))
        self.assertEqual(base64.b64decode(result.text), raw)
        self.assertFalse(result.data_uri)

    def test_jpeg_bytes_are_not_reencoded(self):
        """**原样编码**：文件来的字节不做解码-再编码，零质量损失。

        这条是设计约束而不是实现细节：一旦中间过一遍 PIL，JPEG 会被重采样，
        用户拿到的 base64 与原始文件就不再是同一张图。
        """
        raw = make_jpeg()
        result = encode_bytes(raw)
        self.assertEqual(result.format, 'JPEG')
        self.assertEqual(result.mime, 'image/jpeg')
        self.assertEqual(base64.b64decode(result.text), raw)

    def test_web_unsafe_format_falls_back_to_png(self):
        """BMP 转 PNG：浏览器与多数客户端不认 BMP，且体积大得离谱。"""
        result = encode_bytes(make_bmp())
        self.assertEqual(result.format, 'PNG')
        self.assertEqual(result.mime, 'image/png')
        self.assertTrue(result.text)

    def test_data_uri_prefix(self):
        result = encode_bytes(make_png(), data_uri=True)
        self.assertTrue(result.data_uri)
        self.assertTrue(result.text.startswith('data:image/png;base64,'))
        self.assertEqual(decode_base64(result.text).format, 'PNG')

    def test_encode_image_always_png(self):
        result = encode_image(Image.new('RGBA', (5, 7), (0, 0, 0, 0)))
        self.assertEqual(result.format, 'PNG')
        self.assertEqual((result.width, result.height), (5, 7))

    def test_rejects_empty(self):
        with self.assertRaises(ImageB64Error):
            encode_bytes(b'')

    def test_rejects_non_image(self):
        with self.assertRaises(ImageB64Error) as ctx:
            encode_bytes(b'this is definitely not an image')
        self.assertIn('图片', str(ctx.exception))

    def test_rejects_oversized_source(self):
        """超限要**先拒绝**，不能先编码再发现塞不进文本框。"""
        giant = b'\x89PNG\r\n\x1a\n' + b'\x00' * (image_b64.MAX_SOURCE_BYTES + 1)
        with self.assertRaises(ImageB64Error) as ctx:
            encode_bytes(giant)
        self.assertIn('过大', str(ctx.exception))


class StripPayloadTests(unittest.TestCase):
    def test_removes_data_uri(self):
        payload, mime = strip_payload('data:image/png;base64,AAAA')
        self.assertEqual(payload, 'AAAA')
        self.assertEqual(mime, 'image/png')

    def test_removes_all_whitespace(self):
        self.assertEqual(strip_payload('AA\nBB\r\nCC\tDD')[0], 'AABBCCDD')

    def test_removes_quotes(self):
        self.assertEqual(strip_payload('"AAAA"')[0], 'AAAA')
        self.assertEqual(strip_payload("'AAAA'")[0], 'AAAA')

    def test_unescapes_json_slashes(self):
        self.assertEqual(strip_payload('a\\/b')[0], 'a/b')

    def test_no_prefix_yields_empty_mime(self):
        self.assertEqual(strip_payload('AAAA'), ('AAAA', ''))


class DecodeTests(unittest.TestCase):
    def test_roundtrip_from_encoder(self):
        raw = make_png()
        encoded = encode_bytes(raw)
        decoded = decode_base64(encoded.text)
        self.assertEqual(decoded.format, 'PNG')
        self.assertEqual((decoded.width, decoded.height), (8, 6))
        self.assertEqual(decoded.raw_bytes, len(raw))
        # 结果必须已脱离源字节：源 buffer 用完就丢，图仍要能取像素。
        self.assertEqual(decoded.image.getpixel((0, 0)), (200, 40, 40))

    def test_accepts_wrapped_and_quoted_input(self):
        raw = make_png()
        b64 = base64.b64encode(raw).decode('ascii')
        wrapped = '\n'.join(b64[i:i + 40] for i in range(0, len(b64), 40))
        self.assertEqual(decode_base64(f'"{wrapped}"').format, 'PNG')

    def test_accepts_missing_padding(self):
        raw = make_png()
        b64 = base64.b64encode(raw).decode('ascii').rstrip('=')
        self.assertEqual(decode_base64(b64).format, 'PNG')

    def test_accepts_url_safe_alphabet(self):
        """URL 参数里的 base64 用 ``-_``，不能因此拒收。"""
        raw = make_png()
        standard = base64.b64encode(raw).decode('ascii')
        standard = standard.rstrip('=')
        url_safe = standard.replace('+', '-').replace('/', '_')
        self.assertEqual(decode_base64(url_safe).format, 'PNG')

    def test_rejects_empty(self):
        with self.assertRaises(ImageB64Error):
            decode_base64('   ')

    def test_rejects_illegal_characters(self):
        with self.assertRaises(ImageB64Error) as ctx:
            decode_base64('@@@@')
        self.assertIn('Base64', str(ctx.exception))

    def test_rejects_truncated_payload(self):
        """长度 4n+1 无解，必须报「可能被截断」而不是含糊的解码错误。"""
        with self.assertRaises(ImageB64Error) as ctx:
            decode_base64('A')
        self.assertIn('截断', str(ctx.exception))

    def test_rejects_valid_base64_that_is_not_an_image(self):
        b64 = base64.b64encode(b'plain text, no image here').decode('ascii')
        with self.assertRaises(ImageB64Error) as ctx:
            decode_base64(b64)
        self.assertIn('图片', str(ctx.exception))

    def test_rejects_header_only_image(self):
        """只有文件头、像素数据被截断：必须靠 ``load()`` 才发现，不能假成功。

        **截断长度是关键**。只砍一半会把 PNG 砍到连 ``Image.open`` 都认不出
        （抛 ``UnidentifiedImageError``），那样测试在「不调 load()」的实现下
        也照样绿——变异验证时这一条漏过正是因为砍得太狠。有效的输入是
        「文件头完整、像素数据缺一截」：实测 120×90 的 PNG 砍掉尾部 40 字节
        后 ``open`` 仍返回 ``(120, 90)``，只有 ``load()`` 抛 OSError。
        """
        buffer = io.BytesIO()
        Image.new('RGB', (120, 90), (200, 30, 30)).save(buffer, 'PNG')
        raw = buffer.getvalue()
        truncated = raw[:-40]

        # 先钉死输入的有效性：open 必须成功，否则这条测试失去判别力。
        with Image.open(io.BytesIO(truncated)) as probe:
            self.assertEqual(probe.size, (120, 90),
                             '截断后的输入连文件头都坏了，用例失效')

        b64 = base64.b64encode(truncated).decode('ascii')
        with self.assertRaises(ImageB64Error):
            decode_base64(b64)

    def test_decode_pressure_tests_copy_path(self):
        """「截断的图必须报错」这条靠的是 ``copy()``，不是额外的 ``load()``。

        实现里只写 ``opened.copy()``——它的内部就会完整解码。**不要再补一行
        ``opened.load()``**：变异验证显示删掉 ``load()`` 后全套测试依然全绿，
        因为 ``copy()`` 已经做了同样的事。那是等价变异（代码冗余），不是测试
        盲区；这一条把「冗余从哪来」记下来，免得有人以后又加回去。
        """
        from PIL import Image as PILImage
        buffer = io.BytesIO()
        PILImage.new('RGB', (120, 90), (0, 0, 0)).save(buffer, 'PNG')
        truncated = buffer.getvalue()[:-40]
        opened = PILImage.open(io.BytesIO(truncated))
        with self.assertRaises(OSError):
            opened.copy()

    def test_rejects_image_over_pixel_limit(self):
        """像素上限必须真的拦得住压缩炸弹。

        构造方式：PNG 的头（IHDR）里直接写一个超大的宽高，再用一小段全零
        zlib 流填上——文件只有几百字节，声明出来的像素数却远超上限。
        ``Image.open`` 会照单全收地报告这个尺寸，``load()`` 才会因为数据
        不足而失败。所以只靠 ``load()`` 的实现在这里会报成一个含糊的
        「数据损坏」，而不是明确说「像素过多」——两条上限的失败原因不同，
        不能互相替代。

        **尺寸要落在「项目上限」与「Pillow 自己的两条线」之间**：
        - ``89,478,485``（``Image.MAX_IMAGE_PIXELS``）：超过它就发
          ``DecompressionBombWarning``。测试数据不该顺手污染警告输出，
          跑全量时那一行 warn 会让人以为新代码有问题。
        - ``178,956,970``（上面的 2 倍）：超过它 ``open()`` 直接抛错，
          项目自己的检查根本没机会执行，变异就测不出来。

        8000 × 8200 ≈ 6560 万，恰好夹在「项目 6400 万」与「Pillow 8948 万」
        之间，既触发项目上限又不惊动 Pillow。
        """
        import struct
        import zlib

        width, height = 8_000, 8_200
        pixels = width * height
        self.assertGreater(pixels, image_b64.MAX_IMAGE_PIXELS,
                           '构造的尺寸没有超过项目上限，用例失效')
        self.assertLess(pixels, Image.MAX_IMAGE_PIXELS,
                        '构造的尺寸会触发 Pillow 的 DecompressionBombWarning')

        def chunk(tag, data):
            body = tag + data
            return (struct.pack('>I', len(data)) + body
                    + struct.pack('>I', zlib.crc32(body) & 0xFFFFFFFF))

        ihdr = struct.pack('>IIBBBBB', width, height, 8, 0, 0, 0, 0)
        # 只给一行全零（每行 1 个过滤字节 + width 个灰度样本）。
        rows = b'\x00' * (1 + width)
        bomb = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr)
                + chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b''))

        b64 = base64.b64encode(bomb).decode('ascii')
        with self.assertRaises(ImageB64Error) as ctx:
            decode_base64(b64)
        self.assertIn('像素', str(ctx.exception),
                      '报的不是像素超限，说明上限检查没生效')


class SaveFormatTests(unittest.TestCase):
    """落盘格式：扩展名跟实际格式走，且选的是 PIL **写得出来**的那个。

    这两件事在 UI 层是分开的两个变量（``save_suffix`` 给对话框、
    ``writable_format`` 给 ``save()``），曾经用「读 ``export`` 源码里有没有
    ``write_fmt``」来守——那是拿测试盯代码长相。抽成纯函数后直接测行为。
    """

    def test_suffix_follows_actual_format(self):
        self.assertEqual(image_b64.save_suffix('PNG'), '.png')
        self.assertEqual(image_b64.save_suffix('JPEG'), '.jpg')
        self.assertEqual(image_b64.save_suffix('GIF'), '.gif')
        self.assertEqual(image_b64.save_suffix('WEBP'), '.webp')
        self.assertEqual(image_b64.save_suffix('TIFF'), '.tif')
        self.assertEqual(image_b64.save_suffix('ICO'), '.ico')

    def test_lowercase_format_also_works(self):
        """格式名大小写不该影响结果：PIL 给的是大写，但别处传小写也别崩。"""
        self.assertEqual(image_b64.save_suffix('jpeg'), '.jpg')

    def test_unknown_format_falls_back_to_png(self):
        """不认识的格式按 PNG 落盘——PIL 一定写得出，不至于拿到空文件。

        **回退值必须是 ``.png``**：这是「宁可换格式也不给用户一个打不开的
        文件」这条策略的落点，改成 ``.bin`` 之类的话用户双击就不知道拿什么开。
        """
        self.assertEqual(image_b64.save_suffix('WHATEVER'), '.png')
        self.assertEqual(image_b64.save_suffix(''), '.png')
        self.assertEqual(image_b64.save_suffix(None), '.png')

    def test_mpo_saves_as_jpeg(self):
        """``MPO`` 是相机多图 JPEG，PIL **能读不能写**，落盘必须改成 JPEG。

        扩展名已经是 ``.jpg``，所以只查后缀看不出这个问题——``writable_format``
        必须单独断言，否则 ``save(path, "MPO")`` 会在运行时抛 KeyError。
        """
        self.assertEqual(image_b64.writable_format('MPO'), 'JPEG')
        self.assertEqual(image_b64.save_suffix('MPO'), '.jpg')

    def test_known_formats_pass_through(self):
        for name in ('PNG', 'JPEG', 'GIF', 'WEBP', 'BMP', 'TIFF', 'ICO'):
            with self.subTest(fmt=name):
                self.assertEqual(image_b64.writable_format(name), name)

    def test_empty_format_becomes_png(self):
        self.assertEqual(image_b64.writable_format(''), 'PNG')
        self.assertEqual(image_b64.writable_format(None), 'PNG')


class WrapTests(unittest.TestCase):
    """折行：**这是性能修复，不是排版偏好**，所以要有功能性守卫。

    背景（实测数据，别再被「排版细节而已」误导）：
    ``tk.Text(wrap="char")`` 遇到一整行几十万字符的 base64 时，会为它逐字符
    找折行点。1.79 M 字符实测 **310 秒**（另一轮 48.7 秒 / 8.3 万 ms），
    同一段文本折成 76 字符的行再插入只要 **18.4 ms**——而 ``insert()`` 本身
    始终只花 2.4 ms。也就是说全部代价都在「Tk 找不到折行点」上。
    折行等于把这件事提前替 Tk 做完。

    76 也不是随便挑的：MIME / GNU ``base64`` 都用它，导出的 .txt 能直接
    喂给 ``base64 -d``。
    """

    def test_wraps_at_fixed_width(self):
        text = 'A' * 200
        wrapped = wrap_text(text)
        lines = wrapped.split('\n')
        self.assertEqual(len(lines), 3, '200 字符按 76 折应得 3 行')
        self.assertEqual([len(x) for x in lines], [76, 76, 48])

    def test_exact_multiple_has_no_trailing_newline(self):
        """恰好多行时**不能多出一个尾换行**。

        用 ``join`` 逐片拼的实现天然满足；换成「每写满一行就补一个 \\n」的
        实现会多出结尾空行，让行数断言和 ``base64 -d`` 的观感都对不上。
        """
        wrapped = wrap_text('A' * 152, 76)
        self.assertEqual(wrapped.count('\n'), 1)
        self.assertFalse(wrapped.endswith('\n'))

    def test_short_text_is_untouched(self):
        """短于一行时不该被改动——base64 短串（小图标）要原样。"""
        self.assertEqual(wrap_text('QUJD'), 'QUJD')

    def test_empty_input_returns_empty(self):
        """空输入返回空串，不是 ``'\\n'``。

        逐片切分的实现面对空串天然返回 ``''``，但写成「先 append 再 join」
        的实现很容易吐出一个孤零零的换行，让空态判断失效。
        """
        self.assertEqual(wrap_text(''), '')
        self.assertEqual(wrap_text(None), '')

    def test_wrap_round_trips_through_decode(self):
        """折行过的东西必须还能解回来——这是整条链路的底线。

        ``strip_payload`` 吃掉全部空白，所以换行对解码完全透明。这条测试
        挡的是「折行引入了解码器不认的字符」这类改动。
        """
        raw = make_png((20, 16))
        flat = base64.b64encode(raw).decode('ascii')
        wrapped = wrap_text(flat)
        self.assertIn('\n', wrapped)

        decoded = decode_base64(wrapped)
        self.assertEqual(decoded.image.size, (20, 16))
        # 也要能经 Data URI 形态回来（前缀 + 折行同时存在）
        decoded = decode_base64('data:image/png;base64,' + wrapped)
        self.assertEqual(decoded.format, 'PNG')

    def test_unwrap_text_is_the_inverse(self):
        """``unwrap_text`` 必须恰好还原成单行原串。"""
        flat = base64.b64encode(make_png((30, 20))).decode('ascii')
        self.assertEqual(unwrap_text(wrap_text(flat)), flat)
        # 别名必须与 strip_payload 行为一致，不能各走一套
        self.assertEqual(unwrap_text(wrap_text(flat)), strip_payload(wrap_text(flat))[0])

    def test_compact_keeps_the_data_uri_prefix(self):
        """``compact`` 只去空白，**保留** ``data:`` 前缀。

        这是它与 ``unwrap_text`` 的唯一区别，也是它存在的理由：复制结果要
        带前缀，若兜底路径用了 ``unwrap_text``，有缓冲时带前缀、没缓冲时不带，
        同一份数据出现两种形态。
        """
        uri = 'data:image/png;base64,' + base64.b64encode(
            make_png((12, 10))).decode('ascii')
        wrapped = wrap_text(uri, 40)
        self.assertIn('\n', wrapped)
        self.assertEqual(compact(wrapped), uri)
        self.assertTrue(compact(wrapped).startswith('data:image/png;base64,'))
        # 对照：unwrap_text 会把前缀也吃掉，两者语义确实不同
        self.assertFalse(unwrap_text(wrapped).startswith('data:'))
        # 去掉前缀后两者一致（折行处理的实质相同）
        self.assertEqual(compact(wrapped), 'data:image/png;base64,'
                         + unwrap_text(wrapped))

    def test_compact_tolerates_wrapping_inside_the_prefix(self):
        """前缀被折行截断时也要能拼回原样——用户复制时常见这种形态。"""
        self.assertEqual(compact('data:image/png;base64,\nAAAA'),
                         'data:image/png;base64,AAAA')
        self.assertEqual(compact(None), '')

    def test_wrap_width_is_76(self):
        """折行宽度钉死 76。改成别的值会让导出的 .txt 不再符合 MIME 惯例，
        而且**不会有任何症状**——所以必须显式断言。"""
        self.assertEqual(image_b64.WRAP_WIDTH, 76)
        self.assertEqual(wrap_text('A' * 77).index('\n'), 76)

    def test_zero_or_negative_width_does_not_crash(self):
        """宽度为 0 / 负数时不能除零或死循环——防御性，但很容易写漏。"""
        self.assertEqual(wrap_text('ABC', 0), 'ABC')
        self.assertEqual(wrap_text('ABC', -5), 'ABC')


class HumanSizeTests(unittest.TestCase):
    def test_units(self):
        self.assertEqual(human_size(0), '0 B')
        self.assertEqual(human_size(512), '512 B')
        self.assertEqual(human_size(2048), '2.0 KB')
        self.assertEqual(human_size(5 * 1024 * 1024), '5.0 MB')

    def test_negative_is_clamped(self):
        self.assertEqual(human_size(-5), '0 B')


if __name__ == '__main__':
    unittest.main()
