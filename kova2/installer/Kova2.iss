; =========================================================================
; Kova2 Inno Setup 6+ 인스톨러 빌드 스크립트
; =========================================================================

#define MyAppName "Kova2"
#define MyAppVersion "3.16.1"
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
; 업그레이드(이미 등록된 서비스)도 이번 설치 경로의 실행 파일을 가리키게 한다(따옴표 포함 경로)
Filename: "{sys}\sc.exe"; Parameters: "config KovaSvc binPath= ""\""{app}\kova-service.exe\"""""; Flags: runhidden waituntilterminated
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

{ 화면 앱과 WinPE 복원 도구만 끝낸다(서비스는 StopKovaService 로 정상 중지). }
procedure KillKovaApps();
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /T /IM kova-app.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /T /IM kova-pe.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

{ 서비스를 sc stop 으로 멈추고 STOPPED 가 될 때까지 기다린다.
  서비스는 중지 요청을 받으면 실행 중 작업을 취소하고(섀도 복사본·잠금 정리) 최대 25초 기다린 뒤 멈춘다.
  상한(45초)을 넘겨도 남아 있을 때만 강제 종료한다. }
procedure StopKovaService();
var
  ResultCode: Integer;
  I: Integer;
begin
  if IsKovaSvcInstalled() and not IsKovaSvcStopped() then
  begin
    Exec(ExpandConstant('{sys}\sc.exe'), 'stop KovaSvc', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    for I := 1 to 45 do
    begin
      if IsKovaSvcStopped() then
        Break;
      Sleep(1000);
    end;
  end;
  { 서비스가 멈췄으면 프로세스가 없어 아무 일도 하지 않는다(콘솔 실행본·응답 없는 서비스만 끝낸다) }
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /T /IM kova-service.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

function InitializeSetup(): Boolean;
begin
  { 설치를 확정하기 전(취소할 수도 있음)에는 아무 프로세스도 끝내지 않는다. 중지는 PrepareToInstall 에서 한다. }
  Result := True;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  KillKovaApps();
  StopKovaService();
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
    KillKovaApps();
    StopKovaService();
  end;
  if CurStep = ssPostInstall then
  begin
    CleanTempFiles();
  end;
end;

{ 제거: 서비스를 먼저 정상 중지하고, 끝난 뒤 비밀 자료(구글 토큰·저장소 암호화 키) 삭제 여부를 묻는다.
  조용한 제거(/SILENT)에서는 묻지 않고 남겨 둔다(키를 잃으면 암호화 백업을 복원할 수 없으므로 기본은 보존). }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Token: String;
  Keys: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    KillKovaApps();
    StopKovaService();
  end;
  if (CurUninstallStep = usPostUninstall) and not UninstallSilent() then
  begin
    Token := ExpandConstant('{commonappdata}\Kova2\config\gdrive_token.bin');
    Keys := ExpandConstant('{commonappdata}\Kova2\keys');
    if FileExists(Token) then
    begin
      if MsgBox('구글 드라이브 연결 토큰을 이 PC에서 삭제할까요?' + #13#10 + #13#10 + Token + #13#10 + #13#10 +
                '삭제하면 다시 설치한 뒤 구글 드라이브를 다시 연결해야 합니다.', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DeleteFile(Token);
    end;
    if DirExists(Keys) then
    begin
      if MsgBox('저장소 암호화 키 폴더를 삭제할까요?' + #13#10 + #13#10 + Keys + #13#10 + #13#10 +
                '주의: 키를 따로 보관(내보내기)하지 않았다면, 삭제 후에는 암호화된 백업을 복원할 수 없습니다.' + #13#10 +
                '확실하지 않으면 [아니요]를 누르세요.', mbError, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(Keys, True, True, True);
    end;
  end;
end;
