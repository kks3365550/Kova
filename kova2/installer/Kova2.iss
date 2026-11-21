; =========================================================================
; Kova2 Inno Setup 6+ 인스톨러 빌드 스크립트
; =========================================================================

#define MyAppName "Kova2"
#define MyAppVersion "3.15.4"
#define MyAppPublisher "kks3365550"

[Setup]
AppId={{6D2D7FAF-4511-4204-B992-03B93CBDE655}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=Kova2_Setup_v{#MyAppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
SetupIconFile=..\crates\kova-app\assets\kova.ico
UninstallDisplayIcon={app}\kova-app.exe

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\target\release\kova-service.exe"; DestDir: "{app}"; Flags: ignoreversion restartreplace
Source: "..\target\release\kova-app.exe"; DestDir: "{app}"; Flags: ignoreversion restartreplace
Source: "..\target\release\kova-pe.exe"; DestDir: "{app}"; Flags: ignoreversion restartreplace
Source: "make_pe.ps1"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\Kova2"; Filename: "{app}\kova-app.exe"
Name: "{autodesktop}\Kova2"; Filename: "{app}\kova-app.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\kova-service.exe"; Parameters: "--install"; Flags: runhidden waituntilterminated; Check: not IsKovaSvcInstalled
Filename: "{app}\kova-service.exe"; Parameters: "--harden"; Flags: runhidden waituntilterminated
Filename: "{sys}\sc.exe"; Parameters: "failureflag KovaSvc 1"; Flags: runhidden waituntilterminated
Filename: "{sys}\sc.exe"; Parameters: "start KovaSvc"; Flags: runhidden waituntilterminated
Filename: "{app}\kova-app.exe"; Description: "Kova2 앱 실행"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /T /IM kova-app.exe /IM kova-pe.exe"; Flags: runhidden waituntilterminated
Filename: "{sys}\sc.exe"; Parameters: "stop KovaSvc"; Flags: runhidden waituntilterminated
Filename: "{app}\kova-service.exe"; Parameters: "--uninstall"; Flags: runhidden waituntilterminated
Filename: "{sys}\taskkill.exe"; Parameters: "/F /T /IM kova-service.exe"; Flags: runhidden waituntilterminated

[InstallDelete]
Type: filesandordirs; Name: "{app}\is-*.tmp"

[Code]
procedure KillKovaProcesses();
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /T /IM kova-app.exe /IM kova-service.exe /IM kova-pe.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /T /IM kova-app.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /T /IM kova-pe.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /T /IM kova-service.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

function InitializeSetup(): Boolean;
begin
  KillKovaProcesses();
  Result := True;
end;

function IsKovaSvcInstalled(): Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec(ExpandConstant('{sys}\sc.exe'), 'query KovaSvc', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
end;

function IsKovaSvcStopped(): Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec(ExpandConstant('{cmd}'), '/c sc query KovaSvc | findstr /i STOPPED', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
  I: Integer;
begin
  Result := '';
  KillKovaProcesses();
  if IsKovaSvcInstalled() then
  begin
    Exec(ExpandConstant('{sys}\sc.exe'), 'stop KovaSvc', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    for I := 1 to 30 do
    begin
      if IsKovaSvcStopped() then
        Break;
      Sleep(1000);
    end;
  end;
  KillKovaProcesses();
end;

procedure CleanTempFiles();
var
  FindRec: TFindRec;
  Target: String;
begin
  if FindFirst(ExpandConstant('{app}\is-*.tmp'), FindRec) then
  begin
    try
      repeat
        Target := ExpandConstant('{app}\') + FindRec.Name;
        if (FindRec.Attributes and 16) <> 0 then
          DelTree(Target, True, True, True)
        else
          DeleteFile(Target);
      until not FindNext(FindRec);
    finally
      FindClose(FindRec);
    end;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
  begin
    KillKovaProcesses();
  end;
  if CurStep = ssPostInstall then
  begin
    CleanTempFiles();
  end;
end;
