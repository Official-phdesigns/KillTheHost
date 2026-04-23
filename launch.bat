@echo off
:: ============================================================
::  KillTheHost — Windows Launcher
::  Place this file in:  KillTheHost\
::  Double-click or run from any terminal.
::
::  AGPL-3.0  |  KillTheHost Launcher v1.4 
:: ============================================================
setlocal EnableDelayedExpansion

set "SCRIPT=%~dp0Launcher\launcher.py"

:: ── Find Python ─────────────────────────────────────────────
set "PYTHON="

for %%P in (python python3) do (
    if not defined PYTHON (
        where %%P >nul 2>&1 && set "PYTHON=%%P"
    )
)

if not defined PYTHON (
    echo.
    echo  [ERROR] Python was not found on your system.
    echo.
    echo  Download Python 3.8+ from https://python.org/downloads/
    echo  Make sure to tick "Add Python to PATH" during install.
    echo.
    pause
    exit /b 1
)

:: ── Check launcher script exists ────────────────────────────
if not exist "%SCRIPT%" (
    echo.
    echo  [ERROR] Launcher script not found:
    echo  %SCRIPT%
    echo.
    echo  Expected folder layout:
    echo    KillTheHost\
    echo      launch.bat          ^<-- this file
    echo      Launcher\
    echo        launcher.py
    echo        assets\
    echo          main\
    echo            PHP-MNGR v2.5\
    echo            DB-3NGIN3 v1.2\
    echo.
    pause
    exit /b 1
)

:: ── Launch ──────────────────────────────────────────────────
echo.
echo  KillTheHost Launcher
echo  Starting: %PYTHON% "%SCRIPT%"
echo.

%PYTHON% "%SCRIPT%"

:: Keep window open if launcher exits with an error
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo  Launcher exited with code %ERRORLEVEL%.
    pause
)
