@echo off
rem Launcher for Windows (PowerShell / Command Prompt). The implementation is bin\mvm.py.
rem Needs Python 3.9+. py -3 first, then python. The Microsoft Store stub fails the version check, so it is skipped.
setlocal
py -3 -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>nul
if not errorlevel 1 goto usepy
python -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>nul
if not errorlevel 1 goto usepython
for /f "tokens=2 delims=:." %%c in ('chcp') do set "CP=%%c"
chcp 65001 >nul
1>&2 echo mvm には Python 3.9 以上が要ります。次のどちらかで入れてから、新しいターミナルでもう一度実行してください。
1>&2 echo   winget install Python.Python.3.12
1>&2 echo   https://www.python.org/downloads/
chcp %CP% >nul
exit /b 1
:usepy
py -3 "%~dp0mvm.py" %*
exit /b %ERRORLEVEL%
:usepython
python "%~dp0mvm.py" %*
exit /b %ERRORLEVEL%
