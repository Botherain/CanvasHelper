"""Windows 计划任务注册（每日同步 / 每周摘要）。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from .paths import PROJECT_DIR, get_logger

log = get_logger()

TASK_SYNC = "CanvasHelper-DailySync"
TASK_REPORT = "CanvasHelper-WeeklyReport"


def _python() -> str:
    return sys.executable


def _cmd(sub: str) -> str:
    # schtasks 没有「起始位置」参数，默认从 System32 起跑——那时既 import 不到源码目录
    # 的包（未 pip install 时必然 No module named canvas_helper），日志与相对路径也全错位。
    # 显式 cd 过去：源码运行与 pip 安装两种形态都能工作。
    return f'cmd /c cd /d "{PROJECT_DIR}" && "{_python()}" -m canvas_helper {sub}'


def _run(args: list[str]) -> tuple[bool, str]:
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=30,
                             encoding="locale", errors="replace", creationflags=0x08000000)
    except Exception as exc:
        log.error("schtasks 执行失败: %s", exc)
        return False, str(exc)
    text = (out.stdout or "") + (out.stderr or "")
    return out.returncode == 0, text.strip()


def create_daily(time_str: str = "08:30") -> tuple[bool, str]:
    ok, msg = _run([
        "schtasks", "/Create", "/TN", TASK_SYNC, "/TR", _cmd("sync --headless"),
        "/SC", "DAILY", "/ST", time_str, "/F",
    ])
    log.info("创建每日同步任务: %s %s", ok, msg)
    return ok, msg


def create_weekly(weekday: str = "MON", time_str: str = "09:00") -> tuple[bool, str]:
    ok, msg = _run([
        "schtasks", "/Create", "/TN", TASK_REPORT, "/TR", _cmd("report --headless"),
        "/SC", "WEEKLY", "/D", weekday.upper(), "/ST", time_str, "/F",
    ])
    log.info("创建每周摘要任务: %s %s", ok, msg)
    return ok, msg


def remove(name: str) -> tuple[bool, str]:
    return _run(["schtasks", "/Delete", "/TN", name, "/F"])


def status() -> dict[str, dict]:
    """只查询本程序自己的两个任务，避免全表 /V 查询拖慢启动。"""
    out: dict[str, dict] = {}
    for t in (TASK_SYNC, TASK_REPORT):
        ok, text = _run(["schtasks", "/Query", "/TN", t, "/FO", "LIST", "/V"])
        if ok and "ERROR" not in text.upper()[:200]:
            out[t] = {"name": t, "detail": text}
    return out


def enable(schedule: dict) -> tuple[bool, str]:
    msgs = []
    ok1, m1 = create_daily(schedule.get("sync_time", "08:30"))
    msgs.append(f"每日同步: {m1 or 'OK'}" if ok1 else f"每日同步失败: {m1}")
    ok2 = True
    if schedule.get("report_enabled", True):
        ok2, m2 = create_weekly(
            schedule.get("report_weekday", "MON"), schedule.get("report_time", "09:00")
        )
        msgs.append(f"每周摘要: {m2 or 'OK'}" if ok2 else f"每周摘要失败: {m2}")
    return ok1 and ok2, "\n".join(msgs)


def disable() -> tuple[bool, str]:
    msgs = []
    ok = True
    for t in (TASK_SYNC, TASK_REPORT):
        o, m = remove(t)
        missing = ("cannot find" in (m or "").lower()) or ("找不到" in (m or ""))
        ok = ok and (o or missing)
        msgs.append(f"{t}: {'已删除' if o else ('本就不存在' if missing else m)}")
    return ok, "\n".join(msgs)
