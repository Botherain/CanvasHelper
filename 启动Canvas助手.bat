@echo off
rem Pure ASCII on purpose: cmd reads this file with the console code page, and
rem "chcp 65001" below switches that code page mid-file -- a non-ASCII byte then
rem shifts the line offsets, a rem line gets re-read from the middle, and its
rem tail is executed as a command. Keep the Chinese wording in the README.
chcp 65001 >nul
rem %~dp0 is this script's own directory, so it works from any drive/location.
cd /d "%~dp0"
set PYTHONUTF8=1
rem Launch the GUI without leaving a black console window behind:
rem   1) "start" returns right away, so this cmd window closes at once.
rem      Previously python.exe blocked here, so the black window stayed up for
rem      the whole session, right beside the main window.
rem   2) pythonw.exe is a no-console interpreter, so it opens no second window.
rem Fall back to python.exe only when pythonw.exe is missing: better to keep a
rem visible console than to fail silently with no way to see the error.
where pythonw >nul 2>nul
if errorlevel 1 (
    python -m canvas_helper
    if errorlevel 1 pause
) else (
    start "" pythonw -m canvas_helper
)
