"""Canvas REST API (/api/v1) 封装：分页、模块、文件、作业、公告、成绩。"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from urllib.parse import quote, unquote

from .auth import AuthRequired, CanvasSession
from .coursecode import course_code
from .paths import cfg_discussions, get_logger

log = get_logger()

# GitHub / Colab 直链：blob 页面换成 raw.githubusercontent.com 即可直接下载。
#   colab.research.google.com/github/{owner}/{repo}/blob/{ref}/{path}
#   github.com/{owner}/{repo}/blob/{ref}/{path}（/raw/ 同理）
# 路径里不含 ? # ' " < > ) ]（HTML href / markdown / query 的天然边界）。
_GH_BLOB_RE = re.compile(
    r"https?://(?:colab\.research\.google\.com/github/|github\.com/)"
    r"([\w.\-]+)/([\w.\-]+)/(?:blob|raw)/([\w.\-]+)/([^\s\"'<>)\]?#]+)",
    re.IGNORECASE,
)
_RAW_GH_RE = re.compile(
    r"https?://raw\.githubusercontent\.com/([\w.\-]+)/([\w.\-]+)/"
    r"([\w.\-]+)/([^\s\"'<>)\]?#]+)",
    re.IGNORECASE,
)
# Drive 型 Colab 链接（老师个人 Drive 上的笔记本，CS0004 的 week2-7 全是这种）。
# 文档 id 20+ 位，链接本身不含文件名。
_COLAB_DRIVE_RE = re.compile(
    r"https?://colab\.research\.google\.com/drive/([\w\-]{20,})", re.IGNORECASE
)


@dataclass
class Course:
    id: int
    name: str
    code: str
    workflow_state: str = "available"
    html_url: str = ""

    @property
    def short(self) -> str:
        """课程代码（跨院校通用），例如 CS5187 / MATH101。

        提取规则见 :mod:`canvas_helper.coursecode`：课程代码字段优先，
        剥掉学期数字前缀；课程名次之；都不行才回落到原始字符串。
        """
        return course_code(self.code, self.name, fallback=str(self.id))


@dataclass
class Item:
    """一条可同步的内容（通常是文件，也可能是作业/页面）。"""

    course_id: int
    module_name: str
    title: str
    kind: str  # File / Assignment / Page / Quiz / ExternalUrl / Discussion / ...
    html_url: str = ""
    file_id: int | None = None
    file_name: str | None = None
    file_url: str | None = None
    file_size: int | None = None
    folder_path: str = ""  # Canvas 文件夹全路径，例如 course files/Lecture/Week3
    due_at: str | None = None
    points: float | None = None
    description: str = ""
    indent: int = 0
    extra: dict = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        return decode_name(self.file_name or self.title)


def decode_name(name: str | None) -> str:
    """Canvas 的 filename 是 form 编码的：`Tutorial+1.pdf`、`%28new%29`。

    只把「单个 +」当作空格，保留 `C++.pdf` 这类名字里的连续加号。
    """
    if not name:
        return name or ""
    from urllib.parse import unquote

    out = unquote(name)
    out = re.sub(r"(?<!\+)\+(?!\+)", " ", out)
    return out


class CanvasAPI:
    def __init__(self, session: CanvasSession):
        self.s = session

    # ---------- 基础 ----------

    def _page(self, path: str, params: dict | None = None):
        """请求一页，返回 (items, next_url)。"""
        if self.s._ctx is None:
            self.s.start(headless=True)
        ctx = self.s._ctx
        if ctx is None:  # start() 成功后必不为 None；此处仅为给类型检查器收窄
            raise RuntimeError("浏览器会话未能启动")
        resp = ctx.request.get(self.s.url(path), params=params, timeout=60_000, fail_on_status_code=False)
        if resp.status in (403, 429):
            # Canvas 限流会以 403 返回，先甄别再决定是否重试
            try:
                body = (resp.text() or "").lower()
            except Exception:
                body = ""
            if resp.status == 429 or "rate" in body or "too many" in body:
                log.warning("Canvas 限流(HTTP %s)，5 秒后重试: %s", resp.status, path)
                time.sleep(5)
                resp = ctx.request.get(self.s.url(path), params=params, timeout=60_000,
                                       fail_on_status_code=False)
        if resp.status == 401:
            raise AuthRequired(f"{path} -> HTTP {resp.status}")
        if resp.status == 403:
            # 学生常被禁止列出课程全部文件，但模块/单文件仍然可读 -> 降级而不是中断
            log.warning("403 无权限，跳过该接口: %s", path)
            return [], None
        if resp.status >= 400:
            log.warning("%s -> HTTP %s", path, resp.status)
            return [], None
        try:
            data = resp.json()
        except Exception:
            return [], None
        items = data if isinstance(data, list) else [data]
        headers = resp.headers if hasattr(resp, "headers") else {}
        link = headers.get("link") or headers.get("Link") or ""
        nxt = None
        for part in link.split(","):
            if 'rel="next"' in part:
                m = re.search(r"<([^>]+)>", part)
                if m:
                    nxt = m.group(1)
        return items, nxt

    def get_all(self, path: str, params: dict | None = None, limit: int = 50) -> list:
        """跟随 Link 头翻页。"""
        out: list = []
        url = path
        first = True
        while url and len(out) < limit * 100:
            items, url = self._page(url, params if first else None)
            first = False
            if not items:
                break
            out.extend(items)
        return out

    def get(self, path: str, params: dict | None = None):
        items, _ = self._page(path, params)
        return items[0] if items else None

    # ---------- 课程 ----------

    def courses(self) -> list[Course]:
        data = self.get_all(
            "/api/v1/courses",
            params={
                "enrollment_state": "active",
                "include[]": ["total_students", "term"],
                "per_page": 100,
            },
        )
        out = []
        for c in data:
            if c.get("enrollment_state") not in (None, "active"):
                continue
            if c.get("deleted_at"):
                continue
            out.append(
                Course(
                    id=int(c["id"]),
                    name=c.get("name") or c.get("course_code") or str(c["id"]),
                    code=c.get("course_code") or "",
                    workflow_state=c.get("workflow_state") or "available",
                    html_url=c.get("html_url") or self.s.url(f"/courses/{c['id']}"),
                )
            )
        out.sort(key=lambda c: c.short)
        return out

    # ---------- 模块 / 内容 ----------

    def module_items(self, course_id: int) -> list[dict]:
        """返回带 module_name 的原始 item 列表。"""
        modules = self.get_all(
            f"/api/v1/courses/{course_id}/modules",
            params={"per_page": 100},
        )
        result = []
        for mod in modules:
            if mod.get("workflow_state") not in (None, "active", "unpublished"):
                continue
            name = mod.get("name") or ""
            items = self.get_all(
                f"/api/v1/courses/{course_id}/modules/{mod['id']}/items",
                params={"include[]": ["content_details", "submission"], "per_page": 100},
            )
            if not items and mod.get("items"):
                items = mod["items"]
            for raw in items:
                it = dict(raw)
                it["_module_name"] = name
                result.append(it)
        return result

    def files(self, course_id: int) -> list[dict]:
        """课程文件索引。学生账号可能被禁用该索引（403），此时返回空列表。"""
        out = self.get_all(
            f"/api/v1/courses/{course_id}/files",
            params={"per_page": 100, "include[]": ["folder"]},
        )
        return out

    def folder_map(self, course_id: int) -> dict[int, str]:
        """{folder_id: full_name}，例如 2669334 -> 'course files/Lecture/Week3'。"""
        out: dict[int, str] = {}
        for fo in self.get_all(f"/api/v1/courses/{course_id}/folders", params={"per_page": 100}):
            try:
                out[int(fo["id"])] = fo.get("full_name") or fo.get("name") or ""
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def file_meta(self, file_id: int) -> dict | None:
        """单文件元数据。即使 /files 索引 403，这个接口仍然可用。"""
        if not hasattr(self, "_meta_cache"):
            self._meta_cache: dict[int, dict | None] = {}
        if file_id in self._meta_cache:
            return self._meta_cache[file_id]
        data = self.get(f"/api/v1/files/{file_id}")
        meta = data if isinstance(data, dict) and data.get("id") else None
        self._meta_cache[file_id] = meta
        return meta

    def page_body(self, course_id: int, page_url: str, html_url: str = "") -> str:
        """页面正文（HTML 片段）。

        这个租户把 ``/courses/:id/pages`` 的**列表**接口关掉了（404），模块条目的
        ``content_details`` 也一律是空的——实测 CS0003 的 14 个 Page 一个 ``body``
        键都没有。但只要拿到 ``page_url``（模块条目本身就带着），show 接口照常 200；
        show 也失败时退回抓 ``html_url`` 的源码。

        这是 CS0003 / CS0005 的文件主干：课程级 ``/files`` 403、46 个文件夹逐个列也
        403、模块里没有一条 File 型条目——页面正文是唯一还能挖出文件链接的地方
        （实测补回 38 / 33 个，相对原有 21 / 9 是 +181% / +367%）。
        """
        if not page_url:
            return ""
        if not hasattr(self, "_page_cache"):
            self._page_cache: dict[tuple[int, str], str] = {}
        key = (course_id, page_url)
        if key in self._page_cache:
            return self._page_cache[key]

        body = ""
        data = self.get(f"/api/v1/courses/{course_id}/pages/{quote(page_url, safe='')}")
        if isinstance(data, dict):
            body = data.get("body") or ""
        if not body and html_url:
            body = self._raw_html(html_url)
        self._page_cache[key] = body
        return body

    def _raw_html(self, url: str) -> str:
        """抓页面源码（页面 show 接口不可用时的退路）。"""
        if self.s._ctx is None:
            self.s.start(headless=True)
        ctx = self.s._ctx
        if ctx is None:
            return ""
        resp = ctx.request.get(url, timeout=60_000, fail_on_status_code=False)
        if resp.status == 401:
            # 掉线必须向上抛，交给登录过期契约处理，不能在这里吞掉
            raise AuthRequired(f"{url} -> HTTP {resp.status}")
        if resp.status != 200:
            log.warning("%s -> HTTP %s", url, resp.status)
            return ""
        try:
            return resp.text()
        except Exception as exc:
            log.warning("页面源码读取失败 %s: %s", url, exc)
            return ""

    def assignments(self, course_id: int) -> list[dict]:
        return self.get_all(
            f"/api/v1/courses/{course_id}/assignments",
            params={"include[]": ["submission", "all_dates"], "per_page": 100},
        )

    def announcements(self, course_id: int) -> list[dict]:
        """公告。

        Canvas **没有** `/api/v1/courses/:id/announcements`（404）。可用的是
        `/api/v1/announcements?context_codes[]=course_N`，旧版是
        `/api/v1/courses/N/discussion_topics?only_announcements=true`。
        依次尝试，拿到非空即返回。
        """
        attempts = (
            ("/api/v1/announcements",
             {"context_codes[]": f"course_{course_id}", "per_page": 100}),
            (f"/api/v1/courses/{course_id}/discussion_topics",
             {"only_announcements": "true", "per_page": 100}),
        )
        for path, params in attempts:
            try:
                items = self.get_all(path, params=params)
            except AuthRequired:
                raise
            except Exception as exc:
                log.debug("公告接口失败 %s: %s", path, exc)
                continue
            if items:
                return [r for r in items if r.get("is_announcement") is not False]
        return []

    def activity_stream(self) -> list[dict]:
        try:
            return self.get_all("/api/v1/users/self/activity_stream", params={"per_page": 50})
        except AuthRequired:
            raise
        except Exception as exc:
            log.warning("activity_stream 失败: %s", exc)
            return []

    def conversations(self) -> list[dict]:
        try:
            return self.get_all(
                "/api/v1/conversations",
                params={"scope": "inbox", "per_page": 50},
            )
        except Exception as exc:
            log.warning("conversations 失败: %s", exc)
            return []

    def scores(self) -> list[dict]:
        """当前成绩（已出分的评分项 + 总评）。"""
        try:
            return self.get_all(
                "/api/v1/users/self/enrollments",
                params={
                    "include[]": ["current_scores", "current_grading_period_scores", "total"],
                    "per_page": 100,
                },
            )
        except Exception as exc:
            log.warning("成绩获取失败: %s", exc)
            return []

    # ---------- 组装为 Item ----------

    @staticmethod
    def _item_from_module_item(course_id: int, raw: dict) -> Item | None:
        title = (raw.get("title") or "").strip()
        kind = (raw.get("type") or "Page").strip()
        details = raw.get("content_details") or {}
        it = Item(
            course_id=course_id,
            module_name=raw.get("_module_name") or "",
            title=title,
            kind=kind,
            html_url=raw.get("html_url") or raw.get("url") or "",
            due_at=details.get("due_at"),
            points=details.get("points_possible"),
            indent=int(raw.get("indent") or 0),
            extra=raw,
        )
        if kind == "File":
            it.file_id = raw.get("content_id") or details.get("id")
            it.file_name = details.get("filename") or title
            it.file_url = details.get("url")
            it.file_size = details.get("size")
        if kind == "Assignment" and not it.due_at:
            it.due_at = details.get("due_at")
        return it

    @staticmethod
    def _file_ids_from(text: str) -> list[int]:
        """从作业描述 / 页面正文里抽取引用的文件 id。"""
        if not text:
            return []
        out: list[int] = []
        for m in re.finditer(r"/courses/\d+/files/(\d+)", text):
            try:
                fid = int(m.group(1))
            except ValueError:
                continue
            if fid not in out:
                out.append(fid)
        return out

    @staticmethod
    def _link_targets(text: str) -> list[tuple[str, str]]:
        """讨论正文里的可下载链接 -> [(直链, 文件名)]，同链接去重。

        老师常把 tutorial 笔记本以 Colab 链接发在讨论区，实测两种形态：

        * GitHub 型 ``colab.research.google.com/github/.../blob/...`` 与
          ``github.com/.../blob/...`` —— 换成 raw 直链即可下载；
        * Drive 型 ``colab.research.google.com/drive/{文档id}``（CS0004 的
          week2-7 全是这种）—— 转 ``drive.google.com/uc?export=download``，
          公开分享的文件可匿名下载，文件名链接里没有（名字为空，由调用方
          用帖子标题命名）。

        私有仓库/未公开分享会 404，由调用方降级为只存帖子快照。
        """
        if not text:
            return []
        out: list[tuple[str, str]] = []
        seen: set[str] = set()

        def _add(url: str, name: str) -> None:
            if url in seen:
                return
            seen.add(url)
            out.append((url, name))

        for rx in (_GH_BLOB_RE, _RAW_GH_RE):
            for m in rx.finditer(text):
                url = "https://raw.githubusercontent.com/" + "/".join(m.groups())
                name = unquote(url.rpartition("/")[2]).strip()
                if name:
                    _add(url, name)
        for m in _COLAB_DRIVE_RE.finditer(text):
            _add(f"https://drive.google.com/uc?export=download&id={m.group(1)}", "")
        return out

    @staticmethod
    def _entry_texts(entry: dict) -> list[str]:
        """回帖正文（含嵌套 replies）。"""
        out = [entry.get("message") or ""]
        for r in entry.get("replies") or []:
            out.extend(CanvasAPI._entry_texts(r))
        return out

    def discussion_entries(self, course_id: int, topic_id: int) -> list[dict]:
        """讨论帖的回帖（顶层列表，更深的 replies 嵌在各条目里）。"""
        return self.get_all(
            f"/api/v1/courses/{course_id}/discussion_topics/{topic_id}/entries",
            params={"per_page": 100},
        )

    def collect_items(self, course_id: int) -> tuple[list[Item], list[dict]]:
        """收集一门课的全部内容。

        实测这几门课的 Canvas 用法差异很大，任何单一接口都会漏：
        - CS0001/CS0002/CS0004/CS0006：没有模块，内容全在**文件夹 + 文件**里，
          课程级 ``/files`` 索引可读；
        - CS0003/CS0005：课程级 ``/files`` 索引 403，**逐文件夹列文件也 403**，
          模块里一条 File 型条目都没有，课件全挂在模块内的 **Page 正文**里；
          只能靠 ``page_url`` 调页面 show 接口 + 单文件 ``/files/{id}`` 查询。

        因此五条来源都要采，失败互相降级：
          1) 模块条目（含 Page 正文，见 page_body）
          2) 作业描述
          3) 公告 / 讨论正文
          4) 课程级文件索引（可能 403 -> 空）
        """
        items: list[Item] = []
        seen_files: set[int] = set()
        seen_keys: set[str] = set()

        # 文件夹路径（分类与归档的主要依据）
        folder_paths: dict[int, str] = {}
        try:
            folder_paths = self.folder_map(course_id)
        except Exception as exc:
            log.warning("%s 文件夹读取失败: %s", course_id, exc)

        # 文件索引（可能 403 -> 空）
        file_index: dict[int, dict] = {}
        for f in self.files(course_id):
            try:
                file_index[int(f["id"])] = f
            except (KeyError, TypeError, ValueError):
                continue

        def _folder_of(meta: dict) -> str:
            fid = meta.get("folder_id")
            if fid is not None:
                path = folder_paths.get(int(fid))
                if path:
                    return path
            folder = meta.get("folder") or {}
            return folder.get("full_name") or ""

        def _meta_of(file_id: int) -> dict | None:
            return file_index.get(file_id) or self.file_meta(file_id)

        def _make_file(file_id: int, module_name: str = "", title: str | None = None) -> Item | None:
            if file_id in seen_files:
                return None
            meta = _meta_of(file_id)
            if not meta:
                return None
            if meta.get("hidden") or meta.get("locked_for_user"):
                log.info("跳过隐藏/锁定文件 %s", meta.get("filename"))
                seen_files.add(file_id)
                return None
            seen_files.add(file_id)
            name = meta.get("filename") or title or str(file_id)
            return Item(
                course_id=course_id,
                module_name=module_name,
                title=decode_name(name),
                kind="File",
                html_url=meta.get("html_url") or self.s.url(f"/files/{file_id}"),
                file_id=file_id,
                file_name=name,
                file_url=meta.get("url"),
                file_size=meta.get("size"),
                folder_path=_folder_of(meta),
                extra=meta,
            )

        # ---- 1) 模块条目（页面 / 作业 / 子标题 / 文件）----
        for raw in self.module_items(course_id):
            it = self._item_from_module_item(course_id, raw)
            if it is None:
                continue
            mod_name = it.module_name
            if it.kind == "File" and it.file_id:
                fid = int(it.file_id)
                made = _make_file(fid, mod_name, it.title)
                if made is not None:
                    items.append(made)
                continue
            if it.title:
                seen_keys.add(f"{it.kind}:{it.title}")
            items.append(it)
            # 条目正文里引用的文件。模块条目的 content_details.body 在这个租户
            # 基本是空的（CS0003 实测 14/14 个 Page 连 body 键都没有），所以
            # Page 型条目必须改用 page_url 去调页面 show 接口——这是这类课程
            # 唯一的文件来源。
            body = (raw.get("content_details") or {}).get("body") or ""
            if not body and it.kind == "Page":
                body = self.page_body(course_id, raw.get("page_url") or "",
                                      raw.get("html_url") or "")
            for fid in self._file_ids_from(body):
                made = _make_file(fid, mod_name)
                if made is not None:
                    items.append(made)

        # ---- 2) 作业（截止时间的唯一可靠来源，也常带文件链接）----
        try:
            assigns = self.assignments(course_id)
        except AuthRequired:
            raise
        except Exception as exc:
            log.warning("%s 作业读取失败: %s", course_id, exc)
            assigns = []

        for a in assigns:
            aid = a.get("id")
            if aid is None:
                continue
            title = decode_name(a.get("name") or f"作业 {aid}")
            key = f"Assignment:{aid}"
            if key not in seen_keys:
                seen_keys.add(key)
                dates = a.get("all_dates") or []
                due = a.get("due_at") or (dates[0].get("due_at") if dates else None)
                items.append(
                    Item(
                        course_id=course_id,
                        module_name="",
                        title=title,
                        kind="Assignment",
                        html_url=a.get("html_url") or self.s.url(
                            f"/courses/{course_id}/assignments/{aid}"),
                        due_at=due,
                        points=a.get("points_possible"),
                        extra=a,
                    )
                )
            for fid in self._file_ids_from(a.get("description") or ""):
                made = _make_file(fid, title)
                if made is not None:
                    items.append(made)

        # ---- 3) 公告 / 讨论正文（老师常在这里直接甩附件链接）----
        try:
            topics = self.get_all(
                f"/api/v1/courses/{course_id}/discussion_topics",
                params={"per_page": 100},
            )
        except AuthRequired:
            raise
        except Exception as exc:
            log.warning("%s 讨论区读取失败: %s", course_id, exc)
            topics = []

        dsc = cfg_discussions()
        seen_urls: set[str] = set()

        for t in topics:
            msg = t.get("message") or ""
            title = decode_name(t.get("title") or "").strip()
            tid = t.get("id")
            # 列表接口常常不带正文（CS0004 实测 7/7 个 message 全空），
            # 要用 show 接口补——链接和快照都靠这份正文
            if not msg and isinstance(tid, int) and (
                    dsc.get("links", True) or dsc.get("snapshot", True)):
                try:
                    full = self.get(
                        f"/api/v1/courses/{course_id}/discussion_topics/{tid}")
                    if isinstance(full, dict):
                        msg = full.get("message") or ""
                except AuthRequired:
                    raise
                except Exception as exc:
                    log.warning("%s 讨论帖正文读取失败 topic=%s: %s", course_id, tid, exc)
            for fid in self._file_ids_from(msg):
                made = _make_file(fid)
                if made is not None:
                    items.append(made)

            if not dsc.get("links", True):
                continue

            # 直链：正文里没有才去翻回帖（省 API 调用）
            links = self._link_targets(msg)
            if not links and tid and not t.get("is_announcement"):
                try:
                    for e in self.discussion_entries(course_id, int(tid)):
                        for text in self._entry_texts(e):
                            for fid in self._file_ids_from(text):
                                made = _make_file(fid)
                                if made is not None:
                                    items.append(made)
                            links += self._link_targets(text)
                except AuthRequired:
                    raise
                except Exception as exc:
                    log.warning("%s 讨论回帖读取失败 topic=%s: %s", course_id, tid, exc)

            # 同一帖里多条不带文件名的链接 -> 第 2 条起加序号，避免互相覆盖
            titled: dict[str, int] = {}
            for url, link_name in links:
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                name = link_name
                if not name:
                    # Drive 型 Colab 链接不带文件名 -> 用帖子标题命名
                    stem = re.sub(r'[\\/:*?"<>|]', "_", title).strip() or f"colab-{tid}"
                    titled[stem] = titled.get(stem, 0) + 1
                    n = titled[stem]
                    name = f"{stem}.ipynb" if n == 1 else f"{stem}-{n}.ipynb"
                items.append(Item(
                    course_id=course_id,
                    module_name=title,
                    title=name,
                    kind="File",
                    html_url=t.get("html_url") or "",
                    file_name=name,
                    file_url=url,
                    extra={"link_source": "discussion"},
                ))

            # 帖子本身存快照。公告已有通知渠道聚合，只扫链接不快照
            if (not dsc.get("snapshot", True) or not msg
                    or t.get("is_announcement")):
                continue
            if title:
                key = f"Discussion:{title}"
                if key in seen_keys:
                    # 模块里已挂过同名讨论条目 -> 回填正文，不重复入列
                    for it in items:
                        if (it.kind == "Discussion" and it.title == title
                                and not it.extra.get("message")):
                            it.extra["message"] = msg
                            break
                    continue
                seen_keys.add(key)
            items.append(Item(
                course_id=course_id,
                module_name="",
                title=title or f"讨论 {tid}",
                kind="Discussion",
                html_url=t.get("html_url") or self.s.url(
                    f"/courses/{course_id}/discussion_topics/{tid}"),
                file_id=tid if isinstance(tid, int) else None,
                extra={"message": msg, "topic_id": tid},
            ))

        # ---- 4) 文件索引里剩下的（没挂模块、也没被作业/页面/讨论引用）----
        for fid in file_index:
            if fid in seen_files:
                continue
            made = _make_file(fid)
            if made is not None:
                items.append(made)

        return items, list(file_index.values())

    # ---------- 下载 ----------

    def download(self, url: str) -> tuple[bytes, str, str]:
        """下载文件，返回 (内容, 文件名, content-type)。"""
        if self.s._ctx is None:
            self.s.start(headless=True)
        ctx = self.s._ctx
        if ctx is None:  # start() 成功后必不为 None；此处仅为给类型检查器收窄
            raise RuntimeError("浏览器会话未能启动")
        resp = ctx.request.get(self.s.url(url), timeout=180_000, fail_on_status_code=False)
        if resp.status >= 400:
            raise RuntimeError(f"下载失败 HTTP {resp.status}: {url}")
        headers = dict(resp.headers or {})
        ctype = headers.get("content-type", "")
        name = _filename_from_headers(headers) or ""
        return resp.body(), name, ctype


def _filename_from_headers(headers: dict) -> str | None:
    cd = headers.get("content-disposition", "")
    if not cd:
        return None
    m = re.search(r"filename\*\s*=\s*UTF-8''([^;]+)", cd, re.IGNORECASE)
    if m:
        from urllib.parse import unquote

        return unquote(m.group(1)).strip()
    m = re.search(r'filename\s*=\s*"([^"]+)"', cd, re.IGNORECASE) or re.search(
        r"filename\s*=\s*([^;]+)", cd, re.IGNORECASE
    )
    if m:
        return m.group(1).strip().strip('"')
    return None


def iter_assignable(items: Iterable[Item]) -> list[Item]:
    """需要提交的项（作业/测验），带 due_at。"""
    return [
        i
        for i in items
        if i.kind in ("Assignment", "Quiz") and i.due_at
    ]
