@echo off
chcp 65001 >nul
cd /d "%~dp0"
set OPENBLAS_NUM_THREADS=1
set OMP_NUM_THREADS=1
if not exist ".venv\Scripts\python.exe" powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\setup.ps1"
if errorlevel 1 (
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m erp.migrate
if errorlevel 1 (
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m erp.launcher
if errorlevel 1 pause
