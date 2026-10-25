; Installer for GifMaker, built by the GitHub workflow with Inno Setup 6:
;   iscc /DAppVersion=1.0.42 installer\GifMaker.iss
; It installs for the current user (no admin needed), registers GifMaker as a
; video app so it can be picked in Settings > Default apps and "Open with", and
; installs over an older version when run again (which is how updates work).

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6F0E5B7A-2C1D-4E8B-9A57-3B1F0C9D4E21}
AppName=GifMaker
AppVersion={#AppVersion}
AppVerName=GifMaker {#AppVersion}
AppPublisher=GifMaker
DefaultDirName={autopf}\GifMaker
DefaultGroupName=GifMaker
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=GifMaker-Setup
SetupIconFile=..\assets\gifmaker.ico
UninstallDisplayIcon={app}\GifMaker.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ChangesAssociations=yes
CloseApplications=yes
RestartApplications=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[InstallDelete]
; Files from the previous version that the new one may no longer have.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\GifMaker\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[INI]
; Tells the app it was installed, so it can update itself.
Filename: "{app}\install.ini"; Section: "GifMaker"; Key: "version"; String: "{#AppVersion}"

[Icons]
Name: "{group}\GifMaker"; Filename: "{app}\GifMaker.exe"
Name: "{autodesktop}\GifMaker"; Filename: "{app}\GifMaker.exe"; Tasks: desktopicon

[Registry]
; How GifMaker opens a video.
Root: HKA; Subkey: "Software\Classes\GifMaker.Video"; ValueType: string; ValueData: "Video"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\GifMaker.Video\DefaultIcon"; ValueType: string; ValueData: "{app}\GifMaker.exe,0"
Root: HKA; Subkey: "Software\Classes\GifMaker.Video\shell\open\command"; ValueType: string; ValueData: """{app}\GifMaker.exe"" ""%1"""
Root: HKA; Subkey: "Software\Classes\Applications\GifMaker.exe"; ValueType: string; ValueName: "FriendlyAppName"; ValueData: "GifMaker"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\Applications\GifMaker.exe\shell\open\command"; ValueType: string; ValueData: """{app}\GifMaker.exe"" ""%1"""
; What it can open, listed under Settings > Default apps.
Root: HKA; Subkey: "Software\GifMaker"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\GifMaker\Capabilities"; ValueType: string; ValueName: "ApplicationName"; ValueData: "GifMaker"
Root: HKA; Subkey: "Software\GifMaker\Capabilities"; ValueType: string; ValueName: "ApplicationDescription"; ValueData: "Play videos and turn parts of them into GIFs; view pictures"
Root: HKA; Subkey: "Software\RegisteredApplications"; ValueType: string; ValueName: "GifMaker"; ValueData: "Software\GifMaker\Capabilities"; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\.mp4\OpenWithProgids"; ValueType: string; ValueName: "GifMaker.Video"; ValueData: ""; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\Applications\GifMaker.exe\SupportedTypes"; ValueType: string; ValueName: ".mp4"; ValueData: ""
Root: HKA; Subkey: "Software\GifMaker\Capabilities\FileAssociations"; ValueType: string; ValueName: ".mp4"; ValueData: "GifMaker.Video"
Root: HKA; Subkey: "Software\Classes\.mkv\OpenWithProgids"; ValueType: string; ValueName: "GifMaker.Video"; ValueData: ""; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\Applications\GifMaker.exe\SupportedTypes"; ValueType: string; ValueName: ".mkv"; ValueData: ""
Root: HKA; Subkey: "Software\GifMaker\Capabilities\FileAssociations"; ValueType: string; ValueName: ".mkv"; ValueData: "GifMaker.Video"
Root: HKA; Subkey: "Software\Classes\.mov\OpenWithProgids"; ValueType: string; ValueName: "GifMaker.Video"; ValueData: ""; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\Applications\GifMaker.exe\SupportedTypes"; ValueType: string; ValueName: ".mov"; ValueData: ""
Root: HKA; Subkey: "Software\GifMaker\Capabilities\FileAssociations"; ValueType: string; ValueName: ".mov"; ValueData: "GifMaker.Video"
Root: HKA; Subkey: "Software\Classes\.avi\OpenWithProgids"; ValueType: string; ValueName: "GifMaker.Video"; ValueData: ""; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\Applications\GifMaker.exe\SupportedTypes"; ValueType: string; ValueName: ".avi"; ValueData: ""
Root: HKA; Subkey: "Software\GifMaker\Capabilities\FileAssociations"; ValueType: string; ValueName: ".avi"; ValueData: "GifMaker.Video"
Root: HKA; Subkey: "Software\Classes\.webm\OpenWithProgids"; ValueType: string; ValueName: "GifMaker.Video"; ValueData: ""; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\Applications\GifMaker.exe\SupportedTypes"; ValueType: string; ValueName: ".webm"; ValueData: ""
Root: HKA; Subkey: "Software\GifMaker\Capabilities\FileAssociations"; ValueType: string; ValueName: ".webm"; ValueData: "GifMaker.Video"
Root: HKA; Subkey: "Software\Classes\.wmv\OpenWithProgids"; ValueType: string; ValueName: "GifMaker.Video"; ValueData: ""; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\Applications\GifMaker.exe\SupportedTypes"; ValueType: string; ValueName: ".wmv"; ValueData: ""
Root: HKA; Subkey: "Software\GifMaker\Capabilities\FileAssociations"; ValueType: string; ValueName: ".wmv"; ValueData: "GifMaker.Video"
Root: HKA; Subkey: "Software\Classes\.flv\OpenWithProgids"; ValueType: string; ValueName: "GifMaker.Video"; ValueData: ""; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\Applications\GifMaker.exe\SupportedTypes"; ValueType: string; ValueName: ".flv"; ValueData: ""
Root: HKA; Subkey: "Software\GifMaker\Capabilities\FileAssociations"; ValueType: string; ValueName: ".flv"; ValueData: "GifMaker.Video"
Root: HKA; Subkey: "Software\Classes\.m4v\OpenWithProgids"; ValueType: string; ValueName: "GifMaker.Video"; ValueData: ""; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\Applications\GifMaker.exe\SupportedTypes"; ValueType: string; ValueName: ".m4v"; ValueData: ""
Root: HKA; Subkey: "Software\GifMaker\Capabilities\FileAssociations"; ValueType: string; ValueName: ".m4v"; ValueData: "GifMaker.Video"
Root: HKA; Subkey: "Software\Classes\.mpg\OpenWithProgids"; ValueType: string; ValueName: "GifMaker.Video"; ValueData: ""; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\Applications\GifMaker.exe\SupportedTypes"; ValueType: string; ValueName: ".mpg"; ValueData: ""
Root: HKA; Subkey: "Software\GifMaker\Capabilities\FileAssociations"; ValueType: string; ValueName: ".mpg"; ValueData: "GifMaker.Video"
Root: HKA; Subkey: "Software\Classes\.mpeg\OpenWithProgids"; ValueType: string; ValueName: "GifMaker.Video"; ValueData: ""; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\Applications\GifMaker.exe\SupportedTypes"; ValueType: string; ValueName: ".mpeg"; ValueData: ""
Root: HKA; Subkey: "Software\GifMaker\Capabilities\FileAssociations"; ValueType: string; ValueName: ".mpeg"; ValueData: "GifMaker.Video"
Root: HKA; Subkey: "Software\Classes\.ts\OpenWithProgids"; ValueType: string; ValueName: "GifMaker.Video"; ValueData: ""; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\Applications\GifMaker.exe\SupportedTypes"; ValueType: string; ValueName: ".ts"; ValueData: ""
Root: HKA; Subkey: "Software\GifMaker\Capabilities\FileAssociations"; ValueType: string; ValueName: ".ts"; ValueData: "GifMaker.Video"
Root: HKA; Subkey: "Software\Classes\.gif\OpenWithProgids"; ValueType: string; ValueName: "GifMaker.Video"; ValueData: ""; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\Applications\GifMaker.exe\SupportedTypes"; ValueType: string; ValueName: ".gif"; ValueData: ""
Root: HKA; Subkey: "Software\GifMaker\Capabilities\FileAssociations"; ValueType: string; ValueName: ".gif"; ValueData: "GifMaker.Video"
; Pictures open in GifMaker too.
Root: HKA; Subkey: "Software\Classes\GifMaker.Picture"; ValueType: string; ValueData: "Picture"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\GifMaker.Picture\DefaultIcon"; ValueType: string; ValueData: "{app}\GifMaker.exe,0"
Root: HKA; Subkey: "Software\Classes\GifMaker.Picture\shell\open\command"; ValueType: string; ValueData: """{app}\GifMaker.exe"" ""%1"""
Root: HKA; Subkey: "Software\Classes\.jpg\OpenWithProgids"; ValueType: string; ValueName: "GifMaker.Picture"; ValueData: ""; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\Applications\GifMaker.exe\SupportedTypes"; ValueType: string; ValueName: ".jpg"; ValueData: ""
Root: HKA; Subkey: "Software\GifMaker\Capabilities\FileAssociations"; ValueType: string; ValueName: ".jpg"; ValueData: "GifMaker.Picture"
Root: HKA; Subkey: "Software\Classes\.jpeg\OpenWithProgids"; ValueType: string; ValueName: "GifMaker.Picture"; ValueData: ""; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\Applications\GifMaker.exe\SupportedTypes"; ValueType: string; ValueName: ".jpeg"; ValueData: ""
Root: HKA; Subkey: "Software\GifMaker\Capabilities\FileAssociations"; ValueType: string; ValueName: ".jpeg"; ValueData: "GifMaker.Picture"
Root: HKA; Subkey: "Software\Classes\.png\OpenWithProgids"; ValueType: string; ValueName: "GifMaker.Picture"; ValueData: ""; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\Applications\GifMaker.exe\SupportedTypes"; ValueType: string; ValueName: ".png"; ValueData: ""
Root: HKA; Subkey: "Software\GifMaker\Capabilities\FileAssociations"; ValueType: string; ValueName: ".png"; ValueData: "GifMaker.Picture"
Root: HKA; Subkey: "Software\Classes\.bmp\OpenWithProgids"; ValueType: string; ValueName: "GifMaker.Picture"; ValueData: ""; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\Applications\GifMaker.exe\SupportedTypes"; ValueType: string; ValueName: ".bmp"; ValueData: ""
Root: HKA; Subkey: "Software\GifMaker\Capabilities\FileAssociations"; ValueType: string; ValueName: ".bmp"; ValueData: "GifMaker.Picture"
Root: HKA; Subkey: "Software\Classes\.webp\OpenWithProgids"; ValueType: string; ValueName: "GifMaker.Picture"; ValueData: ""; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\Applications\GifMaker.exe\SupportedTypes"; ValueType: string; ValueName: ".webp"; ValueData: ""
Root: HKA; Subkey: "Software\GifMaker\Capabilities\FileAssociations"; ValueType: string; ValueName: ".webp"; ValueData: "GifMaker.Picture"
Root: HKA; Subkey: "Software\Classes\.tif\OpenWithProgids"; ValueType: string; ValueName: "GifMaker.Picture"; ValueData: ""; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\Applications\GifMaker.exe\SupportedTypes"; ValueType: string; ValueName: ".tif"; ValueData: ""
Root: HKA; Subkey: "Software\GifMaker\Capabilities\FileAssociations"; ValueType: string; ValueName: ".tif"; ValueData: "GifMaker.Picture"
Root: HKA; Subkey: "Software\Classes\.tiff\OpenWithProgids"; ValueType: string; ValueName: "GifMaker.Picture"; ValueData: ""; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\Applications\GifMaker.exe\SupportedTypes"; ValueType: string; ValueName: ".tiff"; ValueData: ""
Root: HKA; Subkey: "Software\GifMaker\Capabilities\FileAssociations"; ValueType: string; ValueName: ".tiff"; ValueData: "GifMaker.Picture"

[Run]
; A normal install offers to start the app and to open the default apps settings;
; a silent one (an update from inside the app) just starts it again.
Filename: "{app}\GifMaker.exe"; Description: "Start GifMaker"; Flags: nowait postinstall skipifsilent
Filename: "ms-settings:defaultapps?registeredAppUser=GifMaker"; Description: "Make GifMaker the default for videos or pictures (opens Windows Settings)"; Flags: shellexec postinstall skipifsilent unchecked
Filename: "{app}\GifMaker.exe"; Flags: nowait; Check: ShouldRelaunch

[Code]
// After a silent update, start the app again (unless /NOLAUNCH, used by the build test).
function ShouldRelaunch: Boolean;
begin
  Result := WizardSilent and (Pos('/NOLAUNCH', UpperCase(GetCmdTail)) = 0);
end;
