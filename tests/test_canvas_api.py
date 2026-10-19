"""文件发现策略的离线回归测试。

背景：Canvas 这个租户对 CS0003/CS0005 关掉了课程级 ``/files``（403），逐文件夹
列文件**也**是 403，模块里一条 File 型条目都没有，模块条目的 ``content_details``
是空的——课件全挂在模块内的 Page 正文里，只有 ``page_url`` 能把正文调出来。

这些断言锁死的就是这套退化路径，避免下次有人"顺手简化"回去。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import canvas_helper.canvas_api as canvas_api_mod  # noqa: E402
from canvas_helper.canvas_api import CanvasAPI  # noqa: E402

BASE = "https://canvas.example"


# --------------------------------------------------------------------------- #
# 最小会话桩：够 CanvasAPI._page 用就行
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, status=200, data=None, text="", headers=None):
        self.status = status
        self._data = data
        self._text = text
        self.headers = headers or {}

    def json(self):
        return self._data

    def text(self):
        return self._text


class _Request:
    def __init__(self, session):
        self._session = session

    def get(self, url, params=None, timeout=None, fail_on_status_code=False):
        self._session.calls.append(url)
        path = url.removeprefix(BASE)
        if path not in self._session.routes:
            return _Resp(404, text="not found")
        payload = self._session.routes[path]
        if isinstance(payload, _Resp):
            return payload
        return _Resp(200, data=payload)


class _Ctx:
    def __init__(self, session):
        self.request = _Request(session)


class _Session:
    """形状兼容 CanvasSession 的桩。"""

    def __init__(self, routes):
        self.routes = routes
        self.calls: list[str] = []
        self._ctx = _Ctx(self)

    def url(self, path: str) -> str:
        return BASE + path

    def start(self, headless: bool = True) -> None:
        pass


def _api(routes) -> tuple[CanvasAPI, _Session]:
    sess = _Session(routes)
    return CanvasAPI(sess), sess


def _page_item(cid: int, page_url: str, html_url: str = "", title="Week 1") -> dict:
    return {
        "id": 9001,
        "type": "Page",
        "title": title,
        "page_url": page_url,
        "html_url": html_url or f"{BASE}/courses/{cid}/pages/{page_url}",
    }


def _routes_for_degraded_course(cid: int) -> dict:
    return {
        f"/api/v1/courses/{cid}/folders": [
            {"id": 11, "full_name": "course files/lectures"},
        ],
        # 关键：课程级索引被挡
        f"/api/v1/courses/{cid}/files": _Resp(403, text="forbidden"),
        f"/api/v1/courses/{cid}/modules": [{"id": 1, "name": "Week 1"}],
        f"/api/v1/courses/{cid}/modules/1/items": [_page_item(cid, "week-1")],
        f"/api/v1/courses/{cid}/pages/week-1": {
            "url": "week-1",
            "body": '<a href="/courses/10003/files/111">Lecture1.pdf</a>',
        },
        f"/api/v1/courses/{cid}/assignments": [],
        f"/api/v1/courses/{cid}/discussion_topics": [],
        "/api/v1/files/111": {
            "id": 111,
            "filename": "Lecture1.pdf",
            "folder_id": 11,
            "size": 1024,
            "url": f"{BASE}/dl/111",
        },
    }


# --------------------------------------------------------------------------- #
# 页面正文挖掘
# --------------------------------------------------------------------------- #
def test_模块索引被403时从页面正文挖出文件():
    cid = 10003
    api, sess = _api(_routes_for_degraded_course(cid))

    items, index = api.collect_items(cid)

    assert index == [], "课程级索引应当是空的（403）"
    files = [i for i in items if i.kind == "File"]
    assert [f.file_id for f in files] == [111]
    assert files[0].folder_path == "course files/lectures"
    assert files[0].title == "Lecture1.pdf"
    # 确认真的走了页面 show 接口，而不是靠 content_details.body
    assert any(p.endswith(f"/api/v1/courses/{cid}/pages/week-1") for p in sess.calls)


def test_页面show接口404时退回抓HTML源码():
    cid = 10003
    routes = _routes_for_degraded_course(cid)
    # show 接口不给，但渲染出来的页面给
    routes[f"/api/v1/courses/{cid}/pages/week-1"] = _Resp(404, text="nope")
    routes[f"/courses/{cid}/pages/week-1"] = _Resp(
        200, text='<a href="/courses/10003/files/222">SVM.pdf</a>'
    )
    routes["/api/v1/files/222"] = {
        "id": 222, "filename": "SVM.pdf", "folder_id": 11, "size": 5
    }
    api, sess = _api(routes)

    items, _ = api.collect_items(cid)

    assert [i.file_id for i in items if i.kind == "File"] == [222]
    assert any(p.endswith(f"/courses/{cid}/pages/week-1") for p in sess.calls)


def test_页面正文抓取结果会被缓存():
    cid = 10003
    api, sess = _api(_routes_for_degraded_course(cid))

    first = api.page_body(cid, "week-1", f"{BASE}/courses/{cid}/pages/week-1")
    second = api.page_body(cid, "week-1", f"{BASE}/courses/{cid}/pages/week-1")

    assert first == second
    hits = [p for p in sess.calls if p.endswith(f"/api/v1/courses/{cid}/pages/week-1")]
    assert len(hits) == 1, f"同一页面不应重复请求，实际 {len(hits)} 次"


def test_页面正文里的死链被容错跳过():
    cid = 10003
    routes = _routes_for_degraded_course(cid)
    # 文件元数据 404（死链示例：/files/{id} 查不到时按 404 跳过）
    routes["/api/v1/files/111"] = _Resp(404, text="gone")
    routes[f"/api/v1/courses/{cid}/pages/week-1"] = {
        "body": '<a href="/courses/10003/files/111">gone</a>',
    }
    api, _ = _api(routes)

    items, _ = api.collect_items(cid)

    assert [i.kind for i in items] == ["Page"], "死链不应产生文件条目，也不该中断收集"


# --------------------------------------------------------------------------- #
# 其余来源与去重
# --------------------------------------------------------------------------- #
def test_文件索引可用时页面来源不产生重复():
    cid = 10001
    routes = {
        f"/api/v1/courses/{cid}/folders": [{"id": 21, "full_name": "course files"}],
        f"/api/v1/courses/{cid}/files": [
            {"id": 333, "filename": "intro.pdf", "folder_id": 21, "size": 7},
        ],
        f"/api/v1/courses/{cid}/modules": [{"id": 5, "name": "Week 1"}],
        f"/api/v1/courses/{cid}/modules/5/items": [_page_item(cid, "intro")],
        f"/api/v1/courses/{cid}/pages/intro": {
            "body": '<a href="/courses/10001/files/333">intro</a>',
        },
        f"/api/v1/courses/{cid}/assignments": [],
        f"/api/v1/courses/{cid}/discussion_topics": [],
    }
    api, _ = _api(routes)

    items, index = api.collect_items(cid)

    files = [i for i in items if i.kind == "File"]
    assert len(index) == 1
    assert [f.file_id for f in files] == [333], "同一文件不能被索引与页面各来一遍"


def test_讨论区正文里的文件也会被收进来():
    cid = 10004
    routes = {
        f"/api/v1/courses/{cid}/folders": [{"id": 31, "full_name": "course files"}],
        f"/api/v1/courses/{cid}/files": _Resp(403, text="forbidden"),
        f"/api/v1/courses/{cid}/modules": [],
        f"/api/v1/courses/{cid}/assignments": [],
        f"/api/v1/courses/{cid}/discussion_topics": [
            {"id": 1, "message": "slides at /courses/10004/files/444"},
        ],
        "/api/v1/files/444": {
            "id": 444, "filename": "week1.pdf", "folder_id": 31, "size": 3,
        },
    }
    api, _ = _api(routes)

    items, _ = api.collect_items(cid)

    assert [i.file_id for i in items if i.kind == "File"] == [444]


def test_文件id正则只认课程正文里的链接不认API地址():
    text = (
        '<a href="/courses/10003/files/111">a</a> '
        '<a href="/courses/10003/files/222/download?x=1">b</a> '
        "<img src='/api/v1/files/999'> "
        "duplicate /courses/10003/files/111"
    )
    got = CanvasAPI._file_ids_from(text)
    assert got == [111, 222], f"got={got}"
    assert CanvasAPI._file_ids_from("") == []
    assert CanvasAPI._file_ids_from(None) == []


# --------------------------------------------------------------------------- #
# 讨论区：Colab / GitHub 链接（CS0004 的 tutorial 就是这么发的）
# --------------------------------------------------------------------------- #
_COLAB_MSG = (
    'this week tutorial: <a href="https://colab.research.google.com/github/'
    'prof/cs5494/blob/main/cs5494_tutorial_1_MNIST.ipynb?usp=sharing">colab</a>'
)


@pytest.fixture
def discussions_on(monkeypatch):
    """锁死配置：讨论区功能开，不依赖机器上的 config.json。"""
    monkeypatch.setattr(
        canvas_api_mod, "cfg_discussions",
        lambda cfg=None: {"links": True, "snapshot": True},
    )


def _routes_with_topics(cid: int, topics: list) -> dict:
    return {
        f"/api/v1/courses/{cid}/folders": [],
        f"/api/v1/courses/{cid}/files": _Resp(403, text="forbidden"),
        f"/api/v1/courses/{cid}/modules": [],
        f"/api/v1/courses/{cid}/assignments": [],
        f"/api/v1/courses/{cid}/discussion_topics": topics,
    }


def test_讨论区Colab链接转成raw直链下载条目(discussions_on) -> None:
    cid = 70594
    routes = _routes_with_topics(cid, [
        {"id": 71, "title": "Week 1 Tutorial", "message": _COLAB_MSG,
         "html_url": f"{BASE}/courses/{cid}/discussion_topics/71"},
    ])
    api, sess = _api(routes)

    items, _ = api.collect_items(cid)

    links = [i for i in items
             if i.kind == "File" and i.extra.get("link_source") == "discussion"]
    assert [i.title for i in links] == ["cs5494_tutorial_1_MNIST.ipynb"]
    assert links[0].file_url == (
        "https://raw.githubusercontent.com/prof/cs5494/main/"
        "cs5494_tutorial_1_MNIST.ipynb"), "colab 链接要转成 raw 直链"
    assert links[0].module_name == "Week 1 Tutorial", "帖子标题要参与分类"
    # 正文里已经有链接 -> 不该再额外请求回帖
    assert not any(p.endswith("/entries") for p in sess.calls)
    snaps = [i for i in items if i.kind == "Discussion"]
    assert len(snaps) == 1, "帖子本身要入列快照条目"
    assert snaps[0].title == "Week 1 Tutorial"
    assert snaps[0].extra["message"] == _COLAB_MSG
    assert snaps[0].file_id == 71, "快照用 topic id 做幂等键"


def test_正文没链接时才去回帖里翻(discussions_on) -> None:
    cid = 70595
    routes = _routes_with_topics(cid, [
        {"id": 81, "title": "Week 2 Tutorial", "message": "see replies"},
    ])
    routes[f"/api/v1/courses/{cid}/discussion_topics/81/entries"] = [
        {"message": "slides /courses/70595/files/555",
         "replies": [
             {"message": '<a href="https://github.com/prof/cs5495/blob/main/'
                         'tut2.ipynb">nb</a>'},
         ]},
    ]
    routes["/api/v1/files/555"] = {
        "id": 555, "filename": "week2.pdf", "size": 3,
    }
    api, sess = _api(routes)

    items, _ = api.collect_items(cid)

    assert 555 in [i.file_id for i in items if i.kind == "File"], \
        "回帖里的 Canvas 文件也要收"
    links = [i for i in items if i.extra.get("link_source") == "discussion"]
    assert [i.file_url for i in links] == [
        "https://raw.githubusercontent.com/prof/cs5495/main/tut2.ipynb"], \
        "嵌套 replies 里的链接也要收"
    assert any(p.endswith("/discussion_topics/81/entries") for p in sess.calls)


def test_公告只扫链接不出快照(discussions_on) -> None:
    cid = 70593
    routes = _routes_with_topics(cid, [
        {"id": 91, "title": "Tutorial 3 posted", "is_announcement": True,
         "message": _COLAB_MSG},
    ])
    api, sess = _api(routes)

    items, _ = api.collect_items(cid)

    assert any(i.extra.get("link_source") == "discussion" for i in items), \
        "公告里的链接照样收"
    assert not any(i.kind == "Discussion" for i in items), "公告不再存快照"
    assert not any(p.endswith("/entries") for p in sess.calls), "公告不去翻回帖"


def test_链接解析边界() -> None:
    got = CanvasAPI._link_targets(
        "https://github.com/a/b/blob/main/x.ipynb?plain=1#L2 "
        "https://raw.githubusercontent.com/a/b/main/data.pdf "
        "https://github.com/a/b/blob/main/x.ipynb "
        "https://github.com/a/b/issues/1 "
        "https://colab.research.google.com/drive/1abcDEF_-2345678901234567890"
    )
    assert got == [
        ("https://raw.githubusercontent.com/a/b/main/x.ipynb", "x.ipynb"),
        ("https://raw.githubusercontent.com/a/b/main/data.pdf", "data.pdf"),
        ("https://drive.google.com/uc?export=download&id=1abcDEF_-2345678901234567890", ""),
    ], f"got={got}"
    assert CanvasAPI._link_targets("") == []
    assert CanvasAPI._link_targets(None) == []


def test_列表不带正文时补show接口(discussions_on) -> None:
    cid = 70592
    routes = _routes_with_topics(cid, [
        {"id": 61, "title": "Week 3 Tutorial", "message": ""},
    ])
    routes[f"/api/v1/courses/{cid}/discussion_topics/61"] = {
        "id": 61,
        "message": '<a href="https://github.com/prof/cs5494/blob/main/t3.ipynb">x</a>',
    }
    api, sess = _api(routes)

    items, _ = api.collect_items(cid)

    links = [i for i in items if i.extra.get("link_source") == "discussion"]
    assert [i.file_url for i in links] == [
        "https://raw.githubusercontent.com/prof/cs5494/main/t3.ipynb"], \
        "列表 message 为空时要补 show 接口取正文（CS0004 实测 7/7 个都是空）"
    assert any(p.endswith(f"/api/v1/courses/{cid}/discussion_topics/61") for p in sess.calls)
    snaps = [i for i in items if i.kind == "Discussion"]
    assert snaps and "t3.ipynb" in snaps[0].extra["message"], \
        "快照正文也要用 show 补的"


def test_colab_drive链接转下载直链并用帖子标题命名(discussions_on) -> None:
    cid = 10002
    routes = _routes_with_topics(cid, [
        {"id": 72, "title": "Colab sharing for week7",
         "message": "<p>https://colab.research.google.com/drive/"
                    "1-CNfyNMUbrML8sXFCYPt3JBq9QGrVbTa?usp=sharing "
                    "https://colab.research.google.com/drive/"
                    "1m2yskhCVehbvP0u_Icstwr-M2MEZ8s5L</p>"},
    ])
    api, _ = _api(routes)

    items, _ = api.collect_items(cid)

    links = [i for i in items if i.extra.get("link_source") == "discussion"]
    assert [i.file_url for i in links] == [
        "https://drive.google.com/uc?export=download&id=1-CNfyNMUbrML8sXFCYPt3JBq9QGrVbTa",
        "https://drive.google.com/uc?export=download&id=1m2yskhCVehbvP0u_Icstwr-M2MEZ8s5L",
    ]
    assert [i.title for i in links] == [
        "Colab sharing for week7.ipynb",
        "Colab sharing for week7-2.ipynb"], "同帖多条无名链接 -> 序号避免互相覆盖"
    assert links[0].module_name == "Colab sharing for week7"
