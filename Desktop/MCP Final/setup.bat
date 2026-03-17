@echo off
echo ========================================
echo   Figure Data Extractor - Setup
echo ========================================
echo.

REM Check if venv exists
if not exist "venv" (
    echo Creating virtual environment...
    python -m venv venv
    echo.
)

echo Activating venv...
call venv\Scripts\activate.bat

echo Installing dependencies...
pip install -r requirements.txt
echo.

echo ========================================
echo   Setup complete!
echo   
echo   IMPORTANT: Edit .env file and add your
echo   ANTHROPIC_API_KEY before running.
echo.
echo   To run: call venv\Scripts\activate.bat
echo           python server.py
echo ========================================
pause
