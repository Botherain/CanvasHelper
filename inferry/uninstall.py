"""一键卸载：删掉程序目录与使用数据，**永远不动课程文件**。

三种入口共用同一套语义：

* Release zip 里附带的 ``uninstall.bat`` + ``uninstall.ps1``（打包版用户，无需 Python）；
* 命令行 ``python -m inferry uninstall [--yes] [--keep-data]``（源码用户）；
* 本模块的函数（供上面两者复用与离线测试）。

删除范围严格限定为「我们自己造的东西」：

1. 程序目录（exe 与 ``_internal``）；
2. 使用数据 ``%LOCALAPPDATA%\\Inferry``（配置、登录态、日志、更新缓存…），
   以及 1.x 的旧目录 ``%LOCALAPPDATA%\\CanvasHelper``（存在才删，避免升级后残留）；
3. 计划任务：新名两条 + 1.x 旧名两条（升级后立刻卸载也清得干净）；
4. 桌面快捷方式 ``Inferry.lnk`` 与旧的 ``CanvasHelper.lnk``（存在才删）。

**不删**：课程文件夹（``classes_root``）、DeskBox 里的待办、你放在程序目录里的任何
其他文件（脚本调用方只删程序目录本身，父目录一旦还有别人的东西就整个保留）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

from .paths import DATA_DIR, LEGACY_DATA_DIR, PROJECT_DIR, get_logger, load_config

log = get_logger()

#: 计划任务名。**新名 + 旧名兜底**：升级到 2.0.0 后立刻卸载，也必须把 1.x 留下的
#: 两条任务清掉，否则「任务计划程序」里会永远留着两条跑不起来的僵尸任务。
TASKS: tuple[str, ...] = (
    "Inferry-DailySync", "Inferry-WeeklyReport",
    "CanvasHelper-DailySync", "CanvasHelper-WeeklyReport",
)
#: 卸载器自己的文件名：父目录里只剩这些时，才允许把父目录也删掉
OWN_FILES = {"readme.txt", "uninstall.bat", "uninstall.ps1"}
#: 桌面快捷方式文件名（旧名一并清理）
LNK_NAMES: tuple[str, ...] = ("Inferry.lnk", "CanvasHelper.lnk")


@dataclass
class Plan:
    """要删什么、留什么（``dry_run`` 与界面提示都用它）。"""

    app_dir: Path | None
    data_dirs: list[Path]
    tasks: tuple[str, ...]
    shortcuts: list[Path] = field(default_factory=list)
    classes_root: str = ""
    keep_data: bool = False
    extra: list[str] = field(default_factory=list)

    @property
    def data_size(self) -> int:
        return sum(_tree_size(d) for d in self.data_dirs)


def _tree_size(root: Path) -> int:
    total = 0
    try:
        for path in root.rglob("*"):
            if path.is_file():
                with suppress(OSError):
                    total += path.stat().st_size
    except OSError:
        return 0
    return total


def desktop_dir() -> Path:
    """桌面目录。OneDrive 重定向时 USERPROFILE\\Desktop 会不存在，退回用注册表同款 API。"""
    candidate = Path(os.environ.get("USERPROFILE") or str(Path.home())) / "Desktop"
    if candidate.is_dir():
        return candidate
    with suppress(Exception):
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "[Environment]::GetFolderPath('Desktop')"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15, check=False,
        )
        path = Path((out.stdout or "").strip())
        if str(path) and path.is_dir():
            return path
    return candidate


def app_dir_of(exe: Path | None = None) -> Path | None:
    """当前程序所在目录；源码运行返回 None（源码没有「程序目录」可删）。"""
    if not getattr(sys, "frozen", False):
        return None
    return Path(exe or sys.executable).resolve().parent


def data_dirs() -> list[Path]:
    """要删除的数据目录：当前名字 + 1.x 旧名字（迁移还没跑过时数据仍在旧目录里）。"""
    out = [Path(DATA_DIR)]
    legacy = Path(LEGACY_DATA_DIR)
    if legacy != out[0] and legacy.is_dir():
        out.append(legacy)
    return out


def plan(*, keep_data: bool = False, app_dir: Path | None = None) -> Plan:
    """算出卸载计划（不碰磁盘）。"""
    target = Path(app_dir) if app_dir is not None else app_dir_of()
    links = [desktop_dir() / name for name in LNK_NAMES]
    classes = ""
    with suppress(Exception):
        classes = str(load_config().get("classes_root") or "")
    return Plan(
        app_dir=target,
        data_dirs=data_dirs(),
        tasks=TASKS,
        shortcuts=[p for p in links if p.is_file()],
        classes_root=classes,
        keep_data=keep_data,
    )


def render(plan_: Plan) -> str:
    """给用户看的「将删除 / 将保留」清单。"""
    lines = ["将删除："]
    if plan_.app_dir is not None:
        lines.append(f"  · 程序目录   {plan_.app_dir}")
    if not plan_.keep_data:
        size = plan_.data_size
        hint = f"（约 {size / 1048576:.1f} MB）" if size else ""
        lines.extend(f"  · 使用数据   {d}{hint}" for d in plan_.data_dirs)
    else:
        lines.append("  · 使用数据   保留（按 --keep-data / KEEP 执行）")
    lines.append("  · 计划任务   " + "、".join(plan_.tasks))
    lines.extend(f"  · 桌面快捷方式 {lnk.name}" for lnk in plan_.shortcuts)
    lines.append("")
    lines.append("将保留：")
    if plan_.classes_root:
        lines.append(f"  · 课程文件   {plan_.classes_root}（不会被删除）")
    lines.append("  · DeskBox 里的待办（属于你的数据，不碰）")
    return "\n".join(lines)


def remove_tasks() -> list[str]:
    """删除计划任务（新名 + 旧名），返回实际删掉的名字（不存在不算失败）。"""
    removed: list[str] = []
    for name in TASKS:
        proc = subprocess.run(
            ["schtasks", "/Delete", "/TN", name, "/F"],
            capture_output=True, text=True, encoding="locale", errors="replace",
            timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
        text = ((proc.stdout or "") + (proc.stderr or "")).lower()
        missing = "cannot find" in text or "找不到" in text or "does not exist" in text
        if proc.returncode == 0:
            removed.append(name)
        elif not missing:
            log.warning("删除计划任务 %s 失败：%s", name, text.strip()[:200])
    return removed


def remove_shortcut(path: Path | None) -> bool:
    if path is None or not path.is_file():
        return False
    try:
        path.unlink()
    except OSError as exc:
        log.warning("删除快捷方式失败: %s", exc)
        return False
    return True


def stop_app(app_dir: Path | None) -> bool:
    """结束正在运行的本程序（按 exe 全路径匹配，绝不误杀其它进程）。"""
    if app_dir is None:
        return False
    exes = [p for p in app_dir.glob("*.exe") if p.is_file()]
    stopped = False
    for exe in exes:
        with suppress(OSError, subprocess.SubprocessError):
            proc = subprocess.run(
                ["taskkill", "/IM", exe.name, "/F"],
                capture_output=True, text=True, encoding="locale", errors="replace",
                timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
            if proc.returncode == 0:
                stopped = True
    return stopped


def remove_path(path: Path | None) -> bool:
    """删目录或文件；不存在也算成功。"""
    if path is None:
        return False
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=False)
            return True
        if path.is_file():
            path.unlink()
            return True
    except OSError as exc:
        log.warning("删除 %s 失败: %s", path, exc)
    return False


def leftover_only_ours(directory: Path) -> bool:
    """目录里是否只剩卸载器自己的几个文件（才允许连目录一起删）。"""
    try:
        names = {p.name.lower() for p in directory.iterdir()}
    except OSError:
        return False
    return names <= OWN_FILES and bool(names)


def schedule_self_delete(directory: Path) -> Path | None:
    """写一个延时自删脚本（等本进程退出后再 rmdir），返回脚本路径。

    只在「目录里只剩我们自己的文件」时调用；否则会把用户的文件夹一起删掉。
    """
    if not leftover_only_ours(directory):
        return None
    import tempfile

    script = Path(tempfile.gettempdir()) / f"ch-uninstall-{os.getpid()}.cmd"
    body = (
        "@echo off\r\n"
        "ping -n 3 127.0.0.1 >nul\r\n"
        f'rmdir /s /q "{directory}"\r\n'
        'del "%~f0"\r\n'
    )
    try:
        # 纯 ASCII + 系统代码页：cmd 读批处理不看 UTF-8
        script.write_text(body, encoding="ascii", errors="ignore")
    except OSError as exc:
        log.warning("写自删脚本失败: %s", exc)
        return None
    return script


def launch_self_delete(script: Path) -> bool:
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
        subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.Popen(["cmd.exe", "/c", str(script)],
                         creationflags=flags, close_fds=True)
    except OSError as exc:
        log.warning("启动自删脚本失败: %s", exc)
        return False
    return True


def run(
    *,
    keep_data: bool = False,
    dry_run: bool = False,
    app_dir: Path | None = None,
    progress: Callable[[str], None] = lambda m: None,
) -> dict:
    """执行卸载，返回统计。``dry_run=True`` 时只算计划、不动磁盘。"""
    plan_ = plan(keep_data=keep_data, app_dir=app_dir)
    report: dict = {
        "app": str(plan_.app_dir) if plan_.app_dir else "",
        "data": [] if keep_data else [str(d) for d in plan_.data_dirs],
        "tasks": [],
        "shortcuts": [],
        "stopped": False,
        "self_deleted": "",
        "errors": [],
        "dry_run": dry_run,
    }
    if dry_run:
        return report

    if plan_.app_dir is not None:
        report["stopped"] = stop_app(plan_.app_dir)
        if report["stopped"]:
            progress("已结束正在运行的程序")
    try:
        report["tasks"] = remove_tasks()
    except Exception as exc:  # 计划任务删不掉不该挡住后面的清理
        report["errors"].append(f"计划任务：{exc}")
    for lnk in plan_.shortcuts:
        if remove_shortcut(lnk):
            report["shortcuts"].append(str(lnk))
    if not keep_data:
        for d in plan_.data_dirs:
            if remove_path(d):
                progress(f"已删除使用数据：{d}")
    if plan_.app_dir is not None:
        parent = plan_.app_dir.parent
        if remove_path(plan_.app_dir):
            progress("已删除程序目录")
        # 只有父目录里只剩卸载器自己的文件时，才把父目录也删掉
        script = schedule_self_delete(parent)
        if script is not None and launch_self_delete(script):
            report["self_deleted"] = str(parent)
    return report


def main(argv: list[str] | None = None) -> int:
    """``python -m inferry uninstall`` 的实现。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    keep_data = "--keep-data" in argv
    assume_yes = "--yes" in argv or "-y" in argv
    plan_ = plan(keep_data=keep_data)
    print(render(plan_))
    print()
    if not assume_yes:
        try:
            answer = input("确认卸载？输入 yes 继续（输入 keep 只删程序、保留数据）：").strip().lower()
        except EOFError:
            answer = ""
        if answer == "keep":
            keep_data = True
            plan_.keep_data = True
            print(render(plan_))
        elif answer != "yes":
            print("已取消，未做任何改动。")
            return 1
    report = run(keep_data=keep_data, progress=print)
    print()
    print("卸载完成：")
    if report["app"]:
        print(f"  · 程序目录   {report['app']}")
    if report["data"]:
        for d in report["data"]:
            print(f"  · 使用数据   {d}")
    else:
        print("  · 使用数据   已保留")
    if report["tasks"]:
        print("  · 计划任务   " + "、".join(report["tasks"]))
    for lnk in report["shortcuts"]:
        print(f"  · 快捷方式   {lnk}")
    if report["self_deleted"]:
        print(f"  · 剩余文件   {report['self_deleted']}（几秒后自动删除）")
    for err in report["errors"]:
        print(f"  ! {err}")
    print("\n课程文件与 DeskBox 待办没有被删除。")
    return 0


def project_dir() -> Path:
    """源码根目录（给测试与提示用）。"""
    return PROJECT_DIR
