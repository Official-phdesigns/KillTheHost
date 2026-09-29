@echo off
REM ============================================================
REM  KillTheHost - Stop Script (Windows)
REM  Stops the launcher running on port 5000.
REM
REM  AGPL-3.0  |  KillTheHost Launcher v1.5
REM ============================================================
setlocal EnableDelayedExpansion

set "PORT=5000"
set "FOUND="

for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:":%PORT% .*LISTENING"') do (
    if not "%%P"=="0" (
        if not defined SEEN_%%P (
            set "SEEN_%%P=1"
            set "FOUND=1"
            echo [KillTheHost] Stopping launcher ^(PID: %%P^)...
            taskkill /PID %%P /T /F >nul 2>&1
        )
    )
)

if exist "%USERPROFILE%\.killthehost\launcher.pid" del /f /q "%USERPROFILE%\.killthehost\launcher.pid" >nul 2>&1

if defined FOUND (
    echo [KillTheHost] Launcher stopped.
) else (
    echo [KillTheHost] Launcher is not running.
)

endlocal
exit /b 0
