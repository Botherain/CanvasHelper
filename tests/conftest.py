"""pytest 公共夹具。

两件事：
1. **隔离副作用** —— 真实 ``state.json`` / ``pending_todos.json`` / ``config.json`` /
   ``canvas-deadlines.ics`` 跑完原样还原，任何小节都不许留下痕迹；
2. **共享准备** —— [1]/[3] 共用课程目录，[5]/[7] 共用待办草稿上下文，
   这样两个小节都能被单独执行（``-k test_7``）而不必先跑它的前置小节。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from canvas_helper import paths  # noqa: E402
from tests import smoke  # noqa: E402


@pytest.fixture(autouse=True)
def preserve_side_effects() -> object:
    """备份真实数据文件，跑完原样还原（含「原本不存在」这种情形）。"""
    watched = (paths.STATE_PATH, paths.PENDING_PATH, paths.CONFIG_PATH, paths.ICS_PATH)
    saved = {p: (p.read_bytes() if p.exists() else None) for p in watched}
    yield
    for p, data in saved.items():
        if data is None:
            p.unlink(missing_ok=True)
        else:
            p.write_bytes(data)


@pytest.fixture(scope="session")
def cfg() -> dict:
    return smoke.load_config()


@pytest.fixture(scope="session")
def folders() -> dict:
    """[1]/[3] 共用：按 COURSE_FIXTURE 临时造课程根（与机器无关，见 build_course_root）。"""
    root, tmp = smoke.build_course_root()
    yield smoke.course_dirs(root)
    if tmp is not None:
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture(scope="session")
def draft_ctx(cfg: dict) -> tuple:
    """[5]/[7] 共用：``(Course, build_drafts 结果)``。纯准备，不含断言。"""
    return smoke.build_drafts_ctx(cfg)
