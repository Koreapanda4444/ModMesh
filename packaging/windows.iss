[Setup]
AppId=ModMesh.Desktop
AppName=ModMesh
AppVersion={#AppVersion}
AppPublisher=Koreapanda4444
AppPublisherURL=https://github.com/Koreapanda4444/ModMesh
DefaultDirName={localappdata}\Programs\ModMesh
DefaultGroupName=ModMesh
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.19041
OutputDir={#OutputRoot}
OutputBaseFilename=ModMesh-{#AppVersion}-windows-x64-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\versions\{#AppVersion}\ModMesh.exe
CloseApplications=yes
RestartApplications=no
SetupLogging=yes

[Files]
Source: "{#BundleRoot}\*"; DestDir: "{app}\versions\{#AppVersion}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\ModMesh"; Filename: "{app}\versions\{#AppVersion}\ModMesh.exe"; WorkingDir: "{app}\versions\{#AppVersion}"

[Run]
Filename: "{app}\versions\{#AppVersion}\ModMesh.exe"; Description: "ModMesh"; Flags: nowait postinstall skipifsilent
