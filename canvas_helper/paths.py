"""路径、配置、日志等基础设施。"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_DIR / "config.json"
STATE_PATH = PROJECT_DIR / "state.json"
LOG_PATH = PROJECT_DIR / "sync.log"
PROFILE_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "CanvasHelper" / "profile"

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
    # 课程根目录（每个课程一个子文件夹）
    "classes_root": r"D:\Courses",
    # canvas 课程 id -> 本地文件夹名；GUI 中勾选后自动写入
    "course_map": {},
    # 勾选同步的 canvas 课程 id
    "selected_courses": [],
    "categories": DEFAULT_CATEGORIES,
    "deskbox": {
        "enabled": True,
        "todo_path": (
            r"%LOCALAPPDATA%\DeskBox\data\widgets"
            r"\e0597764-7b46-4f11-874f-b0f925fdbaba\todo.json"
        ),
        "exe_path": r"D:\DeskBox\DeskBox.exe",
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


def load_config() -> dict[str, Any]:
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            return _deep_merge(DEFAULT_CONFIG, data)
        except (OSError, json.JSONDecodeError):
            get_logger().exception("配置文件读取失败，使用默认配置: %s", CONFIG_PATH)
    return json.loads(json.dumps(DEFAULT_CONFIG))


def save_config(cfg: dict[str, Any]) -> None:
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
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def now_hk():
    from datetime import timezone

    from zoneinfo import ZoneInfo

    return datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Hong_Kong"))


def ensure_dirs() -> None:
    PROJECT_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
