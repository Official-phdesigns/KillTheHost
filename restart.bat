@echo off
REM ============================================================
REM  KillTheHost - Restart Script (Windows)
REM  Stops the launcher (if running) and starts it again.
REM
REM  AGPL-3.0  |  KillTheHost Launcher v1.5
REM ============================================================

echo [KillTheHost] Restarting launcher...
call "%~dp0stop.bat"
timeout /t 2 /nobreak >nul
call "%~dp0launch.bat"
