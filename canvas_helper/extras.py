"""辅助功能：通知聚合、成绩追踪、日历导出、每周摘要。"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import quote
from zoneinfo import ZoneInfo

from .canvas_api import CanvasAPI, Course, Item
from .deskbox import TodoDraft, _parse_due, _strip_html
from .paths import PROJECT_DIR, get_logger, load_state, now_hk, save_state

log = get_logger()
HK = ZoneInfo("Asia/Hong_Kong")


# ---------------- 通知 / 公告 ----------------

@dataclass
class Notice:
    course: str
    title: str
    body: str
    posted_at: str
    url: str
    kind: str  # announcement / conversation / activity

    @property
    def key(self) -> str:
        return self.url or f"{self.course}|{self.title}|{self.posted_at}"


def collect_notices(
    api: CanvasAPI, courses: list[Course], progress: Callable[[str], None] = lambda m: None
) -> list[Notice]:
    out: list[Notice] = []
    for c in courses:
        progress(f"读取公告：{c.short}")
        try:
            rows = api.announcements(c.id)
        except Exception as exc:
            log.warning("%s 公告读取失败: %s", c.short, exc)
            continue
        for r in rows:
            out.append(
                Notice(
                    course=c.short,
                    title=html.unescape(r.get("title") or "(无标题)"),
                    body=_strip_html(r.get("message") or "", 400),
                    posted_at=(r.get("posted_at") or r.get("created_at") or "")[:19].replace("T", " "),
                    url=r.get("html_url") or f"https://canvas.example.edu/courses/{c.id}",
                    kind="announcement",
                )
            )
    try:
        for r in api.activity_stream():
            if r.get("type") not in ("Announcement", "Conversation", "DiscussionTopic"):
                continue
            out.append(
                Notice(
                    course=(r.get("context_name") or "").strip() or "Canvas",
                    title=html.unescape(r.get("title") or r.get("message") or "(无标题)")[:80],
                    body=_strip_html(r.get("message") or "", 300),
                    posted_at=(r.get("created_at") or "")[:19].replace("T", " "),
                    url=r.get("html_url") or r.get("url") or "",
                    kind="activity",
                )
            )
    except Exception as exc:
        log.warning("动态读取失败: %s", exc)

    out.sort(key=lambda n: n.posted_at or "", reverse=True)
    return out


def mark_notices_read(notices: list[Notice]) -> list[Notice]:
    """返回未读（相对上次记录）的通知。"""
    state = load_state()
    seen = set(state.get("notices", {}).get("seen", []))
    unread = [n for n in notices if n.key not in seen]
    state.setdefault("notices", {})["seen"] = sorted({n.key for n in notices})
    state["notices"]["at"] = now_hk().isoformat(timespec="seconds")
    save_state(state)
    return unread


# ---------------- 成绩 ----------------

@dataclass
class GradeRow:
    name: str
    score: float | None
    possible: float | None
    graded: bool

    @property
    def label(self) -> str:
        if self.score is None:
            return f"{self.name}: 未出分 / {self.possible if self.possible is not None else '?'}"
        return f"{self.name}: {self.score:g} / {self.possible if self.possible is not None else '?'}"


def collect_grades(api: CanvasAPI, courses: list[Course]) -> dict[int, dict]:
    """返回 {course_id: {"rows": [GradeRow], "current": float|None}}"""
    out: dict[int, dict] = {}
    enrollments = api.scores()
    current_by_course: dict[int, float | None] = {}
    for e in enrollments:
        cid = (e.get("course") or {}).get("id")
        grades = e.get("grades") or {}
        if cid and grades.get("current_score") is not None:
            try:
                current_by_course[int(cid)] = float(grades["current_score"])
            except (TypeError, ValueError):
                current_by_course[int(cid)] = None

    for c in courses:
        rows: list[GradeRow] = []
        try:
            assignments = api.assignments(c.id)
        except Exception as exc:
            log.warning("%s 成绩读取失败: %s", c.short, exc)
            assignments = []
        for a in assignments:
            sub = a.get("submission") or {}
            score = sub.get("score")
            if score is None and not sub.get("graded_at"):
                continue
            rows.append(
                GradeRow(
                    name=a.get("name") or "?",
                    score=float(score) if score is not None else None,
                    possible=float(a["points_possible"]) if a.get("points_possible") else None,
                    graded=bool(sub.get("graded_at")),
                )
            )
        out[c.id] = {"rows": rows, "current": current_by_course.get(c.id)}
    return out


GRADE_HEADER_RE = re.compile(r"^-{3}\s*Canvas 成绩.*?-{3}\s*$", re.M | re.S)


def update_grade_file(course_dir: Path, course: Course, data: dict) -> Path | None:
    """把成绩追加/刷新到 GradeNeed.txt。"""
    if not course_dir.is_dir():
        return None
    path = course_dir / "GradeNeed.txt"
    block_lines = [f"--- Canvas 成绩（自动更新 {now_hk():%Y-%m-%d %H:%M}）---"]
    if data.get("current") is not None:
        block_lines.append(f"当前总评分: {data['current'] * 100:.1f}%" if data["current"] <= 1
                           else f"当前总评分: {data['current']:.1f}%")
    rows: list[GradeRow] = data.get("rows") or []
    if not rows:
        block_lines.append("（暂无已出分项目）")
    else:
        for r in rows:
            block_lines.append("· " + r.label)
    block = "\n".join(block_lines) + "\n"

    original = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    if GRADE_HEADER_RE.search(original):
        new = GRADE_HEADER_RE.sub(lambda _m: block.rstrip("\n"), original, count=1)
    else:
        new = original.rstrip("\n")
        if new:
            new += "\n\n"
        new += block
    path.write_text(new, encoding="utf-8")
    return path


# ---------------- 日历 (.ics) ----------------

def build_ics(drafts: list[TodoDraft], calendar_name: str = "Canvas 截止日程") -> str:
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//CanvasHelper//Canvas Deadline//CN",
        f"X-WR-CALNAME:{esc(calendar_name)}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
    ]
    for i, d in enumerate(drafts, 1):
        if not d.due:
            continue
        uid = f"canvas{i}-{abs(hash(d.key)) % 10**9}@canvashelper"
        dtstart = d.due.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dtend = (d.due + timedelta(hours=1)).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{dtstart}",
            f"DTEND:{dtend}",
            f"SUMMARY:{esc(d.text)}",
            f"DESCRIPTION:{esc(d.notes or '')}",
            f"URL:{d.url}",
            "BEGIN:VALARM",
            "TRIGGER:-P1D",
            "ACTION:DISPLAY",
            f"DESCRIPTION:{esc('明天截止：' + d.text)}",
            "END:VALARM",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def export_ics(drafts: list[TodoDraft], path: Path | None = None) -> Path:
    path = path or (PROJECT_DIR / "canvas-deadlines.ics")
    path.write_text(build_ics(drafts), encoding="utf-8", newline="")
    return path


# ---------------- 每周摘要 ----------------

def weekly_report(
    course_dir: Path,
    course: Course,
    new_files: list[Path],
    notices: list[Notice],
    drafts: list[TodoDraft],
    grades: dict | None = None,
) -> Path:
    now = now_hk()
    iso = now.isocalendar()
    path = course_dir / f"本周摘要-{now:%Y}-W{iso.week:02d}.md"
    lines = [
        f"# {course.short} 本周摘要（{now:%Y-%m-%d} · W{iso.week:02d}）",
        "",
        "## 新增课件 / 文件",
    ]
    lines += [f"- {p.relative_to(course_dir)}" for p in new_files] or ["- 无"]
    lines += ["", "## 公告（最近）"]
    mine = [n for n in notices if n.course == course.short]
    lines += [f"- **{n.posted_at[:10]}** {n.title}  <{n.url}>" for n in mine[:10]] or ["- 无"]
    lines += ["", "## 本周截止"]
    week_end = now + timedelta(days=7)
    week_items = [d for d in drafts if d.due and now <= d.due <= week_end]
    lines += [
        f"- {d.due:%m-%d %H:%M} {d.text} <{d.url}>" for d in week_items
    ] or ["- 无"]
    if grades and grades.get("rows"):
        lines += ["", "## 已出分项目"]
        lines += [f"- {r.label}" for r in grades["rows"][:20]]
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
