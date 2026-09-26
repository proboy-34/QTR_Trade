@echo off
setlocal EnableExtensions
title QTR_Trade Launcher
rem Always work from the folder this file is in (for example E:\QTR), wherever it was started from.
cd /d "%~dp0"

echo ==============================================
echo  QTR_Trade launcher - PAPER TRADING ONLY
echo ==============================================
echo.

if not exist "app\main.py" goto :no_project
if not exist ".env" goto :no_env

rem ---- prerequisites --------------------------------------------------
where python >nul 2>nul
if errorlevel 1 goto :no_python
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>nul
if errorlevel 1 goto :old_python
where node >nul 2>nul
if errorlevel 1 goto :no_node
where npm >nul 2>nul
if errorlevel 1 goto :no_npm
where git >nul 2>nul
if errorlevel 1 goto :no_git
echo Python, Node.js, npm and Git: found

rem ---- Python environment: reuse .venv, create/install only when missing ----
if exist ".venv\Scripts\python.exe" goto :venv_ok
echo First run: creating the Python environment .venv - this happens only once...
python -m venv .venv
if errorlevel 1 goto :venv_failed
:venv_ok
".venv\Scripts\python.exe" -c "import fastapi, uvicorn, alembic, sqlalchemy, httpx" >nul 2>nul
if not errorlevel 1 goto :python_ready
echo Installing Python packages - first run only, this can take a few minutes...
".venv\Scripts\python.exe" -m pip install -e .
if errorlevel 1 goto :pip_failed
:python_ready
echo Python environment: ready

rem ---- Frontend packages: install only when missing ----
if exist "frontend\node_modules" goto :node_ready
echo Installing frontend packages - first run only, this can take a few minutes...
pushd frontend
call npm install
set NPM_RESULT=%ERRORLEVEL%
popd
if not "%NPM_RESULT%"=="0" goto :npm_failed
:node_ready
echo Frontend packages: ready
echo.

rem ---- migrations, backend, frontend, health checks, browser ----
".venv\Scripts\python.exe" "scripts\qtr_launcher.py" start
if errorlevel 1 goto :failed

echo.
echo You can close THIS launcher window. QTR_Trade keeps running in its
echo Backend and Frontend windows. To stop it, double-click STOP_QTR.bat.
echo.
pause
exit /b 0

:no_project
echo ERROR: The QTR_Trade project files were not found next to RUN_QTR.bat.
echo Please keep RUN_QTR.bat inside the QTR_Trade folder, for example E:\QTR.
goto :failed
:no_env
echo ERROR: The configuration file .env was not found in %CD%.
echo QTR_Trade needs your existing .env file with its settings and API keys.
echo Put your .env file back into this folder and run RUN_QTR.bat again.
goto :failed
:no_python
echo ERROR: Python was not found.
echo Please install Python 3.12 or newer from python.org - tick "Add python.exe to PATH" -
echo and run RUN_QTR.bat again.
goto :failed
:old_python
echo ERROR: Python 3.12 or newer is required, or Python could not be started.
echo Please install Python 3.12 or newer from python.org and run RUN_QTR.bat again.
goto :failed
:no_node
echo ERROR: Node.js was not found.
echo Please install Node.js LTS from nodejs.org and run RUN_QTR.bat again.
goto :failed
:no_npm
echo ERROR: npm was not found. It is installed together with Node.js.
echo Please reinstall Node.js LTS from nodejs.org and run RUN_QTR.bat again.
goto :failed
:no_git
echo ERROR: Git was not found.
echo Please install Git from git-scm.com and run RUN_QTR.bat again.
goto :failed
:venv_failed
echo ERROR: The Python environment .venv could not be created.
goto :failed
:pip_failed
echo ERROR: Installing the Python packages failed. Check your internet connection and try again.
goto :failed
:npm_failed
echo ERROR: Installing the frontend packages failed. Check your internet connection and try again.
goto :failed

:failed
echo.
echo QTR_Trade was NOT started. Nothing was deleted.
echo.
pause
exit /b 1
