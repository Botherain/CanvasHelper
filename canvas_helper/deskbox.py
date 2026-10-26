"""DeskBox 待办集成：解析 Canvas 截止时间 -> 写入 todo.json（副本预写 + 快速关/换/启）。"""

from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable, Iterable
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from .canvas_api import Course, Item
from .paths import (
    PENDING_PATH,
    canvas_home,
    get_logger,
    load_state,
    now_local,
    save_config,
    save_state,
    to_local,
    zone_or_none,
)

# 3.9 兼容：datetime.UTC 这个常量是 3.11 才加的，旧解释器上取不到。
UTC = timezone.utc

log = get_logger()
#: 兼容别名：历史常量（香港时区）。新代码请用 :func:`canvas_helper.paths.to_local`
#: ——它跟随配置项 ``timezone``，换学校时才不会把截止时间算错几个小时。
#: 取不到时区库时为 None（Windows 的 IANA 库来自 tzdata），不影响主流程。
HK = zone_or_none("Asia/Hong_Kong")

HTML_RE = re.compile(r"<[^>]+>")

#: DeskBox 支持的待办颜色（与 TodoItem.SupportedColorMarkers 对齐）。
TODO_COLORS = ("red", "orange", "yellow", "green", "blue", "purple", "teal", "pink")
DEFAULT_MANUAL_COLOR = "pink"

#: Canvas 链接里的课程 id：/courses/70588/assignments/123
_COURSE_URL_RE = re.compile(r"/courses/(\d+)(?:[/?#]|$)")
#: 待办标题开头的课程代码：CS5187 · 标题 / CS5187-标题
_COURSE_HEAD_RE = re.compile(r"^([A-Za-z]{1,8})\s*[-_ ]?\s*(\d{3,5})(?![0-9])")


@lru_cache(maxsize=8)
def _url_re_for(home: str) -> re.Pattern[str]:
    """按配置的站点地址编译待办备注里的链接抽取式（站点换学校即换）。"""
    if not home:
        return re.compile(r"(?!)")  # 未配置站点：不匹配任何链接，去重退回 text|due 键
    return re.compile(re.escape(home) + r"[^\s)\]>]*", re.IGNORECASE)


def _canvas_url_re() -> re.Pattern[str]:
    return _url_re_for(canvas_home())


@dataclass
class TodoDraft:
    item: Item
    course: Course
    text: str
    due: datetime | None
    kind: str  # assignment / project / tutorial / other
    url: str
    color: str
    notes: str
    attachment: Path | None = None
    existing: bool = False
    # "" | "completed"（DeskBox 里已完成，不重复添加） | "reopened"（截止时间变了，重新添加）
    status: str = ""

    @property
    def key(self) -> str:
        return self.url or f"{self.text}|{_fmt_due(self.due) if self.due else ''}"


@dataclass
class TodoBuildResult:
    drafts: list[TodoDraft] = field(default_factory=list)
    skipped_past: int = 0


def _strip_html(text: str, limit: int = 600) -> str:
    if not text:
        return ""
    text = HTML_RE.sub(" ", text)
    text = re.sub(r"&nbsp;?", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def _parse_due(value: str | None) -> datetime | None:
    if not value:
        return None
    # 3.11 起 fromisoformat 才认结尾的 Z；旧解释器上先换成 +00:00，否则 Canvas
    # 返回的 "2026-11-20T15:59:00Z" 会 ValueError，整条截止时间被当成「没有」。
    text = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return to_local(dt)


def _fmt_due(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


# ---------- 待办颜色 ----------

def normalize_color(value: str | None, default: str | None = None) -> str | None:
    """把配置里的颜色名规范化；不认识的颜色回落到 ``default``。"""
    text = (value or "").strip().lower()
    if not text:
        return default
    return text if text in TODO_COLORS else default


def manual_color_of(cfg: dict | None) -> str | None:
    """配置里的「自建待办颜色」；空字符串 / none / off 表示不修改。"""
    raw = (cfg or {}).get("manual_color", DEFAULT_MANUAL_COLOR)
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if not text or text in ("none", "off", "no"):
        return None
    return text if text in TODO_COLORS else DEFAULT_MANUAL_COLOR


def _course_entries(courses: Iterable[Course]) -> list[tuple[str, str]]:
    """把 Course 列表变成 ``(课程 id, 显示名)``，按传入顺序去重。"""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for course in courses or []:
        cid = str(getattr(course, "id", "") or "")
        if not cid or cid == "0" or cid in seen:
            continue
        short = str(getattr(course, "short", "") or cid)
        name = str(getattr(course, "name", "") or "").strip()
        if not name or name == short or name.upper().startswith(short.upper()):
            # 课程名本身已带代码（或课程没有名字）时不再重复拼一遍
            label = name or short
        else:
            label = f"{short} - {name}"
        out.append((cid, label))
        seen.add(cid)
    return out


def ensure_course_colors(
    cfg: dict,
    courses: Iterable[Course],
    *,
    persist: bool = True,
) -> dict[str, str]:
    """给新识别到的课程补上 DeskBox 颜色，并保存本地快照。

    - 已有颜色一律保留，不覆盖用户在设置页改过的值；
    - 新课程按传入顺序拿当前未被占用的颜色；
    - ``manual_color`` 不参与课程分配，避免和自建待办撞色；
    - ``cfg`` 是**完整配置**（含 ``deskbox`` 段），不是 deskbox 子字典。
    """
    db = cfg.setdefault("deskbox", {})
    raw_colors = dict(db.get("course_colors") or {})
    colors: dict[str, str] = {}
    for cid, value in raw_colors.items():
        color = normalize_color(value)
        if color:
            colors[str(cid)] = color

    raw_labels = dict(db.get("course_labels") or {})
    labels = {str(k): str(v) for k, v in raw_labels.items() if v}

    manual = manual_color_of(db)
    palette = [c for c in TODO_COLORS if c != manual] or list(TODO_COLORS)
    entries = _course_entries(courses)
    active = [cid for cid, _ in entries]
    used = {colors[cid] for cid in active if cid in colors}

    for cid, label in entries:
        labels[cid] = label
        if cid in colors:
            continue
        color = next((c for c in palette if c not in used), None)
        if color is None:
            # 课程数超过调色板时才循环复用；先保证每门课都有颜色。
            color = palette[len(used) % len(palette)]
        colors[cid] = color
        used.add(color)

    old_colors = {
        str(k): color
        for k, v in raw_colors.items()
        if (color := normalize_color(v))
    }
    old_labels = {str(k): str(v) for k, v in raw_labels.items() if v}
    if colors != old_colors or labels != old_labels:
        db["course_colors"] = colors
        db["course_labels"] = labels
        if persist:
            try:
                save_config(cfg)
            except Exception as exc:  # 配置只读 / 被占用时不打断同步
                log.warning("保存课程颜色失败: %s", exc)
    return colors


def _course_key(text: str) -> str:
    """标题开头的课程代码（CS5187 · xxx -> CS5187）；没有则返回空串。"""
    match = _COURSE_HEAD_RE.match((text or "").strip())
    return (match.group(1) + match.group(2)).upper() if match else ""


def _label_index(labels: dict[str, str]) -> list[tuple[str, str]]:
    """课程显示名 -> 可匹配的代码前缀，长的优先匹配。"""
    out: list[tuple[str, str]] = []
    for cid, label in labels.items():
        text = str(label or "").strip()
        if not text:
            continue
        key = _course_key(text)
        if key:
            out.append((key, str(cid)))
        head = re.split(r"\s+-\s+|·", text, maxsplit=1)[0].strip()
        head_key = re.sub(r"\s+", "", head).upper()
        if head_key and head_key != key:
            out.append((head_key, str(cid)))
    out.sort(key=lambda item: len(item[0]), reverse=True)
    return out


def _match_course(text: str, index: list[tuple[str, str]]) -> str | None:
    # 两侧都去掉所有空白再比，这样「MATH 101」这种带空格的课程代码也能对上；
    # 命中后必须紧跟分隔符（或正好结束），避免 CS51 把 CS5187 吃进来。
    value = re.sub(r"\s+", "", (text or "")).upper()
    for key, cid in index:
        if not key or not value.startswith(key):
            continue
        if len(value) == len(key) or value[len(key)] in "-·:：/|,，)]":
            return cid
    return None


def recolor_items(
    doc: dict,
    cfg: dict | None,
    course_names: dict[str, str] | None = None,
) -> int:
    """按课程重写待办的 ``colorMarker``，返回被改动的条目数。

    课程识别顺序：
    1. 备注里的 Canvas 链接 ``/courses/<课程id>/...``；
    2. 旧条目没有链接时，用标题开头的课程代码兜底；
    3. 都不匹配、也不像课程待办的，视为自建待办，使用 ``manual_color``。
    """
    db = cfg or {}
    colors: dict[str, str] = {}
    for cid, value in (db.get("course_colors") or {}).items():
        color = normalize_color(value)
        if color:
            colors[str(cid)] = color

    manual = manual_color_of(db)
    labels = {str(k): str(v) for k, v in (db.get("course_labels") or {}).items() if v}
    for cid, label in (course_names or {}).items():
        if label:
            labels[str(cid)] = str(label)
    index = _label_index(labels)

    changed = 0
    for item in doc.get("items") or []:
        text = item.get("text") or ""
        notes = item.get("notes") or ""
        match = _COURSE_URL_RE.search(notes)
        cid = match.group(1) if match else _match_course(text, index)

        if cid and cid in colors:
            new_color = colors[cid]
        elif match or _course_key(text):
            # Canvas 条目但当前没有对应颜色记录：保持原样，避免误标成自建。
            continue
        elif manual:
            new_color = manual
        else:
            continue

        if item.get("colorMarker") != new_color:
            item["colorMarker"] = new_color
            changed += 1
    return changed


def reorder_by_due(doc: dict) -> int:
    """按截止时间升序重排全部待办，并重写 ``sortOrder``。

    - 无截止时间 / 解析失败的排最后；
    - 同一截止时间按原 ``sortOrder`` 排，保持用户手动排好的相对顺序；
    - 返回被改动的条目数（位置变了或 sortOrder 与实际位置不一致），
      0 表示磁盘上的顺序已经是对的，不需要写回。
    """
    items = doc.get("items") or []
    if not items:
        return 0

    far_future = datetime.max.replace(tzinfo=UTC)
    old_pos = {id(item): i for i, item in enumerate(items)}

    def sort_key(pair: tuple[int, dict]) -> tuple:
        pos, item = pair
        due = _parse_due(item.get("dueDate"))
        try:
            order = int(item.get("sortOrder") or 0)
        except (TypeError, ValueError):
            order = 0
        return (due is None, due or far_future, order, pos)

    ordered = [item for _, item in sorted(enumerate(items), key=sort_key)]
    # 位置变了、或 sortOrder 与实际位置不一致（旧文件全是 0、或删过条目）都算“有改动”，
    # 否则副本和磁盘内容一样会被判成未变，DeskBox 永远不会按新顺序显示。
    changed = 0
    for i, item in enumerate(ordered):
        if old_pos[id(item)] != i or item.get("sortOrder") != i:
            changed += 1
        item["sortOrder"] = i
    doc["items"] = ordered
    return changed


def kind_of(item: Item, module_name: str) -> str:
    text = f"{module_name} {item.title}".lower()
    if "project" in text:
        return "project"
    if "tutorial" in text:
        return "tutorial"
    if item.kind in ("Assignment", "Quiz"):
        return "assignment"
    return "other"


_KIND_COLOR = {"assignment": "red", "project": "orange", "tutorial": "yellow", "other": "blue"}
_KIND_CN = {"assignment": "作业", "project": "项目", "tutorial": "练习", "other": "任务"}


def build_drafts(
    courses: dict[int, Course],
    items_by_course: dict[int, list[Item]],
    attachment_lookup: dict[tuple[int, int | None], Path] | None = None,
    only_future_days: int = 365,
    *,
    cfg: dict | None = None,
) -> TodoBuildResult:
    """把需要提交的项变成待办草稿。

    ``cfg`` 是完整配置（可选）。传入时会用 ``deskbox.course_colors`` 里的
    课程颜色；不传则退回按任务类型上色，方便离线测试。
    """
    result = TodoBuildResult()
    attachment_lookup = attachment_lookup or {}
    cutoff = now_local()
    db = (cfg or {}).get("deskbox") or {}
    course_colors: dict[str, str] = {}
    for cid, value in (db.get("course_colors") or {}).items():
        color = normalize_color(value)
        if color:
            course_colors[str(cid)] = color

    for cid, items in items_by_course.items():
        course = courses.get(cid)
        if course is None:
            continue
        for item in items:
            if item.kind not in ("Assignment", "Quiz"):
                continue
            due = _parse_due(item.due_at)
            if due is None:
                continue
            if due < cutoff:
                result.skipped_past += 1
                continue
            if (due - cutoff).days > only_future_days:
                continue
            k = kind_of(item, item.module_name)
            url = item.html_url or ""
            notes_parts = []
            desc = _strip_html(item.extra.get("description") or
                               (item.extra.get("content_details") or {}).get("description") or "")
            if desc:
                notes_parts.append(desc)
            if url:
                notes_parts.append(f"打开：{url}")
            attach = attachment_lookup.get((cid, item.file_id))
            result.drafts.append(
                TodoDraft(
                    item=item,
                    course=course,
                    text=f"{course.short} · {item.title}",
                    due=due,
                    kind=k,
                    url=url,
                    color=course_colors.get(str(cid)) or _KIND_COLOR[k],
                    notes="\n\n".join(notes_parts),
                    attachment=attach,
                )
            )
    result.drafts.sort(key=lambda d: d.due or now_local())
    return result


# ---------- DeskBox todo.json ----------

def load_doc(path: Path) -> dict:
    if not path.exists():
        return {"version": 3, "items": []}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.error("todo.json 解析失败: %s", path)
        return {"version": 3, "items": []}
    doc.setdefault("version", 3)
    doc.setdefault("items", [])
    return doc


def _existing_keys(doc: dict) -> set[str]:
    keys: set[str] = set()
    url_re = _canvas_url_re()
    for it in doc.get("items", []):
        notes = it.get("notes") or ""
        m = url_re.search(notes)
        if m:
            keys.add(m.group(0).rstrip(".,;"))
        keys.add(f"{it.get('text','')}|{it.get('dueDate')}")
    return keys


def _norm_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def _index_items(doc: dict) -> tuple[dict[str, dict], dict[str, dict]]:
    """(按唯一键索引, 按归一化文本索引) -> todo.json 里的条目。"""
    by_key: dict[str, dict] = {}
    by_text: dict[str, dict] = {}
    url_re = _canvas_url_re()
    for it in doc.get("items", []):
        notes = it.get("notes") or ""
        m = url_re.search(notes)
        if m:
            by_key.setdefault(m.group(0).rstrip(".,;"), it)
        by_key.setdefault(f"{it.get('text','')}|{it.get('dueDate')}", it)
        by_text.setdefault(_norm_text(it.get("text", "")), it)
    return by_key, by_text


def _same_due(a: str | None, b: str | None) -> bool:
    if a == b:
        return True
    if not a or not b:
        return False
    da, db = _parse_due(a), _parse_due(b)
    if da is None or db is None:
        return False
    return abs((da - db).total_seconds()) < 60


# ---------- 已完成记录（防止“完成 -> 删除 -> 又冒出来”） ----------

def load_done_index() -> dict[str, dict]:
    try:
        return dict(load_state().get("todo_done") or {})
    except Exception as exc:
        log.warning("读取已完成待办记录失败: %s", exc)
        return {}


def save_done_index(index: dict[str, dict]) -> None:
    try:
        state = load_state()
        state["todo_done"] = index
        save_state(state)
    except Exception as exc:
        log.warning("保存已完成待办记录失败: %s", exc)


def attachment_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tiff", ".tif", ".heic", ".heif"):
        return "image"
    if ext == ".pdf":
        return "pdf"
    return "file"


def _make_item(draft: TodoDraft, sort_order: int, offset_minutes: int) -> dict:
    now = datetime.now(UTC).isoformat(timespec="microseconds")
    attachments = []
    if draft.attachment and draft.attachment.exists():
        attachments.append(
            {
                "id": uuid.uuid4().hex[:32],
                "filePath": str(draft.attachment),
                "displayName": draft.attachment.name,
                "type": attachment_type(draft.attachment),
                "storageMode": "linked",
                "addedAt": now,
            }
        )
    return {
        "id": uuid.uuid4().hex[:32],
        "text": draft.text,
        "isCompleted": False,
        "isImportant": False,
        "colorMarker": draft.color,
        "dueDate": _fmt_due(draft.due) if draft.due else None,
        "recurrence": None,
        "steps": [],
        "notes": draft.notes or None,
        "attachments": attachments,
        "completedAt": None,
        "reminderLastNotifiedAt": None,
        "reminderDismissedForDueDate": None,
        "reminderOffsetMinutes": offset_minutes,
        "snoozedUntil": None,
        "snoozeLastNotifiedAt": None,
        "recurrenceSeriesId": None,
        "generatedNextItemId": None,
        "sortOrder": sort_order,
        "createdAt": now,
        "updatedAt": now,
    }


def merge(
    doc: dict,
    drafts: list[TodoDraft],
    offset_minutes: int = 1440,
    *,
    done_index: dict[str, dict] | None = None,
    stats: dict | None = None,
) -> tuple[dict, list[TodoDraft]]:
    """把草稿合并进 todo.json。

    **已完成的作业如何处理**（四条规则，按顺序判定）：

    1. 清单里已有同 key（Canvas 链接，或 文本+截止时间）的条目 -> 不重复添加；
       若该条目已完成，在 ``done_index`` 里留一条记录，并把草稿标记为 ``completed``。
    2. ``done_index`` 有记录、截止时间没变 -> 不再添加（覆盖“完成了、又被删掉”的情况）。
    3. ``done_index`` 有记录、但截止时间与 Canvas 不一致 -> 视为新截止，**重新添加**，
       状态 ``reopened``，并刷新记录。
    4. key 对不上时用归一化文本兜底匹配，规则同上。

    ``done_index`` / ``stats`` 均为关键字参数，便于离线测试直接调用而不写盘。
    """
    stats = stats if stats is not None else {}
    done = done_index if done_index is not None else {}
    by_key, by_text = _index_items(doc)
    added: list[TodoDraft] = []
    order = max([it.get("sortOrder", 0) for it in doc.get("items", [])] + [0]) + 1
    counts = {"added": 0, "existing": 0, "completed": 0, "reopened": 0}
    dirty = False

    for d in drafts:
        due_text = _fmt_due(d.due) if d.due else None

        item = by_key.get(d.key)
        if item is None and d.text:
            item = by_text.get(_norm_text(d.text))

        if item is not None:
            d.existing = True
            if item.get("isCompleted"):
                counts["completed"] += 1
                d.status = "completed"
                rec = {"dueDate": due_text, "completedAt": item.get("completedAt")}
                if done.get(d.key) != rec:
                    done[d.key] = rec
                    dirty = True
            else:
                counts["existing"] += 1
            continue

        record = done.get(d.key)
        if record is not None:
            if _same_due(record.get("dueDate"), due_text):
                counts["completed"] += 1
                d.status = "completed"
                d.existing = True
                continue
            counts["reopened"] += 1
            d.status = "reopened"
            done[d.key] = {"dueDate": due_text, "completedAt": None}
            dirty = True

        doc["items"].append(_make_item(d, order, offset_minutes))
        by_key[d.key] = doc["items"][-1]
        added.append(d)
        counts["added"] += 1
        order += 1

    stats.update(counts)
    stats["done_dirty"] = dirty
    return doc, added


def preview_status(doc_path: Path, drafts: list[TodoDraft]) -> dict[str, str]:
    """**只读**地判断每条草稿在 DeskBox 里的状态。

    返回 {draft.key: completed / existing / reopened}，供预览与同步前显示，
    不写任何文件。
    """
    if not doc_path.exists():
        return {}
    doc = load_doc(doc_path)
    by_key, by_text = _index_items(doc)
    done = load_done_index()
    out: dict[str, str] = {}
    for d in drafts:
        item = by_key.get(d.key)
        if item is None and d.text:
            item = by_text.get(_norm_text(d.text))
        if item is not None:
            out[d.key] = "completed" if item.get("isCompleted") else "existing"
            continue
        rec = done.get(d.key)
        if rec is None:
            continue
        due_text = _fmt_due(d.due) if d.due else None
        out[d.key] = "completed" if _same_due(rec.get("dueDate"), due_text) else "reopened"
    return out


def dedupe_completed(doc: dict) -> list[dict]:
    """删掉「已完成条目」的未完成重复项（同文本 + 同截止时间）。

    只在这种情况下才删：一组里**至少有一条已完成**，才说明同名同截止的那条未完成条目
    是重复写入的。截止时间不同的同名条目（比如用户自己的每周任务）完全不动。
    """
    groups: dict[str, list[dict]] = {}
    for it in doc.get("items", []):
        key = f"{_norm_text(it.get('text', ''))}|{it.get('dueDate')}"
        groups.setdefault(key, []).append(it)

    doomed: set[str] = set()
    removed: list[dict] = []
    for items in groups.values():
        if len(items) < 2:
            continue
        if not any(i.get("isCompleted") for i in items):
            continue
        for i in items:
            if not i.get("isCompleted") and i.get("id"):
                removed.append(i)
                doomed.add(i["id"])
    if removed:
        doc["items"] = [i for i in doc["items"] if i.get("id") not in doomed]
    return removed


def fix_paths(doc: dict) -> int:
    """修复缺少 Classes 一层的坏附件路径。"""
    fixed = 0
    for it in doc.get("items", []):
        for att in it.get("attachments", []):
            p = att.get("filePath") or ""
            if not p or os.path.exists(p):
                continue
            m = re.search(r"^(.*Courses)\\(.*)$", p, re.IGNORECASE)
            if not m:
                continue
            cand = f"{m.group(1)}\\Classes\\{m.group(2)}"
            if os.path.exists(cand):
                att["filePath"] = cand
                fixed += 1
                log.info("修复附件路径: %s -> %s", p, cand)
    return fixed


def write_copy(doc: dict, todo_path: Path) -> Path:
    """把合并结果写到副本文件（不碰正式文件）。"""
    tmp = todo_path.with_name("todo.json.new")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return tmp


def prepare(doc_path: Path, drafts: list[TodoDraft], cfg: dict) -> tuple[Path, list[TodoDraft], dict]:
    """读取 -> 去重 -> 合并 -> 上色/排序 -> 写副本，返回 (副本路径, 新增草稿, 统计)。"""
    doc = load_doc(doc_path)
    if cfg.get("fix_attachment_paths", True):
        fix_paths(doc)

    # 先清掉「已完成条目的未完成重复项」，再合并，避免重复项被当成已存在
    deduped = dedupe_completed(doc) if cfg.get("remove_duplicates", True) else []

    done_index = load_done_index()
    mstats: dict = {}
    doc, added = merge(
        doc, drafts, int(cfg.get("reminder_offset_minutes", 1440)),
        done_index=done_index, stats=mstats,
    )
    if mstats.get("done_dirty"):
        save_done_index(done_index)

    # 草稿里的课程名用于旧条目（没有 Canvas 链接）的兜底识别；完整映射在
    # resolve_courses() 识别课程时已经写进 cfg["course_colors"]。
    course_names: dict[str, str] = {}
    for draft in drafts:
        cid = getattr(draft.course, "id", 0)
        if cid:
            course_names.setdefault(str(cid), draft.course.short or str(cid))
    recolored = recolor_items(doc, cfg, course_names)
    resorted = reorder_by_due(doc) if cfg.get("sort_by_due", True) else 0

    tmp = write_copy(doc, doc_path)
    stats = {
        "total_items": len(doc["items"]),
        "added": len(added),
        "existing": mstats.get("existing", 0),
        "completed": mstats.get("completed", 0),
        "reopened": mstats.get("reopened", 0),
        "deduped": len(deduped),
        "recolored": recolored,
        "resorted": resorted,
        "fixed_paths": True,
        "changed": bool(added) or bool(deduped) or recolored > 0 or resorted > 0,
    }
    if deduped:
        log.info("已移除 %d 条未完成的重复待办", len(deduped))
    if stats["completed"]:
        log.info("%d 项已完成，未重复添加", stats["completed"])
    if stats["reopened"]:
        log.info("%d 项截止时间已更新，重新添加为新待办", stats["reopened"])
    if recolored:
        log.info("已更新 %d 条待办颜色", recolored)
    if resorted:
        log.info("已按截止时间重排 %d 条待办", resorted)
    return tmp, added, stats


# ---------- DeskBox 进程 ----------

# CREATE_NO_WINDOW：GUI 跑在无控制台的 pythonw 里，裸调 taskkill / Popen(DeskBox)
# 会让 Windows 给每个子进程新开一个可见控制台，子进程一结束窗口立刻销毁——这就是
# 用户看到的「黑框弹出又自动消失」。
CREATE_NO_WINDOW = 0x08000000


class _PROCESSENTRY32W(ctypes.Structure):
    """Process32FirstW / Process32NextW 的条目（tlist 同款结构）。

    th32DefaultHeapID 在 Win64 上是 ULONG_PTR，用 c_size_t 让 ctypes 按平台
    位宽对齐，否则 64 位下从该字段起会整体错位。
    """

    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
_k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_k32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W))
_k32.Process32FirstW.restype = wintypes.BOOL
_k32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W))
_k32.Process32NextW.restype = wintypes.BOOL
_k32.CloseHandle.argtypes = (wintypes.HANDLE,)
_k32.CloseHandle.restype = wintypes.BOOL


def _deskbox_pids(name: str = "DeskBox") -> list[int]:
    """Toolhelp32 进程快照查同名进程的 pid；失败返回 []（与旧 tasklist 版一致）。

    原来每次查询都 spawn 一个 tasklist（实测单次约 245ms CPU）：GUI 每 4 秒轮
    询一次、stop() 关停循环每 0.15 秒一次——GUI 常开一天就是上万次进程创建。
    快照在进程内完成，实测中位数约 10ms（约 23 倍），零子进程，顺带从根本上
    消除了子进程控制台黑框问题。
    """
    target = f"{name}.exe".lower()
    h = _k32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if h in (None, 0, -1, 0xFFFFFFFFFFFFFFFF):  # INVALID_HANDLE_VALUE 的位宽两种形态
        log.warning("CreateToolhelp32Snapshot 失败: %d", ctypes.get_last_error())
        return []
    pids: list[int] = []
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        ok = _k32.Process32FirstW(h, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() == target:
                pids.append(int(entry.th32ProcessID))
            ok = _k32.Process32NextW(h, ctypes.byref(entry))
    finally:
        _k32.CloseHandle(h)
    return pids


def is_running() -> bool:
    return bool(_deskbox_pids())


def stop(timeout_s: float = 2.0) -> bool:
    """优雅关闭，超时后强杀。返回是否已停止。"""
    pids = _deskbox_pids()
    if not pids:
        return True
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid)], capture_output=True, timeout=15,
                       check=False, creationflags=CREATE_NO_WINDOW)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not _deskbox_pids():
            return True
        time.sleep(0.15)
    for pid in _deskbox_pids():
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True,
                       timeout=15, check=False, creationflags=CREATE_NO_WINDOW)
    time.sleep(0.2)
    return not _deskbox_pids()


def start(exe: Path) -> None:
    if not exe.exists():
        log.warning("DeskBox 可执行文件不存在: %s", exe)
        return
    subprocess.Popen([str(exe)], cwd=str(exe.parent), creationflags=CREATE_NO_WINDOW)
    log.info("已启动 DeskBox: %s", exe)


def apply_copy(doc_path: Path, tmp: Path) -> None:
    """把副本原子替换到正式文件（DeskBox 已关闭）。"""
    if not tmp.exists():
        raise FileNotFoundError(tmp)
    if doc_path.exists():
        backup = doc_path.with_name("todo.json.pre-canvas")
        try:
            shutil.copy2(doc_path, backup)
        except OSError as exc:
            log.warning("备份失败: %s", exc)
    os.replace(tmp, doc_path)


def quick_swap(
    doc_path: Path,
    tmp: Path,
    exe: Path,
    prepared_count: int,
    drafts: list[TodoDraft],
    offset_minutes: int = 1440,
    progress: Callable[[str], None] = lambda m: None,
    *,
    cfg: dict | None = None,
) -> dict:
    """关闭 -> 替换 -> 重启，尽量缩短 DeskBox 停机时间。

    ``prepared_count`` 是生成副本时的条目数；如果关闭期间 todo.json 又被改动，
    会以磁盘最新内容重新合并一次（纯内存操作，毫秒级），避免覆盖用户手工新增的待办。
    ``cfg`` 是 ``config["deskbox"]``，用于重新合并后的上色与排序；不传时仍按
    截止时间排序，但不改颜色。
    """
    stat = {"stopped": False, "applied": False, "started": False, "remerged": False}
    progress("正在关闭 DeskBox…")
    stat["stopped"] = stop()
    if not stat["stopped"]:
        raise RuntimeError("DeskBox 未能关闭，已放弃写入（可稍后重试）")

    progress("写入待办…")
    try:
        current = load_doc(doc_path)
        if len(current.get("items", [])) != prepared_count:
            dedupe_completed(current)
            current, _ = merge(
                current, drafts, offset_minutes,
                done_index=load_done_index(),
            )
            course_names: dict[str, str] = {}
            for draft in drafts:
                cid = getattr(draft.course, "id", 0)
                if cid:
                    course_names.setdefault(str(cid), draft.course.short or str(cid))
            recolor_items(current, cfg, course_names)
            if (cfg or {}).get("sort_by_due", True):
                reorder_by_due(current)
            tmp = write_copy(current, doc_path)
            stat["remerged"] = True
            log.info("关闭期间 todo.json 有变化，已重新合并（%s 条）", len(current["items"]))
    except Exception as exc:
        log.warning("重新合并失败，沿用副本: %s", exc)

    apply_copy(doc_path, tmp)
    stat["applied"] = True
    progress("启动 DeskBox…")
    start(exe)
    stat["started"] = True
    return stat


# ---------- 后台策略：排队 ----------

def enqueue_pending(drafts: list[TodoDraft]) -> int:
    now = datetime.now(UTC).isoformat(timespec="microseconds")
    items = [
        {
            "text": d.text,
            "dueDate": _fmt_due(d.due) if d.due else None,
            "colorMarker": d.color,
            "notes": d.notes or None,
            "url": d.url,
            "course": d.course.short,
            "kind": d.kind,
            "attachment": str(d.attachment) if d.attachment else None,
            "createdAt": now,
        }
        for d in drafts
    ]
    existing = []
    if PENDING_PATH.exists():
        try:
            existing = json.loads(PENDING_PATH.read_text(encoding="utf-8"))
        except Exception:
            existing = []
    have = {i.get("url") for i in existing}
    existing.extend(i for i in items if i.get("url") not in have)
    PENDING_PATH.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    return len([i for i in items if i.get("url") not in have])


def pending_drafts() -> list[dict]:
    if not PENDING_PATH.exists():
        return []
    try:
        return json.loads(PENDING_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def clear_pending() -> None:
    if PENDING_PATH.exists():
        PENDING_PATH.unlink()


def drafts_from_pending(rows: list[dict]) -> list[TodoDraft]:
    out = []
    for r in rows:
        due = _parse_due(r.get("dueDate")) if r.get("dueDate") else None
        out.append(
            TodoDraft(
                item=Item(course_id=0, module_name="", title=r.get("text", ""), kind="Assignment"),
                course=Course(id=0, name=r.get("course", ""), code=r.get("course", "")),
                text=r.get("text", ""),
                due=due,
                kind=r.get("kind", "other"),
                url=r.get("url") or "",
                color=r.get("colorMarker") or "blue",
                notes=r.get("notes") or "",
                attachment=Path(r["attachment"]) if r.get("attachment") else None,
            )
        )
    return out
