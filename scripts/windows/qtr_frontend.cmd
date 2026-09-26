@echo off
rem QTR_Trade frontend window (started by RUN_QTR.bat). Keep this window open while using the UI.
title QTR_Trade Frontend
cd /d "%~dp0..\..\frontend"
echo QTR_Trade frontend - keep this window open. Closing it stops the web interface.
echo.
call npm run dev -- --strictPort
echo.
echo The QTR_Trade frontend has stopped. Read any error above.
pause
