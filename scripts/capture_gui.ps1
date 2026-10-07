# Capture the CanvasHelper main window to a PNG for the README.
#
# Deliberately pure ASCII: PowerShell 5.1 reads a .ps1 without BOM as ANSI (cp936
# here), so any non-ASCII byte in this file could corrupt the syntax.
#
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File capture_gui.ps1 <out.png>

param(
    [Parameter(Mandatory = $true)]
    [string]$OutPath
)

$ErrorActionPreference = 'Stop'

Add-Type -AssemblyName System.Drawing

Add-Type @'
using System;
using System.Runtime.InteropServices;

public class Shot {
    [StructLayout(LayoutKind.Sequential)]
    public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }

    [DllImport("user32.dll")]
    public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);

    [DllImport("user32.dll")]
    public static extern bool SetForegroundWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

    [DllImport("user32.dll")]
    public static extern bool PrintWindow(IntPtr hWnd, IntPtr hdc, uint flags);

    [DllImport("user32.dll")]
    public static extern bool IsWindow(IntPtr hWnd);
}
'@

# Per-monitor DPI awareness so GetWindowRect and CopyFromScreen agree on pixels.
# Fails harmlessly if some other layer already set it.
try {
    Add-Type @'
using System.Runtime.InteropServices;
public class DpiAware {
    [DllImport("shcore.dll")]
    public static extern int SetProcessDpiAwareness(int value);
}
'@
    [void][DpiAware]::SetProcessDpiAwareness(2)
} catch { }

# --- locate the GUI process -------------------------------------------------
$target = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.CommandLine -like '*canvas_helper*gui*' -or $_.CommandLine -like '*canvas_helper gui*' } |
    Select-Object -First 1

if (-not $target) {
    Write-Output 'NO_PROCESS'
    exit 2
}

$gui = Get-Process -Id $target.ProcessId
$hwnd = $gui.MainWindowHandle
if ($hwnd -eq [IntPtr]::Zero -or -not [Shot]::IsWindow($hwnd)) {
    Write-Output ('NO_HANDLE pid=' + $target.ProcessId)
    exit 3
}

# --- raise it so CopyFromScreen has something to read -----------------------
[void][Shot]::ShowWindow($hwnd, 9)    # SW_RESTORE
[void][Shot]::SetForegroundWindow($hwnd)
Start-Sleep -Milliseconds 900

$rect = New-Object Shot+RECT
if (-not [Shot]::GetWindowRect($hwnd, [ref]$rect)) {
    Write-Output 'NO_RECT'
    exit 4
}

$w = $rect.Right - $rect.Left
$h = $rect.Bottom - $rect.Top
if ($w -lt 50 -or $h -lt 50) {
    Write-Output ('BAD_SIZE ' + $w + 'x' + $h)
    exit 5
}

$src = New-Object System.Drawing.Rectangle $rect.Left, $rect.Top, $w, $h
$bmp = New-Object System.Drawing.Bitmap $w, $h
$g = [System.Drawing.Graphics]::FromImage($bmp)

try {
    $g.CopyFromScreen($src, [System.Drawing.Point]::Empty, $src.Size)
} catch {
    # Fallback: render the window directly, works even if it is occluded.
    $g.Clear([System.Drawing.Color]::White)
    $hdc = $g.GetHdc()
    try { [void][Shot]::PrintWindow($hwnd, $hdc, 2) } finally { $g.ReleaseHdc($hdc) }
} finally {
    $g.Dispose()
}

$outDir = Split-Path -Parent $OutPath
if ($outDir -and -not (Test-Path $outDir)) { New-Item -ItemType Directory -Path $outDir -Force | Out-Null }

$bmp.Save($OutPath, [System.Drawing.Imaging.ImageFormat]::Png)
$bmp.Dispose()

# Blank-image guard: a capture that is all one colour means we got nothing.
# Sample the top-left corner straight from the loaded bitmap -- Bitmap.Clone with
# a Rectangle overload is resolved unpredictably by PowerShell 5.1.
$check = [System.Drawing.Bitmap]::FromFile($OutPath)
try {
    $maxX = [Math]::Min(40, $check.Width - 1)
    $maxY = [Math]::Min(40, $check.Height - 1)
    $seen = New-Object 'System.Collections.Generic.HashSet[int]'
    $x = 0
    while ($x -le $maxX) {
        $y = 0
        while ($y -le $maxY) {
            [void]$seen.Add($check.GetPixel($x, $y).ToArgb())
            $y += 3
        }
        $x += 3
    }
    $distinct = $seen.Count
    $check.Dispose()
    if ($distinct -lt 4) {
        Write-Output ('BLANK colors=' + $distinct + ' ' + $w + 'x' + $h)
        exit 6
    }
} catch {
    Write-Output ('VERIFY_FAILED ' + $_.Exception.Message)
    exit 7
}

Write-Output ('OK ' + $w + 'x' + $h + ' -> ' + $OutPath)
exit 0
