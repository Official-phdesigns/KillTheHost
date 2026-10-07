@echo off
REM ============================================================
REM  KillTheHost - Restart Script (Windows)
REM  Stops the launcher AND anything holding a suite port
REM  (duplicates / leftovers from earlier runs), then starts
REM  the launcher again.
REM
REM  AGPL-3.0  |  KillTheHost Launcher v1.6
REM ============================================================

echo [KillTheHost] Restarting launcher...
call "%~dp0stop.bat"
timeout /t 2 /nobreak >nul
REM Second pass catches anything that respawned or was slow to release its port
call "%~dp0stop.bat" >nul
call "%~dp0launch.bat"
