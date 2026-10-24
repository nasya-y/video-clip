; Inno Setup script for Video Compilation Maker (per-user install, no administrator rights needed)
; Build:  iscc /DMyAppVersion=1.0.1 packaging\installer.iss   (after PyInstaller has produced dist\VideoCompilationMaker)

#ifndef MyAppVersion
  #define MyAppVersion "1.0.1"
#endif
#define MyAppName "Video Compilation Maker"
#define MyAppExeName "VideoCompilationMaker.exe"
#define MyAppId "VideoCompilationMaker"

[Setup]
AppId={{6F1D2C3B-8A4E-4B5F-9C7D-2E3F4A5B6C7D}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppName}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename=VideoCompilationMaker-Setup-{#MyAppVersion}
SetupIconFile=..\resources\icons\app.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile=..\packaging\installer_license.txt
VersionInfoVersion={#MyAppVersion}
VersionInfoDescription={#MyAppName} installer

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\VideoCompilationMaker\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\README.md"; DestDir: "{app}"; DestName: "README.md"; Flags: ignoreversion
Source: "..\THIRD_PARTY_NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autoprograms}\{#MyAppName} - licences"; Filename: "{app}\_internal\licenses"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; analysis cache and temporary files (settings and logs are kept)
Type: filesandordirs; Name: "{localappdata}\{#MyAppId}\cache"
Type: filesandordirs; Name: "{localappdata}\{#MyAppId}\temp"
