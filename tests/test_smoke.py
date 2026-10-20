"""把离线自检按小节交给 pytest：失败时直接给出该小节的失败明细。

小节与 ``python tests/smoke.py`` 的顺序、断言完全一致（同一份实现），
所以两条入口不会分叉。``smoke.FAILURES`` 收集的是该小节内所有 ``check``
的失败，一次把它们全列出来，而不是在第一条就中止。
"""

from __future__ import annotations

from tests import smoke


def _run(tag: str, fn, *args) -> None:
    smoke.FAILURES.clear()
    fn(*args)
    assert not smoke.FAILURES, "\n".join(
        [f"{tag}：{len(smoke.FAILURES)} 项失败", *smoke.FAILURES]
    )


def test_1_识别本地课程文件夹(folders: dict) -> None:
    _run("[1] 识别本地课程文件夹", smoke.s1_folders, folders)


def test_2_分类规则() -> None:
    _run("[2] 分类规则", smoke.s2_classify)


def test_3_目录解析(folders: dict) -> None:
    _run("[3] 目录解析", smoke.s3_resolve, folders)


def test_4_文件名清洗编码归一化() -> None:
    _run("[4] 文件名清洗 / 编码 / 归一化", smoke.s4_sanitize)


def test_5_待办草稿与合并(cfg: dict, draft_ctx: tuple) -> None:
    c, res = draft_ctx
    _run("[5] 待办草稿 + 合并", smoke.s5_drafts, c, res, cfg)


def test_5b_已完成作业的处理() -> None:
    _run("[5b] 已完成作业的处理", smoke.s5b_completed)


def test_5c_一键清理缓存() -> None:
    _run("[5c] 一键清理缓存", smoke.s5c_cleaner)


def test_6_下载计划() -> None:
    _run("[6] 下载计划（dry-run）", smoke.s6_plan)


def test_6b_本地已有内容的识别() -> None:
    _run("[6b] 本地已有内容的识别", smoke.s6b_local)


def test_6c_改名后缀不重复下载() -> None:
    _run("[6c] 改名后缀 (1)/(2) 不导致重复下载", smoke.s6c_rename)


def test_7_附加功能(draft_ctx: tuple) -> None:
    c, res = draft_ctx
    _run("[7] 附加功能：GradeNeed / .ics / 每周摘要", smoke.s7_extras, c, res.drafts)


def test_8_登录过期不再静默失败() -> None:
    _run("[8] 登录过期不再静默失败", smoke.s8_auth)


def test_9_打包元数据() -> None:
    _run("[9] 打包元数据 pyproject.toml", smoke.s9_packaging)


def test_10_压缩包判定与解压() -> None:
    _run("[10] 压缩包判定与解压", smoke.s10_extract)


def test_11_讲义整理() -> None:
    _run("[11] 讲义整理", smoke.s11_tidy)


def test_12_讨论区链接与快照() -> None:
    _run("[12] 讨论区 Colab 链接与快照", smoke.s12_discussions)


def test_13_跨院校通用化() -> None:
    _run("[13] 跨院校通用化（课程代码 / 目录匹配 / 时区）", smoke.s13_universal)
