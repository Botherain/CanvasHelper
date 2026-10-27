@echo off
rem Pure ASCII on purpose: cmd reads .bat with the OEM code page, and switching
rem the code page mid-file can shift line offsets. All Chinese text (and all the
rem real work) lives in uninstall.ps1, which is UTF-8 with BOM.
rem
rem Usage: double-click, or  uninstall.bat -Yes | -KeepData
setlocal
cd /d "%~dp0"

where powershell.exe >nul 2>nul
if errorlevel 1 goto no_ps

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1" %*
exit /b %errorlevel%

:no_ps
echo.
echo PowerShell not found. Delete this folder manually, and also delete:
echo   %LOCALAPPDATA%\Inferry
echo   %LOCALAPPDATA%\CanvasHelper   (left over from 1.x)
echo Your course files and DeskBox todos are not touched.
echo.
pause
exit /b 1
