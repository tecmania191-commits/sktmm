@echo off
cd /d "%~dp0"
title Push NIFTY 50 AI Project to GitHub
color 0B

echo ===============================================================================
echo                PUSHING SKTMKT TO GITHUB: tecmania191-commits/sktmm
echo ===============================================================================
echo.
echo Current Remote:
git remote -v
echo.
echo -------------------------------------------------------------------------------
echo Initiating git push to branch: main...
echo If GitHub asks you to sign in, please complete the sign-in in your browser window.
echo -------------------------------------------------------------------------------
echo.

git push -u origin main

if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Push failed. 
    echo If you see "Permission denied to abhai99", your Windows is logged in as abhai99.
    echo To fix this:
    echo 1. Either add abhai99 as Collaborator on https://github.com/tecmania191-commits/sktmm/settings/access
    echo 2. OR use a GitHub Personal Access Token (PAT):
    echo    git push https://YOUR_GITHUB_TOKEN@github.com/tecmania191-commits/sktmm.git main
    echo.
) else (
    echo.
    echo ===============================================================================
    echo [SUCCESS] Code successfully pushed to https://github.com/tecmania191-commits/sktmm.git!
    echo ===============================================================================
)

echo.
pause
