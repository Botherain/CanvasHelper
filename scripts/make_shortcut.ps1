# Create a desktop shortcut that starts the GUI directly with pythonw.exe and
# carries the custom icon.
#
# Why pythonw.exe and not the launcher .bat: a .bat always gets a console from
# Windows first, and that window only closes once the script is done - so every
# double click flashed a black box for ~0.2s. pythonw.exe has no console at
# all, so this shortcut starts the GUI with zero windows. The .bat stays
# available for people who want to see errors on screen.
#
# A .bat cannot hold an icon of its own (Windows gives it a fixed one), so a
# shortcut is also the supported place to hang a custom icon on the launcher.
#
# Deliberately pure ASCII: PowerShell 5.1 reads a .ps1 without BOM as ANSI
# (cp936 here), so any non-ASCII byte in this file could corrupt the syntax.
# The shortcut's display name is the fixed ASCII label "CanvasHelper", so no
# Chinese file name ever has to be spelled out in this file.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File make_shortcut.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File make_shortcut.ps1 -LnkPath "D:\somewhere\name.lnk"

param(
    # Empty means: the user's real desktop (handles OneDrive redirection).
    [string]$LnkPath = '',

    # Empty means: pythonw.exe found on PATH.
    [string]$Pythonw = '',

    # Empty means: docs\icons\canvashelper.ico at the project root.
    [string]$Icon = ''
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot

# -X utf8 is the same thing the .bat does with PYTHONUTF8=1.
$arguments = '-X utf8 -m canvas_helper gui'

if (-not $Pythonw) {
    $cmd = Get-Command pythonw.exe -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($cmd) { $Pythonw = $cmd.Source }
}
if (-not $Pythonw -or -not (Test-Path $Pythonw)) {
    Write-Output ('NO_PYTHONW ' + $Pythonw)
    exit 2
}

if (-not $Icon) {
    $Icon = Join-Path $root 'docs\icons\canvashelper.ico'
}
if (-not (Test-Path $Icon)) {
    Write-Output ('NO_ICON ' + $Icon)
    exit 5
}

# Fixed display name (ASCII); the .bat is not the target any more, so its
# Chinese name has nothing to do with the shortcut.
$name = 'CanvasHelper'

if (-not $LnkPath) {
    $desktop = [Environment]::GetFolderPath('Desktop')
    $LnkPath = Join-Path $desktop ($name + '.lnk')
}

$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($LnkPath)
$lnk.TargetPath = $Pythonw
$lnk.Arguments = $arguments
$lnk.WorkingDirectory = $root
# Index 0 = the first image inside the .ico.
$lnk.IconLocation = ($Icon + ',0')
$lnk.Description = 'CanvasHelper - Canvas course sync assistant'
$lnk.Save()

if (-not (Test-Path $LnkPath)) {
    Write-Output ('SAVE_FAILED ' + $LnkPath)
    exit 6
}

Write-Output ('OK ' + $LnkPath)
Write-Output ('    pythonw=' + $Pythonw)
Write-Output ('    arguments=' + $arguments)
Write-Output ('    icon=' + $Icon)
exit 0
