"""双击启动 CanvasHelper 的图形界面（源码方式）。

为什么用 ``.pyw``：Windows 双击 ``.py`` 会附带一个控制台窗口，``.pyw`` 由
``pythonw.exe`` 执行、全程无控制台，也就不需要以前的 ``.bat`` 启动脚本
（那还得处理 ``chcp`` 代码页问题）。

前提：本机已装 Python 3.9+，并在项目目录执行过 ``pip install .``。
没有控制台可看报错，所以任何异常都用消息框说清楚，而不是静默退出。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _fail(message: str) -> None:
    """尽力弹一个消息框；连 tkinter 都起不来时退回 stderr。"""
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("CanvasHelper 启动失败", message)
        root.destroy()
    except Exception:
        print(message, file=sys.stderr)


def main() -> int:
    try:
        from canvas_helper.gui import main as gui_main
    except Exception as exc:
        _fail(
            f"缺少依赖或安装不完整：\n{exc}\n\n"
            "在项目目录里执行一次：\n"
            "    python -m pip install .\n"
            "然后再双击本文件。\n\n"
            "（只想用现成程序的话，下载 Release 里的 win64 zip，"
            "双击里面的 CanvasHelper.exe，不需要 Python。）"
        )
        return 1
    gui_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
