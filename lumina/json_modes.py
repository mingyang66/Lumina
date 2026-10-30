"""JSON 转换模式的**唯一定义处**：模式清单、展示名、下拉菜单项。

在这里出现之前，同一份东西散在三处：``history_panel.DetailPane`` 的
``JSON_MODE_LABELS``、``history_panel`` 里 ``_transform_json`` 的状态文案映射、
``tool_window`` 的 ``JSON_MODE_LABELS`` 与 ``_json_menu`` 的 items 列表。四处
各自写死「格式化 / 压缩 / 展开转义」这些字符串，靠两条测试钉住不许漂移
——那是「用测试补架构缺口」。用户要求两边同步后，这里成为唯一来源。

**为什么只有「模式定义」被抽出来，而不是整段转换流程**：两侧的**线程模型**和
**状态机**本质不同，强行合会破坏详情栏的竞态保护。

======================  ==========================  ====================
维度                    剪贴板详情栏                 工具弹框
======================  ==========================  ====================
线程                    后台线程 + 轮询 collect        同线程直接算
输入                    历史记录 ``row["content"]``    左栏控件文本
忙碌/竞态               ``_json_busy`` + 请求序号      无（同步，不需要）
结果去向                ``_json_text`` → 重建预览      ``output`` 控件
======================  ==========================  ====================

所以边界划在「**模式是什么**」而不是「**怎么转换**」：这里只管模式的名字、
顺序、图标、菜单文案，以及构造可直接交给 ``JsonPopup`` 的 items 列表。转换
本身仍在 ``json_tools.transform_json``（本来就已共用）。
"""

# 模式清单在这里**独立写死**，不 ``from .json_tools import MODES`` 再转手：
#
# - 这里是「展示顺序 + 文案」的唯一来源，``json_tools`` 那边是「哪些值能通过
#   校验」。两者的用途不同，谁也不是谁的上游。
# - 曾经想过再导出一份 ``MODES`` 方便调用方，但没有任何调用方用它（这里是
#   唯一需要用清单的地方），留着就是一段"看起来有人在用"的死代码。
#
# 两边不一致会被 ``tests/test_json_modes.py`` 的
# ``test_mode_set_matches_json_tools`` 拦下。

# 模式 -> 短展示名。用于标题/状态栏的「JSON · 已格式化」与「已压缩 · 45 字符」。
MODE_LABELS = {
    "format": "已格式化",
    "compress": "已压缩",
    "expand": "已展开转义",
    "escape_java": "已转义",
}

# 下拉菜单里每个模式的 (label, hint, kind)。
#
# - ``label`` 是动作名（点它会做什么），``MODE_LABELS`` 是结果态名（做完之后
#   是什么状态）。两者措辞本就不同，**不要为了「少写一个字典」合并**：菜单里
#   写「已格式化」会读成「已经是格式化状态了，点了没反应」。
# - ``kind`` 同时是 ``JsonPopup._icon`` 的图标键，加新项必须同步在那里补图形，
#   否则那一项前面会空一块（``_icon`` 对未知 kind 静默不画）。
MODE_ITEMS = {
    "format": ("格式化", "2 空格", "format"),
    "compress": ("压缩", "紧凑输出", "compress"),
    "expand": ("展开转义", "换行成行", "expand"),
    "escape_java": ("Java 转义", "字符串字面量", "escape_java"),
}

# 菜单里模式的排列顺序。``escape_java`` 排在最后：它和另外三个的区别最大
# ——前三个产物仍是 JSON（或近似 JSON）结构，它的产物是 Java 源码片段。
MODE_ORDER = ("format", "compress", "expand", "escape_java")

# 模式 -> 动作名。用于「已{动作} JSON」这类**状态叙述**，与 ``MODE_LABELS``
# 的「已格式化」不同：那里是结果态名（标题里写 ``JSON · 已格式化``），这里是
# 动作本身（状态栏写 ``已格式化 JSON``）。
#
# 详情栏曾在这里再写死一份 ``{'format': '格式化', ...}``，是第三处平行定义；
# 现在统一从这里取。
ACTION_LABELS = {
    "format": "格式化",
    "compress": "压缩",
    # 比菜单里的「展开转义」更明确：菜单是短标签，状态栏有空间说清「换行」。
    "expand": "展开换行转义",
    "escape_java": "转义",
}

# ``escape_java`` 产物不是 JSON 而是 Java 源码片段，状态文案不能套
# 「已{动作} JSON」这个模板——所以单独给一句。末尾那句提示是功能的一部分：
# 产物不含外层引号，直接贴进 ``setXxx(...)`` 会编译不过。
ESCAPE_JAVA_NOTICE = "Java 字面量内容，粘贴时两侧补双引号"

RESTORE_LABEL = "恢复原文"


def build_menu_items(on_mode, on_restore, active_mode, *, busy=False, can_restore=False):
    """构造 ``JsonPopup`` 的 items 列表，两边**逐字相同**。

    ``on_mode(mode)`` 与 ``on_restore()`` 是各调用方自己的处理器——两侧的转换
    与恢复实现不同（见模块 docstring 的表），但菜单本身必须一模一样。

    ``busy`` 与 ``can_restore`` 各有含义，故意不复用一个开关：

    - ``busy``：转换进行中，禁用四个转换项（详情栏后台线程会用到；工具弹框
      同步执行，永远传 False）。
    - ``can_restore``：当前是否处于某个转换态。False 时「恢复原文」点了等于
      什么都没做，禁掉更诚实。

    「恢复原文」永远是最后一项，且 ``kind`` 固定为 ``restore``——``JsonPopup``
    按这个值在它上方画分隔线，不靠下标判断。
    """
    items = []
    for mode in MODE_ORDER:
        label, hint, kind = MODE_ITEMS[mode]
        items.append({
            "label": label,
            "hint": hint,
            "kind": kind,
            "command": lambda m=mode: on_mode(m),
            "enabled": not busy,
            "selected": active_mode == mode,
        })
    items.append({
        "label": RESTORE_LABEL,
        # 空 hint：它没有可解释的参数，写任何东西都是噪音。
        "hint": "",
        "kind": "restore",
        "command": on_restore,
        "enabled": bool(can_restore),
        "selected": False,
    })
    return items
