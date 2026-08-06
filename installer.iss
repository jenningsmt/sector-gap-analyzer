; Inno Setup script for Sector Surveyor.
;
; Builds a self-extracting, per-user installer (no admin rights required) from
; the PyInstaller onedir output. Build order:
;   pip install -r requirements-dev.txt
;   pyinstaller SectorSurveyor.spec --clean
;   iscc installer.iss
; The resulting installer is written to dist-installer\ and is meant to be
; attached to a GitHub Release, not committed to the repo.

; Bump this together with version_info.txt's filevers/prodvers/FileVersion/
; ProductVersion and gui/config.py's APP_VERSION when cutting a new release.
#define MyAppName "Sector Surveyor"
#define MyAppVersion "1.3.0"
#define MyAppExeName "SectorSurveyor.exe"

[Setup]
; New GUID as of the Sector Gap Analyzer -> Sector Surveyor rebrand (v1.3.0):
; deliberately NOT the old app's AppId. Keeping the old AppId while also
; changing DefaultDirName would make Inno Setup's upgrade path reuse the old
; installed directory (named after the old app) instead of a clean new one.
; This installs as a separate, independent app -- the old "Sector Gap
; Analyzer" Control Panel entry is left alone and can be uninstalled
; separately whenever convenient.
AppId={{B784DDC9-DD80-41A6-88BB-18DBF0C6B1BB}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={localappdata}\Programs\SectorSurveyor
DefaultGroupName={#MyAppName}
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
CloseApplications=yes
RestartApplications=no
OutputDir=dist-installer
OutputBaseFilename=SectorSurveyor-Setup-{#MyAppVersion}
UninstallDisplayIcon={app}\{#MyAppExeName}
SetupIconFile=icon.ico
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible

; Wipe the PyInstaller bundle directory before installing the new one, so an
; upgrade can't accumulate files a prior release shipped but this one no
; longer does (e.g. after a Python/PyInstaller version bump). User data lives
; entirely outside {app} (%LOCALAPPDATA%\SectorSurveyor\workspace and
; %APPDATA%\SectorSurveyor\config.json -- see gui/config.py for the one-time
; migration from the old %APPDATA%\SectorGapAnalyzer\config.json location),
; so this never touches it.
[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "dist\SectorSurveyor\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent
