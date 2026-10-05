"""一键清理缓存：下载状态、通知缓存、排队待办、导出日历、日志、浏览器缓存。

原则：
- 默认只清「可再生」的东西，**不动登录态**（session.json + 浏览器 Cookie）。
- 每一项先扫描出体积再让你勾选，删之前能看见「清多少、会不会有副作用」。
- 浏览器缓存按目录名匹配，永远不会碰到 `Cookies` / `Login Data` / `Local State`。
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import deskbox
from .paths import (LOG_PATH, PROFILE_DIR, PROJECT_DIR, STATE_PATH,
                    get_logger, load_state, save_state)

log = get_logger()

ICS_PATH = PROJECT_DIR / "canvas-deadlines.ics"
BACKUP_LOG_SUFFIXES = (".1", ".2", ".3", ".old")

# 纯缓存目录/文件名（大小写不敏感，任意层级命中即清理）
_CACHE_NAMES = {
    "cache", "code cache", "cachestorage", "gpucache", "shadercache",
    "grshadercache", "gpupersistentcache", "serviceworker", "crashpad",
    "component_crx_cache", "extensions_crx_cache", "safe browsing",
    "optimizationguidemodels", "nurturing",
}
_CACHE_FILES = {"browsermetrics-spare.pma"}

# 绝对不能碰的（登录态与用户数据）
_KEEP_NAMES = {
    "cookies", "login data", "login data for account", "web data",
    "local state", "preferences", "session.storage", "local storage",
    "indexeddb", "session storage", "network", "session.json",
}


@dataclass
class Target:
    key: str
    label: str
    detail: str
    files: int = 0
    bytes: int = 0
    default: bool = False
    destructive: bool = False  # True = 会丢数据/登录态，需额外确认

    @property
    def size_text(self) -> str:
        return fmt_size(self.bytes)


def fmt_size(n: int) -> str:
    if n <= 0:
        return "0 B"
    x = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if x < 1024 or unit == "TB":
            return f"{x:.0f} {unit}" if unit == "B" else f"{x:.1f} {unit}"
        x /= 1024
    return f"{x:.1f} TB"


def _measure(paths: list[Path]) -> tuple[int, int]:
    files = total = 0
    for p in paths:
        try:
            if p.is_file():
                files += 1
                total += p.stat().st_size
            elif p.is_dir():
                for q in p.rglob("*"):
                    if q.is_file():
                        files += 1
                        total += q.stat().st_size
        except OSError:
            continue
    return files, total


# ---------------- 扫描 ----------------

def browser_cache_paths(root: Path | None = None) -> list[Path]:
    """浏览器缓存目录/文件（按名字匹配，避开登录态与用户数据）。"""
    root = root or PROFILE_DIR
    out: list[Path] = []
    if not root.is_dir():
        return out
    for p in root.rglob("*"):
        name = p.name.lower()
        if name in _KEEP_NAMES:
            continue
        # 只在命中缓存名时收，且绝不进入被保留的子树
        if p.is_dir() and name in _CACHE_NAMES:
            out.append(p)
        elif p.is_file() and name in _CACHE_FILES:
            out.append(p)
    return out


def login_paths(root: Path | None = None) -> list[Path]:
    root = root or PROFILE_DIR
    out: list[Path] = []
    session = PROFILE_DIR.parent / "session.json"
    if session.exists():
        out.append(session)
    if root.is_dir():
        for name in ("Cookies", "Login Data", "Login Data For Account", "Web Data"):
            out.extend(p for p in root.rglob(name) if p.is_file())
    return out


def scan() -> list[Target]:
    """列出可清理项与各自体积。"""
    out: list[Target] = []

    # 1) 下载状态 / 通知缓存
    try:
        st = load_state()
        n_files = len(st.get("files") or {})
        n_notices = len(st.get("notices_cache") or [])
        seen = len((st.get("notices") or {}).get("seen") or [])
        size = STATE_PATH.stat().st_size if STATE_PATH.exists() else 0
        out.append(Target(
            "state",
            "下载状态 / 通知缓存",
            f"state.json：{n_files} 条下载记录、{n_notices} 条通知缓存、{seen} 个已读标记。"
            "清除后下次同步会重新核对文件（有本地内容识别兜底，不会重复下载）。",
            3, size, default=True,
        ))
    except Exception as exc:  # noqa: BLE001
        log.warning("扫描 state 失败: %s", exc)

    # 2) 后台排队待办
    try:
        rows = deskbox.pending_drafts()
        if rows:
            size = deskbox.PENDING_PATH.stat().st_size
            out.append(Target(
                "pending", "后台排队待办",
                f"pending_todos.json 中还有 {len(rows)} 条待写入 DeskBox 的待办；"
                "清理后这些截止提醒会丢失。",
                1, size, default=False, destructive=True,
            ))
    except Exception as exc:  # noqa: BLE001
        log.warning("扫描排队待办失败: %s", exc)

    # 3) 导出的日历
    if ICS_PATH.exists():
        out.append(Target(
            "ics", "导出的截止日历 (.ics)",
            f"{ICS_PATH.name}，可随时重新导出。",
            1, ICS_PATH.stat().st_size, default=True,
        ))

    # 4) 日志
    logs = [LOG_PATH] + [LOG_PATH.with_name(LOG_PATH.name + s) for s in BACKUP_LOG_SUFFIXES]
    logs = [p for p in logs if p.exists()]
    if logs:
        f, b = _measure(logs)
        out.append(Target("log", "运行日志", "sync.log（清空后仍会继续记录新日志）。", f, b, default=True))

    # 5) 浏览器缓存
    caches = browser_cache_paths()
    if caches:
        f, b = _measure(caches)
        out.append(Target(
            "browser", "浏览器缓存",
            f"{len(caches)} 个缓存目录（Default/Cache、ShaderCache 等），"
            "不含 Cookie，**清理后登录态仍然有效**。",
            f, b, default=True,
        ))

    # 6) 重复的未完成待办
    try:
        todo_path = _todo_path()
        if todo_path.exists():
            doc = deskbox.load_doc(todo_path)
            removed = deskbox.dedupe_completed({"items": list(doc.get("items", []))})
            if removed:
                out.append(Target(
                    "todos", "DeskBox 中的重复待办",
                    f"发现 {len(removed)} 条「同名同截止且已有完成记录」的未完成重复项，"
                    "清理时保留已完成那条（需要短暂重启 DeskBox）。",
                    len(removed), 0, default=False, destructive=True,
                ))
    except Exception as exc:  # noqa: BLE001
        log.warning("扫描重复待办失败: %s", exc)

    # 7) 登录态
    lg = login_paths()
    if lg:
        f, b = _measure(lg)
        out.append(Target(
            "login", "登录态（需重新登录）",
            "session.json 与浏览器 Cookie；清理后必须重新在浏览器里登录一次。",
            f, b, default=False, destructive=True,
        ))
    return out


def _todo_path() -> Path:
    from .paths import load_config

    return Path(load_config()["deskbox"]["todo_path"])


# ---------------- 执行 ----------------

def _truncate_log() -> int:
    """清空日志文件（保持 FileHandler 打开，避免写到旧偏移处留 NUL）。"""
    size = 0
    try:
        size = LOG_PATH.stat().st_size
    except OSError:
        return 0
    removed = 0
    for h in list(log.handlers):
        if not isinstance(h, logging.FileHandler):
            continue
        if Path(getattr(h, "baseFilename", "")).resolve() != LOG_PATH.resolve():
            continue
        h.acquire()
        try:
            h.flush()
            if h.stream and not h.stream.closed:
                h.stream.seek(0)
                h.stream.truncate(0)
                removed += 1
        finally:
            h.release()
    if not removed:
        # 日志没被本进程打开（例如命令行场景），直接截断
        try:
            with open(LOG_PATH, "w", encoding="utf-8"):
                pass
        except OSError:
            pass
    for s in BACKUP_LOG_SUFFIXES:
        p = LOG_PATH.with_name(LOG_PATH.name + s)
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass
    return size


def _remove(paths: list[Path]) -> tuple[int, int, int]:
    """删除，返回 (文件数, 字节数, 因占用跳过数)。"""
    f, b = _measure(paths)
    skipped = 0
    for p in paths:
        try:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=False)
            elif p.exists():
                p.unlink()
        except OSError:
            skipped += 1
    return f, b, skipped


def run(keys: set[str], progress: Callable[[str], None] = lambda m: None) -> dict:
    """执行清理，返回统计与说明。**不会**清理未勾选项。"""
    report: dict = {"cleaned": [], "bytes": 0, "files": 0, "messages": [], "skipped": 0}
    if not keys:
        return report

    if "state" in keys:
        st = load_state()
        for k in ("files", "notices", "notices_cache"):
            st.pop(k, None)
        save_state(st)
        report["messages"].append("已清空下载状态与通知缓存，下次同步会重新核对文件")
        report["cleaned"].append("state")
        progress("已清空下载状态")

    if "pending" in keys:
        deskbox.clear_pending()
        report["messages"].append("已丢弃后台排队的待办")
        report["cleaned"].append("pending")

    if "ics" in keys:
        f, b, sk = _remove([ICS_PATH]) if ICS_PATH.exists() else (0, 0, 0)
        report["files"] += f
        report["bytes"] += b
        report["skipped"] += sk
        report["cleaned"].append("ics")

    if "log" in keys:
        report["bytes"] += _truncate_log()
        report["files"] += 1
        report["messages"].append("日志已清空")
        report["cleaned"].append("log")

    if "browser" in keys:
        f, b, sk = _remove(browser_cache_paths())
        report["files"] += f
        report["bytes"] += b
        report["skipped"] += sk
        report["cleaned"].append("browser")
        if sk:
            report["messages"].append(f"浏览器有 {sk} 个文件被占用，已跳过（可稍后重试）")

    if "todos" in keys:
        n = clean_duplicate_todos(progress=progress)
        if n:
            report["messages"].append(f"已删除 {n} 条重复待办（保留已完成那条）")
        report["cleaned"].append("todos")

    if "login" in keys:
        f, b, sk = _remove(login_paths())
        report["files"] += f
        report["bytes"] += b
        report["skipped"] += sk
        report["cleaned"].append("login")
        report["messages"].append("登录态已清除，下次同步需要重新登录")

    return report


def clean_duplicate_todos(progress: Callable[[str], None] = lambda m: None) -> int:
    """删除 DeskBox 里「已完成条目」的未完成重复项（同名 + 同截止时间）。"""
    from .paths import load_config

    cfg = load_config()["deskbox"]
    todo_path = Path(cfg["todo_path"])
    if not todo_path.exists():
        return 0
    doc = deskbox.load_doc(todo_path)
    before = len(doc.get("items", []))
    removed = deskbox.dedupe_completed(doc)
    if not removed:
        return 0
    tmp = deskbox.write_copy(doc, todo_path)
    exe = Path(cfg["exe_path"])
    if deskbox.is_running():
        progress("正在重启 DeskBox 以应用去重…")
        deskbox.quick_swap(
            todo_path, tmp, exe,
            prepared_count=before, drafts=[],
            progress=progress,
        )
    else:
        deskbox.apply_copy(todo_path, tmp)
    return len(removed)
