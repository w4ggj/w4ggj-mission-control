@echo off
cd /d "%~dp0"

rem The second window re-invokes this same file with "sdr" and jumps straight
rem to the SDR loop below, so one double-click launches BOTH agents, each in
rem its own auto-restarting window.
if /i "%~1"=="sdr" goto sdrloop

title W4GGJ Mission Control - Home Agent
rem launch the SDR panadapter agent in its own auto-restarting window
start "W4GGJ SDR Agent" cmd /k "%~f0" sdr

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

:sdrloop
title W4GGJ Mission Control - SDR Agent
echo ============================================
echo   W4GGJ Mission Control - SDR AGENT
echo   Feeding the band-scope waterfall (follows the radio)...
echo   (leave this window running)
echo ============================================
:sloop
python sdr_agent.py
echo.
echo [sdr agent exited] restarting in 10 seconds... (Ctrl+C to quit)
timeout /t 10 /nobreak >nul
goto sloop
