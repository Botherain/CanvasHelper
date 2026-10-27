"""PyInstaller 入口。

``inferry/__main__.py`` 用的是相对导入（``from .paths import ...``），
PyInstaller 把它当独立脚本分析时没有包上下文，会直接崩。
所以这里用绝对导入包一层，分析器与运行时都能正确解析。

用法::

    python -m PyInstaller --noconfirm --clean --name Inferry --windowed ^
        --collect-all playwright --collect-all tzdata ^
        scripts\\pyinstaller_entry.py

只支持**单目录**（默认）模式。不要加 ``--onefile``：解包到 ``_MEIPASS`` 临时目录后，
Playwright 的浏览器驱动与 ``%LOCALAPPDATA%`` 数据目录的相对定位都会出问题，
而且每次启动都要完整解包一遍。
"""

from __future__ import annotations

from inferry.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
