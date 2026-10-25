@echo off
rem Builds dist\MultiMic\MultiMic.exe (no Python needed to run it), plus
rem dist\MultiMic-Setup.exe if Inno Setup 6 is installed.
cd /d "%~dp0"
if not exist .venv py -3 -m venv .venv || python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt pyinstaller
.venv\Scripts\pyinstaller --noconfirm --windowed --name MultiMic --icon multimic.ico --add-data "multimic.ico;." --collect-data sv_ttk launch.py
set ISCC="%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
for /f %%v in ('.venv\Scripts\python tools\version.py') do set VERSION=%%v
if exist %ISCC% %ISCC% /DAppVersion=%VERSION% packaging\installer.iss
echo.
echo Done: dist\MultiMic\MultiMic.exe
if exist dist\MultiMic-Setup.exe echo Installer: dist\MultiMic-Setup.exe
pause
