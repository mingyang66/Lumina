"""Lumina 单元测试包。

此文件必须存在：`python -m unittest discover` 只递归进入含 `__init__.py`
的子目录，移除它会让整个测试集被静默跳过。作为包成员运行时，测试模块可用
相对导入互相引用（如 ``from .test_diff_ui_regressions import UiServer``）。

运行方式（项目根目录下）::

    python -m unittest discover

顶层目录由 discover 的起始目录决定；也可显式指定
``python -m unittest discover -s tests -t .``，此时项目根会被加入 sys.path，
测试内的 ``import main`` 与 ``from lumina import ...`` 均可正常解析。
"""
