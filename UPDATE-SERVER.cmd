@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0deployment\Apply-ServerUpdate.ps1" -PackagePath "%~dp0." -AppPath "%LOCALAPPDATA%\OzonSalesBot" > "%~dp0UPDATE-RESULT.txt" 2>&1
set "UPDATE_EXIT=%ERRORLEVEL%"
type "%~dp0UPDATE-RESULT.txt"
if not "%UPDATE_EXIT%"=="0" (
  echo UPDATE_FAILED
  pause
  exit /b 1
)
echo UPDATE_OK
pause
