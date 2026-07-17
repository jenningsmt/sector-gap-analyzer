; Inno Setup script for Sector Gap Analyzer.
;
; Builds a self-extracting, per-user installer (no admin rights required) from
; the PyInstaller onedir output. Build order:
;   pip install -r requirements-dev.txt
;   pyinstaller SectorGapAnalyzer.spec --clean
;   iscc installer.iss
; The resulting installer is written to dist-installer\ and is meant to be
; attached to a GitHub Release, not committed to the repo.

#define MyAppName "Sector Gap Analyzer"
#define MyAppVersion "1.1.0"
#define MyAppExeName "SectorGapAnalyzer.exe"

[Setup]
AppId={{F2DB19EC-321A-4637-B529-35BBCADD5428}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={localappdata}\Programs\SectorGapAnalyzer
DefaultGroupName={#MyAppName}
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
CloseApplications=yes
RestartApplications=no
OutputDir=dist-installer
OutputBaseFilename=SectorGapAnalyzer-Setup-{#MyAppVersion}
UninstallDisplayIcon={app}\{#MyAppExeName}
SetupIconFile=icon.ico
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible

; Wipe the PyInstaller bundle directory before installing the new one, so an
; upgrade can't accumulate files a prior release shipped but this one no
; longer does (e.g. after a Python/PyInstaller version bump). User data lives
; entirely outside {app} (%LOCALAPPDATA%\SectorGapAnalyzer\workspace and
; %APPDATA%\SectorGapAnalyzer\config.json), so this never touches it.
[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "dist\SectorGapAnalyzer\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent
