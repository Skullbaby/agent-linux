# Agent-Lite (Windows) — Headless Service (Admin Only)

No UI. No prompts. Admin installs and manages the service.

## Config
Config file (admin-owned):
- `%ProgramData%\AgentLite\agent.env`

Logs:
- `%ProgramData%\AgentLite\service.log` (service wrapper)
- `%ProgramData%\AgentLite\agent.log` (agent runtime)

## Install (Admin)
Right-click `install.bat` → Run as administrator

## Verify
`sc query AgentLite`

## Logs
`type "%ProgramData%\AgentLite\service.log"`
`type "%ProgramData%\AgentLite\agent.log"`

## Uninstall (Admin)
Right-click `uninstall.bat` → Run as administrator
