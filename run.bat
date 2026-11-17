@echo off
rem Dugwali launcher: double-click to start. No venv activation needed.
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
title Dugwali

if not exist ".venv\Scripts\python.exe" (
    echo [setup] Creating virtual environment...
    python -m venv .venv || goto :setup_failed
    echo [setup] Installing packages. First run only, this can take several minutes...
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt -r requirements-stt.txt || goto :setup_failed
)

if not exist ".env" (
    copy config.example.env .env >nul
    echo [setup] Created .env from the example. Fill in the Slack tokens, save, then run this file again.
    start "" notepad .env
    pause
    exit /b 1
)

:loop
".venv\Scripts\python.exe" -m dugwali.app
set CODE=%errorlevel%
if "%CODE%"=="0" exit /b 0
if "%CODE%"=="2" (
    echo.
    echo [dugwali] Configuration problem. Fix .env and run this file again.
    pause
    exit /b 2
)
echo.
echo [dugwali] Stopped with code %CODE%. Restarting in 10 seconds. Close this window to quit.
timeout /t 10 /nobreak >nul
goto loop

:setup_failed
echo.
echo [setup] Setup failed. Check that Python is installed and on PATH, then run this file again.
pause
exit /b 1
