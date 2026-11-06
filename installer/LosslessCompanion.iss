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
Filename: "{app}\LosslessCompanion.exe"; Description: "Launch LS Companion as administrator"; Flags: nowait postinstall skipifsilent runascurrentuser
Filename: "{app}\LosslessCompanion.exe"; Flags: nowait runascurrentuser; Check: IsAutomaticUpdate

[Code]
var
  AutomaticUpdateSucceeded: Boolean;

function OpenProcess(Access: LongWord; InheritHandle: Boolean; ProcessId: LongWord): THandle;
  external 'OpenProcess@kernel32.dll stdcall';
function WaitForSingleObject(Handle: THandle; Milliseconds: LongWord): LongWord;
  external 'WaitForSingleObject@kernel32.dll stdcall';
function CloseHandle(Handle: THandle): Boolean;
  external 'CloseHandle@kernel32.dll stdcall';

function IsAutomaticUpdate: Boolean;
begin
  Result := ExpandConstant('{param:AUTOMATICUPDATE|0}') = '1';
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssDone then AutomaticUpdateSucceeded := True;
end;

procedure DeinitializeSetup;
var
  PreviousApplication: String;
  ResultCode: Integer;
begin
  if not IsAutomaticUpdate or AutomaticUpdateSucceeded then exit;
  PreviousApplication := ExpandConstant('{param:DIR|}') + '\LosslessCompanion.exe';
  if FileExists(PreviousApplication) then
    Exec(PreviousApplication, '', ExtractFileDir(PreviousApplication), SW_SHOWNORMAL, ewNoWait, ResultCode);
end;

function InitializeSetup: Boolean;
var
  ProcessId: Integer;
  ProcessHandle: THandle;
begin
  Result := True;
  if not IsAutomaticUpdate then exit;
  ProcessId := StrToIntDef(ExpandConstant('{param:WAITPID|0}'), 0);
  if ProcessId <= 0 then begin Result := False; exit; end;
  ProcessHandle := OpenProcess($00100000, False, ProcessId);
  if ProcessHandle = 0 then begin
    { ERROR_INVALID_PARAMETER means Companion has already exited. }
    Result := DLLGetLastError = 87;
    exit;
  end;
  try
    Result := WaitForSingleObject(ProcessHandle, 60000) = 0;
    if not Result then Log('Companion did not exit; automatic update aborted before replacing files.');
  finally
    CloseHandle(ProcessHandle);
  end;
end;
