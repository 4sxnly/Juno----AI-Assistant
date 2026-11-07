; Inno Setup script — produces build\Output\MultiTool-Setup-<version>.exe
;   ISCC.exe /DAppVersion=0.2.0 build\installer.iss
;
; Per-user install into %LOCALAPPDATA%\Programs\Multi-Tool:
;   * no admin prompt
;   * plugins\ and bin\ stay writable, so the app can add plugins and update yt-dlp itself
; Everything the app needs (Python runtime, libraries, ffmpeg, yt-dlp, deno) is inside this installer.

#define AppName "Multi-Tool"
#define AppExe "MultiTool.exe"
#ifndef AppVersion
  #define AppVersion "0.5.0"
#endif
#define IconFile AddBackslash(SourcePath) + "..\assets\icon.ico"

[Setup]
AppId={{7E3C1B52-9A4D-4F0E-B6C2-3D8F51A2C9E4}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=Output
OutputBaseFilename=MultiTool-Setup-{#AppVersion}
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

[Types]
Name: "full"; Description: "All tools"
Name: "custom"; Description: "Choose tools"; Flags: iscustom

[Components]
Name: "core"; Description: "{#AppName} app + bundled engines (required)"; Types: full custom; Flags: fixed
Name: "tools"; Description: "Tools"; Types: full
Name: "tools\media"; Description: "Media Downloader — YouTube, TikTok, X and more"; Types: full
Name: "tools\discord"; Description: "Discord Heads-Up — pop-ups for Discord messages with avatar and your notes"; Types: full
Name: "tools\demo"; Description: "Drop Zone demo (for plugin developers)"
Name: "extras"; Description: "Extras"; Types: full
Name: "extras\overdrive"; Description: "Overdrive 2099 theme (animated, with sounds)"; Types: full
Name: "extras\phosphor"; Description: "Phosphor CRT theme (green terminal)"; Types: full
Name: "extras\amber"; Description: "Amber Lab 86 theme (orange retro)"; Types: full

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "..\dist\MultiTool\*"; DestDir: "{app}"; Components: core; Flags: ignoreversion recursesubdirs createallsubdirs
; the app itself as a plain file, so Settings > Updates can replace it without a new installer
Source: "..\main.py"; DestDir: "{app}\core"; Components: core; Flags: ignoreversion
Source: "..\build_info.json"; DestDir: "{app}"; Components: core; Flags: ignoreversion skipifsourcedoesntexist
Source: "..\bin\*"; DestDir: "{app}\bin"; Components: core; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\plugins\media_downloader.py"; DestDir: "{app}\plugins"; Components: tools\media; Flags: ignoreversion skipifsourcedoesntexist
Source: "..\plugins\discord_notifier.py"; DestDir: "{app}\plugins"; Components: tools\discord; Flags: ignoreversion skipifsourcedoesntexist
Source: "..\plugins\dropzone_demo.py"; DestDir: "{app}\plugins"; Components: tools\demo; Flags: ignoreversion skipifsourcedoesntexist
; themes go straight into the user's themes folder so they show up in Settings ▸ Themes
Source: "..\extras\themes\overdrive-2099.mttheme"; DestDir: "{userappdata}\MultiTool\themes"; Components: extras\overdrive; Flags: ignoreversion skipifsourcedoesntexist
Source: "..\extras\themes\phosphor-crt.mttheme"; DestDir: "{userappdata}\MultiTool\themes"; Components: extras\phosphor; Flags: ignoreversion skipifsourcedoesntexist
Source: "..\extras\themes\amber-lab-86.mttheme"; DestDir: "{userappdata}\MultiTool\themes"; Components: extras\amber; Flags: ignoreversion skipifsourcedoesntexist

[Dirs]
Name: "{app}\core"
Name: "{app}\plugins"
Name: "{app}\bin"

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
; no skipifsilent: after an in-app update runs this installer silently, the app reopens by itself
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; Flags: nowait postinstall

[InstallDelete]
; the desktop companion moved out into the Juno app
Type: files; Name: "{app}\plugins\desktop_companion.py"

[UninstallDelete]
; yt-dlp self-updates leave a backup next to the exe
Type: files; Name: "{app}\bin\*.old"
Type: filesandordirs; Name: "{app}\plugins\__pycache__"
Type: filesandordirs; Name: "{app}\core"
Type: files; Name: "{app}\plugins\*.mtupdate"
