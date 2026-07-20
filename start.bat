@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Please run setup.bat first.
  pause
  exit /b
)
echo ============================================
echo  Starting the Cram School Scheduler...
echo  Your browser will open in a few seconds.
echo  KEEP THIS BLACK WINDOW OPEN while using
echo  the app. Close it to stop the app.
echo ============================================
start "" cmd /c "timeout /t 3 >nul & start http://127.0.0.1:8000"
".venv\Scripts\python" -m uvicorn app.main:app
pause
