"""CanvasHelper 测试包。

这个空标记的作用是把 ``tests/smoke.py`` 变成模块 ``tests.smoke``：

- 没有它时 mypy 只认得 ``smoke``，``pyproject.toml`` 里的
  ``[[tool.mypy.overrides]] module = "tests.*"`` 永远匹配不上；
- 有了它 pytest 也统一按包方式导入，后续新增测试文件的模块路径保持稳定。

``python tests/smoke.py`` 直接执行不受影响（脚本入口自己会把仓库根插进 sys.path）。
"""
