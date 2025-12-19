@echo off
setlocal

REM Headless Agent-Lite installer (ADMIN ONLY, NO PROMPTS)

set "SCRIPT_DIR=%~dp0"
set "PROGRAM_DATA=%ProgramData%\AgentLite"
set "ENV_DST=%PROGRAM_DATA%\agent.env"
set "ENV_TEMPLATE=%SCRIPT_DIR%agent.env.template"

echo ========================================
echo Agent-Lite Service Installer (Headless)
echo ========================================

REM Admin check
net session >nul 2>&1
if %errorlevel% neq 0 (
  echo ERROR: Run this as Administrator.
  exit /b 1
)

REM Ensure ProgramData folder exists
mkdir "%PROGRAM_DATA%" >nul 2>&1

REM Python check
python --version >nul 2>&1
if %errorlevel% neq 0 (
  echo ERROR: Python is not installed or not in PATH.
  exit /b 1
)

REM Install deps
echo Installing dependencies...
python -m pip install --upgrade pip >nul
python -m pip install -r "%SCRIPT_DIR%requirements.txt"
if %errorlevel% neq 0 (
  echo ERROR: Failed to install requirements.txt
  exit /b 1
)

python -m pip install pywin32
if %errorlevel% neq 0 (
  echo ERROR: Failed to install pywin32
  exit /b 1
)

REM Config: copy template if provided and no config exists
if not exist "%ENV_DST%" (
  if exist "%ENV_TEMPLATE%" (
    copy /Y "%ENV_TEMPLATE%" "%ENV_DST%" >nul
    echo Config installed: "%ENV_DST%"
  ) else (
    echo ERROR: Missing config.
    echo Provide "%ENV_TEMPLATE%" OR pre-create "%ENV_DST%"
    exit /b 1
  )
) else (
  echo Config exists: "%ENV_DST%"
)

REM Install service
pushd "%SCRIPT_DIR%"
echo Installing Windows Service...
python Service.py install
if %errorlevel% neq 0 (
  popd
  echo ERROR: Service install failed.
  exit /b 1
)

REM Recovery + auto start
sc failure AgentLite reset= 86400 actions= restart/60000/restart/60000/restart/60000 >nul
sc config AgentLite start= auto >nul

REM Start
net start AgentLite >nul 2>&1

popd

echo Done.
echo Logs: "%PROGRAM_DATA%\service.log"
exit /b 0
