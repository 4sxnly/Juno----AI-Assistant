; Inno Setup script — produces juno\build\Output\Juno-Setup-<version>.exe
;   ISCC.exe /DAppVersion=1.0.0 juno\build\juno.iss
; Per-user install into %LOCALAPPDATA%\Programs\Juno — no admin prompt. Everything Juno needs (Python
; runtime, libraries, the llama.cpp engine) is inside. AI models and voices are downloaded from inside Juno.

#define AppName "Juno"
#define AppExe "Juno.exe"
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#define IconFile AddBackslash(SourcePath) + "..\assets\icon.ico"

[Setup]
AppId={{B8F2A6D1-4C3E-4E7B-9A51-2D6C0F7E3B19}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Juno
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=Output
OutputBaseFilename=Juno-Setup-{#AppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
#if FileExists(IconFile)
SetupIconFile={#IconFile}
#endif

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "startup"; Description: "Start Juno with Windows (in mini mode)"; GroupDescription: "Start-up:"; Flags: unchecked

[Dirs]
Name: "{app}\core"
Name: "{app}\integrations"
Name: "{app}\extras\characters"

[Files]
Source: "..\..\dist\Juno\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; the app as plain files, so Settings > Updates can replace them without a new installer
Source: "..\juno_app\*.py"; DestDir: "{app}\core\juno_app"; Flags: ignoreversion
Source: "..\bin\*"; DestDir: "{app}\bin"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\integrations\*"; DestDir: "{app}\integrations"; Flags: ignoreversion skipifsourcedoesntexist
Source: "..\extras\characters\*.mtchar"; DestDir: "{app}\extras\characters"; Flags: ignoreversion skipifsourcedoesntexist
Source: "..\build_info.json"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "Juno"; ValueData: """{app}\{#AppExe}"" --mini"; Tasks: startup; Flags: uninsdeletevalue

[Run]
; no skipifsilent: after an in-app update runs this installer silently, Juno reopens by itself
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; Flags: nowait postinstall

[UninstallDelete]
Type: filesandordirs; Name: "{app}\core"
Type: filesandordirs; Name: "{app}\integrations\__pycache__"
; downloaded AI models and voices (can be several GB)
Type: filesandordirs; Name: "{localappdata}\Juno\models"

[UninstallRun]
Filename: "{cmd}"; Parameters: "/C reg delete HKCU\Software\Microsoft\Windows\CurrentVersion\Run /v Juno /f"; Flags: runhidden; RunOnceId: "RemoveJunoStartup"
