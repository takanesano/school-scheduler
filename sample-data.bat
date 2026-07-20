@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Please run setup.bat first.
  pause
  exit /b
)
".venv\Scripts\python" -m app.load_sample sample_data
echo.
echo Sample data loaded. Start (or restart) the app with start.bat
pause
