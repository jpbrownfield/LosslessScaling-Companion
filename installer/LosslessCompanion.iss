#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

[Setup]
AppId={{C12E54C7-4244-4C74-A277-109A802AEC12}
AppName=LS Companion
AppVersion={#MyAppVersion}
AppPublisher=LS Companion
DefaultDirName={autopf}\LS Companion
DefaultGroupName=LS Companion
DisableDirPage=no
DisableProgramGroupPage=yes
OutputDir=..\dist\installer
OutputBaseFilename=LosslessCompanion-Setup-x64
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\LosslessCompanion.exe

[Files]
Source: "..\dist\LosslessCompanion\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Tasks]
Name: "startmenuicon"; Description: "Create a Start Menu shortcut"; GroupDescription: "Shortcuts:"; Flags: checkedonce
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Icons]
Name: "{autoprograms}\LS Companion"; Filename: "{app}\LosslessCompanion.exe"; Tasks: startmenuicon
Name: "{autodesktop}\LS Companion"; Filename: "{app}\LosslessCompanion.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\LosslessCompanion.exe"; Description: "Launch LS Companion"; Flags: nowait postinstall skipifsilent
