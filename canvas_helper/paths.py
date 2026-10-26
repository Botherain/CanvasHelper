"""路径、配置、日志等基础设施。"""

from __future__ import annotations

import copy
import json
import logging
import os
import shutil
import sys
from contextlib import suppress
from datetime import datetime, timezone, tzinfo
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# 3.9 兼容：datetime.UTC 这个常量是 3.11 才加的，旧解释器上取不到。
UTC = timezone.utc

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
#: 更新检查结果的缓存（含公告正文），与 state.json 分开：清理下载状态时不必重拉
UPDATE_CACHE_PATH = DATA_DIR / "update_cache.json"
#: 更新包下载 / 解压 / 替换脚本的落地目录（体积可达上百 MB，可随时重下）
UPDATES_DIR = DATA_DIR / "updates"

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


# 刻意不在 import 时执行 _bootstrap()：建目录 + 搬旧数据是「入口动作」，由
# ensure_dirs()（__main__ / gui 两个入口都会先调）承担——纯 import（如
# --version、跑测试、被别的模块引用）不再动一次磁盘。

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
    # 你学校的 Canvas 站点地址（结尾不带 /），例如 https://canvas.example.edu。
    # 留空 -> 启动后在设置页「Canvas 站点地址」填写，未配置时同步会明确报错。
    "canvas_home": "",
    # 时区（IANA 名，如 Asia/Hong_Kong / America/New_York / Asia/Shanghai）。
    # 留空 -> 跟随**本机系统时区**。它决定截止时间的显示、过期判定与每周摘要
    # 的周界；写错会导致待办里的截止时刻整体偏几个小时。
    "timezone": "",
    # 课程代码提取用的自定义正则（可选）。留空 -> 用通用规则
    # （字母前缀 + 数字，如 CS5187 / MATH101）；院校的课程代码形态古怪时，
    # 在这里写一个正则（第一个捕获组优先，没有捕获组则取整个匹配）。
    "course_code_pattern": "",
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
    # 讨论区（Canvas Discussion）：帖子正文/回帖里常直接放 Colab、GitHub 链接
    # （CS0004 的 tutorial 就是这么发的）。链接转成 raw 直链下载，帖子存 HTML 快照。
    "discussions": {
        # 把 colab.research.google.com / github.com 的 blob 链接转成
        # raw.githubusercontent.com 直链下载（教程笔记本、讲义都可能）
        "links": True,
        # 讨论帖本身存一份 HTML 快照（私有仓库下不了时的兜底）
        "snapshot": True,
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
        # 写入时是否按截止时间重排 todo.json（DeskBox 按 sortOrder 显示）
        "sort_by_due": True,
        # 课程 id -> DeskBox 颜色；首次识别课程时按选课顺序自动分配
        "course_colors": {},
        # 课程 id -> 课程显示名；只给设置页显示用，不参与配色逻辑
        "course_labels": {},
        # 自建待办使用的额外颜色；空字符串表示不改它们的颜色
        "manual_color": "pink",
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
    # 版本校验与更新公告（数据源是 GitHub Releases，失败一律静默降级）
    "update": {
        # 启动 GUI / 后台同步时自动检查一次；关掉就只在设置页或命令行手动查
        "check_on_start": True,
        # 两次真正联网检查的最小间隔（小时）；0 = 每次启动都查
        "interval_hours": 24,
        # 是否把 GitHub 上标了 prerelease 的版本也算作可用更新
        "include_prerelease": False,
    },
}

_logger: logging.Logger | None = None
_DEFAULT_STATE: dict[str, Any] = {
    "files": {},
    "todo_keys": {},
    "todo_done": {},
    "notices": {},
    "last_sync": None,
    # 更新公告的已读 / 已跳过版本号（"1.4.0" 形式）；与网络缓存无关
    "update": {},
    # 注册计划任务时用的命令；程序被移动后启动时据此重建（否则任务静默失效）
    "schedule_cmd": {},
}


def get_logger() -> logging.Logger:
    global _logger
    if _logger is None:
        logger = logging.getLogger("canvas_helper")
        if not logger.handlers:
            logger.setLevel(logging.INFO)
            fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
            try:
                LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
                # 日志轮转：常驻机器上长期运行时，避免 sync.log 无上限增长。
                # cleaner.py 已兼容 .1/.2/.3 轮转后缀并会一并清理。
                fh = RotatingFileHandler(
                    LOG_PATH, encoding="utf-8", maxBytes=1_000_000, backupCount=3
                )
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


# load_config 的进程内缓存，键 = (路径, mtime_ns, size)：同步热路径里
# canvas_home() / cfg_discussions() 会被逐条待办反复调用，原来每次都读盘 +
# 解析 + 深合并；解析失败也按同一键缓存成默认配置，坏配置只刷一次异常日志。
# 键里带上路径，是防测试把 CONFIG_PATH 换到临时文件后串缓存。
_cfg_cache: tuple[tuple[str, int, int], dict[str, Any]] | None = None


def _config_cached() -> dict[str, Any]:
    """返回**缓存本体**（不复制）。仅供内部只读用途，外部请用 load_config。"""
    global _cfg_cache
    try:
        st = CONFIG_PATH.stat()
    except OSError:
        # 配置还没建立（或 stat 失败）：用默认配置且不缓存，文件出现后
        # 下一次调用自然会走正常缓存路径。
        return _resolve_defaults(copy.deepcopy(DEFAULT_CONFIG))
    key = (str(CONFIG_PATH), st.st_mtime_ns, st.st_size)
    if _cfg_cache is not None and _cfg_cache[0] == key:
        return _cfg_cache[1]
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        cfg = _resolve_defaults(_deep_merge(DEFAULT_CONFIG, data))
    except (OSError, json.JSONDecodeError):
        get_logger().exception("配置文件读取失败，使用默认配置: %s", CONFIG_PATH)
        cfg = _resolve_defaults(copy.deepcopy(DEFAULT_CONFIG))
    _cfg_cache = (key, cfg)
    return cfg


def load_config() -> dict[str, Any]:
    """读配置（带 mtime 缓存）。返回的是副本，调用方改写自己那份不会污染缓存。"""
    return copy.deepcopy(_config_cached())


def cfg_str(key: str, default: str = "") -> str:
    """读配置里的一个字符串项（不复制整份配置，热路径用）。"""
    val = _config_cached().get(key)
    if val is None:
        return default
    return str(val).strip()


def canvas_home() -> str:
    """配置里的 Canvas 站点地址（不带结尾斜杠）；未配置时返回空串。"""
    return cfg_str("canvas_home").rstrip("/")


def cfg_discussions(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """取 ``discussions`` 配置段；允许直接把整份配置或该段传进来。"""
    if cfg is None:
        return load_config().get("discussions") or {}
    if "classes_root" in cfg or "discussions" in cfg:  # 整份配置
        return cfg.get("discussions") or {}
    return cfg  # 本身就是 discussions 段


def save_config(cfg: dict[str, Any]) -> None:
    global _cfg_cache
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _cfg_cache = None  # 显式清一次：紧跟着的 load_config 拿到的必是刚写的内容


def load_state() -> dict[str, Any]:
    """读状态（带 mtime 缓存）。返回副本，调用方可直接改不污染缓存。"""
    global _state_cache
    try:
        st = STATE_PATH.stat()
    except OSError:
        # 状态文件还不存在：返回空状态且不缓存，文件落地后自然切到缓存路径。
        return copy.deepcopy(_DEFAULT_STATE)
    key = (str(STATE_PATH), st.st_mtime_ns, st.st_size)
    if _state_cache is not None and _state_cache[0] == key:
        return copy.deepcopy(_state_cache[1])
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        get_logger().exception("状态文件读取失败，重新初始化: %s", STATE_PATH)
        state = copy.deepcopy(_DEFAULT_STATE)
    _state_cache = (key, state)
    return copy.deepcopy(state)


def save_state(state: dict[str, Any]) -> None:
    global _state_cache
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _state_cache = None


# load_state 的进程内缓存，键 = (路径, mtime_ns, size)：通知已读/鉴权标记/清理器
# 都会在一次运行里多次读取 state.json，缓存可避免重复读盘 + JSON 解析。
_state_cache: tuple[tuple[str, int, int], dict[str, Any]] | None = None


def mark_auth_needed(source: str = "") -> None:
    """记录「Canvas 登录已过期」，供 GUI 启动时醒目提示。

    这是「不再静默失败」的关键：后台同步失败时把信号写进状态，
    而不是只留一条没人会看的 sync.log。
    """
    state = load_state()
    state["auth_needed"] = {"at": now_local().isoformat(timespec="seconds"), "source": source}
    save_state(state)


def clear_auth_needed() -> bool:
    """登录成功后清掉过期标记；本来就没有时直接返回，不写盘。"""
    state = load_state()
    if not state.pop("auth_needed", None):
        return False
    save_state(state)
    return True


def zone_or_none(name: str) -> tzinfo | None:
    """按 IANA 名取时区；取不到就返回 None，**绝不抛异常**。

    Windows 自身不带 IANA 时区库，全靠依赖里的 ``tzdata``。打包漏带、或用户的
    环境装坏了，模块级的 ``ZoneInfo(...)`` 就会在 import 阶段把整个程序打死——
    所以凡是「可能拿不到」的地方都走这里，最差退化成「跟随系统时区」。
    """
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except Exception:  # ZoneInfoNotFoundError / ValueError 等
        return None


# 兼容别名：历史常量（香港时区）。新代码请用 now_local / to_local——它们跟随
# 配置项 timezone，换学校时才不会把截止时间算错几个小时。
# 取不到时区库时为 None：只影响这个别名，主流程照旧走系统时区。
HK = zone_or_none("Asia/Hong_Kong")

#: 时区解析缓存：(配置里的时区名, 解析结果)。None 表示「跟随系统本地时区」。
_tz_cache: tuple[str, tzinfo | None] | None = None


def tz() -> tzinfo | None:
    """配置指定的时区；``None`` 表示「跟随本机系统时区」。

    配置项 ``timezone`` 写 IANA 名（如 ``America/New_York``）时返回对应
    ``ZoneInfo``；留空则返回 None，由 :func:`to_local` / :func:`now_local` 走
    系统本地时区（``astimezone()`` 无参形式，夏令时由系统自己处理）。
    名称非法时回落系统时区并记一条警告，绝不让同步崩掉。
    """
    global _tz_cache
    name = cfg_str("timezone")
    if _tz_cache is not None and _tz_cache[0] == name:
        return _tz_cache[1]
    zone = zone_or_none(name)
    if name and zone is None:
        get_logger().warning("时区配置无效（%s），改用系统时区", name)
    _tz_cache = (name, zone)
    return zone


def to_local(dt: datetime, zone: tzinfo | None = None) -> datetime:
    """把带时区的时间转换到本地时区（naive 视为 UTC）。

    配了 ``timezone`` 就按它转；没配则按系统本地时区转（夏令时自动处理）。
    批量处理时可以先取一次 :func:`tz` 再传进来，省掉重复读配置。
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    if zone is None:
        zone = tz()
    return dt.astimezone(zone) if zone is not None else dt.astimezone()


def now_local() -> datetime:
    """当前时间（本地时区，取决于配置 ``timezone``）。"""
    return to_local(datetime.now(UTC))


def now_hk() -> datetime:
    """兼容别名：等同 :func:`now_local`。

    历史名（now_hk / HK）保留只为不破坏既有调用，新代码一律用 now_local / to_local。
    """
    return now_local()


def ensure_dirs() -> None:
    for d in (DATA_DIR, PROFILE_DIR):
        with suppress(OSError):
            d.mkdir(parents=True, exist_ok=True)
    _bootstrap()  # 建目录 + 搬 v1.0 之前留在源码目录的旧数据（只在入口执行）
