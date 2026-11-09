@echo off
rem Fallback for aplite.exe (if antivirus or SmartScreen blocks it): Archipelago Lite with the bundled Python. Prefers Windows Terminal; set APLITE_NO_WT=1 to stay in this console.
setlocal
if not defined APLITE_NO_WT if not defined WT_SESSION (
    where wt >nul 2>nul && (
        rem "call" first: cmd /c strips the outer quotes of a line that starts with one
        start "" wt -d . cmd /c call "%~f0" %*
        exit /b 0
    )
)
set "HERE=%~dp0"
set SKIP_REQUIREMENTS_UPDATE=1
rem user data in this folder, as upstream's Windows build keeps it (aplite/datadir.py)
set "APLITE_DATA_DIR=%HERE%"
set "PYTHONPATH=%HERE%app;%PYTHONPATH%"
rem aplite exits with this code to be started again, fresh (aplite.RELAUNCH in aplite/__init__.py)
set APLITE_RELAUNCH=75
"%HERE%python\python.exe" -m aplite %*
:again
if not "%ERRORLEVEL%"=="75" exit /b %ERRORLEVEL%
"%HERE%python\python.exe" -m aplite
goto again
