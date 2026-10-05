"""文件同步：先生成计划（dry-run），再执行下载与归档。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from .canvas_api import CanvasAPI, Course, Item
from .classify import build_local_index, ensure_parent, plan_item, sanitize, unique_path
from .paths import get_logger, load_state, now_hk, save_state

log = get_logger()
ProgressCb = Callable[[str], None]


@dataclass
class PlannedFile:
    item: Item
    target: Path
    action: str  # download / exists / rename / page / skip
    size: int | None = None
    note: str = ""


@dataclass
class CoursePlan:
    course: Course
    folder: Path
    files: list[PlannedFile] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def downloads(self) -> list[PlannedFile]:
        return [f for f in self.files if f.action in ("download", "rename", "page")]

    @property
    def up_to_date(self) -> list[PlannedFile]:
        return [f for f in self.files if f.action == "exists"]


def _state_key(course_id: int, item: Item) -> str:
    ident = item.file_id or item.title
    return f"file:{course_id}:{ident}"


def _page_html(course: Course, item: Item, body: str) -> str:
    return (
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        f"<title>{item.title}</title>"
        "<style>body{font-family:'Segoe UI',sans-serif;max-width:900px;margin:2em auto;"
        "line-height:1.6;color:#222} a{color:#1a73e8}</style></head><body>"
        f"<p style=\"color:#888;font-size:.85em\">CanvasHelper 自动保存 · "
        f"{course.name} · {item.module_name} · "
        f"<a href=\"{item.html_url}\">打开原文</a></p>"
        f"<h1>{item.title}</h1>{body}</body></html>"
    )


def build_plan(course: Course, folder: Path, items: list[Item], state: dict) -> CoursePlan:
    plan = CoursePlan(course=course, folder=folder)
    local = build_local_index(folder)  # 索引：归一化名 -> [(大小, 路径)]

    for item in items:
        if item.kind == "File" and not (item.file_url or item.file_id):
            continue
        if item.kind not in ("File", "Page"):
            continue

        key = _state_key(course.id, item)
        recorded = state.get("files", {}).get(key)

        if item.kind == "Page":
            body = (item.extra.get("content_details") or {}).get("body")
            if not body:
                continue
            target = plan_item(folder, item)
            if not target.name.lower().endswith((".html", ".htm")):
                target = target.with_suffix(".html")
            if target.exists() and recorded and recorded.get("kind") == "page":
                plan.files.append(PlannedFile(item, target, "exists", note="页面已保存"))
            else:
                plan.files.append(PlannedFile(item, target, "page", note="网页内容"))
            continue

        target = plan_item(folder, item)

        size = item.file_size
        if recorded and recorded.get("size") == size and Path(recorded.get("path", "")).exists():
            plan.files.append(PlannedFile(item, Path(recorded["path"]), "exists", size, "已是最新"))
            continue

        # 同名不同内容 -> 内容可能放在别的分类里，或上次已存成 x (1).pptx
        hit = local.find(item.display_name, size)
        same_content = hit is not None

        if target.exists():
            actual = target.stat().st_size
            if size is None or actual == size:
                plan.files.append(PlannedFile(item, target, "exists", actual, "本地已存在"))
                continue
            if same_content and hit != target:
                plan.files.append(
                    PlannedFile(item, hit, "exists", size, "本地已有同内容文件"))
                continue
            target = unique_path(target)
            plan.files.append(PlannedFile(item, target, "rename", size, "重名已改名"))
            continue

        if same_content:
            plan.files.append(
                PlannedFile(item, hit, "exists", size, "本地已有同内容文件"))
            continue

        plan.files.append(PlannedFile(item, target, "download", size))
    return plan


def execute_plan(
    plans: list[CoursePlan],
    api: CanvasAPI,
    state: dict,
    progress: ProgressCb = lambda m: None,
    stop_flag: Callable[[], bool] | None = None,
) -> dict:
    """执行下载，返回统计。"""
    stats = {"downloaded": 0, "pages": 0, "exists": 0, "bytes": 0, "errors": 0}
    dirty = False

    for plan in plans:
        for pf in plan.files:
            if stop_flag and stop_flag():
                progress("已中止")
                _flush_state(state)
                return stats
            if pf.action in ("exists",):
                stats["exists"] += 1
                continue
            try:
                ensure_parent(pf.target)
                if pf.action == "page":
                    body = (pf.item.extra.get("content_details") or {}).get("body") or ""
                    pf.target.write_text(
                        _page_html(plan.course, pf.item, body), encoding="utf-8"
                    )
                    stats["pages"] += 1
                    _record(state, plan.course.id, pf.item, pf.target, kind="page")
                    dirty = True
                else:
                    progress(f"下载 {plan.course.short} · {pf.target.name}")
                    data, header_name, _ctype = api.download(pf.item.file_url)
                    if header_name and not pf.target.suffix:
                        pf.target = pf.target.with_name(sanitize(header_name))
                    pf.target.write_bytes(data)
                    stats["downloaded"] += 1
                    stats["bytes"] += len(data)
                    _record(state, plan.course.id, pf.item, pf.target, size=len(data))
                    dirty = True
            except Exception as exc:
                stats["errors"] += 1
                plan.errors.append(f"{pf.target.name}: {exc}")
                log.error("下载失败 %s: %s", pf.target.name, exc)

    if dirty:
        _flush_state(state)
    return stats


def _record(state: dict, course_id: int, item: Item, path: Path, size: int | None = None, kind: str = "file"):
    state.setdefault("files", {})[_state_key(course_id, item)] = {
        "path": str(path),
        "size": size,
        "kind": kind,
        "title": item.display_name,
        "course_id": course_id,
        "synced_at": now_hk().isoformat(timespec="seconds"),
    }


def _flush_state(state: dict) -> None:
    from .paths import save_state as _save

    _save(state)


def describe(plans: list[CoursePlan]) -> str:
    """把计划渲染成文本，供 GUI 预览。"""
    lines: list[str] = []
    for plan in plans:
        lines.append(f"\n【{plan.course.short}】{plan.course.name}  ->  {plan.folder}")
        if not plan.files:
            lines.append("   （没有可同步内容）")
            continue
        for pf in plan.files:
            mark = {"download": "＋", "rename": "＋*", "page": "＋◇", "exists": "＝"}[pf.action]
            try:
                shown = pf.target.relative_to(plan.folder)
            except ValueError:
                shown = pf.target
            size_txt = f"  ({_fmt_size(pf.size)})" if (pf.size and pf.action != "exists") else ""
            lines.append(
                f"   {mark} {shown}  [{_kind_label(pf)}]{size_txt}"
                f"{('  ' + pf.note) if pf.note else ''}"
            )
    total_dl = sum(len(p.downloads) for p in plans)
    total_ok = sum(len(p.up_to_date) for p in plans)
    total_bytes = sum(pf.size or 0 for p in plans for pf in p.downloads)
    lines.append(
        f"\n合计：新增/更新 {total_dl} 项（约 {_fmt_size(total_bytes)}），"
        f"已存在 {total_ok} 项"
    )
    return "\n".join(lines)


def _fmt_size(n: int | None) -> str:
    if not n:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _kind_label(pf: PlannedFile) -> str:
    k = pf.item.kind
    return {"File": "文件", "Page": "网页"}.get(k, k)
