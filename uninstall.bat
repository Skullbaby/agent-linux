@echo off
REM Agent-Lite USB Uninstaller
REM Run this as Administrator

echo ========================================
echo Agent-Lite Uninstaller
echo ========================================
echo.

REM Check for admin rights
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo ERROR: This script must be run as Administrator!
    echo Right-click uninstall.bat and select "Run as administrator"
    pause
    exit /b 1
)

echo [1/2] Stopping Agent-Lite service...
net stop AgentLite
echo.

echo [2/2] Removing Agent-Lite service...
python Service.py remove
if %errorLevel% neq 0 (
    echo WARNING: Failed to remove service
    echo It may have already been removed
)
echo.

echo ========================================
echo Uninstallation Complete!
echo ========================================
echo.
echo Note: Log files remain at:
echo   %USERPROFILE%\AppData\Local\AgentLite\
echo.
echo To completely remove all data, delete that folder manually.
echo.
pause
