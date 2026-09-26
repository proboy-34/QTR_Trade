@echo off
setlocal EnableExtensions
title QTR_Trade Status
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" goto :no_venv
".venv\Scripts\python.exe" "scripts\qtr_launcher.py" status
echo.
pause
exit /b 0
:no_venv
echo QTR_Trade is not set up yet. Double-click RUN_QTR.bat first.
pause
exit /b 0
