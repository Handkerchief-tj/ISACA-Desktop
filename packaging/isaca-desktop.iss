#define AppName "ISACA Desktop"
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#ifndef SourceDir
  #error SourceDir must point to the verified standalone directory.
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif

[Setup]
AppId={{8C82951A-DF91-4B61-9205-25BB51803835}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=ISACA Project Contributors
DefaultDirName={localappdata}\Programs\ISACA
DefaultGroupName=ISACA
AllowNoIcons=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
OutputDir={#OutputDir}
OutputBaseFilename=ISACA-Desktop-{#AppVersion}-win64-setup
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\ISACA.exe
VersionInfoVersion={#AppVersion}
VersionInfoProductName={#AppName}
VersionInfoProductVersion={#AppVersion}
VersionInfoCompany=ISACA Project Contributors
VersionInfoDescription=Intelligent Symbolic Analog Circuit Analyzer

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Dirs]
Name: "{localappdata}\ISACA"
Name: "{localappdata}\ISACA\logs"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\ISACA Desktop"; Filename: "{app}\ISACA.exe"; WorkingDir: "{localappdata}\ISACA"
Name: "{autodesktop}\ISACA Desktop"; Filename: "{app}\ISACA.exe"; WorkingDir: "{localappdata}\ISACA"; Tasks: desktopicon

[Run]
Filename: "{app}\ISACA.exe"; WorkingDir: "{localappdata}\ISACA"; Description: "Launch ISACA Desktop"; Flags: nowait postinstall skipifsilent
