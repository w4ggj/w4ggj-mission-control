@echo off
cd /d "%~dp0"

rem ============================================================
rem  W4GGJ Field Cloner launcher (laptop / POTA)
rem  Double-click this to start forwarding WSJT-X to the home
rem  shack. Edit HOME_TARGETS in field_cloner.py first (put your
rem  home Tailscale IP). Then point GridTracker's Forward UDP at
rem  127.0.0.1:2235.  Leave this window running.
rem ============================================================

title W4GGJ Mission Control - Field Cloner
echo ============================================
echo   W4GGJ Mission Control - FIELD CLONER
echo   Forwarding WSJT-X (incl. logged QSOs) to home...
echo   (leave this window running)
echo ============================================
:loop
python field_cloner.py
echo.
echo [field cloner exited] restarting in 10 seconds... (Ctrl+C to quit)
timeout /t 10 /nobreak >nul
goto loop
