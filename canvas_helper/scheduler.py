"""Windows 计划任务注册（每日同步 / 每周摘要）。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .paths import PROJECT_DIR, get_logger

log = get_logger()

TASK_SYNC = "CanvasHelper-DailySync"
TASK_REPORT = "CanvasHelper-WeeklyReport"


def _pythonw() -> str:
    """同目录的 ``pythonw.exe``（无控制台解释器）；找不到才退回 ``sys.executable``。

    退回时计划任务会重新变成一个控制台进程，但总比注册不上去强。
    """
    w = Path(sys.executable).with_name("pythonw.exe")
    return str(w) if w.exists() else sys.executable


def _cmd(sub: str) -> str:
    # 从前是：cmd /c cd /d "<项目目录>" && "<python.exe>" -m canvas_helper ...
    #
    # 两个毛病，一起治：
    #
    # 1. 每天弹一个黑框。cmd 与 python.exe 都是控制台程序，schtasks 一到点
    #    就开一个窗口，而这条命令的唯一目的就是后台同步。pythonw.exe 没有
    #    控制台，但它也不会 cd。
    # 2. 起始目录必须对。schtasks 的 /Create 没有「起始目录」参数，默认从
    #    System32 起跑——那时未 pip install 的源码目录根本 import 不到包，
    #    过去只能靠 `cmd /c cd /d` 兜住，于是连带把控制台也带上了。
    #
    # 现在把「定位源码目录」挪进 scripts/headless_entry.py（用 __file__ 塞
    # sys.path），命令里就只剩一个无控制台解释器，起始目录从此无关紧要。
    entry = PROJECT_DIR / "scripts" / "headless_entry.py"
    return f'"{_pythonw()}" "{entry}" {sub}'


def _run(args: list[str]) -> tuple[bool, str]:
    try:
        # check=False 是刻意的：调用方按 rc 分支处理，失败信息靠下面的日志回报
        out = subprocess.run(args, capture_output=True, text=True, timeout=30,
                             encoding="locale", errors="replace", creationflags=0x08000000,
                             check=False)
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
