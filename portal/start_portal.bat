@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
title CHUNIL Smart Mail Portal - Team Server

echo ============================================================
echo   CHUNIL Smart Mail Portal - Team Server
echo ============================================================
echo.

where python >nul 2>&1
if errorlevel 1 (
  echo [X] Python is not installed.
  echo     Download from https://www.python.org/downloads/
  echo     Check "Add python.exe to PATH" during install.
  echo.
  pause
  exit /b 1
)

python -c "import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
if errorlevel 1 (
  echo [X] Python 3.10+ is required.
  python --version
  pause
  exit /b 1
)

echo [1/3] Checking Python packages...
python -m pip install -q --disable-pip-version-check -r scripts\requirements.txt
if errorlevel 1 (
  echo [X] pip install failed. Check internet connection.
  pause
  exit /b 1
)

if not exist ".env" (
  echo.
  echo [2/3] First run setup (.env)
  set /p "GK=      Enter GEMINI_API_KEY (or press Enter to skip): "
  > ".env" echo # CHUNIL Smart Mail Portal
  >> ".env" echo PORT=8080
  if defined GK (
    >> ".env" echo GEMINI_API_KEY=!GK!
    echo       Saved GEMINI_API_KEY.
  ) else (
    echo       Skipped GEMINI_API_KEY.
  )
) else (
  echo [2/3] Using existing .env
)

echo [3/3] Opening firewall port 8080 (skipped if not admin)...
netsh advfirewall firewall add rule name="CHUNIL Portal 8080" dir=in action=allow protocol=TCP localport=8080 >nul 2>&1

echo.
echo Starting server... Keep this window open.
echo.
python server.py --port 8080
echo.
echo Server stopped.
pause
