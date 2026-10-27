"""计划任务入口：无控制台运行，且不依赖工作目录。

为什么需要这个文件
------------------
计划任务里的命令原来写成::

    cmd /c cd /d "<项目目录>" && "<python.exe>" -m inferry sync --headless

两个问题：

1. **每天都闪一个黑框。** ``cmd`` 和 ``python.exe`` 都是控制台程序，
   schtasks 一到点就弹一个窗口，而这条命令的唯一目的就是后台同步。
   改用 ``pythonw.exe`` 后没有控制台，但 ``pythonw`` 又没有 `cd` 的能力——
   schtasks 的 ``/Create`` 也不提供「起始目录」参数，默认从 ``System32`` 起跑。

2. **起始目录错了就 import 不到包。** 未 ``pip install`` 时必然
   ``No module named inferry``，日志与相对路径也会错位。

所以把「定位源码目录」这件事从 shell 挪进 Python：本脚本用
``__file__`` 把项目根塞进 ``sys.path``，因此无论 schtasks 从哪个目录起跑
都能正常导入。计划任务于是可以写成一条**不含任何控制台程序**的命令::

    "<pythonw.exe>" "<项目目录>\\scripts\\headless_entry.py" sync --headless

用法
----
与 ``python -m inferry`` 完全一致，子命令与参数原样透传::

    headless_entry.py sync --headless
    headless_entry.py report --headless
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from inferry.__main__ import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
