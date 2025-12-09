# Agent-Lite Installation

## Quick Install

1. Right-click `install.bat` → Run as administrator
2. Enter your controller URL when prompted
3. Done! Agent runs in background.

## Requirements

- Windows 10/11
- Python 3.8+ installed
- Administrator rights

## Verify Installation
```cmd
sc query AgentLite
```

Should show `STATE: 4 RUNNING`

## View Logs
```cmd
type %USERPROFILE%\AppData\Local\AgentLite\service.log
```

## Uninstall

Right-click `uninstall.bat` → Run as administrator
