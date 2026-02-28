@echo off
title Video Transcoder - GUI Edition

REM === Check Python ===
python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo   Python is not installed or not in PATH.
    echo   Download from: https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

REM === Install all dependencies if any are missing ===
python -c "import rich; import customtkinter" >nul 2>&1
if errorlevel 1 (
    echo   Installing dependencies...
    pip install -r "%~dp0requirements.txt"
    echo.
)

REM === Run the GUI transcoder ===
python "%~dp0gui.py" %*
