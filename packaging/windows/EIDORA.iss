#ifndef SourceDir
  #error SourceDir must be provided with /DSourceDir=...
#endif
#ifndef OutputDir
  #error OutputDir must be provided with /DOutputDir=...
#endif
#ifndef AppVersion
  #error AppVersion must be provided with /DAppVersion=...
#endif
#ifndef AppIcon
  #error AppIcon must be provided with /DAppIcon=...
#endif

[Setup]
AppId={{A86E9212-6134-4E39-B4BD-973039662176}
AppName=EIDORA
AppVersion={#AppVersion}
AppPublisher=EIDORA
DefaultDirName={autopf}\EIDORA
DefaultGroupName=EIDORA
DisableProgramGroupPage=yes
OutputDir={#OutputDir}
OutputBaseFilename=EIDORA-{#AppVersion}-windows-x86_64-setup
SetupIconFile={#AppIcon}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
UninstallDisplayIcon={app}\EIDORA.exe
#ifdef SignWindows
SignTool=EIDORASign
SignedUninstaller=yes
#endif

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\EIDORA"; Filename: "{app}\EIDORA.exe"
Name: "{autodesktop}\EIDORA"; Filename: "{app}\EIDORA.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Run]
Filename: "{app}\EIDORA.exe"; Description: "Launch EIDORA"; Flags: nowait postinstall skipifsilent
