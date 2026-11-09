@echo off
setlocal
cd /d "%~dp0"
where pythonw >nul 2>nul
if not errorlevel 1 (
    start "" pythonw "%~dp0app.py" %*
    exit /b
)
where py >nul 2>nul
if not errorlevel 1 (
    py -3 "%~dp0app.py" %*
    if errorlevel 1 pause
    exit /b
)
python "%~dp0app.py" %*
if errorlevel 1 pause
