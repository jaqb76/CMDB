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
    [string] $ServerUrl,
    [string] $Token,
    [string] $AgentExe,
    [string] $TrayExe, # przestarzaly parametr; jeden EXE zawiera oba tryby
    # ProgramW6432 wskazuje 64-bitowy Program Files takze wtedy, gdy skrypt
    # uruchomiono z 32-bitowego PowerShella - inaczej agent laduje w
    # "Program Files (x86)", co dla programu 64-bitowego jest mylace.
    [string] $InstallDir = "$(if ($env:ProgramW6432) { $env:ProgramW6432 } else { $env:ProgramFiles })\CMDB Agent",
    [string] $DataDir    = "$env:ProgramData\CMDB",
    [string] $CaBundle,
    [string] $PinSha256,
    [int]    $IntervalHours = 4,
    [string] $TaskName = "CMDB Agent",
    [string] $MonitorTaskName = "CMDB Agent Monitor",
    [switch] $NoProcessList,
    [switch] $NoTray,
    # Tryb dla instalatora graficznego: bez interaktywnych komunikatow.
    [switch] $Silent
)

$ErrorActionPreference = "Stop"

# $PSScriptRoot bywa pusty w bloku param(), a wtedy "$PSScriptRoot\..\dist"
# rozwija sie do "\..\dist" - czyli katalogu glownego dysku. Sciezki
# domyslne liczymy dopiero tutaj.
$scriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $AgentExe) { $AgentExe = Join-Path $scriptDir "..\dist\cmdb-agent.exe" }
$configPath = Join-Path $DataDir "agent.conf"
$config = @{}
if (Test-Path $configPath) {
    $existing = Get-Content -Raw -Encoding UTF8 $configPath | ConvertFrom-Json
    foreach ($property in $existing.PSObject.Properties) { $config[$property.Name] = $property.Value }
}
if (-not $ServerUrl) { $ServerUrl = $config.server_url }
$reuseEnrollment = $false
$statePath = Join-Path $DataDir "agent-state.json"
if (-not $Token -and (Test-Path $statePath) -and $ServerUrl) {
    $state = Get-Content -Raw -Encoding UTF8 $statePath | ConvertFrom-Json
    $reuseEnrollment = $state.agent_token -and $state.asset_id -and
        ([string]$state.server_url).TrimEnd('/') -eq $ServerUrl.TrimEnd('/')
}

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
if (-not $reuseEnrollment -and $Token -notmatch '^cmdb_ent_') {
    throw "Pierwsza rejestracja wymaga tokenu zaczynajacego sie od 'cmdb_ent_'. Przy aktualizacji tej samej rejestracji pozostaw Token pusty."
}
if (-not (Test-Path $AgentExe)) {
    throw "Nie znaleziono $AgentExe. Zbuduj agenta skryptem build-agent.ps1 albo wskaz plik parametrem -AgentExe."
}

# Kazdy plik da sie skopiowac pod nazwa .exe, ale nie kazdy da sie uruchomic.
# Bez tej kontroli skrypt kopiowal np. plik .py, a blad wychodzil dopiero przy
# probie rejestracji - z komunikatem systemu o "nieprawidlowej aplikacji".
$naglowek = [System.IO.File]::ReadAllBytes((Resolve-Path $AgentExe).Path) |
            Select-Object -First 2
if (($naglowek.Count -lt 2) -or ($naglowek[0] -ne 0x4D) -or ($naglowek[1] -ne 0x5A)) {
    throw @"
Plik $AgentExe nie jest programem Windows (brak sygnatury MZ).

Parametr -AgentExe wskazuje na zbudowany cmdb-agent.exe, a nie na plik zrodlowy.
Zbuduj agenta:

    cd $PSScriptRoot
    .\build-agent.ps1

a nastepnie uruchom instalacje ponownie bez parametru -AgentExe.
"@
}

# Sprawdz kontrakt pojedynczego EXE przed zatrzymaniem starej instalacji.
# Stopka poprzedza ewentualny podpis Authenticode; szukamy w ograniczonym ogonie.
$binary = [System.IO.File]::OpenRead((Resolve-Path $AgentExe).Path)
try {
    [void]$binary.Seek([Math]::Max(0, $binary.Length - 65536), [System.IO.SeekOrigin]::Begin)
    $tail = New-Object byte[] ([int]($binary.Length - $binary.Position))
    [void]$binary.Read($tail, 0, $tail.Length)
} finally { $binary.Dispose() }
$match = [regex]::Match([System.Text.Encoding]::UTF8.GetString($tail), '<<<CMDB-AGENT-META>>>([^\r\n]*?)<<<KONIEC>>>', 'RightToLeft')
if (-not $match.Success -or ($match.Groups[1].Value | ConvertFrom-Json).entry_mode -ne 'unified') {
    throw "Wymagany polaczony cmdb-agent.exe (build 0.5.9 lub nowszy). Wgraj i aktywuj nowe wydanie na serwerze. Instalacja nie zostala zmieniona."
}

Write-Step "== Instalacja agenta CMDB =="

# --- 1. pliki programu ------------------------------------------------------
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$targetExe = Join-Path $InstallDir "cmdb-agent.exe"
$legacyTray = Join-Path $InstallDir "cmdb-agent-tray.exe"

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    Write-Step "Zatrzymuje istniejace zadanie przed podmiana plikow..."
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}

# Zatrzymaj tylko procesy z aktualizowanej instalacji, nie inne kopie programu.
Get-Process -Name "cmdb-agent", "cmdb-agent-tray" -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and ($_.Path -eq $targetExe -or $_.Path -eq $legacyTray) } |
    Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 500

# Gdy pliki leza juz na miejscu (umiescil je instalator), kopiowanie pomijamy -
# Copy-Item pliku na samego siebie konczy sie bledem.
$sourceExe = (Resolve-Path $AgentExe).Path
if ($sourceExe -ne [System.IO.Path]::GetFullPath($targetExe)) {
    Copy-Item $sourceExe $targetExe -Force
}

# Pozostalosc po instalacji w drugim Program Files (np. po uruchomieniu
# skryptu z 32-bitowego PowerShella) tylko myli przy diagnozie.
foreach ($inny in @("${env:ProgramFiles(x86)}\CMDB Agent", "${env:ProgramFiles}\CMDB Agent",
                    "$env:ProgramW6432\CMDB Agent")) {
    if ($inny -and (Test-Path $inny) -and
        ([System.IO.Path]::GetFullPath($inny) -ne [System.IO.Path]::GetFullPath($InstallDir))) {
        Remove-Item $inny -Recurse -Force -ErrorAction SilentlyContinue
        Write-Step "  usunieto stara instalacje: $inny"
    }
}
if (Test-Path $legacyTray) {
    # Zachowaj stary plik do odzyskania; autostart bedzie wskazywal nowy EXE.
    Move-Item $legacyTray ($legacyTray + ".legacy.bak") -Force
}
# Sprawdzamy, ze skopiowany plik naprawde dziala. Bez tego blad w samym
# programie wychodzi dopiero przy rejestracji - juz po zapisaniu konfiguracji
# z tokenem - a przy podmianie na nowsza wersje latwo nie zauwazyc, ze
# w katalogu docelowym zostal stary plik.
function Invoke-Agent([string[]] $AgentArguments) {
    $start = New-Object System.Diagnostics.ProcessStartInfo
    $start.FileName = $targetExe
    # Argumenty to polecenia stale i sciezka konfiguracji (bez danych logowania).
    $start.Arguments = ($AgentArguments | ForEach-Object { '"' + $_ + '"' }) -join ' '
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $start
    try {
        [void]$process.Start()
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(1800000)) {
            $process.Kill()
            throw "Przekroczono czas operacji agenta."
        }
        return @{ Code = $process.ExitCode; Output = $stdout.Result + $stderr.Result }
    }
    finally { $process.Dispose() }
}
$versionResult = Invoke-Agent @("--version")
if ($versionResult.Code -ne 0 -or $versionResult.Output -notmatch 'cmdb-agent ') {
    throw "Skopiowany $targetExe nie uruchamia sie poprawnie: $($versionResult.Output)"
}
$wersja = $versionResult.Output.Trim()
Write-Step "  program        : $targetExe ($wersja)"

# --- 2. konfiguracja --------------------------------------------------------
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
$config.server_url = $ServerUrl.TrimEnd('/')
$config.data_dir = $DataDir
if ($reuseEnrollment) { $config.Remove("enrollment_token") }
else { $config.enrollment_token = $Token }
if ($PSBoundParameters.ContainsKey("IntervalHours") -or -not $config.ContainsKey("report_interval_seconds")) {
    $config.report_interval_seconds = $IntervalHours * 3600
}
if (-not $config.ContainsKey("log_level")) { $config.log_level = "INFO" }
if ($NoProcessList -or -not $config.ContainsKey("collect_processes")) { $config.collect_processes = (-not $NoProcessList.IsPresent) }
if ($CaBundle)  { $config.ca_bundle  = $CaBundle }
if ($PinSha256) { $config.pin_sha256 = $PinSha256 }

# Set-Content -Encoding UTF8 dopisuje w Windows PowerShell 5.1 znacznik BOM,
# na ktorym parser JSON w Pythonie sie wywraca. UTF8Encoding($false) zapisuje
# czyste UTF-8, bez znacznika.
$json = $config | ConvertTo-Json -Depth 3
[System.IO.File]::WriteAllText($configPath, $json, (New-Object System.Text.UTF8Encoding $false))

# Konfiguracja i poswiadczenie: tylko SYSTEM i administratorzy.
& icacls $DataDir /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" | Out-Null

# Status czytany przez ikone w zasobniku - osobny podkatalog, bez sekretow,
# z prawem odczytu dla grupy Uzytkownicy.
$publicDir = Join-Path $DataDir "public"
New-Item -ItemType Directory -Force -Path $publicDir | Out-Null
& icacls $publicDir /grant:r "*S-1-5-32-545:(OI)(CI)RX" | Out-Null
Write-Step "  konfiguracja   : $configPath (dostep: SYSTEM + Administratorzy)"

# --- 3. rejestracja ---------------------------------------------------------
if (-not $reuseEnrollment) {
    Write-Step "Rejestruje maszyne w serwerze..."
    $enrollment = Invoke-Agent @("--config", $configPath, "enroll")
    if ($enrollment.Code -ne 0) {
        throw "Rejestracja nie powiodla sie (kod $($enrollment.Code)). Sprawdz dziennik agenta."
    }
}

# --- 4. zadanie harmonogramu ------------------------------------------------
$action = New-ScheduledTaskAction -Execute $targetExe -Argument "--config `"$configPath`" run" -WorkingDirectory $InstallDir

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

# Monitorowanie uslug to OSOBNE zadanie, dlugo zyjace. Inwentaryzacja odpala
# sie raz na kilka godzin i konczy; sonda dostepnosci ma chodzic co minute,
# wiec musi zyc miedzy jej przebiegami - jedno w drugim zmiescic sie nie da.
# Zadanie startuje przy uruchomieniu systemu i nie ma limitu czasu; harmonogram
# odtwarza je, gdyby proces padl.
$monitorAction = New-ScheduledTaskAction -Execute $targetExe `
    -Argument "--config `"$configPath`" monitor" -WorkingDirectory $InstallDir
$monitorTrigger = New-ScheduledTaskTrigger -AtStartup
$monitorSettings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $MonitorTaskName `
    -Description "Monitorowanie dostepnosci uslug CMDB - cele ustawia sie w panelu." `
    -Action $monitorAction -Trigger $monitorTrigger `
    -Principal $principal -Settings $monitorSettings -Force | Out-Null
# Bez przypisanych celow zadanie tylko pyta serwer o polityke i spi, wiec
# uruchamiamy je zawsze - cel dodany w panelu zaczyna byc sprawdzany bez
# wchodzenia na maszyne.
Start-ScheduledTask -TaskName $MonitorTaskName -ErrorAction SilentlyContinue

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
Write-Step "  monitorowanie  : $MonitorTaskName (SYSTEM, ciagle - cele z panelu CMDB)"

# --- 5. ikona w zasobniku ---------------------------------------------------
# Wpis zakladamy jawnie w widoku 64-bitowym. Przy uruchomieniu instalatora
# z 32-bitowego PowerShella zwykly zapis do HKLM:\Software\... jest przez
# system przekierowywany do WOW6432Node - wpis powstaje, ale nie tam, gdzie
# go potem szukamy przy diagnozie i deinstalacji.
$sciezkaRun = "Software\Microsoft\Windows\CurrentVersion\Run"
$bazaRejestru = [Microsoft.Win32.RegistryKey]::OpenBaseKey("LocalMachine", "Registry64")
try {
    $kluczRun = $bazaRejestru.CreateSubKey($sciezkaRun, $true)
    try {
        if (-not $NoTray) {
            $kluczRun.SetValue("CMDB Agent Tray", "`"$targetExe`" --config `"$configPath`" gui", "String")
            Write-Step "  ikona          : $targetExe gui (autostart dla kazdego uzytkownika)"
        }
        else {
            $kluczRun.DeleteValue("CMDB Agent Tray", $false)
        }
    }
    finally { $kluczRun.Close() }

    # Sprzatamy pozostalosc po zapisie w widoku 32-bitowym.
    $baza32 = [Microsoft.Win32.RegistryKey]::OpenBaseKey("LocalMachine", "Registry32")
    try {
        $klucz32 = $baza32.OpenSubKey($sciezkaRun, $true)
        if ($klucz32) {
            $klucz32.DeleteValue("CMDB Agent Tray", $false)
            $klucz32.Close()
        }
    }
    finally { $baza32.Close() }
}
finally { $bazaRejestru.Close() }

# Autostart zadziala dopiero przy nastepnym logowaniu, wiec uruchamiamy ikone
# od razu. Posrednictwo explorer.exe jest tu celowe: instalator dziala z
# podniesionymi uprawnieniami, a ikona ma chodzic na zwyklym koncie
# zalogowanego uzytkownika - explorer startuje ja na swoim poziomie.
if (-not $NoTray) {
    try {
        Start-Process "explorer.exe" -ArgumentList "`"$targetExe`"" -ErrorAction Stop
        Write-Step "  ikona uruchomiona (bez czekania na ponowne logowanie)"
    }
    catch {
        Write-Warning "Nie udalo sie uruchomic ikony teraz - pojawi sie przy nastepnym logowaniu."
    }
}

# --- 6. pierwszy przebieg ---------------------------------------------------
Write-Step "Wysylam pierwszy raport..."
$firstRun = Invoke-Agent @("--config", $configPath, "run")
if ($firstRun.Code -ne 0) {
    Write-Warning "Pierwszy raport nie zostal wyslany (kod $($firstRun.Code)). Zadanie sprobuje ponownie."
}

Write-Host ""
Write-Host "Gotowe." -ForegroundColor Green
$status = Invoke-Agent @("--config", $configPath, "status")
Write-Host $status.Output
Write-Host ""
Write-Host "Dziennik agenta: $DataDir\agent.log"
Write-Host "Reczny przebieg: `"$targetExe`" run"
