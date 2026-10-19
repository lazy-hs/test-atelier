@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0configure_environment_windows.ps1" %*
if errorlevel 1 pause
endlocal
