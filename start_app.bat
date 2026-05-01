:: Make a shortcut to this file with the following syntax in the target field:
:: C:\Windows\System32\cmd.exe /c "C:\folder\location\walkingpad\start_app.bat"

@echo off
ECHO Activating virtual environment...

REM Activate the virtual environment
call "%~dp0.venv\Scripts\activate.bat"

ECHO Starting WalkingDad...

REM Run the application
python "%~dp0run.py"