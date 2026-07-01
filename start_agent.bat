@echo off
title W4GGJ Mission Control - Home Agent
cd /d "%~dp0"
echo ============================================
echo   W4GGJ Mission Control - HOME AGENT
echo   Pushing live telemetry to Render...
echo   (leave this window running)
echo ============================================
:loop
python station_agent.py
echo.
echo [agent exited] restarting in 10 seconds... (Ctrl+C to quit)
timeout /t 10 /nobreak >nul
goto loop
