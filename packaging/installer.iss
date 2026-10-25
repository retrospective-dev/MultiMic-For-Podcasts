; Inno Setup script for the MultiMic installer.
; Build the app first (PyInstaller, one-folder mode) so dist\MultiMic exists,
; then: iscc /DAppVersion=1.2.3 packaging\installer.iss

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6D0B6F3E-2C1A-4E0B-9A57-4D5C3B8E1F21}
AppName=MultiMic
AppVersion={#AppVersion}
AppVerName=MultiMic {#AppVersion}
DefaultDirName={autopf}\MultiMic
DefaultGroupName=MultiMic
DisableProgramGroupPage=yes
; installs for the current user without asking for admin rights
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename=MultiMic-Setup
SetupIconFile=..\multimic.ico
UninstallDisplayIcon={app}\MultiMic.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\MultiMic\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\MultiMic"; Filename: "{app}\MultiMic.exe"
Name: "{autodesktop}\MultiMic"; Filename: "{app}\MultiMic.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\MultiMic.exe"; Description: "{cm:LaunchProgram,MultiMic}"; Flags: nowait postinstall skipifsilent
