; Instalator agenta CMDB (Inno Setup 6).
;
; Budowanie:  ISCC.exe cmdb-agent.iss
; Wymaga wczesniejszego zbudowania plikow exe skryptem build-agent.ps1.
;
; Kreator pyta o adres serwera i token rejestracyjny, po czym cala logike
; instalacji wykonuje install-agent.ps1 - ten sam skrypt, ktorego uzywa
; wdrozenie masowe przez GPO. Dzieki temu jest jedno zrodlo prawdy zamiast
; dwoch rozjezdzajacych sie sciezek instalacji.

#define AppName        "CMDB Agent"
#ifndef AppVersion
#define AppVersion     "0.5.9"
#endif
#ifndef BuildDir
#define BuildDir       "..\dist"
#endif
#define AppPublisher   "Dzial IT"
#define AgentExe       "cmdb-agent.exe"

[Setup]
AppId={{7B3A9C42-5E1D-4F8B-9A67-CMDBAGENT0001}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\CMDB Agent
DefaultGroupName=CMDB Agent
DisableProgramGroupPage=yes
OutputDir={#BuildDir}
OutputBaseFilename=CMDB-Agent-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; Agent zbiera dane systemowe i zaklada zadanie dzialajace jako SYSTEM.
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#AgentExe}
SetupLogging=yes
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "polski"; MessagesFile: "compiler:Languages\Polish.isl"

[Files]
Source: "{#BuildDir}\{#AgentExe}";        DestDir: "{app}"; Flags: ignoreversion
Source: "install-agent.ps1";          DestDir: "{app}"; Flags: ignoreversion
Source: "uninstall-agent.ps1";        DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Status agenta CMDB"; Filename: "{app}\{#AgentExe}"; Parameters: "gui"
Name: "{group}\Odinstaluj agenta CMDB"; Filename: "{uninstallexe}"

[Code]
var
  ServerPage: TInputQueryWizardPage;
  OptionsPage: TInputOptionWizardPage;
  CaPage: TInputFileWizardPage;

procedure InitializeWizard;
begin
  ServerPage := CreateInputQueryPage(wpSelectDir,
    'Polaczenie z serwerem CMDB',
    'Podaj dane otrzymane od administratora',
    'Agent bedzie wysylal informacje o tej maszynie na wskazany serwer.' + #13#10 +
    'Token sluzy tylko do pierwszej rejestracji - potem maszyna posluguje sie' + #13#10 +
    'wlasnym poswiadczeniem.');
  ServerPage.Add('Adres serwera (wymagane https):', False);
  ServerPage.Add('Token (pozostaw pusty przy aktualizacji tej samej rejestracji):', True);
  ServerPage.Values[0] := 'https://';

  OptionsPage := CreateInputOptionPage(ServerPage.ID,
    'Opcje agenta', 'Zakres zbieranych danych i czestotliwosc',
    'Ustawienia mozna zmienic pozniej z ikony agenta w zasobniku.',
    False, False);
  OptionsPage.Add('Raportuj co 4 godziny (odznacz, aby raportowac co 8 godzin)');
  OptionsPage.Add('Zbieraj liste uruchomionych procesow');
  OptionsPage.Add('Pokazuj ikone agenta w zasobniku systemowym');
  OptionsPage.Values[0] := True;
  OptionsPage.Values[1] := True;
  OptionsPage.Values[2] := True;

  CaPage := CreateInputFilePage(OptionsPage.ID,
    'Certyfikat CA (opcjonalnie)',
    'Tylko gdy serwer uzywa certyfikatu wewnetrznego CA firmy',
    'Jesli serwer ma certyfikat publicznego urzedu (np. Let''s Encrypt),' + #13#10 +
    'pozostaw to pole puste.');
  CaPage.Add('Plik certyfikatu CA:', 'Certyfikaty|*.pem;*.crt;*.cer|Wszystkie pliki|*.*', '.pem');
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  Server, Token: String;
begin
  Result := True;
  if CurPageID = ServerPage.ID then
  begin
    Server := Trim(ServerPage.Values[0]);
    Token := Trim(ServerPage.Values[1]);

    if Pos('https://', LowerCase(Server)) <> 1 then
    begin
      MsgBox('Adres serwera musi zaczynac sie od https://' + #13#10 + #13#10 +
             'Agent nie wysyla danych ani tokenu po nieszyfrowanym polaczeniu.',
             mbError, MB_OK);
      Result := False;
      Exit;
    end;
    if Length(Server) <= 8 then
    begin
      MsgBox('Podaj pelny adres serwera, np. https://cmdb.twojafirma.pl', mbError, MB_OK);
      Result := False;
      Exit;
    end;
    if (Token <> '') and (Pos('cmdb_ent_', Token) <> 1) then
    begin
      MsgBox('To nie wyglada na token rejestracyjny.' + #13#10 + #13#10 +
             'Token wydany przez administratora zaczyna sie od "cmdb_ent_".',
             mbError, MB_OK);
      Result := False;
      Exit;
    end;
  end;
end;

function BuildInstallArguments(): String;
var
  Args, CaFile: String;
begin
  Args := '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{app}\install-agent.ps1') + '"' +
          ' -ServerUrl "' + Trim(ServerPage.Values[0]) + '"' +
          ' -Token "' + Trim(ServerPage.Values[1]) + '"' +
          ' -AgentExe "' + ExpandConstant('{app}\{#AgentExe}') + '"' +
          ' -InstallDir "' + ExpandConstant('{app}') + '"' +
          ' -Silent';

  if OptionsPage.Values[0] then
    Args := Args + ' -IntervalHours 4'
  else
    Args := Args + ' -IntervalHours 8';

  if not OptionsPage.Values[1] then
    Args := Args + ' -NoProcessList';
  if not OptionsPage.Values[2] then
    Args := Args + ' -NoTray';

  CaFile := Trim(CaPage.Values[0]);
  if CaFile <> '' then
    Args := Args + ' -CaBundle "' + CaFile + '"';

  Result := Args;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
begin
  if CurStep = ssPostInstall then
  begin
    WizardForm.StatusLabel.Caption := 'Rejestruje maszyne w serwerze CMDB...';
    if not Exec('powershell.exe', BuildInstallArguments(), '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    begin
      MsgBox('Nie udalo sie uruchomic skryptu instalacyjnego (kod ' + IntToStr(ResultCode) + ').',
             mbError, MB_OK);
      Exit;
    end;
    if ResultCode <> 0 then
      MsgBox('Agent zostal zainstalowany, ale rejestracja w serwerze nie powiodla sie.' + #13#10 + #13#10 +
             'Sprawdz adres serwera, token i polaczenie sieciowe, a nastepnie' + #13#10 +
             'popraw ustawienia z ikony agenta w zasobniku (Ustawienia...).',
             mbInformation, MB_OK);
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
begin
  if CurUninstallStep = usUninstall then
    Exec('powershell.exe',
         '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{app}\uninstall-agent.ps1') + '"' +
         ' -InstallDir "' + ExpandConstant('{app}') + '" -KeepFiles',
         '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

[Run]
Filename: "{app}\{#AgentExe}"; Parameters: "gui"; Description: "Uruchom ikone agenta w zasobniku"; \
    Flags: postinstall nowait skipifsilent skipifdoesntexist
