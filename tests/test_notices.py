"""通知不再落盘的回归测试。

旧版把通知正文写进 ``state.json`` 的 ``notices_cache``，而公告接口
``/api/v1/announcements`` 返回的是**全部历史**——于是只要登录过，公告就从
首次同步起一直攒在本地（实测 25 条、跨 40 天）；课程内容却是每次现拉、
不带历史。改版后 ``state.json`` 只留**已读标记**（``notices.seen``，
一串 key），正文一律只活在内存里，由界面拿最近一次拉取的结果渲染。

这几条锁住新行为，防止哪天顺手又把正文写回去。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from inferry.extras import Notice, mark_notices_read  # noqa: E402
from inferry.paths import STATE_PATH, load_state, save_state  # noqa: E402
from inferry.runner import SyncResult  # noqa: E402

BODY = "正文不该进 state.json"
LEGACY = "8 月的老公告正文，新版不许再攒着"


def _notice(url: str, title: str = "Assignment 2 released") -> Notice:
    return Notice(
        course="CS0003",
        title=title,
        body=BODY,
        posted_at="2026-10-07 12:00:00",
        url=url,
        kind="announcement",
    )


def _seed(state: dict | None = None) -> None:
    """写入一份可控的 state；跑完由 conftest 的 autouse 夹具原样还原真实文件。"""
    base = {
        "files": {"file:10003:1": {"path": r"D:\x\a.pdf", "size": 1,
                                    "kind": "file", "title": "a.pdf",
                                    "course_id": 10003, "synced_at": "2026-10-07T19:34:11+08:00"}},
        "todo_keys": {},
        "todo_done": {},
        "notices": {},
        "last_sync": "2026-10-07T19:34:11+08:00",
    }
    base.update(state or {})
    save_state(base)


def test_通知正文与标题都不落盘():
    _seed()
    mark_notices_read([_notice("https://example.test/courses/1/announcements/9")])

    st = load_state()
    raw = STATE_PATH.read_text(encoding="utf-8")
    assert "notices_cache" not in st, "正文缓存又写回 state.json 了"
    assert "Assignment 2 released" not in raw, "标题进了 state.json"
    assert BODY not in raw, "正文进了 state.json"
    # 已读标记才是唯一该留下的东西
    assert st["notices"]["seen"] == ["https://example.test/courses/1/announcements/9"]


def test_老版本残留的缓存会被顺手抹掉():
    _seed({"notices": {"seen": ["u1"]},
           "notices_cache": [{"title": "老公告", "body": LEGACY}]})

    mark_notices_read([_notice("u1"), _notice("u2")])

    st = load_state()
    raw = STATE_PATH.read_text(encoding="utf-8")
    assert "notices_cache" not in st, "残留的旧版通知缓存没被清掉"
    assert LEGACY not in raw, "残留的旧正文还在文件里"
    # 只抹通知缓存，下载记录不能被误伤
    assert st["files"], "清理通知缓存时把下载记录也带走了"
    assert st["notices"]["seen"] == ["u1", "u2"]


def test_未读判定不受影响():
    _seed()
    first = mark_notices_read([_notice("u1"), _notice("u2")])
    assert len(first) == 2, "首次出现的两条应当都是未读"

    again = mark_notices_read([_notice("u1"), _notice("u2")])
    assert again == [], "同一批通知第二次不该再算未读"

    grown = mark_notices_read([_notice("u1"), _notice("u2"), _notice("u3")])
    assert [n.url for n in grown] == ["u3"], "只有新增的那条该算未读"


def test_通知条数涨十倍state的顶层键也不变():
    """「不一直保留」的核心：通知再多，state.json 的结构不跟着膨胀。"""
    _seed()
    mark_notices_read([_notice(f"u{i}") for i in range(50)])
    keys_after_small = set(load_state())

    mark_notices_read([_notice(f"u{i}") for i in range(500)])
    keys_after_big = set(load_state())

    assert keys_after_small == keys_after_big, "通知变多导致 state 顶层键变化"
    assert "notices_cache" not in keys_after_big


def test_SyncResult用None表示本轮压根没拉通知():
    """GUI 靠 None 区分「没拉」与「拉到 0 条」，避免关掉通知聚合后误清空表格。"""
    assert SyncResult().notices is None
    assert SyncResult(notices=[]) == SyncResult(notices=[])
    assert SyncResult(notices=[_notice("u1")]).notices is not None
