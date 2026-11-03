"""UI 测试的共用工具：让测试窗口**不闪屏**。

## 为什么要这个东西

跑测试时屏幕上会不停闪出窗口。根因是 UI 测试必须让窗口处于 **mapped**（已映射）
状态才能拿到真实几何——``winfo_width()``／``winfo_rootx()``／``pack_info()``
这些在 ``withdraw()`` 状态下返回的是过时或占位值，测不出布局缺陷。所以测试不能
简单地 ``withdraw()``，只能 ``deiconify()``，窗口就真的出现在屏幕上了。

一个用例建一次、``setUp`` 里循环 ``update()`` 几十次，整个测试集跑下来就是几百次
弹窗。

## 怎么解决

把窗口**映射成完全透明**（``-alpha 0``）。关键性质：

- 窗口仍然是 mapped 的，``winfo_*``／``pack_info()``／``winfo_rootx()`` 全部正常，
  几何与不透明时**逐控件完全一致**（实测 46 个控件的类名＋宽高指纹相同）。
- Windows 的 layered window 全透明时，屏幕上**一个像素都不画**。用一个纯色参照
  窗口做确定性验证：把 ``-alpha 0`` 的窗口盖上去，参照窗口的像素与未被遮挡时
  完全一致（``ImageChops.difference().getbbox() is None``）；把 alpha 调回 1，
  差异立刻变成整块区域。
- 实测：不静默时高频采样能看到 20+ 乃至 336 帧窗口内容，静默后 **0 帧**。

**必须在窗口被映射之前设**。窗口一旦映射，那一帧就已经画到屏幕上了。

## 用法

挂钩在 ``tests/__init__.py`` 里装一次，**所有测试文件自动生效，什么都不用写**。

之所以用全局挂钩而不是在每个 ``setUp`` 里逐个 ``silence()``：动态创建的窗口
（下拉菜单、工具窗、重建的面板）不继承 ``-alpha``，逐个设置总会漏——实测漏掉
它们时单个文件仍有 336 帧可见。打桩是**结构性覆盖**，新增代码不需要记得加什么。

## 有测试确实需要窗口可见怎么办

``-alpha 0`` 只影响「画没画到屏幕上」，不影响窗口的存在、层级与几何。但有两类
用例会被它破坏：

- ``ImageGrab`` 逐像素扫描（全透明窗口抓到的是背景）
- ``winfo_containing`` 屏幕坐标命中测试（实测 alpha=0 时返回 ``None``，
  全透明的 layered window 不参与命中）

这类用例在断言前把 alpha 显式调回 1 即可，用完再压回去：

    make_visible(win)      # 只有这一条需要真可见
    ...抓屏 / 命中测试...
    silence(win)

参考 ``test_panel_left_align`` 的像素版对齐、``test_tool_window`` 的边缘热区。
"""
import tkinter as tk

_INSTALLED = False


def _silence_new_window(original):
    """包装 ``__init__``，让新建窗口默认全透明。"""

    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        try:
            self.attributes('-alpha', 0.0)
        except tk.TclError:
            pass    # 无图形环境

    init._lumina_silent = True
    return init


def silence_windows():
    """让此后创建的 Tk / Toplevel 默认完全透明。

    在测试模块顶部调用一次。幂等：重复调用只装一次。
    """
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    if not getattr(tk.Tk.__init__, '_lumina_silent', False):
        tk.Tk.__init__ = _silence_new_window(tk.Tk.__init__)
    if not getattr(tk.Toplevel.__init__, '_lumina_silent', False):
        tk.Toplevel.__init__ = _silence_new_window(tk.Toplevel.__init__)


def silence(*windows):
    """把已存在的窗口设为完全透明（挂钩之外的补丁手段）。"""
    for window in windows:
        if window is None:
            continue
        try:
            window.attributes('-alpha', 0.0)
        except tk.TclError:
            pass


def make_visible(*windows):
    """临时恢复可见——只有 ``ImageGrab`` / ``winfo_containing`` 类用例需要。"""
    for window in windows:
        if window is None:
            continue
        try:
            window.attributes('-alpha', 1.0)
        except tk.TclError:
            pass
