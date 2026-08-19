<#
.SYNOPSIS
    Instaluje agenta CMDB na maszynie Windows i rejestruje zadanie cykliczne.

.DESCRIPTION
    Kroki:
      1. kopiuje cmdb-agent.exe do "%ProgramFiles%\CMDB Agent",
      2. zapisuje konfiguracje w "%ProgramData%\CMDB\agent.conf" i zaweza do niej dostep,
      3. rejestruje maszyne w serwerze (wymiana tokenu firmowego na wlasny),
      4. tworzy zadanie harmonogramu dzialajace jako SYSTEM: przy starcie i co N godzin.

    Token firmowy jest potrzebny wylacznie do rejestracji. Po niej maszyna
    posluguje sie wlasnym poswiadczeniem, ktore mozna uniewaznic pojedynczo
    z poziomu panelu.

.EXAMPLE
    .\install-agent.ps1 -ServerUrl https://cmdb.firma.pl -Token cmdb_ent_...

.EXAMPLE
    .\install-agent.ps1 -ServerUrl https://cmdb.firma.pl -Token cmdb_ent_... `
                        -CaBundle C:\pki\firma-ca.pem -IntervalHours 6
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $ServerUrl,
    [Parameter(Mandatory = $true)] [string] $Token,
    [string] $AgentExe   = "$PSScriptRoot\..\dist\cmdb-agent.exe",
    [string] $InstallDir = "$env:ProgramFiles\CMDB Agent",
    [string] $DataDir    = "$env:ProgramData\CMDB",
    [string] $CaBundle,
    [string] $PinSha256,
    [int]    $IntervalHours = 4,
    [string] $TaskName = "CMDB Agent",
    [switch] $NoProcessList
)

$ErrorActionPreference = "Stop"

# --- 0. warunki wstepne -----------------------------------------------------
$identity = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Skrypt wymaga uprawnien administratora (zbiera dane systemowe i tworzy zadanie SYSTEM)."
}
if ($ServerUrl -notmatch '^https://') {
    throw "Adres serwera musi zaczynac sie od https:// - agent nie wysyla danych po nieszyfrowanym polaczeniu."
}
if (-not (Test-Path $AgentExe)) {
    throw "Nie znaleziono $AgentExe. Zbuduj agenta skryptem build-agent.ps1 albo wskaz plik parametrem -AgentExe."
}

Write-Host "== Instalacja agenta CMDB ==" -ForegroundColor Cyan

# --- 1. pliki programu ------------------------------------------------------
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$targetExe = Join-Path $InstallDir "cmdb-agent.exe"

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    Write-Host "Zatrzymuje istniejace zadanie przed podmiana pliku..." -ForegroundColor Yellow
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}
Copy-Item $AgentExe $targetExe -Force
Write-Host "  program        : $targetExe"

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

# Konfiguracja zawiera token firmowy - tylko SYSTEM i administratorzy.
& icacls $DataDir /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" | Out-Null
Write-Host "  konfiguracja   : $configPath (dostep: SYSTEM + Administratorzy)"

# --- 3. rejestracja ---------------------------------------------------------
Write-Host "Rejestruje maszyne w serwerze..." -ForegroundColor Cyan
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

Write-Host "  zadanie        : $TaskName (SYSTEM, przy starcie + co $IntervalHours h)"

# --- 5. pierwszy przebieg ---------------------------------------------------
Write-Host "Wysylam pierwszy raport..." -ForegroundColor Cyan
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
