@echo off
cd /d "%~dp0"
if not exist "dist\FlowAgentConsole.exe" (
  echo FlowAgentConsole.exe is missing. Run Build-FlowConsoleExe.ps1 first.
  pause
  exit /b 1
)
"dist\FlowAgentConsole.exe" --lan
if errorlevel 1 pause
