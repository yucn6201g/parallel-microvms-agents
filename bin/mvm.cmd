@echo off
rem Launcher for Windows (PowerShell / Command Prompt). The implementation is bin\mvm.py.
setlocal
where py >nul 2>nul
if %ERRORLEVEL%==0 (
  py -3 "%~dp0mvm.py" %*
) else (
  python "%~dp0mvm.py" %*
)
exit /b %ERRORLEVEL%
