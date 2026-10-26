"""创建桌面快捷方式（不依赖仓库里的任何脚本）。

Windows 的 ``.lnk`` 只能通过 COM（``WScript.Shell``）创建，而本项目运行时
**零第三方依赖**，所以这里直接调用系统自带的 PowerShell 执行一段临时脚本——
和「一键更新」用的是同一条路（临时 ``.ps1`` + ``-File``），中文路径也不会
因为代码页问题被破坏。

打包版指向 ``CanvasHelper.exe``（图标用它自带的），源码版指向 ``pythonw.exe``
+ ``CanvasHelper.pyw``（无控制台）或 ``-m canvas_helper gui``。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from .paths import PROJECT_DIR, get_logger

log = get_logger()

#: 快捷方式显示名（ASCII，避免在 .ps1 里写中文；桌面上的 .lnk 文件名就是它）
LNK_NAME = "CanvasHelper"
LNK_DESCRIPTION = "CanvasHelper - Canvas course sync assistant"


@dataclass
class Target:
    """快捷方式要启动什么。"""

    path: str
    args: str
    workdir: str
    icon: str = ""


def is_frozen() -> bool:
    """是不是 PyInstaller 打包出来的 exe。"""
    return bool(getattr(sys, "frozen", False))


def target_for(
    exe: Path | None = None,
    project_dir: Path | None = None,
    *,
    frozen: bool | None = None,
) -> Target:
    """算出快捷方式的目标。

    - 打包版：直接指 exe，参数为空，图标取 exe 自带资源；
    - 源码版：优先 ``pythonw.exe`` + ``CanvasHelper.pyw``（双击无控制台）；
      仓库里没有那个文件（例如 pip 装进 site-packages）时退回
      ``-m canvas_helper gui``。
    """
    packed = is_frozen() if frozen is None else frozen
    if packed:
        exe_path = Path(exe or sys.executable).resolve()
        return Target(
            path=str(exe_path),
            args="",
            workdir=str(exe_path.parent),
            icon=f"{exe_path},0",
        )

    root = Path(project_dir or PROJECT_DIR).resolve()
    pyw = root / "CanvasHelper.pyw"
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    interpreter = pythonw if pythonw.is_file() else Path(sys.executable)
    icon = root / "docs" / "icons" / "canvashelper.ico"
    return Target(
        path=str(interpreter),
        args=f'"{pyw}"' if pyw.is_file() else "-X utf8 -m canvas_helper gui",
        workdir=str(root),
        icon=f"{icon},0" if icon.is_file() else "",
    )


def ps_quote(text: str | Path) -> str:
    """PowerShell 单引号字符串：内部的单引号改成两个。"""
    return "'" + str(text).replace("'", "''") + "'"


def build_ps1(target: Target, name: str = LNK_NAME) -> str:
    """生成创建快捷方式的 PowerShell 脚本内容（纯函数，便于离线断言）。"""
    lines = [
        "# CanvasHelper 桌面快捷方式（由程序生成，可随时删除后重建）",
        "$ErrorActionPreference = 'Stop'",
        "$desktop = [Environment]::GetFolderPath('Desktop')",
        f"$lnkPath = Join-Path $desktop {ps_quote(name + '.lnk')}",
        "$shell = New-Object -ComObject WScript.Shell",
        "$lnk = $shell.CreateShortcut($lnkPath)",
        f"$lnk.TargetPath = {ps_quote(target.path)}",
        f"$lnk.Arguments = {ps_quote(target.args)}",
        f"$lnk.WorkingDirectory = {ps_quote(target.workdir)}",
    ]
    if target.icon:
        lines.append(f"$lnk.IconLocation = {ps_quote(target.icon)}")
    lines += [
        f"$lnk.Description = {ps_quote(LNK_DESCRIPTION)}",
        "$lnk.Save()",
        "if (-not (Test-Path -LiteralPath $lnkPath)) { throw 'shortcut not created' }",
        "Write-Output $lnkPath",
    ]
    return "\n".join(lines) + "\n"


def create(
    target: Target | None = None,
    *,
    name: str = LNK_NAME,
    timeout: float = 30.0,
) -> tuple[bool, str]:
    """创建（或覆盖）桌面快捷方式，返回 ``(是否成功, 说明)``。

    失败不抛异常：调用方据此提示用户改用「右键 exe → 发送到 → 桌面快捷方式」。
    """
    shell = shutil.which("powershell") or shutil.which("pwsh")
    if not shell:
        return False, "找不到 PowerShell，无法创建快捷方式"
    tgt = target or target_for()
    script_dir = Path(tempfile.mkdtemp(prefix="ch_lnk_"))
    script = script_dir / "make-shortcut.ps1"
    try:
        # 带 BOM：Windows PowerShell 5.1 默认按 ANSI 读 .ps1，中文路径会乱码
        script.write_text(build_ps1(tgt, name), encoding="utf-8-sig")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.run(
            [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace", creationflags=flags, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("创建快捷方式失败: %s", exc)
        return False, f"创建快捷方式失败：{exc}"
    finally:
        with suppress(OSError):
            shutil.rmtree(script_dir, ignore_errors=True)

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0 or not out:
        detail = err or out or f"退出码 {proc.returncode}"
        log.warning("创建快捷方式失败: %s", detail)
        return False, f"创建快捷方式失败：{detail}"
    log.info("桌面快捷方式已创建：%s", out)
    return True, out
