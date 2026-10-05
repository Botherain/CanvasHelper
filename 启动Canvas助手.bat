@echo off
chcp 65001 >nul
cd /d D:\Projects\CanvasHelper
set PYTHONUTF8=1
python -m canvas_helper
if errorlevel 1 pause
