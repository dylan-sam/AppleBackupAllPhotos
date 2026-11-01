@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv 2>nul
  if errorlevel 1 py -3 -m venv .venv
  if not exist ".venv\Scripts\python.exe" (
    echo Install Python 3.11 or newer from python.org, then try again.
    pause
    exit /b 1
  )
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo Dependency installation failed. Check the message above.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" iphone_photo_copier.py
