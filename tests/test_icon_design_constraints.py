"""图标画法必须满足的三条硬约束（全部有实测支撑）。

对应缺陷：*「图标太丑了」*。根因不在细节，而在三条设计约束都被违反：

1. **底色亮度必须落在 0.143 ~ 0.269。** 图标要同时在浅色与深色任务栏上成立，
   低于 0.143 在深色栏上糊、高于 0.269 在浅色栏上糊。这两个边界由两侧各需
   3:1 对比度反解得到。旧方案深色玻璃底亮度 0.02，实测深色任务栏轮廓对比
   仅 1.41:1。

2. **小尺寸下不能有文字。** 16px 下「LUMINA」只剩约 4px 高，必然糊成灰噪声。
   旧 `app_icon.py` 与 `system_tray.py` 都画了这行字。

3. **必须超采样抗锯齿。** PIL 的 ``ImageDraw`` 原语不做抗锯齿——实测直接绘制
   只有 2 种 alpha 层级（0/255 硬台阶），超采样后 172 种。旧实现直接绘制。

另守一条结构约束：**画法必须只有一份实现**。旧代码在 `app_icon.py` 与
`system_tray.py` 里各写了一段坐标完全相同的大段绘制代码，改一处必然导致
任务栏与托盘长得不一样。

⚠️ **对比度的判据只能取「实心填充色」，不能取「边界像素的最小对比度」。**
抗锯齿渐隐带最外圈的像素 alpha 趋近 0、颜色已贴近背景，对比度在数学上必然
趋近 1:1。曾用「边界像素最小对比度」评估配色，据此误判现用配色「16px 深色
栏仅 2.92:1 不达标」，白跑了一轮调色——实际实测实心填充是 3.72 / 3.95，
本来就达标。:class:`RenderedContrastTests` 里有一条自检专门守这个坑。
"""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lumina import app_icon  # noqa: E402

# 结构约束那几条读的是源码而不是调用模块（模块本身依赖 pystray，
# 且导入即起线程），所以不 import system_tray

LIGHT_TASKBAR = (243, 243, 243)
DARK_TASKBAR = (32, 32, 32)
# 两侧各需 3:1 反解出的亮度可行域
LUM_MIN, LUM_MAX = 0.143, 0.269


def _lum(rgb):
    def channel(value):
        value /= 255.0
        return value / 12.92 if value <= 0.03928 \
            else ((value + 0.055) / 1.055) ** 2.4
    return (0.2126 * channel(rgb[0]) + 0.7152 * channel(rgb[1])
            + 0.0722 * channel(rgb[2]))


def _contrast(a, b):
    la, lb = _lum(a), _lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def _composite(pixel, background):
    """把 RGBA 像素按 alpha 合成到背景色上，返回 RGB。"""
    alpha = pixel[3] / 255.0
    return tuple(round(pixel[i] * alpha + background[i] * (1 - alpha))
                 for i in range(3))


def _pixels(image):
    """逐像素读出 RGBA。

    不用 ``Image.getdata()``——它在 Pillow 14 会被移除，当前版本已发
    ``DeprecationWarning``。
    """
    width, height = image.size
    get = image.load()
    return [get[x, y] for y in range(height) for x in range(width)]


def _is_white(pixel):
    return min(pixel[:3]) > 205


def _solid_fill(image):
    """取渲染后的**实心填充色**：完全实心、且不是白色图形的像素取平均。

    这才是与「图标 vs 任务栏」对比度对应的量。白色剪贴板同样是不透明的，
    必须排除，否则紫色底会被白色拉亮、浅色任务栏上的对比度虚低。
    """
    solid = [p for p in _pixels(image) if p[3] >= 250 and not _is_white(p)]
    if not solid:
        return None
    return tuple(round(sum(p[i] for p in solid) / len(solid)) for i in range(3))


def _min_composited_contrast(image, background, min_alpha=0):
    """所有可见像素（alpha > min_alpha）合成后的**最小**对比度。

    这个量没有判别价值——见 :meth:`RenderedContrastTests.`
    :meth:`test_boundary_minimum_metric_must_not_be_used` 的自检。留在这里
    正是为了让那条自检能复现这个事实。
    """
    values = [_contrast(_composite(p, background), background)
              for p in _pixels(image) if p[3] > min_alpha]
    return min(values) if values else float('nan')


class RenderedContrastTests(unittest.TestCase):
    """约束①的实测版：**渲染成品**在两种任务栏上都过 3:1。

    :class:`GradientFeasibilityTests` 只看常量，测不出渲染之后的实际情况。
    这一组直接像素实测，且用 WCAG 的正确口径——**实心填充色 vs 背景**。
    """

    def test_rendered_16px_fill_clears_three_to_one_on_both_taskbars(self):
        """16px 是最坏情形：边缘占比最大、抗锯齿混合最重。"""
        fill = _solid_fill(app_icon.draw_mark(16))
        self.assertIsNotNone(fill, '16px 渲染后找不到实心填充像素')
        for label, background in (('浅色', LIGHT_TASKBAR),
                                  ('深色', DARK_TASKBAR)):
            value = _contrast(fill, background)
            self.assertGreaterEqual(
                value, 3.0,
                f'16px 实心填充 {fill} 在{label}任务栏上仅 {value:.2f}:1，'
                f'图标轮廓会糊进任务栏')

    def test_all_shipped_sizes_clear_three_to_one(self):
        """ico 里实际打包的尺寸都要过线，不能只有 16px 幸免。"""
        for size in (16, 32, 48, 256):
            fill = _solid_fill(app_icon.draw_mark(size))
            for label, background in (('浅色', LIGHT_TASKBAR),
                                      ('深色', DARK_TASKBAR)):
                value = _contrast(fill, background)
                self.assertGreaterEqual(
                    value, 3.0,
                    f'{size}px 实心填充 {fill} 在{label}任务栏上仅 '
                    f'{value:.2f}:1')

    def test_boundary_minimum_metric_must_not_be_used(self):
        """自检：钉死「逐像素最小对比度」这个错误判据。

        抗锯齿渐隐带最外圈的像素 alpha 趋近 0、合成后颜色已与背景几乎相同，
        其对比度**必然趋近 1:1**。曾用它评估配色，据此误判现用配色「16px
        深色栏仅 2.92:1 不达标」，白跑了一轮调色——实际实心填充是 3.72/3.95，
        本来就达标。

        更要命的是这个指标**可以被阈值随意操纵**（实测同一张图）：
        alpha 阈值 0 → 最小值 1.16；阈值提到 50 → 2.62；阈值 250 → 2.94。
        没有有原则的分界点，所以它不可能成为判据。这条自检锁住这个事实。
        """
        art = app_icon.draw_mark(16)
        fill = _solid_fill(art)
        self.assertGreaterEqual(
            _contrast(fill, DARK_TASKBAR), 3.0,
            '实心填充口径本身就不达标，那问题不在判据上，先修配色')

        loose = _min_composited_contrast(art, DARK_TASKBAR, min_alpha=0)
        tight = _min_composited_contrast(art, DARK_TASKBAR, min_alpha=250)

        self.assertLess(
            loose, 1.5,
            f'不设阈值时最小值是 {loose:.2f}:1，居然没跌到 1:1 附近 —— '
            f'渐隐带消失了？这条自检已失去意义')
        self.assertGreater(
            tight, loose + 0.5,
            f'收窄阈值后最小值反而没拉开（{tight:.2f} vs {loose:.2f}）—— '
            f'说明该指标不再随阈值漂移，这条自检的前提变了')

    def test_feasible_range_matches_the_two_taskbars(self):
        """亮度可行域的边界确实由两侧 3:1 反解而来，不是拍脑袋的数。

        上界：底色在浅色栏上要 3:1，亮度不能太高。
        下界：底色在深色栏上要 3:1，亮度不能太低。
        """
        low = (_lum(DARK_TASKBAR) + 0.05) * 3 - 0.05
        high = (_lum(LIGHT_TASKBAR) + 0.05) / 3 - 0.05
        self.assertAlmostEqual(low, LUM_MIN, delta=0.002,
                               msg=f'下界应由深色任务栏反解为 {low:.4f}')
        self.assertAlmostEqual(high, LUM_MAX, delta=0.005,
                               msg=f'上界应由浅色任务栏反解为 {high:.4f}')

    def test_palette_lies_inside_that_range(self):
        """实际配色（渲染后的实心填充）必须落在反解出的可行域内。"""
        fill = _solid_fill(app_icon.draw_mark(256))
        value = _lum(fill)
        self.assertGreaterEqual(value, LUM_MIN,
                                f'实心填充 {fill} 亮度 {value:.3f} 低于下界')
        self.assertLessEqual(value, LUM_MAX,
                             f'实心填充 {fill} 亮度 {value:.3f} 高于上界')


class GradientFeasibilityTests(unittest.TestCase):
    """约束①：渐变的**两端**都必须在亮度可行域内。"""

    def test_gradient_endpoints_inside_feasible_range(self):
        top, bottom = app_icon._GRAD_TOP, app_icon._GRAD_BOTTOM
        for name, rgb in (('顶端', top), ('底端', bottom)):
            value = _lum(rgb)
            self.assertGreaterEqual(
                value, LUM_MIN,
                f'渐变{name} {rgb} 亮度 {value:.3f} < {LUM_MIN} —— '
                f'在深色任务栏上会糊掉')
            self.assertLessEqual(
                value, LUM_MAX,
                f'渐变{name} {rgb} 亮度 {value:.3f} > {LUM_MAX} —— '
                f'在浅色任务栏上会糊掉')

    def test_gradient_endpoints_clear_three_to_one_on_both_taskbars(self):
        """最坏情形也要过 3:1：亮端对浅色栏、暗端对深色栏。"""
        light = _contrast(app_icon._GRAD_TOP, LIGHT_TASKBAR)
        dark = _contrast(app_icon._GRAD_BOTTOM, DARK_TASKBAR)
        self.assertGreaterEqual(light, 3.0, f'浅色任务栏仅 {light:.2f}:1')
        self.assertGreaterEqual(dark, 3.0, f'深色任务栏仅 {dark:.2f}:1')


class SupersampleTests(unittest.TestCase):
    """约束③：必须超采样，边缘不能是硬台阶。"""

    def test_supersample_factor_is_at_least_two(self):
        self.assertGreaterEqual(
            app_icon._SUPERSAMPLE, 2,
            '超采样倍数 < 2 时边缘抗锯齿不足（PIL 绘图原语本身不做抗锯齿）')

    def test_rendered_edges_are_antialiased(self):
        """成品边缘必须有中间 alpha —— 锯齿时只有 0/255 两级。

        判据不能只看绝对级数：边缘周长随尺寸变化，64px 下过渡层级天然比
        256px 少。所以这里**自建一个无超采样的基线**，直接对比两者——这才
        真正锁住「超采样有没有生效」。
        """
        art = app_icon.draw_mark(128)
        levels = {pixel[3] for pixel in _pixels(art)}
        transitional = [v for v in levels if 8 < v < 247]

        # 基线：同样的圆角方块，**直接在目标尺寸**绘制。
        # 注意不能先画大再缩小——LANCZOS 缩放本身就会产生抗锯齿，那就不算
        # 「无超采样」基线了。
        from PIL import Image, ImageDraw
        plain = Image.new('L', (128, 128), 0)
        ImageDraw.Draw(plain).rounded_rectangle((0, 0, 127, 127), radius=29,
                                                fill=255)
        baseline = {v for v in _pixels(plain) if 8 < v < 247}

        self.assertLessEqual(
            len(baseline), 2,
            '基线本身就有抗锯齿，这个测试失去意义了')
        self.assertGreater(
            len(transitional), 20,
            f'过渡 alpha 只有 {len(transitional)} 级（无超采样基线 '
            f'{len(baseline)} 级）—— 边缘是硬台阶，超采样没生效')

    def test_supersample_flag_changes_nothing_about_output_size(self):
        """``draw_mark(size)`` 返回的边长必须正好是 ``size``。

        超采样只在内部发生；若忘了降采样，调用方（如托盘按 48 渲染）会拿到
        3 倍大的图。
        """
        for size in (16, 32, 48, 256):
            art = app_icon.draw_mark(size)
            self.assertEqual(art.size, (size, size))


class ShapeLegibilityTests(unittest.TestCase):
    """约束②：小尺寸下必须靠形状可辨，不能靠文字。"""

    def test_no_text_drawing_in_icon_code(self):
        """图标画法里不允许出现文字绘制（字体 / text 调用）。"""
        source = (ROOT / 'lumina' / 'app_icon.py').read_text(encoding='utf-8')
        body = source[source.index('def draw_mark'):]
        body = body[:body.index('\ndef ')]
        for forbidden in ('ImageFont', 'truetype', '.text(', 'textbbox'):
            self.assertNotIn(
                forbidden, body,
                f'draw_mark 里出现了 {forbidden!r} —— 16px 下文字必然糊成噪声')

    def test_white_shape_survives_downscale_to_16px(self):
        """缩到 16px 后，白色图形必须仍有足量实心像素（不是几个噪点）。"""
        small = app_icon.draw_mark(16)
        solid = sum(1 for pixel in _pixels(small)
                    if pixel[3] > 200 and pixel[0] > 190)
        ratio = solid / (16 * 16)
        self.assertGreater(
            ratio, 0.10,
            f'16px 下白色像素仅占 {ratio:.0%}，图形太细看不清')

    def test_shape_stays_inside_canvas(self):
        """图形不能溢出圆角底（夹子向上探出也必须被裁掉）。"""
        art = app_icon.draw_mark(96)
        width, height = art.size
        corners = ((0, 0), (width - 1, 0), (0, height - 1),
                   (width - 1, height - 1))
        for x, y in corners:
            self.assertEqual(
                art.getpixel((x, y))[3], 0,
                f'角落 ({x}, {y}) 不透明 —— 圆角没裁掉，图形溢出了')

    def test_interior_stays_opaque(self):
        """中心区域必须完全不透明，否则圆角遮罩切多了。"""
        art = app_icon.draw_mark(96)
        self.assertEqual(art.getpixel((48, 48))[3], 255,
                         '图标中心是透明的')

    def test_corner_radius_is_not_silently_clamped_by_pil(self):
        """圆角半径不能超过 PIL 的钳制上限，否则「请求值 ≠ 实际值」。

        ``rounded_rectangle`` 把 ``radius`` 静默截断在画布边长的 1/4。实测
        请求 60 与请求 200 得到**完全相同**的图形（实心占比都是 78.51%），
        超标的部分不会有任何报错。这类「请求值 vs 实际值」的差异，只看四角
        透明 / 中心不透明是抓不到的——两个断言在半径 60 时依然通过。
        """
        limit = app_icon._DESIGN_SIZE / 4.0
        self.assertLessEqual(
            app_icon._CORNER_RADIUS, limit,
            f'圆角半径 {app_icon._CORNER_RADIUS} 超过 PIL 钳制上限 {limit}，'
            f'超出部分被静默截断，设计意图与实际渲染不一致')

    def test_rounded_square_does_not_degenerate_into_a_circle(self):
        """底色必须还是「圆角方形」，不能被超大圆角压成近乎圆形。

        判据用**顶行不透明游程**：半径越大，顶行越短。实测 21.5 → 62 单位，
        60 → 14 单位（顶行几乎全被切掉，视觉上已是椭圆）。取 50 单位作下限
        （16px 下约 8px），既能容纳半径 24 的钳制上限（58 单位），又能拦住
        退化成圆形的情形。
        """
        art = app_icon.draw_mark(96)
        width = art.size[0]
        get = art.load()
        run = sum(1 for x in range(width) if get[x, 0][3] > 128)
        self.assertGreaterEqual(
            run, 50,
            f'顶行不透明游程只有 {run}/96 单位 —— 圆角太大，底色已不是方形')


class SingleImplementationTests(unittest.TestCase):
    """结构约束：画法只能有一份实现。"""

    def test_tray_reuses_app_icon_draw_mark(self):
        """托盘必须复用 ``app_icon.draw_mark``，不能自己再画一遍。"""
        source = (ROOT / 'lumina' / 'system_tray.py').read_text(encoding='utf-8')
        self.assertIn(
            'draw_mark', source,
            'system_tray 没有复用 app_icon.draw_mark —— 两套画法必然不一致')

    def test_tray_has_no_local_pixel_drawing(self):
        """托盘里不允许出现像素级绘制调用。"""
        source = (ROOT / 'lumina' / 'system_tray.py').read_text(encoding='utf-8')
        for forbidden in ('ImageDraw', 'rounded_rectangle', 'ImageFilter',
                          'ImageFont', 'LUMINA'):
            self.assertNotIn(
                forbidden, source,
                f'system_tray 里还有 {forbidden!r} —— 画法应当只在 app_icon 里')

    def test_draw_mark_is_the_only_shape_definition(self):
        """几何常量只在 app_icon 里定义一次。"""
        source = (ROOT / 'lumina' / 'app_icon.py').read_text(encoding='utf-8')
        self.assertEqual(
            len(re.findall(r'def draw_mark', source)), 1,
            'draw_mark 定义了不止一次')


if __name__ == '__main__':
    unittest.main()
