"""创建桌面快捷方式（不依赖仓库里的任何脚本）。

Windows 的 ``.lnk`` 只能通过 COM（``WScript.Shell``）创建，而本项目运行时
**零第三方依赖**，所以这里直接调用系统自带的 PowerShell 执行一段临时脚本——
和「一键更新」用的是同一条路（临时 ``.ps1`` + ``-File``），中文路径也不会
因为代码页问题被破坏。

打包版指向 ``Inferry.exe``（图标用它自带的），源码版指向 ``pythonw.exe``
+ ``Inferry.pyw``（无控制台）或 ``-m inferry gui``。
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
LNK_NAME = "Inferry"
LNK_DESCRIPTION = "Inferry - Canvas course sync assistant"
#: 1.x 留下的快捷方式名：它指向已经不存在（被镜像替换掉）的 CanvasHelper.exe
LEGACY_LNK_NAME = "CanvasHelper.lnk"


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
    - 源码版：优先 ``pythonw.exe`` + ``Inferry.pyw``（双击无控制台）；
      仓库里没有那个文件（例如 pip 装进 site-packages）时退回
      ``-m inferry gui``。
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
    pyw = root / "Inferry.pyw"
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    interpreter = pythonw if pythonw.is_file() else Path(sys.executable)
    icon = root / "docs" / "icons" / "inferry.ico"
    return Target(
        path=str(interpreter),
        args=f'"{pyw}"' if pyw.is_file() else "-X utf8 -m inferry gui",
        workdir=str(root),
        icon=f"{icon},0" if icon.is_file() else "",
    )


def ps_quote(text: str | Path) -> str:
    """PowerShell 单引号字符串：内部的单引号改成两个。"""
    return "'" + str(text).replace("'", "''") + "'"


def build_ps1(target: Target, name: str = LNK_NAME) -> str:
    """生成创建快捷方式的 PowerShell 脚本内容（纯函数，便于离线断言）。"""
    lines = [
        "# Inferry 桌面快捷方式（由程序生成，可随时删除后重建）",
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


def _run_ps(body: str, timeout: float = 30.0) -> tuple[int, str, str]:
    """把一段 PowerShell 写进临时 ``.ps1`` 再执行，返回 ``(退出码, stdout, stderr)``。

    用 ``-File`` 而不是 ``-Command``：参数里可能带中文路径与引号，只有独立脚本文件
    才能稳定带 BOM（Windows PowerShell 5.1 默认按 ANSI 读，中文会乱码）。
    """
    shell = shutil.which("powershell") or shutil.which("pwsh")
    if not shell:
        return -1, "", "找不到 PowerShell"
    script_dir = Path(tempfile.mkdtemp(prefix="ch_lnk_"))
    script = script_dir / "shortcut.ps1"
    try:
        script.write_text(body, encoding="utf-8-sig")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.run(
            [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace", creationflags=flags, check=False,
        )
    finally:
        with suppress(OSError):
            shutil.rmtree(script_dir, ignore_errors=True)
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def create(
    target: Target | None = None,
    *,
    name: str = LNK_NAME,
    timeout: float = 30.0,
) -> tuple[bool, str]:
    """创建（或覆盖）桌面快捷方式，返回 ``(是否成功, 说明)``。

    失败不抛异常：调用方据此提示用户改用「右键 exe → 发送到 → 桌面快捷方式」。
    """
    tgt = target or target_for()
    try:
        code, out, err = _run_ps(build_ps1(tgt, name), timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("创建快捷方式失败: %s", exc)
        return False, f"创建快捷方式失败：{exc}"

    if code != 0 or not out:
        detail = err or out or f"退出码 {code}"
        log.warning("创建快捷方式失败: %s", detail)
        return False, f"创建快捷方式失败：{detail}"
    log.info("桌面快捷方式已创建：%s", out)
    return True, out


def build_remove_ps1(name: str = LEGACY_LNK_NAME) -> str:
    """生成「删掉旧快捷方式」的脚本（纯函数，便于离线断言）。

    只在**目标确实已经不在了**时才删：万一你机器上还留着一份能用的 1.3.4，
    那个快捷方式就仍然有意义，不该被顺手抹掉。
    """
    return "\n".join([
        "# 清理 1.x 留下的快捷方式（目标 exe 已随改名消失）",
        "$ErrorActionPreference = 'SilentlyContinue'",
        "$desktop = [Environment]::GetFolderPath('Desktop')",
        f"$lnkPath = Join-Path $desktop {ps_quote(name)}",
        "if (-not (Test-Path -LiteralPath $lnkPath)) { exit 0 }",
        "$shell = New-Object -ComObject WScript.Shell",
        "$lnk = $shell.CreateShortcut($lnkPath)",
        "$target = [string]$lnk.TargetPath",
        "if ($target -and (Test-Path -LiteralPath $target)) { exit 1 }",
        "Remove-Item -LiteralPath $lnkPath -Force",
        "if (Test-Path -LiteralPath $lnkPath) { exit 2 }",
        "Write-Output $lnkPath",
        "exit 0",
    ]) + "\n"


def remove_legacy(name: str = LEGACY_LNK_NAME, *, timeout: float = 20.0) -> Path | None:
    """删掉 1.x 的桌面快捷方式，返回被删掉的路径（没删任何东西返回 None）。

    退出码约定：0 = 已删或本来就没有，1 = 目标 exe 还在（保留），2 = 删除失败。
    """
    try:
        code, out, err = _run_ps(build_remove_ps1(name), timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        log.info("清理旧快捷方式跳过：%s", exc)
        return None
    if code == 1:
        log.info("旧版快捷方式仍指向存在的程序，保留未动：%s", name)
        return None
    if code != 0 or not out:
        if code not in (0, -1):
            log.info("清理旧快捷方式未成功（退出码 %s）：%s", code, err or out)
        return None
    log.info("已删除 1.x 留下的桌面快捷方式：%s", out)
    return Path(out)
