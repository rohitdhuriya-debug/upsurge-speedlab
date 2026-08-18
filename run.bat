@echo off
REM UPSURGE SpeedLab - launch on port 5070 and open the browser.
cd /d "%~dp0"

set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

%PY% -c "import fastapi, uvicorn, multipart" 2>nul
if errorlevel 1 (
  echo Installing dependencies...
  %PY% -m pip install -r requirements.txt
)

start "" cmd /c "timeout /t 2 >nul & start http://localhost:5070"
%PY% -m uvicorn app.main:app --host 127.0.0.1 --port 5070
pause
