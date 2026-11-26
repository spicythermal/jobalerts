; Windows installer for the desktop app (Inno Setup 6). Built by .github/workflows/release.yml:
;   iscc /DAppVersion=1.2.3 packaging\jobalerts.iss      (after pyinstaller has made dist\jobalerts)
; Installs for the current user only (no administrator prompt). Data stays in %LOCALAPPDATA%\jobalerts
; and is kept when uninstalling.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{5B358780-ECB4-4AE0-9887-6D1946103380}
AppName=jobalerts
AppVersion={#AppVersion}
AppPublisher=jobalerts contributors
AppPublisherURL=https://github.com/spicythermal/jobalerts
DefaultDirName={localappdata}\Programs\jobalerts
DefaultGroupName=jobalerts
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=jobalerts-{#AppVersion}-windows-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=force
RestartApplications=no
UninstallDisplayIcon={app}\jobalerts.exe
LicenseFile=..\LICENSE

[Files]
Source: "..\dist\jobalerts\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[InstallDelete]
; files of the previous version that this one no longer has
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{autoprograms}\jobalerts"; Filename: "{app}\jobalerts.exe"
Name: "{autodesktop}\jobalerts"; Filename: "{app}\jobalerts.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Run]
Filename: "{app}\jobalerts.exe"; Description: "Start jobalerts"; Flags: nowait postinstall skipifsilent
; an update from the app runs this installer silently: start the new version without opening the browser
Filename: "{app}\jobalerts.exe"; Parameters: "--background"; Flags: nowait; Check: WizardSilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM jobalerts.exe"; Flags: runhidden; RunOnceId: "StopApp"
Filename: "{sys}\reg.exe"; Parameters: "delete HKCU\Software\Microsoft\Windows\CurrentVersion\Run /v jobalerts /f"; Flags: runhidden; RunOnceId: "NoAutostart"
