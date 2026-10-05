@echo off
chcp 65001 >nul
rem %~dp0 = 本脚本所在目录，换机器/换盘符都能用
cd /d "%~dp0"
set PYTHONUTF8=1
python -m canvas_helper
if errorlevel 1 pause
