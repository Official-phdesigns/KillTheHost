@echo off
REM ============================================================
REM  KillTheHost - Stop Script (Windows)
REM  Stops the launcher (port 5000) AND every suite service
REM  listed in Launcher\launcher.py SERVICES. Ports are read
REM  from launcher.py, so new services are stopped automatically.
REM
REM  AGPL-3.0  |  KillTheHost Launcher v1.6
REM ============================================================
setlocal EnableDelayedExpansion

set "LAUNCHER_PY=%~dp0Launcher\launcher.py"
set "LAUNCHER_PORT=5000"
set "PORTS="

if exist "%LAUNCHER_PY%" (
    for /f "tokens=2 delims=:," %%A in ('findstr /C:"\"port\"" "%LAUNCHER_PY%"') do (
        set /a "P=%%A" 2>nul
        if !P! GTR 0 set "PORTS=!PORTS! !P!"
    )
)
if not defined PORTS set "PORTS= 4280 7734 6060 6161 7272 8080"

REM ── 1) Launcher first (its children are killed with /T) ──
call :killport %LAUNCHER_PORT% launcher
if not defined KILLED_%LAUNCHER_PORT% echo [KillTheHost] Launcher is not running.
if exist "%USERPROFILE%\.killthehost\launcher.pid" del /f /q "%USERPROFILE%\.killthehost\launcher.pid" >nul 2>&1

timeout /t 1 /nobreak >nul

REM ── 2) Any suite service still holding its port ──
for %%P in (%PORTS%) do (
    if not "%%P"=="%LAUNCHER_PORT%" call :killport %%P service
)

echo [KillTheHost] All suite ports free: %LAUNCHER_PORT%%PORTS%
endlocal
exit /b 0

:killport
REM %1 = port, %2 = label
for /f "tokens=5" %%I in ('netstat -ano ^| findstr /R /C:":%1 .*LISTENING"') do (
    if not "%%I"=="0" if not defined SEEN_%%I (
        set "SEEN_%%I=1"
        set "KILLED_%1=1"
        echo [KillTheHost] Stopping %2 on port %1 ^(PID: %%I^)...
        taskkill /PID %%I /T /F >nul 2>&1
    )
)
exit /b 0
