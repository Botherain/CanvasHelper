"""路径、配置、日志等基础设施。"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# ---------- 目录约定 ----------
# 源码 / 安装位置。只读用途：定位包、作为计划任务的工作目录。
# 注意：打包成 exe 后这里会是临时解压目录，**绝不能拿它存可写数据**。
PROJECT_DIR = Path(__file__).resolve().parent.parent

# 可写数据目录：配置、状态、日志、排队待办、导出的日历。
# 与安装位置解耦，换机器、换安装盘符、打包分发都不受影响。
DATA_DIR = Path(os.environ.get("LOCALAPPDATA") or str(Path.home())) / "CanvasHelper"

CONFIG_PATH = DATA_DIR / "config.json"
STATE_PATH = DATA_DIR / "state.json"
LOG_PATH = DATA_DIR / "sync.log"
PENDING_PATH = DATA_DIR / "pending_todos.json"
ICS_PATH = DATA_DIR / "canvas-deadlines.ics"
PROFILE_DIR = DATA_DIR / "profile"
SESSION_PATH = DATA_DIR / "session.json"

# 旧版本（v1.0 之前）把可写数据放在源码目录下，启动时自动搬一次
_LEGACY_FILES = ("config.json", "state.json", "pending_todos.json", "canvas-deadlines.ics")


def _bootstrap() -> None:
    """建立数据目录，并把源码目录里的旧数据搬到 DATA_DIR（只搬目标缺失的）。"""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    for name in _LEGACY_FILES:
        dst, src = DATA_DIR / name, PROJECT_DIR / name
        if src.is_file() and not dst.exists():
            with suppress(OSError):
                shutil.copy2(src, dst)


_bootstrap()

# 每门课在本地的分类前缀 -> 默认目录名（以磁盘上真实存在的目录为准）
CATEGORY_PREFIXES = {
    "lecture": "1.",
    "tutorial": "2.",
    "assignment": "3.",
    "project": "4.",
    "book": "5.",
    "other": "6.",
}

DEFAULT_CATEGORIES = {
    "lecture": "1.Lecture session",
    "tutorial": "2.Tutorial session",
    "assignment": "3.Assignments",
    "project": "4.Projects",
    "book": "5.Books",
    "other": "6.Others",
}

DEFAULT_CONFIG: dict[str, Any] = {
    # 课程根目录（每个课程一个子文件夹）。留空 -> 启动时取 <用户目录>/CanvasCourses，
    # 在设置页「浏览…」选成你自己的目录。
    "classes_root": "",
    # canvas 课程 id -> 本地文件夹名；GUI 中勾选后自动写入
    "course_map": {},
    # 勾选同步的 canvas 课程 id
    "selected_courses": [],
    "categories": DEFAULT_CATEGORIES,
    # 下载后的压缩包：内容包（讲义/代码）自动解压，数据包（ipynb 要读的
    # 原始数据）原样保留。判定逻辑见 canvas_helper/extract.py。
    "archives": {
        "extract": True,
        # 跳过 __MACOSX / ._ / .DS_Store 这类 macOS 解压垃圾
        "skip_junk": True,
        # 单包解压体积上限，超出即放弃（防 zip 炸弹）
        "max_total_bytes": 2 * 1024 * 1024 * 1024,
        # 包名命中这些词直接判为数据包、不解压；想解压改小这个词表
        "data_name_hints": [
            "data", "dataset", "datasets", "train", "test",
            "validation", "labels", "mnist", "cifar", "imagenet", "audio",
        ],
    },
    "deskbox": {
        "enabled": True,
        # 留空 -> 启动时自动扫描 %LOCALAPPDATA%\DeskBox\data\widgets\*\todo.json
        "todo_path": "",
        # 留空 -> 启动时按常见安装位置搜索 DeskBox.exe
        "exe_path": "",
        # 附件路径是否补上缺失的 Classes 一层
        "fix_attachment_paths": True,
        # 后台同步时如何处理运行中的 DeskBox: queue / restart / skip
        "background_policy": "queue",
        # 提前提醒分钟数
        "reminder_offset_minutes": 1440,
        # 写入时是否清理「已完成条目」的未完成重复项
        "remove_duplicates": True,
    },
    "schedule": {
        "enabled": False,
        "sync_time": "08:30",
        "report_enabled": True,
        "report_weekday": "MON",
        "report_time": "09:00",
    },
    "extras": {
        "ics": True,
        "notices": True,
        "grades": True,
        "report": True,
    },
    # 登录态过期时的处理
    "auth": {
        # 后台（计划任务）同步发现登录过期时，弹出可见登录窗口尝试自愈。
        # 关掉则只记成「待重新登录」，由 GUI 顶部提示，不打扰无人值守的机器。
        "popup_on_expiry": True,
        # 弹窗等待秒数，超时自动关闭并记为待重新登录
        "popup_timeout_s": 180,
    },
}

_logger: logging.Logger | None = None


def get_logger() -> logging.Logger:
    global _logger
    if _logger is None:
        logger = logging.getLogger("canvas_helper")
        if not logger.handlers:
            logger.setLevel(logging.INFO)
            fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
            try:
                LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
                fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
                fh.setFormatter(fmt)
                logger.addHandler(fh)
            except OSError:
                pass
            sh = logging.StreamHandler(sys.stdout)
            sh.setFormatter(fmt)
            logger.addHandler(sh)
        _logger = logger
    return _logger


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def discover_deskbox() -> tuple[str, str]:
    """自动寻找 DeskBox 的 ``(todo.json, exe路径)``。找不到的返回空串。

    首次在一台新机器上运行时，配置文件里这两项是空的，靠这里猜；猜错了在
    设置页「浏览…」里手选即可。
    """
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")

    exe = ""
    for c in (
        Path(local) / "Programs" / "DeskBox" / "DeskBox.exe",
        Path(local) / "DeskBox" / "DeskBox.exe",
        Path(r"C:\Program Files") / "DeskBox" / "DeskBox.exe",
        Path(r"C:\Program Files (x86)") / "DeskBox" / "DeskBox.exe",
        Path(r"D:\DeskBox") / "DeskBox.exe",
    ):
        if c.is_file():
            exe = str(c)
            break

    todo = ""
    best = -1
    widgets = Path(local) / "DeskBox" / "data" / "widgets"
    if widgets.is_dir():
        for cand in sorted(widgets.glob("*/todo.json")):
            try:
                data = json.loads(cand.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            n = len((data or {}).get("items") or [])
            if n > best:
                best, todo = n, str(cand)
    return todo, exe


def _resolve_defaults(cfg: dict[str, Any]) -> dict[str, Any]:
    """填上只有在这台机器上才知道的路径（首次运行时配置文件里是空的）。"""
    if not cfg.get("classes_root"):
        cfg["classes_root"] = str(Path.home() / "CanvasCourses")
    db = cfg.setdefault("deskbox", {})
    need_todo = not db.get("todo_path")
    need_exe = not db.get("exe_path")
    if need_todo or need_exe:
        todo, exe = discover_deskbox()
        if need_todo:
            db["todo_path"] = todo
        if need_exe:
            db["exe_path"] = exe
    return cfg


def load_config() -> dict[str, Any]:
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            return _resolve_defaults(_deep_merge(DEFAULT_CONFIG, data))
        except (OSError, json.JSONDecodeError):
            get_logger().exception("配置文件读取失败，使用默认配置: %s", CONFIG_PATH)
    return _resolve_defaults(json.loads(json.dumps(DEFAULT_CONFIG)))


def save_config(cfg: dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_state() -> dict[str, Any]:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            get_logger().exception("状态文件读取失败，重新初始化: %s", STATE_PATH)
    return {"files": {}, "todo_keys": {}, "todo_done": {}, "notices": {}, "last_sync": None}


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def mark_auth_needed(source: str = "") -> None:
    """记录「Canvas 登录已过期」，供 GUI 启动时醒目提示。

    这是「不再静默失败」的关键：后台同步失败时把信号写进状态，
    而不是只留一条没人会看的 sync.log。
    """
    state = load_state()
    state["auth_needed"] = {"at": now_hk().isoformat(timespec="seconds"), "source": source}
    save_state(state)


def clear_auth_needed() -> bool:
    """登录成功后清掉过期标记；本来就没有时直接返回，不写盘。"""
    state = load_state()
    if not state.pop("auth_needed", None):
        return False
    save_state(state)
    return True


def now_hk():

    from zoneinfo import ZoneInfo

    return datetime.now(UTC).astimezone(ZoneInfo("Asia/Hong_Kong"))


def ensure_dirs() -> None:
    for d in (DATA_DIR, PROFILE_DIR):
        with suppress(OSError):
            d.mkdir(parents=True, exist_ok=True)
    _bootstrap()  # 兜底：import 时目录若没建成，这里再搬一次旧数据
