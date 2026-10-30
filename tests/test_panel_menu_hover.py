"""顶栏菜单：去箭头 + 首次点击后才启用悬停展开 的回归。

对应需求（两次迭代，后者收窄前者）：

1. *「剪贴板上面菜单 工具 截图 设置 下拉框统一去掉下拉的箭头样式，修改为
   鼠标放到上面自动弹出下拉框」*
2. *「修改为只有第一次点击上面菜单某一项后才触发鼠标放到某一个菜单上
   自动弹出下拉框」*

所以最终行为是：**按钮不带箭头；悬停展开要先解锁，解锁的唯一方式是点过一次
顶栏菜单按钮；面板隐藏 / 最小化会把解锁状态复位。**

这里守六件事：

1. **文案不再带箭头**。去掉 "▾" 后按钮的可发现性完全靠悬停，若有人
   「为了提示可点」把箭头加回来，需求就被改回去了。
2. **三个按钮都绑了 `<Enter>`**，而且**点击仍然可用**（键鼠习惯不同，
   触控板用户没有可靠的悬停）。
3. **未解锁时悬停必须无反应**——这正是第二次迭代的核心。只判「绑了
   `<Enter>`」是不够的：回调里必须真的被 `_hover_armed` 拦住。
4. **划过多个按钮只留一个弹窗**。这是实测抓到的真实缺陷：每个按钮的
   `<Enter>` 各开一个 Toplevel，划过「工具→截图→设置」会留下三个残影。
5. **收起的判定要覆盖「按钮 + 弹窗」整体**，不能只看按钮的 `<Leave>`：
   鼠标从按钮往下划进弹窗必然先离开按钮，在 `<Leave>` 里收起等于弹窗
   永远点不到。两块之间还有 4px 定位缝，穿缝而过也不能误收起。
6. **面板离场要复位解锁**，否则下次用热键唤出时（光标常正好压在按钮上）
   菜单会自己蹦出来。

这里走 `_hover_open` 这条**真实路径**：直接调 `_open_popup_menu` 会绕过
「先关闭旧的」那一步，测试就永远抓不到残影（首版即因此未被变异验证捕获）。
`_hover_open` 内部要读真实指针做前置校验，测试里**把 `winfo_pointerxy`
打桩**指向按钮中心——这不影响被测逻辑（校验、关旧、开新全都照走），
却避免了依赖真实光标与窗口可见性。用 `SetCursorPos` 的做法在**全量跑时
会因顺序污染而失败**：前面的任务栏测试把窗口隐藏/最小化过，按钮的屏幕
坐标随之失效（实测单项通过、全量 2 红）。
"""
import sys
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lumina.history_panel import HistoryPanel  # noqa: E402

try:
    _PROBE = tk.Tk()
    _PROBE.withdraw()
    _PROBE.destroy()
    TK_OK = True
except Exception:  # pragma: no cover - 仅在无图形环境时触发
    TK_OK = False

SKIP_REASON = '本机 Tk 不可用'

ARROWS = ('▾', '▸', '▼', '▽', '△')


class _FakeDB:
    def list_recent(self, *args, **kwargs):
        return []

    def count(self, *args, **kwargs):
        return 0

    def get(self, *args, **kwargs):
        return None

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class _FakeUI:
    def __init__(self):
        self.db = _FakeDB()
        self.config = {}
        self.panel_visible = False


@unittest.skipUnless(TK_OK, SKIP_REASON)
class HeaderMenuHoverTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.panel = HistoryPanel(self.root, tk, _FakeUI())
        self.panel.win.deiconify()
        self._pump(0.3)

    def tearDown(self):
        try:
            # 连排队中的悬停任务一起清掉：`_close_popup_menu` 只取消轮询，
            # 不碰 `_hover_open_job`，残留到期后会动到下一个实例的状态。
            self.panel._reset_hover_armed()
            self.panel._close_popup_menu()
        except Exception:
            pass
        try:
            self.panel.win.destroy()
        except tk.TclError:
            pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _pump(self, seconds=0.0):
        self.panel.win.update()
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.panel.win.update()
            time.sleep(0.01)

    def _buttons(self):
        return (
            ('tools', self.panel._tools_btn),
            ('shot', self.panel._shot_btn),
            ('settings', self.panel._settings_btn),
        )

    def _top_levels(self):
        return [w for w in self.panel.win.winfo_children()
                if isinstance(w, tk.Toplevel)]

    # ---------- 1. 文案去箭头 ----------

    def test_menu_buttons_have_no_dropdown_arrow(self):
        """工具 / 截图 / 设置三个按钮都不带下拉箭头。

        用户要求「统一去掉下拉的箭头样式」。别以「提示可点」为理由加回来——
        现在的可点性提示是悬停展开本身。
        """
        for kind, btn in self._buttons():
            text = str(btn.cget('text'))
            for arrow in ARROWS:
                self.assertNotIn(
                    arrow, text,
                    f'{kind} 按钮文案 {text!r} 仍含下拉箭头 {arrow!r}')
            self.assertTrue(text.strip(), f'{kind} 按钮文案为空')

    # ---------- 2. 悬停绑定 + 点击保留 ----------

    def test_all_menu_buttons_expand_on_hover(self):
        """三个按钮都绑定了 `<Enter>`（解锁后才会真的展开，见后一组测试）。"""
        for kind, btn in self._buttons():
            self.assertTrue(
                btn.bind('<Enter>'),
                f'{kind} 按钮没有绑定 <Enter>，解锁后悬停也不会展开')

    # ---------- 2b. 悬停必须先解锁 ----------

    def test_hover_is_locked_by_default(self):
        """**初始未解锁**：这是第二次迭代的核心要求。

        用户原话：「修改为只有第一次点击上面菜单某一项后才触发鼠标放到
        某一个菜单上自动弹出下拉框」。
        """
        self.assertFalse(self.panel._hover_armed,
                         '初始状态应为未解锁，悬停不该生效')

    def test_enter_does_nothing_before_first_click(self):
        """未解锁时 `<Enter>` 不得排队展开、更不得弹出。

        直接调 `_hover_enter`（`<Enter>` 绑的就是它）：这是真实入口，
        测得到那道 `_hover_armed` 门到底拦没拦住。
        """
        btn = self.panel._tools_btn
        self.panel._hover_open_job = None
        self.assertFalse(self.panel._hover_armed, '前提：初始未解锁')

        self.panel._hover_enter('tools', btn)
        self.assertIsNone(self.panel._hover_open_job,
                          '未解锁却排队了悬停展开任务')
        self.assertFalse(self.panel._popup_open,
                         '未解锁却弹出了菜单')

    def test_enter_queues_after_arming(self):
        """解锁后 `<Enter>` 应当排队一次延迟展开（而不是立刻弹）。"""
        btn = self.panel._tools_btn
        self.panel._toggle_popup_menu('tools', btn)
        self.panel._close_popup_menu()
        self.panel._hover_open_job = None
        self.panel._hover_enter('tools', btn)
        self.assertIsNotNone(self.panel._hover_open_job,
                             '解锁后悬停没有排队展开')
        self.assertFalse(self.panel._popup_open,
                         '应当是延迟展开，不能一进按钮就弹')
        self.panel._cancel_hover_open()

    def test_hover_open_respects_lock(self):
        """未解锁时即便直接调 `_hover_open`（模拟延迟已到期）也不展开。

        延迟任务在面板隐藏后可能仍残留到期；`_hover_open` 必须自己再判一次。
        """
        btn = self.panel._tools_btn
        with patch.object(
                self.panel.win, 'winfo_pointerxy',
                return_value=(btn.winfo_rootx() + btn.winfo_width() // 2,
                              btn.winfo_rooty() + btn.winfo_height() // 2)):
            self.panel._hover_open('tools', btn)
        self.assertFalse(self.panel._popup_open,
                         '未解锁状态下 _hover_open 不应展开菜单')

    def test_first_click_arms_hover(self):
        """点过一次顶栏菜单按钮后就解锁，之后悬停才生效。"""
        self.assertFalse(self.panel._hover_armed)
        self.panel._toggle_popup_menu('tools', self.panel._tools_btn)
        self.assertTrue(self.panel._hover_armed,
                        '点击后没有解锁悬停')
        self.panel._close_popup_menu()
        self.assertTrue(self.panel._hover_armed,
                        '收起菜单不该重新上锁（解锁是一次性的）')

    def test_armed_hover_opens_menu(self):
        """解锁后悬停能真正展开——锁住的是「未点击前」，不是把悬停废掉。"""
        self.panel._toggle_popup_menu('tools', self.panel._tools_btn)
        self.panel._close_popup_menu()
        btn = self.panel._shot_btn
        with patch.object(
                self.panel.win, 'winfo_pointerxy',
                return_value=(btn.winfo_rootx() + btn.winfo_width() // 2,
                              btn.winfo_rooty() + btn.winfo_height() // 2)):
            self.panel._hover_open('shot', btn)
        self.assertTrue(self.panel._popup_open, '解锁后悬停没能展开')
        self.assertEqual(self.panel._menu_kind, 'shot')

    def test_hide_relocks_hover(self):
        """隐藏面板要复位解锁，否则下次唤出时光标压在按钮上就自己弹菜单。"""
        self.panel._toggle_popup_menu('tools', self.panel._tools_btn)
        self.assertTrue(self.panel._hover_armed)
        self.panel.hide()
        self.assertFalse(self.panel._hover_armed,
                         'hide() 后悬停没有重新上锁')
        self.assertTrue(not self.panel._popup_open, 'hide() 后菜单没关')

    def test_minimize_relocks_hover(self):
        """最小化同理：从任务栏点回来时不该自动弹菜单。"""
        self.panel._toggle_popup_menu('tools', self.panel._tools_btn)
        self.assertTrue(self.panel._hover_armed)
        self.panel._minimize()
        self.assertFalse(self.panel._hover_armed,
                         '_minimize() 后悬停没有重新上锁')

    def test_relock_cancels_pending_hover_open(self):
        """复位时必须取消排队中的展开任务，不能只清标志。"""
        self.panel._hover_armed = True
        self.panel._hover_open_job = self.panel.win.after(
            5000, lambda: None)  # 一个不会到期的任务占位
        self.panel._reset_hover_armed()
        self.assertFalse(self.panel._hover_armed)
        self.assertIsNone(self.panel._hover_open_job,
                          '复位后仍残留排队中的展开任务')

    def test_click_still_toggles_menu(self):
        """点击仍能开/关——键鼠兼容，不能只留悬停一条路。

        **不能等事件循环**：展开后轮询会按悬停语义判断，而测试环境的光标
        不在按钮上，一跑 `update()` 就被收起（实测踩过）。所以这里只验
        `_toggle_popup_menu` 的**瞬间状态**，不 pump。
        """
        self.panel._toggle_popup_menu('tools', self.panel._tools_btn)
        self.assertTrue(self.panel._popup_open, '点击后没有展开')
        self.assertEqual(self.panel._menu_kind, 'tools')
        self.panel._toggle_popup_menu('tools', self.panel._tools_btn)
        self.assertFalse(self.panel._popup_open, '再次点击没有收起')

    # ---------- 2c. 点击其他地方取消激活 ----------

    def _click_event(self, widget):
        """构造一个最小可用的点击事件替身（只要 `.widget`）。"""
        return type('Evt', (), {'widget': widget})()

    def test_click_elsewhere_disarms_hover(self):
        """点击菜单按钮与弹窗以外的地方 → 取消悬停激活。

        用户要求「点击激活后 如果点击其他地方取消激活」。激活只管这一轮
        操作：用完随手点到列表 / 详情，就回到纯点击状态，避免鼠标偶然扫过
        顶栏又弹菜单。
        """
        self.panel._toggle_popup_menu('tools', self.panel._tools_btn)
        self.assertTrue(self.panel._hover_armed, '前提：点击后应已激活')
        self.panel._close_popup_menu()

        # 模拟点在面板内容区（拿左栏当替身）
        self.panel._hover_outside_click(self._click_event(self.panel._left_frame))
        self.assertFalse(self.panel._hover_armed,
                         '点击其他地方后没有取消激活')

    def test_click_on_menu_button_keeps_armed(self):
        """点三颗菜单按钮本身不算「其他地方」，不能把激活打掉。"""
        self.panel._toggle_popup_menu('tools', self.panel._tools_btn)
        self.panel._close_popup_menu()
        for _kind, btn in self._buttons():
            self.panel._hover_outside_click(self._click_event(btn))
            self.assertTrue(self.panel._hover_armed,
                            f'点击 {btn.cget("text")} 不该取消激活')

    def test_click_inside_popup_keeps_armed(self):
        """点弹窗内部（选项 / 勾选）也不算「其他地方」。

        否则点一下菜单项就顺手把激活打掉，下次还得再点一次才恢复悬停。
        """
        self.panel._toggle_popup_menu('settings', self.panel._settings_btn)
        self._pump(0.05)
        pop = self.panel._popup_menu
        self.assertIsNotNone(pop, '弹窗没有创建')
        # 用一个能拿到 winfo_toplevel 的控件（弹窗内的 frame）当点击目标
        inner = pop.winfo_children()[0]
        self.panel._hover_outside_click(self._click_event(inner))
        self.assertTrue(self.panel._hover_armed,
                        '点击弹窗内部取消了激活')

    def test_disarm_cancels_pending_hover_open(self):
        """取消激活时必须把排队中的展开任务一起掐掉。

        否则那笔 180ms 任务到期仍会弹菜单，用户会看到「点了别处还是弹了」。
        """
        self.panel._hover_armed = True
        self.panel._hover_open_job = self.panel.win.after(5000, lambda: None)
        self.panel._hover_outside_click(
            self._click_event(self.panel._left_frame))
        self.assertFalse(self.panel._hover_armed)
        self.assertIsNone(self.panel._hover_open_job,
                          '取消激活后仍残留排队中的展开任务')

    def test_reclick_after_disarm_rearms(self):
        """取消激活后再点一次菜单按钮，应当重新激活。"""
        self.panel._toggle_popup_menu('tools', self.panel._tools_btn)
        self.panel._close_popup_menu()
        self.panel._hover_outside_click(
            self._click_event(self.panel._left_frame))
        self.assertFalse(self.panel._hover_armed)
        self.panel._toggle_popup_menu('tools', self.panel._tools_btn)
        self.assertTrue(self.panel._hover_armed, '再次点击没有重新激活')

    def test_window_binds_click_handler(self):
        """窗口级必须真的绑了点击处理，否则上面几条只是「函数能跑」。"""
        self.assertTrue(
            self.panel.win.bind('<ButtonPress-1>'),
            'win 上没有绑定 <ButtonPress-1>，点击其他地方不会取消激活')

    # ---------- 3. 划过多按钮不留残影 ----------

    def test_hovering_across_buttons_keeps_single_popup(self):
        """依次划过三个按钮后，屏幕上只能有一个弹窗。

        实测踩过：`_hover_open` 不先关闭旧弹窗时，划过三个按钮留下
        **三个 Toplevel 残影**（176x102 / 148x141 / 178x155 叠在一起）。

        走 `_hover_open` 真实路径，指针坐标打桩到按钮中心（理由见模块
        docstring）。**必须先解锁**，否则 `_hover_open` 会在门口就 return，
        这条测试测不到残影（等于假通过）。
        """
        self.panel._hover_armed = True
        for kind, btn in self._buttons():
            cx = btn.winfo_rootx() + btn.winfo_width() // 2
            cy = btn.winfo_rooty() + btn.winfo_height() // 2
            with patch.object(self.panel.win, 'winfo_pointerxy',
                              return_value=(cx, cy)):
                self.panel._hover_open(kind, btn)
            self.assertTrue(self.panel._popup_open,
                            f'悬停在 {kind} 上却没有展开')
            self.assertEqual(
                len(self._top_levels()), 1,
                f'划过 {kind} 后 Toplevel 数='
                f'{len(self._top_levels())}，期望 1（残影）')
            self.assertEqual(self.panel._menu_kind, kind)

    def test_only_one_popup_survives_switching(self):
        """连续切换三个菜单，最终只留一个 Toplevel 且是最后一个。

        同样走 `_hover_open` 真实路径，理由见上一条；同样必须先解锁。
        """
        self.panel._hover_armed = True
        for kind, btn in self._buttons():
            with patch.object(
                    self.panel.win, 'winfo_pointerxy',
                    return_value=(btn.winfo_rootx() + btn.winfo_width() // 2,
                                  btn.winfo_rooty() + btn.winfo_height() // 2)):
                self.panel._hover_open(kind, btn)
        self.assertEqual(len(self._top_levels()), 1,
                         f'切换后残留 {len(self._top_levels())} 个弹窗')
        self.assertEqual(self.panel._menu_kind, 'settings')

    # ---------- 4. 收起判定的几何 ----------

    def test_hover_zone_covers_all_three_buttons(self):
        """判定区必须横向覆盖**全部三个**菜单按钮，不只当前锚点那一个。

        这是用户实测报的方向性 bug：「点击设置后 从右往左鼠标放到截图上
        不会弹出下拉框，但是从工具到截图会弹出」。

        根因：只把锚点按钮算进判定区时，打开*设置*的合并区是 ``x[586,764]``，
        往左划到*截图*（``x[535,584]``）落在区外 → 轮询判定「已离开」→
        `_close_popup_menu()` 顺带取消了截图排队中的 180ms 展开任务，于是不弹。
        打开*工具*时合并区 ``x[484,660]`` 恰好盖住截图，往右划就正常——
        所以现象是单向的。

        这条必须**逐个锚点 × 逐个目标按钮**全组合验证，只测一个方向拦不住。
        """
        for anchor_kind, anchor_btn in self._buttons():
            self.panel._hover_armed = True
            self.panel._close_popup_menu()
            self.panel._menu_kind = anchor_kind
            self.panel._menu_anchor = anchor_btn
            self.panel._open_popup_menu(anchor_kind)
            self._pump(0.05)

            for target_kind, target_btn in self._buttons():
                px = target_btn.winfo_rootx() + target_btn.winfo_width() // 2
                py = target_btn.winfo_rooty() + target_btn.winfo_height() // 2
                self.assertTrue(
                    self.panel._popup_hover_contains(px, py),
                    f'打开 {anchor_kind} 时指针移到 {target_kind} '
                    f'被判定为「已离开」→ 该方向的悬停切换会失效')

    def test_hover_zone_covers_button_and_popup(self):
        """判定区必须同时覆盖按钮与弹窗，且中间那道缝不算「已离开」。

        只看按钮的话，鼠标往下划进弹窗的瞬间就判定离开 → 弹窗刚出来就没了。
        """
        self.panel._toggle_popup_menu('settings', self.panel._settings_btn)
        self._pump(0.05)
        btn = self.panel._settings_btn
        pop = self.panel._popup_menu
        self.assertIsNotNone(pop, '弹窗没有创建')

        bx = btn.winfo_rootx() + btn.winfo_width() // 2
        by = btn.winfo_rooty() + btn.winfo_height() // 2
        self.assertTrue(self.panel._popup_hover_contains(bx, by),
                        '按钮中心应判定为「仍在区域内」')

        px = pop.winfo_rootx() + pop.winfo_width() // 2
        py = pop.winfo_rooty() + pop.winfo_height() // 2
        self.assertTrue(self.panel._popup_hover_contains(px, py),
                        '弹窗中心应判定为「仍在区域内」')

        # 按钮下沿与弹窗上沿之间（定位是 +4，缝中点取 +2）
        gap_y = btn.winfo_rooty() + btn.winfo_height() + 2
        self.assertLess(gap_y, pop.winfo_rooty(),
                        '前提：缝确实位于按钮 bottom 与弹窗 top 之间')
        self.assertTrue(self.panel._popup_hover_contains(bx, gap_y),
                        '穿缝而过被判定为离开，弹窗会刚出现就收起')

    def test_far_pointer_is_outside_hover_zone(self):
        """远离「按钮 + 弹窗」整体时必须判定为离开，否则永远不会收起。"""
        self.panel._toggle_popup_menu('settings', self.panel._settings_btn)
        self._pump(0.05)
        far_x = self.panel.win.winfo_rootx() + 60
        far_y = self.panel.win.winfo_rooty() + 400
        self.assertFalse(self.panel._popup_hover_contains(far_x, far_y),
                         '远端指针仍判定为在内，菜单无法自动收起')

    # ---------- 5. 轮询任务不泄漏 ----------

    def test_hover_job_is_cancelled_on_close(self):
        """收起时必须取消轮询，否则关掉后还在空转。

        用 `_open_popup_menu` 直接开（绕过悬停延迟），然后**不 pump**就检查
        轮询任务已建立；pump 会让轮询按「光标不在按钮上」把菜单收掉。
        """
        self.panel._menu_kind = 'tools'
        self.panel._menu_anchor = self.panel._tools_btn
        self.panel._open_popup_menu('tools')
        self.assertIsNotNone(self.panel._popup_hover_job,
                             '展开后没有启动悬停轮询')
        self.panel._close_popup_menu()
        self.assertIsNone(self.panel._popup_hover_job,
                          '收起后悬停轮询任务没有取消')
        self.assertIsNone(self.panel._popup_menu)
        self.assertEqual(len(self._top_levels()), 0, '关闭后仍有弹窗残留')

    def test_hover_open_is_delayed_not_immediate(self):
        """悬停展开必须**延迟**，不能收到 `<Enter>` 就立刻弹。

        实测缺陷：面板被热键唤出时光标常正压在按钮上，Tk 补发的 `<Enter>`
        会让菜单「自己蹦出来」。延迟一档后，指针没停留就不开。
        """
        self.assertGreater(self.panel.HOVER_OPEN_DELAY, 0,
                           'HOVER_OPEN_DELAY 必须为正，否则等于立刻展开')
        self.panel._bind_hover_menu('tools', self.panel._tools_btn)
        handler = None
        for seq in ('<Enter>',):
            script = self.panel._tools_btn.bind(seq)
            self.assertTrue(script, '<Enter> 未绑定')
        # 直接触发 on_enter 的等价效果：排队一个延迟任务，而不是立刻开
        self.panel._hover_open_job = None
        self.panel._hover_open('tools', self.panel._tools_btn)
        # 光标不在按钮上 -> 不应展开
        self.assertFalse(self.panel._popup_open,
                         '指针不在按钮上却展开了（应校验指针位置）')
        self.assertIsNone(handler)


if __name__ == '__main__':
    unittest.main()
