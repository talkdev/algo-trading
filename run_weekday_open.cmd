@echo off
REM Interactive weekday launcher for VPS:
REM - skip NSE holidays
REM - single instance (lock file)
REM - upstox_token.py then main.py serially
REM Window stays open for monitoring after unlock/login.
setlocal EnableExtensions
cd /d "%~dp0"

set "PY=C:\Users\Administrator\AppData\Local\Programs\Python\Python313\python.exe"
if not exist "%PY%" set "PY=python"

set "LOCK=%TEMP%\nifty_algo_weekday_open.lock"
if exist "%LOCK%" (
  echo Another NiftyAlgo launcher is already running.
  echo Close that CMD window or delete:
  echo   %LOCK%
  echo.
  pause
  exit /b 0
)
echo %DATE% %TIME% > "%LOCK%"

echo ===== %date% %time% =====
echo Working dir: %CD%
echo User: %USERNAME%  Session interactive — leave this window open to monitor.

"%PY%" -c "from datetime import date; from core import ExpiryCalendar; import sys; d=date.today(); sys.exit(2 if ExpiryCalendar.is_holiday(d) else 0)"
if errorlevel 2 (
  echo NSE holiday — skipping upstox_token.py and main.py
  goto :cleanup
)
if errorlevel 1 (
  echo Holiday check failed — aborting
  goto :cleanup
)

echo.
echo [1/2] Refreshing Upstox access token...
"%PY%" upstox_token.py
if errorlevel 1 (
  echo upstox_token.py FAILED — not starting main.py
  goto :cleanup
)

echo.
echo [2/2] Starting main.py...
"%PY%" main.py
echo.
echo main.py exited with code %ERRORLEVEL%

:cleanup
del "%LOCK%" >nul 2>&1
echo.
echo Done. Press any key to close this window.
pause >nul
