@echo off
rem QTR_Trade backend window (started by RUN_QTR.bat). Keep this window open while trading.
title QTR_Trade Backend
cd /d "%~dp0..\.."
echo QTR_Trade backend - paper trading only. Keep this window open.
echo Logs appear below. Closing this window stops the backend.
echo.
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
echo.
echo The QTR_Trade backend has stopped. Read any error above.
pause
