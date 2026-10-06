"""同步流程编排：登录 -> 拉取 -> 归档 -> 待办 -> 附加功能。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Callable

from . import deskbox, extras, scheduler
from .auth import AuthRequired, CanvasSession
from .canvas_api import CanvasAPI, Course, Item
from .classify import course_dirs, match_folder
from .filesync import CoursePlan, build_plan, describe, execute_plan
from .paths import (DEFAULT_CATEGORIES, get_logger, load_config, load_state,
                    mark_auth_needed, now_hk, save_config, save_state)

log = get_logger()
Progress = Callable[[str], None]
_nop = lambda m: None  # noqa: E731

_STATUS_CN = {"completed": "已完成", "existing": "已存在", "reopened": "截止已更新"}


@dataclass
class SyncResult:
    courses: list[Course] = field(default_factory=list)
    plans: list[CoursePlan] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    drafts: list = field(default_factory=list)
    todo_added: list = field(default_factory=list)
    todo_total_drafts: int = 0
    todo_mode: str = "none"
    todo_stats: dict = field(default_factory=dict)
    unread: list = field(default_factory=list)
    grades: dict = field(default_factory=dict)
    grade_files: list[str] = field(default_factory=list)
    ics_path: str = ""
    errors: list[str] = field(default_factory=list)
    # 登录态失效导致本轮未执行（区别于普通的 errors）
    auth_required: bool = False
    text: str = ""


def _todo_summary(added: int, st: dict) -> str:
    """把待办统计拼成一句人话。"""
    parts = [f"待办新增 {added} 条"]
    if st.get("completed"):
        parts.append(f"{st['completed']} 项已完成，未重复添加")
    if st.get("reopened"):
        parts.append(f"{st['reopened']} 项截止时间已更新，重新提醒")
    if st.get("existing"):
        parts.append(f"{st['existing']} 项已存在")
    if st.get("deduped"):
        parts.append(f"清理重复 {st['deduped']} 条")
    return "，".join(parts)


def _relogin_headless(cfg: dict, session: CanvasSession, progress: Progress) -> bool:
    """后台（计划任务）模式发现登录过期时的自愈：弹出可见窗口等用户完成 SSO。

    后台没有 GUI，唯一能自愈的路径就是弹一次浏览器窗口；但必须克制：
    没有交互会话时弹了也没人看得见，配置关掉时则完全不打扰。
    成功返回 True，调用方可继续本轮同步；失败由调用方记为「待重新登录」。
    """
    auth_cfg = cfg.get("auth") or {}
    if not auth_cfg.get("popup_on_expiry", True):
        progress("登录已过期（已配置不弹窗），跳过本次后台任务")
        return False
    if not os.environ.get("SESSIONNAME"):
        # 服务 / 会话 0 里没有桌面，弹窗只会白白等满超时
        progress("登录已过期且当前没有交互会话，无法弹窗")
        return False
    timeout = max(10, int(auth_cfg.get("popup_timeout_s", 180)))
    progress(f"登录已过期，弹出登录窗口等待（{timeout} 秒）…")
    return session.ensure_login(on_status=progress, timeout_s=timeout)


def resolve_courses(cfg: dict, api: CanvasAPI) -> tuple[list[Course], dict[int, Path]]:
    """选中的课程 + 本地文件夹映射（自动匹配，必要时按模板新建）。"""
    all_courses = {c.id: c for c in api.courses()}
    root = Path(cfg["classes_root"])
    folders = course_dirs(root)
    course_map = cfg.get("course_map") or {}
    selected = [all_courses[i] for i in cfg.get("selected_courses", []) if i in all_courses]

    mapping: dict[int, Path] = {}
    for c in selected:
        pinned = course_map.get(str(c.id))
        if pinned:
            mapping[c.id] = root / pinned
            continue
        folder = match_folder(c.short, folders)
        if folder is None:
            # 本地没有对应文件夹 -> 以 Canvas 名称新建
            safe = f"{c.short} - {c.name}".replace("/", "-").strip(" .")
            folder = root / safe
            log.info("为 %s 使用文件夹 %s", c.short, folder)
        mapping[c.id] = folder
    return selected, mapping


def attachment_lookup(plans: list[CoursePlan]) -> dict[tuple[int, int | None], Path]:
    out: dict[tuple[int, int | None], Path] = {}
    for plan in plans:
        for pf in plan.files:
            if pf.item.file_id and pf.action != "exists":
                out[(plan.course.id, pf.item.file_id)] = pf.target
            elif pf.item.file_id:
                out.setdefault((plan.course.id, pf.item.file_id), pf.target)
    return out


def run_sync(
    cfg: dict | None = None,
    course_ids: list[int] | None = None,
    session: CanvasSession | None = None,
    dry_run: bool = False,
    headless: bool = False,
    progress: Progress = _nop,
    include_extras: bool = True,
) -> SyncResult:
    cfg = cfg or load_config()
    if course_ids:
        cfg = dict(cfg)
        cfg["selected_courses"] = course_ids
    res = SyncResult()
    own_session = session is None
    session = session or CanvasSession()

    try:
        if not headless:
            if not session.ensure_login(on_status=progress):
                res.errors.append("登录失败/超时")
                return res
        else:
            session.start(headless=True)
            if not session.is_logged_in() and not _relogin_headless(cfg, session, progress):
                # 关键：把失败写进状态，GUI 启动时会醒目提示，而不是只进 sync.log
                mark_auth_needed("sync --headless")
                res.auth_required = True
                res.errors.append("登录已过期，本次后台同步未执行")
                return res

        api = CanvasAPI(session)
        progress("读取课程列表…")
        courses, folders = resolve_courses(cfg, api)
        if not courses:
            res.errors.append("没有勾选任何课程")
            return res
        res.courses = courses

        state = load_state()
        items_by_course: dict[int, list[Item]] = {}
        for c in courses:
            progress(f"读取课程结构：{c.short}")
            try:
                items, _ = api.collect_items(c.id)
            except AuthRequired as exc:
                # cookie 中途失效：整轮作废，标记待重新登录后立即收工，
                # 绝不带着过期会话继续下载 / 写待办
                mark_auth_needed("sync --headless" if headless else "sync")
                res.auth_required = True
                res.errors.append(f"{c.short}: 登录中途失效（{exc}）")
                log.error("登录中途失效，中止本次同步: %s", exc)
                break
            except Exception as exc:
                res.errors.append(f"{c.short}: {exc}")
                log.exception("读取 %s 失败", c.short)
                continue
            items_by_course[c.id] = items

        if res.auth_required:
            return res

        plans: list[CoursePlan] = []
        for c in courses:
            folder = folders.get(c.id) or (Path(cfg["classes_root"]) / c.short)
            if c.id not in items_by_course:
                continue
            plans.append(build_plan(c, folder, items_by_course[c.id], state))
        res.plans = plans
        res.text = describe(plans)

        # 待办草稿在预览阶段就算出来，便于界面上直接看到截止时间
        drafts = deskbox.build_drafts(
            {c.id: c for c in courses}, items_by_course, attachment_lookup(plans)
        )
        res.drafts = drafts.drafts
        res.todo_total_drafts = len(drafts.drafts)

        # 预判这些截止时间在 DeskBox 里的现状（只读），避免重复添加已完成的作业
        # 未配置 todo_path 时（新机器自动发现失败）直接跳过待办相关逻辑
        if cfg.get("deskbox", {}).get("enabled", True) and cfg["deskbox"].get("todo_path"):
            try:
                statuses = deskbox.preview_status(
                    Path(cfg["deskbox"]["todo_path"]), drafts.drafts)
                for d in drafts.drafts:
                    s = statuses.get(d.key)
                    if s:
                        d.status = s
                        d.existing = True
            except Exception as exc:  # noqa: BLE001
                log.debug("待办状态预判失败: %s", exc)

        if drafts.drafts:
            res.text += "\n\n【待写入 DeskBox 的待办】\n" + "\n".join(
                f"   · {d.due:%m-%d %H:%M}  {d.text}"
                f"{f'  [{_STATUS_CN[d.status]}]' if d.status in _STATUS_CN else ''}"
                for d in drafts.drafts
            )
        if drafts.skipped_past:
            res.text += f"\n（已跳过 {drafts.skipped_past} 项过期未完成任务）"

        if dry_run:
            progress("预览完成（未写入）")
            return res

        progress("开始下载与归档…")
        res.stats = execute_plan(plans, api, state, progress=progress)

        # ---------- 待办 ----------
        # 未配置 todo_path 时（新机器自动发现失败）直接跳过待办相关逻辑
        if cfg.get("deskbox", {}).get("enabled", True) and cfg["deskbox"].get("todo_path"):
            policy = cfg["deskbox"].get("background_policy", "queue")
            draft_list = drafts.drafts
            if headless and policy == "queue":
                n = deskbox.enqueue_pending(draft_list)
                res.todo_added = draft_list
                res.todo_mode = "queued" if n else "none"
                progress(f"待办已排队 {n} 条（下次打开 GUI 时写入）")
            else:
                todo_path = Path(cfg["deskbox"]["todo_path"])
                tmp, added, st = deskbox.prepare(todo_path, draft_list, cfg["deskbox"])
                res.todo_added = added
                res.todo_stats = st
                if not st.get("changed"):
                    res.todo_mode = "none"
                elif deskbox.is_running():
                    progress("DeskBox 待办写入（关闭→替换→重启）…")
                    deskbox.quick_swap(
                        todo_path, tmp, Path(cfg["deskbox"]["exe_path"]),
                        prepared_count=st["total_items"], drafts=added,
                        offset_minutes=int(cfg["deskbox"].get("reminder_offset_minutes", 1440)),
                        progress=progress,
                    )
                    res.todo_mode = "swapped"
                else:
                    deskbox.apply_copy(todo_path, tmp)
                    res.todo_mode = "applied"
                progress(_todo_summary(len(added), st))
        else:
            res.todo_mode = "disabled"

        # ---------- 附加功能 ----------
        if include_extras and cfg.get("extras", {}).get("ics", True):
            try:
                res.ics_path = str(extras.export_ics(drafts.drafts))
            except Exception as exc:
                res.errors.append(f"ics 导出失败: {exc}")

        if include_extras and cfg.get("extras", {}).get("grades", True):
            try:
                grades = extras.collect_grades(api, courses)
                res.grades = grades
                for c in courses:
                    p = extras.update_grade_file(folders[c.id], c, grades.get(c.id, {}))
                    if p:
                        res.grade_files.append(str(p))
            except Exception as exc:
                res.errors.append(f"成绩更新失败: {exc}")

        if include_extras and cfg.get("extras", {}).get("notices", True):
            try:
                notices = extras.collect_notices(api, courses, progress)
                res.unread = extras.mark_notices_read(notices)
                state = load_state()
                state["notices_cache"] = [
                    {"course": n.course, "title": n.title, "body": n.body,
                     "posted_at": n.posted_at, "url": n.url, "kind": n.kind}
                    for n in notices[:300]
                ]
                save_state(state)
            except Exception as exc:
                res.errors.append(f"通知读取失败: {exc}")

        state = load_state()
        state["last_sync"] = now_hk().isoformat(timespec="seconds")
        save_state(state)
        progress("同步完成")
        return res
    finally:
        if own_session:
            session.close()


# ---------------- 轻量采集（不下载文件） ----------------

def collect_deadlines(cfg: dict | None = None, progress: Progress = _nop) -> list:
    """只读取作业/测验截止时间，生成待办草稿。"""
    cfg = cfg or load_config()
    session = CanvasSession()
    try:
        if not session.ensure_login(on_status=progress):
            return []
        api = CanvasAPI(session)
        courses, _folders = resolve_courses(cfg, api)
        items_by_course: dict[int, list[Item]] = {}
        for c in courses:
            progress(f"读取 {c.short} 作业…")
            try:
                items_by_course[c.id] = api.collect_items(c.id)[0]
            except Exception as exc:
                log.warning("%s 读取失败: %s", c.short, exc)
        drafts = deskbox.build_drafts({c.id: c for c in courses}, items_by_course)
        progress(f"共 {len(drafts.drafts)} 项未截止任务")
        return drafts.drafts
    finally:
        session.close()


def collect_notice_list(cfg: dict | None = None, progress: Progress = _nop) -> tuple[list, int]:
    cfg = cfg or load_config()
    session = CanvasSession()
    try:
        if not session.ensure_login(on_status=progress):
            return [], 0
        api = CanvasAPI(session)
        courses, _f = resolve_courses(cfg, api)
        notices = extras.collect_notices(api, courses, progress)
        unread = extras.mark_notices_read(notices)
        state = load_state()
        state["notices_cache"] = [
            {"course": n.course, "title": n.title, "body": n.body,
             "posted_at": n.posted_at, "url": n.url, "kind": n.kind}
            for n in notices[:300]
        ]
        save_state(state)
        return notices, len(unread)
    finally:
        session.close()


# ---------------- 每周摘要 ----------------

def run_report(cfg: dict | None = None, headless: bool = False, progress: Progress = _nop) -> list[str]:
    cfg = cfg or load_config()
    session = CanvasSession()
    written: list[str] = []
    try:
        if headless:
            session.start(headless=True)
            if not session.is_logged_in():
                # 摘要不弹窗（每日同步在前已经弹过，避免连弹两次），只留标记
                mark_auth_needed("report --headless")
                log.error("登录已过期，跳过本周摘要（已标记待重新登录）")
                progress("登录过期，跳过摘要")
                return written
        elif not session.ensure_login(on_status=progress):
            return written
        api = CanvasAPI(session)
        courses, folders = resolve_courses(cfg, api)

        week_start = now_hk() - timedelta(days=7)
        state = load_state()
        new_by_course: dict[int, list[Path]] = {}
        for key, rec in state.get("files", {}).items():
            try:
                synced = rec.get("synced_at") or ""
                if synced >= week_start.isoformat(timespec="seconds"):
                    new_by_course.setdefault(int(rec.get("course_id", 0)), []).append(Path(rec["path"]))
            except ValueError:
                continue

        items_by_course: dict[int, list[Item]] = {}
        for c in courses:
            try:
                items_by_course[c.id] = api.collect_items(c.id)[0]
            except Exception:
                items_by_course[c.id] = []

        notices = extras.collect_notices(api, courses, progress) if cfg["extras"].get("notices") else []
        drafts = deskbox.build_drafts({c.id: c for c in courses}, items_by_course).drafts

        grades: dict[int, dict] = {}
        if cfg["extras"].get("grades"):
            try:
                grades = extras.collect_grades(api, courses)
            except Exception:
                grades = {}

        for c in courses:
            folder = folders.get(c.id)
            if folder is None or not folder.is_dir():
                continue
            path = extras.weekly_report(
                folder, c, new_by_course.get(c.id, []), notices, drafts, grades.get(c.id)
            )
            written.append(str(path))
            progress(f"已生成 {path.name}")
    finally:
        session.close()
    return written


# ---------------- 待办补写（后台排队 -> GUI 首次启动） ----------------

def flush_pending(cfg: dict | None = None, progress: Progress = _nop) -> int:
    rows = deskbox.pending_drafts()
    if not rows:
        return 0
    cfg = cfg or load_config()
    if not cfg["deskbox"].get("todo_path"):
        # DeskBox 尚未配置：保留排队的待办，等配置好再写
        progress("DeskBox todo.json 未配置，暂不补写（排队待办已保留）")
        return 0
    drafts = deskbox.drafts_from_pending(rows)
    todo_path = Path(cfg["deskbox"]["todo_path"])
    tmp, added, st = deskbox.prepare(todo_path, drafts, cfg["deskbox"])
    if not st.get("changed"):
        deskbox.clear_pending()
        if st.get("completed"):
            progress(f"{st['completed']} 项排队待办已完成，未重复添加")
        return 0
    if deskbox.is_running():
        deskbox.quick_swap(
            todo_path, tmp, Path(cfg["deskbox"]["exe_path"]),
            prepared_count=st["total_items"], drafts=added,
            offset_minutes=int(cfg["deskbox"].get("reminder_offset_minutes", 1440)),
            progress=progress,
        )
    else:
        deskbox.apply_copy(todo_path, tmp)
    deskbox.clear_pending()
    progress(_todo_summary(len(added), st))
    return len(added)
