"""讲义整理：把分类目录根部散落的章文件移进各自的章节目录（先预览再移动）。

只处理**讲义分类的第一层文件**：`Lecture3a.pdf` 会被算出章节目录并移动，
`data.zip` / `jupyter-shortcuts.pdf` 这类识别不到章节号的散件保持原样。
移动不删除任何文件——目标已存在同内容就跳过，同名不同大小自动加 `(1)`，
与同步的「只增不删」口径一致；移动后同步回写 `state.json` 里的路径，
免得下次同步又按旧路径找文件。
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .classify import CategoryDirs, lecture_dir, unique_path
from .paths import get_logger, load_state, now_local, save_state

log = get_logger()


@dataclass
class Move:
    src: Path
    dest: Path
    action: str = "move"  # move / skip
    reason: str = ""


@dataclass
class TidyPlan:
    course: Path
    moves: list[Move] = field(default_factory=list)

    @property
    def todo(self) -> list[Move]:
        return [m for m in self.moves if m.action == "move"]


def plan_course(course_dir: Path) -> TidyPlan:
    """扫描一门课的讲义目录，算出每个文件应去的章节目录（不移动）。"""
    out = TidyPlan(course=course_dir)
    lec = CategoryDirs.scan(course_dir).get("lecture")
    if not lec.is_dir():
        return out
    for p in sorted(lec.iterdir()):
        if not p.is_file():
            continue
        # 只在讲义分类内部重新分组，不重判分类（data.zip 仍归 data.zip）
        dest_dir = lecture_dir(lec, "", "", p.name)
        if dest_dir == lec:
            continue  # 识别不到章节 -> 保持原样
        dest = dest_dir / p.name
        if dest.exists():
            if dest.stat().st_size == p.stat().st_size:
                out.moves.append(Move(p, dest, "skip", "目标已存在同内容"))
                continue
            dest = unique_path(dest)  # 同名不同大小 -> x (1).ext
        out.moves.append(Move(p, dest, "move", f"归入 {dest_dir.name}"))
    return out


def plan_all(root: Path) -> list[TidyPlan]:
    """扫描课程根目录下所有课程文件夹。"""
    if not root.is_dir():
        return []
    return [plan_course(p) for p in sorted(root.iterdir()) if p.is_dir()]


def render(plans: list[TidyPlan]) -> str:
    """把移动方案渲染成文本；一行一个文件，未执行任何移动。"""
    lines: list[str] = []
    total = 0
    for plan in plans:
        if not plan.moves:
            continue
        todo = len(plan.todo)
        total += todo
        lines.append(f"\n【{plan.course.name}】待移动 {todo} 项：")
        for m in plan.moves:
            if m.action == "skip":
                lines.append(f"   = {m.src.name}  （{m.reason}，保持原样）")
                continue
            mark = "→"
            ren = "  （重名已改名）" if m.dest.name != m.src.name else ""
            lines.append(f"   {mark} {m.src.name}  ->  "
                         f"{m.dest.relative_to(plan.course)}{ren}")
    if not lines:
        return "没有需要整理的讲义文件：各章内容都已在章节目录里。"
    lines.append(f"\n合计：{total} 项待移动（只移动不删除；同名不同大小自动加 (1)）")
    return "\n".join(lines)


def apply_plans(plans: list[TidyPlan], state: dict,
                progress: Callable[[str], None] | None = None) -> dict:
    """执行移动，并把 `state` 里的路径一起改到新位置（不负责落盘）。"""
    moved = skipped = errors = 0
    files_state = state.get("files") or {}
    archives = state.setdefault("archives", {})
    for plan in plans:
        for m in plan.moves:
            if m.action != "move":
                skipped += 1
                continue
            try:
                m.dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(m.src), str(m.dest))
            except OSError as exc:
                errors += 1
                log.error("移动失败 %s: %s", m.src, exc)
                if progress:
                    progress(f"移动失败 {m.src.name}: {exc}")
                continue
            moved += 1
            old, new = str(m.src), str(m.dest)
            for rec in files_state.values():
                if isinstance(rec, dict) and rec.get("path") == old:
                    rec["path"] = new  # 同步状态跟着搬家
            if old in archives:  # 解压记录按路径存，键也要跟着走
                archives[new] = archives.pop(old)
            if progress:
                progress(f"{m.src.name} → {m.dest.relative_to(plan.course)}")
    if moved:
        state["tidied_at"] = now_local().isoformat(timespec="seconds")
    return {"moved": moved, "skipped": skipped, "errors": errors}


def run(plans: list[TidyPlan],
        progress: Callable[[str], None] | None = None) -> dict:
    """CLI / GUI 的入口：读状态 -> 移动 -> 落盘。"""
    state = load_state()
    rep = apply_plans(plans, state, progress)
    if rep["moved"]:
        save_state(state)
    log.info("讲义整理: %s", rep)
    return rep
