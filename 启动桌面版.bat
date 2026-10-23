@echo off
REM ============================================================
REM  Desktop launcher - double-click this file.
REM  Opens a standalone app window (no address bar, no tabs).
REM  Keep this file ASCII-only: Windows cmd's codepage will
REM  mangle Chinese text here. All user-facing messages are
REM  printed by Python instead (see jobforge/desktop.py).
REM ============================================================
setlocal
cd /d "%~dp0"
title JobBench

set "PY="
py -3 -c "print(1)" >nul 2>&1 && set "PY=py -3"
if not defined PY (
  python -c "print(1)" >nul 2>&1 && set "PY=python"
)
if not defined PY (
  python3 -c "print(1)" >nul 2>&1 && set "PY=python3"
)

if not defined PY (
  echo.
  echo   Python was not found on this computer.
  echo.
  echo   Download and install it from:
  echo       https://www.python.org/downloads/
  echo.
  echo   IMPORTANT: tick "Add Python to PATH" on the first
  echo   screen of the installer, otherwise this will fail again.
  echo.
  echo   Opening the download page in your browser now...
  start "" "https://www.python.org/downloads/"
  echo.
  pause
  exit /b 1
)

%PY% -X utf8 -m jobforge.desktop %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo   JobBench stopped with an error. Please screenshot the
  echo   messages above and send them to whoever helped you.
  echo.
  pause
)
exit /b %RC%
