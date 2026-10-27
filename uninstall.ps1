<#
Inferry 一键卸载（Windows PowerShell）

删除什么：
  · 程序目录（默认：本脚本旁边的 Inferry\，兼容 1.x 的 CanvasHelper\）
  · 使用数据（默认：%LOCALAPPDATA%\Inferry —— 配置、登录态、日志、更新缓存；
    1.x 留下的 %LOCALAPPDATA%\CanvasHelper 也一并清掉，避免升级后残留）
  · 计划任务：Inferry-DailySync / -WeeklyReport 与 1.x 的 CanvasHelper-* 两条
  · 桌面快捷方式 Inferry.lnk 与 1.x 的 CanvasHelper.lnk（存在才删）

不删什么：
  · 课程文件夹（classes_root）
  · DeskBox 里的待办
  · 解压目录里不属于本程序的其它文件（只有只剩我们自己的文件时才会连目录一起删）

用法：
  双击 uninstall.bat                    交互确认
  powershell -File uninstall.ps1 -Yes   不询问（脚本/自动化用）
  powershell -File uninstall.ps1 -KeepData   只删程序，保留使用数据
#>
param(
    [switch]$Yes,
    [switch]$KeepData,
    [string]$AppDir = "",
    # 逗号分隔；留空 = 当前名 + 1.x 旧名
    [string]$DataDir = "",
    # 逗号分隔；留空 = Inferry.lnk + CanvasHelper.lnk
    [string]$Shortcut = "",
    # 测试用：逗号分隔的任务名；留空 = 真实的四条计划任务（新名 + 旧名）
    [string]$Tasks = ""
)

$ErrorActionPreference = 'Continue'
$script:ours = @('readme.txt', 'uninstall.bat', 'uninstall.ps1')
# 1.x 的名字：升级到 2.0.0 后立刻卸载，也要把这些残留一并清干净
$script:legacyName = 'CanvasHelper'

function Write-Head([string]$text) { Write-Host $text -ForegroundColor Cyan }
function Write-Warn([string]$text) { Write-Host $text -ForegroundColor Yellow }
function Write-Ok([string]$text) { Write-Host $text -ForegroundColor Green }

function Split-List([string]$text) {
    @($text.Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ })
}

# ---- 参数默认值 ---------------------------------------------------------
if (-not $AppDir) {
    foreach ($name in @('Inferry', $script:legacyName)) {
        $candidate = Join-Path $PSScriptRoot $name
        if (Test-Path -LiteralPath $candidate) { $AppDir = $candidate; break }
    }
    if (-not $AppDir) {
        # 兜底：找「唯一一个里面有 exe 的子目录」，名字怎么改都能对上
        $dirs = @(Get-ChildItem -LiteralPath $PSScriptRoot -Directory -ErrorAction SilentlyContinue |
            Where-Object {
                @(Get-ChildItem -LiteralPath $_.FullName -Filter *.exe -File -ErrorAction SilentlyContinue).Count -gt 0
            })
        if ($dirs.Count -eq 1) { $AppDir = $dirs[0].FullName }
    }
}
if (-not $DataDir) {
    $local = [Environment]::GetFolderPath('LocalApplicationData')
    $DataDir = (@('Inferry', $script:legacyName) |
        ForEach-Object { Join-Path $local $_ } |
        Where-Object { Test-Path -LiteralPath $_ }) -join ','
    if (-not $DataDir) { $DataDir = Join-Path $local 'Inferry' }
}
$dataDirs = Split-List $DataDir
if (-not $Shortcut) {
    $desktop = [Environment]::GetFolderPath('Desktop')
    $Shortcut = (Join-Path $desktop 'Inferry.lnk') + ',' + (Join-Path $desktop ($script:legacyName + '.lnk'))
}
$shortcuts = Split-List $Shortcut
$tasks = @('Inferry-DailySync', 'Inferry-WeeklyReport',
    ($script:legacyName + '-DailySync'), ($script:legacyName + '-WeeklyReport'))
if ($Tasks) { $tasks = Split-List $Tasks }

# 从配置里读课程目录，仅用于提示「不会被删除」
$classesRoot = ''
foreach ($dir in $dataDirs) {
    $configPath = Join-Path $dir 'config.json'
    if ($classesRoot -or -not (Test-Path -LiteralPath $configPath)) { continue }
    try {
        $cfg = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $classesRoot = [string]$cfg.classes_root
    } catch { }
}

# ---- 展示将删除 / 将保留 ------------------------------------------------
Write-Head '=== Inferry 卸载 ==='
Write-Host '将删除：'
if ($AppDir -and (Test-Path -LiteralPath $AppDir)) { Write-Host "  · 程序目录     $AppDir" }
if (-not $KeepData) {
    foreach ($dir in $dataDirs) { Write-Host "  · 使用数据     $dir" }
} else {
    Write-Host '  · 使用数据     保留'
}
Write-Host ("  · 计划任务     " + ($tasks -join '、'))
foreach ($lnk in $shortcuts) {
    if (Test-Path -LiteralPath $lnk) { Write-Host "  · 桌面快捷方式 $lnk" }
}
Write-Host ''
Write-Host '将保留：'
if ($classesRoot) { Write-Host "  · 课程文件     $classesRoot" }
Write-Host '  · DeskBox 里的待办'
Write-Host ''

if (-not $Yes) {
    $answer = Read-Host '确认卸载？输入 YES 继续，输入 KEEP 只删程序并保留使用数据'
    switch ($answer.Trim().ToUpper()) {
        'YES' { }
        'KEEP' { $KeepData = $true }
        default {
            Write-Host '已取消，未做任何改动。'
            Read-Host '按回车退出' | Out-Null
            exit 1
        }
    }
}

# ---- 1) 结束正在运行的本程序（按 exe 全路径匹配，不误杀别的进程） -------
if ($AppDir -and (Test-Path -LiteralPath $AppDir)) {
    Get-Process -ErrorAction SilentlyContinue | ForEach-Object {
        $proc = $_
        try {
            if ($proc.Path -and $proc.Path.StartsWith($AppDir, 'OrdinalIgnoreCase')) {
                Write-Host "  结束进程 $($proc.ProcessName)（PID $($proc.Id)）"
                Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
            }
        } catch { }
    }
    Start-Sleep -Milliseconds 600
}

# ---- 2) 计划任务 --------------------------------------------------------
foreach ($task in $tasks) {
    & schtasks.exe /Delete /TN $task /F *> $null
    if ($LASTEXITCODE -eq 0) { Write-Host "  已删除计划任务 $task" }
}

# ---- 3) 桌面快捷方式 ----------------------------------------------------
foreach ($lnk in $shortcuts) {
    if (Test-Path -LiteralPath $lnk) {
        Remove-Item -LiteralPath $lnk -Force -ErrorAction SilentlyContinue
        if (-not (Test-Path -LiteralPath $lnk)) { Write-Host "  已删除桌面快捷方式 $lnk" }
    }
}

# ---- 4) 使用数据 --------------------------------------------------------
if (-not $KeepData) {
    foreach ($dir in $dataDirs) {
        if (-not (Test-Path -LiteralPath $dir)) { continue }
        Remove-Item -LiteralPath $dir -Recurse -Force -ErrorAction SilentlyContinue
        if (Test-Path -LiteralPath $dir) {
            Write-Warn "  ! 使用数据没能完全删除（可能被占用）：$dir"
        } else {
            Write-Ok "  已删除使用数据（配置 / 登录态 / 日志）：$dir"
        }
    }
}

# ---- 5) 程序目录 --------------------------------------------------------
$removedApp = $false
if ($AppDir -and (Test-Path -LiteralPath $AppDir)) {
    Remove-Item -LiteralPath $AppDir -Recurse -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $AppDir) {
        Write-Warn "  ! 程序目录没能删除（可能还在运行）：$AppDir"
    } else {
        Write-Ok '  已删除程序目录'
        $removedApp = $true
    }
}

# ---- 6) 只剩我们自己的文件时，连解压目录一起删（延时到本进程退出后） ----
if ($removedApp -and $PSScriptRoot) {
    $dir = (Resolve-Path -LiteralPath $PSScriptRoot -ErrorAction SilentlyContinue)
    if ($dir) {
        $dirPath = $dir.Path
        $others = @(Get-ChildItem -LiteralPath $dirPath -Force -ErrorAction SilentlyContinue |
            Where-Object { $script:ours -notcontains $_.Name.ToLower() })
        if ($others.Count -eq 0) {
            $tmp = Join-Path $env:TEMP ('ch-uninstall-' + [guid]::NewGuid().ToString('N') + '.cmd')
            $body = "@echo off`r`nping -n 3 127.0.0.1 >nul`r`nrmdir /s /q `"$dirPath`"`r`ndel `"%~f0`"`r`n"
            try {
                [IO.File]::WriteAllText($tmp, $body, [Text.Encoding]::ASCII)
                Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', "`"$tmp`"" -WindowStyle Hidden
                Write-Host "  解压目录 $dirPath 将在几秒后自动删除"
            } catch {
                Write-Warn "  请手动删除目录：$dirPath"
            }
        } else {
            Write-Host "  其余文件已保留（$dirPath）"
        }
    }
}

Write-Host ''
Write-Ok '卸载完成。课程文件与 DeskBox 待办没有被删除。'
if (-not $Yes) { Read-Host '按回车退出' | Out-Null }
exit 0
