@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "PORT=8080"
if not "%~1"=="" set "PORT=%~1"

echo ============================================================
echo   CHUNIL Smart Mail Portal - stop the background server
echo   port %PORT%
echo ============================================================
echo.

set "FOUND=0"
for /f "tokens=5" %%P in ('netstat -ano -p tcp ^| findstr /R /C:":%PORT% " ^| findstr /C:"LISTENING"') do (
  set "FOUND=1"
  echo   - port %PORT% is held by PID %%P
  tasklist /FI "PID eq %%P" /NH 2>nul | findstr /I "python" >nul
  if errorlevel 1 (
    echo     [i] PID %%P is not python.exe / pythonw.exe - not touched.
    echo         If this really is the portal, close it yourself.
  ) else (
    taskkill /F /PID %%P >nul 2>&1
    if errorlevel 1 (
      echo     [X] Could not stop PID %%P - run this file as administrator.
    ) else (
      echo     [OK] Stopped PID %%P.
    )
  )
)

if "%FOUND%"=="0" echo   - Nothing is listening on port %PORT% (already stopped).

echo.
echo   Log: data\server.log
echo   Start again: start_hidden.vbs  (background)  or  start_portal.bat  (window)
echo.
pause
