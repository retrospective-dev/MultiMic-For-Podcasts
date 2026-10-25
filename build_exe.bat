@echo off
rem Builds a standalone dist\MultiMic.exe (no Python needed on the target PC).
cd /d "%~dp0"
if not exist .venv py -3 -m venv .venv || python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt pyinstaller
.venv\Scripts\pyinstaller --noconfirm --onefile --windowed --name MultiMic --icon multimic.ico --add-data "multimic.ico;." --collect-data sv_ttk multimic.py
echo.
echo Done: dist\MultiMic.exe
pause
