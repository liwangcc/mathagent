@echo off
setlocal
title MathAgent Platform Node Setup
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup-platform-node.ps1"
set "SETUP_EXIT=%ERRORLEVEL%"
if not "%SETUP_EXIT%"=="0" (
  echo Setup did not complete. See the error above and contact the administrator.
) else (
  echo Setup complete. To manage the node later, open control-platform-node.cmd.
)
pause
exit /b %SETUP_EXIT%
