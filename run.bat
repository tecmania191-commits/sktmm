@echo off
cd /d "%~dp0"
title NIFTY BOSS - AI Live Trading Terminal
color 0A

echo ===============================================================================
echo                      NIFTY BOSS - AI TRADING TERMINAL
echo            Streak-Proof AI Model (+673.9 Pts, 5.47 PF, Max Streak: 2)
echo ===============================================================================
echo.
echo [*] Working folder: %CD%
echo [*] Starting NIFTY BOSS Server and Live Prediction Engine...
echo [*] Your browser will open automatically at: http://localhost:8000/index.html
echo.
echo -------------------------------------------------------------------------------
echo Server is running live. Keep this window open. Press Ctrl+C to stop.
echo -------------------------------------------------------------------------------
echo.

python build_and_run.py
if %errorlevel% neq 0 (
    echo [INFO] Trying py launcher...
    py build_and_run.py
)

echo.
echo ===============================================================================
echo Server stopped.
echo ===============================================================================
pause
