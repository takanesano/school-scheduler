@echo off
cd /d "%~dp0"
echo ============================================
echo  Cram School Scheduler - first-time setup
echo  This may take 5-10 minutes. Please wait...
echo ============================================
echo.
where py >nul 2>nul
if %errorlevel%==0 (set PY=py) else (set PY=python)
%PY% -m venv .venv
if errorlevel 1 goto :error
".venv\Scripts\python" -m pip install --upgrade pip
".venv\Scripts\python" -m pip install -r requirements.txt
if errorlevel 1 goto :error
echo.
echo ============================================
echo  Setup complete!
echo  Next step: double-click  start.bat
echo ============================================
pause
exit /b
:error
echo.
echo ============================================
echo  ERROR: setup did not finish.
echo  Is Python installed? See INSTALL.ja.md
echo  (section: "うまくいかないとき")
echo ============================================
pause
