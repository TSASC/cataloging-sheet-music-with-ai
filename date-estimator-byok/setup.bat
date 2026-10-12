@echo off
cd /d "%~dp0"
python -m venv venv
if errorlevel 1 exit /b 1
venv\Scripts\python -m pip install -r requirements.txt
if errorlevel 1 exit /b 1
echo Setup complete. Run python run.py or open Start.bat.
