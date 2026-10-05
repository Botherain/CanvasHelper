"""DeskBox 待办集成：解析 Canvas 截止时间 -> 写入 todo.json（副本预写 + 快速关/换/启）。"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from .canvas_api import Course, Item
from .paths import PROJECT_DIR, get_logger, load_state, now_hk, save_state

log = get_logger()
HK = ZoneInfo("Asia/Hong_Kong")

PENDING_PATH = PROJECT_DIR / "pending_todos.json"
CANVAS_URL_RE = re.compile(r"https?://canvas\.example\.edu[^\s)\]>]*", re.I)
HTML_RE = re.compile(r"<[^>]+>")


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
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(HK)


def _fmt_due(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


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
) -> TodoBuildResult:
    """把需要提交的项变成待办草稿。"""
    result = TodoBuildResult()
    attachment_lookup = attachment_lookup or {}
    cutoff = now_hk()

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
                    color=_KIND_COLOR[k],
                    notes="\n\n".join(notes_parts),
                    attachment=attach,
                )
            )
    result.drafts.sort(key=lambda d: d.due or now_hk())
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
    for it in doc.get("items", []):
        notes = it.get("notes") or ""
        m = CANVAS_URL_RE.search(notes)
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
    for it in doc.get("items", []):
        notes = it.get("notes") or ""
        m = CANVAS_URL_RE.search(notes)
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
    except Exception as exc:  # noqa: BLE001
        log.warning("读取已完成待办记录失败: %s", exc)
        return {}


def save_done_index(index: dict[str, dict]) -> None:
    try:
        state = load_state()
        state["todo_done"] = index
        save_state(state)
    except Exception as exc:  # noqa: BLE001
        log.warning("保存已完成待办记录失败: %s", exc)


def attachment_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tiff", ".tif", ".heic", ".heif"):
        return "image"
    if ext == ".pdf":
        return "pdf"
    return "file"


def _make_item(draft: TodoDraft, sort_order: int, offset_minutes: int) -> dict:
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
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
            m = re.search(r"^(.*Courses)\\(.*)$", p, re.I)
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
    """读取 -> 去重 -> 合并 -> 写副本，返回 (副本路径, 新增草稿, 统计)。"""
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

    tmp = write_copy(doc, doc_path)
    stats = {
        "total_items": len(doc["items"]),
        "added": len(added),
        "existing": mstats.get("existing", 0),
        "completed": mstats.get("completed", 0),
        "reopened": mstats.get("reopened", 0),
        "deduped": len(deduped),
        "fixed_paths": True,
        "changed": bool(added) or bool(deduped),
    }
    if deduped:
        log.info("已移除 %d 条未完成的重复待办", len(deduped))
    if stats["completed"]:
        log.info("%d 项已完成，未重复添加", stats["completed"])
    if stats["reopened"]:
        log.info("%d 项截止时间已更新，重新添加为新待办", stats["reopened"])
    return tmp, added, stats


# ---------- DeskBox 进程 ----------

def _deskbox_pids(name: str = "DeskBox") -> list[int]:
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {name}.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=15,
            encoding="locale", errors="replace",
        ).stdout
    except Exception as exc:
        log.warning("tasklist 失败: %s", exc)
        return []
    pids = []
    for line in out.splitlines():
        parts = line.replace('"', "").split(",")
        if len(parts) >= 2 and parts[0].lower().startswith(name.lower()):
            try:
                pids.append(int(parts[1]))
            except ValueError:
                pass
    return pids


def is_running() -> bool:
    return bool(_deskbox_pids())


def stop(timeout_s: float = 2.0) -> bool:
    """优雅关闭，超时后强杀。返回是否已停止。"""
    pids = _deskbox_pids()
    if not pids:
        return True
    for pid in pids:
        subprocess.run(["taskkill", "/PID", str(pid)], capture_output=True, timeout=15)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not _deskbox_pids():
            return True
        time.sleep(0.15)
    for pid in _deskbox_pids():
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=15)
    time.sleep(0.2)
    return not _deskbox_pids()


def start(exe: Path) -> None:
    if not exe.exists():
        log.warning("DeskBox 可执行文件不存在: %s", exe)
        return
    subprocess.Popen([str(exe)], cwd=str(exe.parent))
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
) -> dict:
    """关闭 -> 替换 -> 重启，尽量缩短 DeskBox 停机时间。

    ``prepared_count`` 是生成副本时的条目数；如果关闭期间 todo.json 又被改动，
    会以磁盘最新内容重新合并一次（纯内存操作，毫秒级），避免覆盖用户手工新增的待办。
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
    items = []
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    for d in drafts:
        items.append(
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
        )
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
