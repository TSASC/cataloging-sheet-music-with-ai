@echo off
REM IMSLP Date Estimator — Double-click to start
cd /d "%~dp0"

echo ======================================
echo   IMSLP Date Estimator
echo ======================================
echo.

where python >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo Python not found. Installing...
    where winget >nul 2>nul
    if %ERRORLEVEL% equ 0 (
        winget install Python.Python.3.12 --accept-package-agreements --accept-source-agreements
        echo.
        echo Python installed. Please close this window and double-click this file again.
        pause
        exit /b 0
    ) else (
        echo Please install Python from: https://www.python.org/downloads/
        echo IMPORTANT: Check "Add Python to PATH" during installation.
        echo Then double-click this file again.
        pause
        exit /b 1
    )
)

echo Starting...
echo (Close this window to stop the app)
echo.
python run.py
