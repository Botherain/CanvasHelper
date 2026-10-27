"""Windows 计划任务注册（每日同步 / 每周摘要）。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .paths import PROJECT_DIR, get_logger

log = get_logger()

TASK_SYNC = "Inferry-DailySync"
TASK_REPORT = "Inferry-WeeklyReport"

#: 1.x（CanvasHelper）留下的任务名。改名后它们指向的旧 exe 已经不存在，
#: 留着只会每天到点静默失败，所以启动时按名字删掉（找不到不算错）。
LEGACY_TASKS: tuple[str, ...] = ("CanvasHelper-DailySync", "CanvasHelper-WeeklyReport")


def _pythonw() -> str:
    """同目录的 ``pythonw.exe``（无控制台解释器）；找不到才退回 ``sys.executable``。

    退回时计划任务会重新变成一个控制台进程，但总比注册不上去强。
    """
    w = Path(sys.executable).with_name("pythonw.exe")
    return str(w) if w.exists() else sys.executable


def _cmd(sub: str) -> str:
    # 从前是：cmd /c cd /d "<项目目录>" && "<python.exe>" -m inferry ...
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
    #
    # 打包版（PyInstaller）另走一条：exe 自己就是无窗口应用，直接把子命令
    # 交给它即可。脚本目录并不在包里，旧写法注册出来的是一个**死任务**
    # （exe 收到一个不存在的 .py 路径，只会打印用法后退出 2）。
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" {sub}'
    entry = PROJECT_DIR / "scripts" / "headless_entry.py"
    return f'"{_pythonw()}" "{entry}" {sub}'


def _commands() -> dict[str, str]:
    """当前这台机器上「正确的」两条计划任务命令。"""
    return {"sync": _cmd("sync --headless"), "report": _cmd("report --headless")}


def _remember() -> None:
    """把注册时用的命令记进 state，供下次启动比对（换位置后要重建）。"""
    from .paths import load_state, save_state

    state = load_state()
    state["schedule_cmd"] = _commands()
    save_state(state)


def moved() -> bool:
    """计划任务里记下的命令与当前是否一致。

    换过安装位置、把源码版换成 exe（或反过来）都会不一致——此时旧任务指向
    一个不存在的路径，会**静默失败**，所以启动时要重建一次。
    """
    from .paths import load_state

    stored = (load_state() or {}).get("schedule_cmd") or {}
    return any(stored.get(kind) != cmd for kind, cmd in _commands().items())


def refresh(schedule: dict) -> list[str]:
    """按当前程序路径重建已启用的任务，返回给界面看的日志行。"""
    lines: list[str] = []
    ok, msg = create_daily(schedule.get("sync_time", "08:30"))
    lines.append("每日同步已按当前程序路径重建" if ok else f"每日同步重建失败：{msg}")
    if schedule.get("report_enabled", True):
        ok2, msg2 = create_weekly(
            schedule.get("report_weekday", "MON"), schedule.get("report_time", "09:00"))
        lines.append("每周摘要已按当前程序路径重建" if ok2 else f"每周摘要重建失败：{msg2}")
    if ok:
        _remember()
    return lines


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


def _missing_task(msg: str) -> bool:
    """schtasks 说「找不到这个任务」的几种写法（中英文系统各不同）。"""
    text = (msg or "").lower()
    return "cannot find" in text or "找不到" in text or "does not exist" in text


def cleanup_legacy() -> list[str]:
    """删除 1.x 留下的 ``CanvasHelper-*`` 任务，返回实际删掉的名字。

    改名后旧任务里的 exe 路径已经不存在，不删的话「任务计划程序」里会永远留两条
    跑一次失败一次的僵尸任务。删掉之后，``state.schedule_cmd`` 里记的旧命令自然
    与当前不一致，1.3.2 起就有的自愈逻辑会按新名字、新路径重建。
    """
    removed: list[str] = []
    for name in LEGACY_TASKS:
        ok, msg = remove(name)
        if ok:
            removed.append(name)
        elif not _missing_task(msg):
            log.warning("清理旧计划任务 %s 失败：%s", name, msg)
    if removed:
        log.info("已清理旧版计划任务：%s", "、".join(removed))
    return removed


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
    if ok1 and ok2:
        _remember()
    return ok1 and ok2, "\n".join(msgs)


def disable() -> tuple[bool, str]:
    msgs = []
    ok = True
    for t in (TASK_SYNC, TASK_REPORT):
        o, m = remove(t)
        missing = _missing_task(m)
        ok = ok and (o or missing)
        msgs.append(f"{t}: {'已删除' if o else ('本就不存在' if missing else m)}")
    return ok, "\n".join(msgs)
