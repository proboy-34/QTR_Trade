@echo off
setlocal EnableExtensions
title QTR_Trade Stop
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" goto :no_venv
".venv\Scripts\python.exe" "scripts\qtr_launcher.py" stop
echo.
pause
exit /b 0
:no_venv
echo QTR_Trade has never been started with RUN_QTR.bat from this folder - nothing to stop.
echo QTR_Trade stopped.
pause
exit /b 0
