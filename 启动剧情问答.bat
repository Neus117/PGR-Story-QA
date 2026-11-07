@echo off
setlocal
cd /d "%~dp0"

where pythonw >nul 2>nul
if errorlevel 1 (
  echo [PGR Story QA] pythonw not found.
  echo Install Python 3.9+ or run: python app.py
  pause
  exit /b 1
)

if not exist "data\pgr.db" (
  echo [PGR Story QA] data\pgr.db missing.
  echo Build it first:  python scripts\bootstrap.py
  pause
  exit /b 1
)

start "" pythonw "%~dp0app.py"
exit /b 0
