<#
.SYNOPSIS
    Instaluje agenta CMDB na maszynie Windows.

.DESCRIPTION
    Jedno miejsce z logika instalacji - uzywane zarowno przez instalator
    graficzny (setup.exe), jak i przy wdrozeniu masowym przez GPO/Intune.

    Kroki:
      1. kopiuje pliki do "%ProgramFiles%\CMDB Agent" (pomijane, gdy pliki
         umiescil tam juz instalator),
      2. zapisuje konfiguracje w "%ProgramData%\CMDB\agent.conf" i zaweza do
         niej dostep - plik zawiera token firmowy,
      3. tworzy podkatalog "public" na plik statusu, czytelny dla wszystkich
         uzytkownikow (sam status nie zawiera zadnych sekretow),
      4. rejestruje maszyne w serwerze (wymiana tokenu firmowego na wlasny),
      5. tworzy zadanie harmonogramu dzialajace jako SYSTEM: przy starcie
         i co N godzin, z prawem uruchomienia dla zwyklych uzytkownikow,
      6. dodaje ikone w zasobniku do autostartu.

.EXAMPLE
    .\install-agent.ps1 -ServerUrl https://cmdb.firma.pl -Token cmdb_ent_...

.EXAMPLE
    .\install-agent.ps1 -ServerUrl https://cmdb.firma.pl -Token cmdb_ent_... `
                        -CaBundle C:\pki\firma-ca.pem -IntervalHours 6 -Silent
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $ServerUrl,
    [Parameter(Mandatory = $true)] [string] $Token,
    [string] $AgentExe   = "$PSScriptRoot\..\dist\cmdb-agent.exe",
    [string] $TrayExe,
    [string] $InstallDir = "$env:ProgramFiles\CMDB Agent",
    [string] $DataDir    = "$env:ProgramData\CMDB",
    [string] $CaBundle,
    [string] $PinSha256,
    [int]    $IntervalHours = 4,
    [string] $TaskName = "CMDB Agent",
    [switch] $NoProcessList,
    [switch] $NoTray,
    # Tryb dla instalatora graficznego: bez interaktywnych komunikatow.
    [switch] $Silent
)

$ErrorActionPreference = "Stop"

function Write-Step($text) {
    if (-not $Silent) { Write-Host $text -ForegroundColor Cyan } else { Write-Host $text }
}

# --- 0. warunki wstepne -----------------------------------------------------
$identity = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Skrypt wymaga uprawnien administratora (zbiera dane systemowe i tworzy zadanie SYSTEM)."
}
if ($ServerUrl -notmatch '^https://') {
    throw "Adres serwera musi zaczynac sie od https:// - agent nie wysyla danych po nieszyfrowanym polaczeniu."
}
if ($Token -notmatch '^cmdb_ent_') {
    throw "To nie wyglada na token rejestracyjny - powinien zaczynac sie od 'cmdb_ent_'."
}
if (-not (Test-Path $AgentExe)) {
    throw "Nie znaleziono $AgentExe. Zbuduj agenta skryptem build-agent.ps1 albo wskaz plik parametrem -AgentExe."
}

Write-Step "== Instalacja agenta CMDB =="

# --- 1. pliki programu ------------------------------------------------------
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$targetExe = Join-Path $InstallDir "cmdb-agent.exe"
$targetTray = Join-Path $InstallDir "cmdb-agent-tray.exe"

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    Write-Step "Zatrzymuje istniejace zadanie przed podmiana plikow..."
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}

# Gdy pliki leza juz na miejscu (umiescil je instalator), kopiowanie pomijamy -
# Copy-Item pliku na samego siebie konczy sie bledem.
$sourceExe = (Resolve-Path $AgentExe).Path
if ($sourceExe -ne [System.IO.Path]::GetFullPath($targetExe)) {
    Copy-Item $sourceExe $targetExe -Force
}
if (-not $TrayExe) {
    $candidate = Join-Path (Split-Path $AgentExe -Parent) "cmdb-agent-tray.exe"
    if (Test-Path $candidate) { $TrayExe = $candidate }
}
if ($TrayExe -and (Test-Path $TrayExe)) {
    $sourceTray = (Resolve-Path $TrayExe).Path
    if ($sourceTray -ne [System.IO.Path]::GetFullPath($targetTray)) {
        Copy-Item $sourceTray $targetTray -Force
    }
}
Write-Step "  program        : $targetExe"

# --- 2. konfiguracja --------------------------------------------------------
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
$configPath = Join-Path $DataDir "agent.conf"

$config = [ordered]@{
    server_url              = $ServerUrl.TrimEnd('/')
    enrollment_token        = $Token
    report_interval_seconds = $IntervalHours * 3600
    log_level               = "INFO"
    collect_processes       = (-not $NoProcessList.IsPresent)
}
if ($CaBundle)  { $config.ca_bundle  = $CaBundle }
if ($PinSha256) { $config.pin_sha256 = $PinSha256 }

$config | ConvertTo-Json -Depth 3 | Set-Content -Path $configPath -Encoding UTF8

# Konfiguracja i poswiadczenie: tylko SYSTEM i administratorzy.
& icacls $DataDir /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" | Out-Null

# Status czytany przez ikone w zasobniku - osobny podkatalog, bez sekretow,
# z prawem odczytu dla grupy Uzytkownicy.
$publicDir = Join-Path $DataDir "public"
New-Item -ItemType Directory -Force -Path $publicDir | Out-Null
& icacls $publicDir /grant:r "*S-1-5-32-545:(OI)(CI)RX" | Out-Null
Write-Step "  konfiguracja   : $configPath (dostep: SYSTEM + Administratorzy)"

# --- 3. rejestracja ---------------------------------------------------------
Write-Step "Rejestruje maszyne w serwerze..."
& $targetExe enroll
if ($LASTEXITCODE -ne 0) {
    throw "Rejestracja nie powiodla sie (kod $LASTEXITCODE). Sprawdz adres serwera, token i zaufanie do certyfikatu."
}

# --- 4. zadanie harmonogramu ------------------------------------------------
$action = New-ScheduledTaskAction -Execute $targetExe -Argument "run" -WorkingDirectory $InstallDir

# Losowe opoznienie rozklada obciazenie serwera przy duzej flocie maszyn.
$atStartup = New-ScheduledTaskTrigger -AtStartup
$atStartup.Delay = "PT3M"
$periodic = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Hours $IntervalHours)
$periodic.RandomDelay = "PT10M"

$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 10)

Register-ScheduledTask -TaskName $TaskName `
    -Description "Agent inwentaryzacyjny CMDB - zbiera dane o sprzecie, oprogramowaniu i kontach." `
    -Action $action -Trigger @($atStartup, $periodic) `
    -Principal $principal -Settings $settings -Force | Out-Null

# Pozwalamy zwyklym uzytkownikom uruchomic zadanie ("Synchronizuj teraz"
# z ikony w zasobniku), ale nie zmieniac go. Bez tego kazde recznie wymuszone
# odswiezenie prosiloby o haslo administratora.
try {
    $scheduler = New-Object -ComObject Schedule.Service
    $scheduler.Connect()
    $folder = $scheduler.GetFolder("\")
    $registered = $folder.GetTask($TaskName)
    # BA = Administratorzy (pelna), SY = SYSTEM (pelna), BU = Uzytkownicy (odczyt + uruchomienie)
    $registered.SetSecurityDescriptor("D:(A;;GA;;;BA)(A;;GA;;;SY)(A;;GRGX;;;BU)", 0)
}
catch {
    Write-Warning "Nie udalo sie nadac uzytkownikom prawa uruchomienia zadania: $($_.Exception.Message)"
    Write-Warning "Przycisk 'Synchronizuj teraz' bedzie prosil o uprawnienia administratora."
}

Write-Step "  zadanie        : $TaskName (SYSTEM, przy starcie + co $IntervalHours h)"

# --- 5. ikona w zasobniku ---------------------------------------------------
$runKey = "HKLM:\Software\Microsoft\Windows\CurrentVersion\Run"
if (-not $NoTray -and (Test-Path $targetTray)) {
    Set-ItemProperty -Path $runKey -Name "CMDB Agent Tray" -Value "`"$targetTray`"" -Force
    Write-Step "  ikona          : $targetTray (autostart dla kazdego uzytkownika)"
}
else {
    Remove-ItemProperty -Path $runKey -Name "CMDB Agent Tray" -ErrorAction SilentlyContinue
}

# --- 6. pierwszy przebieg ---------------------------------------------------
Write-Step "Wysylam pierwszy raport..."
& $targetExe run
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Pierwszy raport nie zostal wyslany (kod $LASTEXITCODE). Zadanie sprobuje ponownie."
}

Write-Host ""
Write-Host "Gotowe." -ForegroundColor Green
& $targetExe status
Write-Host ""
Write-Host "Dziennik agenta: $DataDir\agent.log"
Write-Host "Reczny przebieg: `"$targetExe`" run"
