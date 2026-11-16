@echo off
chcp 65001 >nul
setlocal
set "PYTHONUTF8=1"
set "PYTHONDONTWRITEBYTECODE=1"
set "HERE=%~dp0"
set "INTERACTIVE=0"

if exist "%HERE%SPCConverter.exe" (
    start "" "%HERE%SPCConverter.exe" %*
    exit /b 0
)

set "INTERACTIVE=1"
python "%HERE%src\spc_converter.py" --gui %*

set "ERR=%ERRORLEVEL%"
if "%INTERACTIVE%"=="1" pause
if not "%ERR%"=="0" pause
exit /b %ERR%
