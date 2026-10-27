"""旧 pip 的兼容垫片：pip < 21.3 执行 ``pip install -e .`` 时不读 pyproject 的 [project] 表。

Python 3.9 / 3.10 自带的 pip 分别是 20.2 与 21.2，都低于 21.3，editable 安装走的是
``setup.py develop`` 老路——那条路只认本文件里的元数据，于是 ``pip install -e .``
会以「没有 setup.py」或「元数据缺失」直接失败。

新 pip 走 PEP 517/621：只要 setuptools >= 61（本项目 build-system 要求 >= 77），
下面直接空调用 ``setup()``，一切以 pyproject.toml 为准，本文件不参与任何决策——
所以元数据副本只出现在旧环境里，不会变成第二份「需要同步的真相」。

版本号从 ``inferry/__init__.py`` 读，不在第三处再写一遍。
"""

from __future__ import annotations

import re
from pathlib import Path

from setuptools import setup

HERE = Path(__file__).parent


def _version() -> str:
    text = (HERE / "inferry" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
    if not m:
        raise SystemExit("读不到 inferry/__init__.py 里的 __version__")
    return m.group(1)


def _setuptools_reads_pep621() -> bool:
    """setuptools >= 61 才认 pyproject 的 [project]；更老的必须吃本文件的副本。"""
    try:
        from importlib.metadata import version

        major, minor = (int(part) for part in version("setuptools").split(".")[:2])
    except Exception:  # 元数据缺失 / 版本串异常：按旧环境处理，给全量元数据最保险
        return False
    return (major, minor) >= (61, 0)


if _setuptools_reads_pep621():
    setup()
else:
    setup(
        name="inferry",
        version=_version(),
        description="Inferry（原 CanvasHelper）：按既有目录规则归档 Canvas 课程文件，"
                    "把截止时间写入 DeskBox 待办",
        python_requires=">=3.9",
        packages=["inferry"],
        install_requires=["playwright>=1.49", "tzdata>=2024.1"],
        entry_points={
            "console_scripts": [
                "inferry = inferry.__main__:main",
                "canvas-helper = inferry.__main__:main",
            ],
            "gui_scripts": [
                "inferry-gui = inferry.gui:main",
                "canvas-helper-gui = inferry.gui:main",
            ],
        },
    )
