@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo Install requirements.txt into .venv before starting.
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" -m fringe_lab.gui
