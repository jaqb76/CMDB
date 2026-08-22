# Instalacja agenta CMDB na Windows jednym poleceniem.
#
#   iwr -useb https://serwer/download/install.ps1 | iex     - nie tak, patrz nizej
#
# PowerShell nie przyjmuje argumentow przy przekazaniu potokiem do iex, wiec
# skrypt zapisuje sie do pliku i uruchamia z parametrami:
#
#   iwr https://serwer/download/install.ps1 -OutFile install.ps1
#   .\install.ps1 -Token cmdb_ent_...
#
# Skrypt pobiera z serwera agenta oraz wlasciwy instalator, sprawdza skrot
# SHA-256 pobranego pliku i przekazuje mu sterowanie. Adres serwera jest
# wpisywany przez serwer w chwili wydania tego pliku.
#
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $Token,
    [string] $Server = "@@ADRES_SERWERA@@",
    [string] $CaBundle,
    [int]    $IntervalHours = 4,
    [switch] $NoProcessList,
    [switch] $NoTray,
    [switch] $Silent
)

$ErrorActionPreference = "Stop"

function Blad($komunikat) {
    Write-Host "BLAD: $komunikat" -ForegroundColor Red
    exit 1
}
function Krok($komunikat) { Write-Host "==> $komunikat" -ForegroundColor Cyan }

# --- warunki wstepne --------------------------------------------------------
if (-not $Token.StartsWith("cmdb_ent_")) {
    Blad "to nie wyglada na token rejestracyjny - powinien zaczynac sie od 'cmdb_ent_'"
}
$Server = $Server.TrimEnd("/")
if (-not $Server.StartsWith("https://")) {
    Blad "adres serwera musi zaczynac sie od https:// - agent nie wysyla danych po nieszyfrowanym polaczeniu"
}

$tozsamosc = [Security.Principal.WindowsIdentity]::GetCurrent()
$rola = New-Object Security.Principal.WindowsPrincipal($tozsamosc)
if (-not $rola.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Blad "uruchom PowerShell jako administrator - instalator zaklada zadanie harmonogramu dzialajace jako SYSTEM"
}

# PowerShell 5.1 domyslnie negocjuje TLS 1.0, ktorego wspolczesne serwery
# nie przyjmuja - bez tego pobieranie konczy sie bledem o zerwanym polaczeniu.
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$roboczy = Join-Path $env:TEMP ("cmdb-agent-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $roboczy | Out-Null

try {
    # --- agent --------------------------------------------------------------
    Krok "Pobieram agenta z $Server"
    $agent = Join-Path $roboczy "cmdb-agent.exe"
    try {
        $odpowiedz = Invoke-WebRequest -Uri "$Server/download/agent-windows.exe" `
            -Headers @{ Authorization = "Bearer $Token" } `
            -OutFile $agent -PassThru -UseBasicParsing
    } catch {
        Write-Host ""
        Write-Host "Nie udalo sie pobrac agenta. Najczestsze przyczyny:" -ForegroundColor Yellow
        Write-Host "  - dla tej firmy nie ustawiono jeszcze wersji agenta dla Windows" -ForegroundColor Yellow
        Write-Host "    (superadmin: Wersje agentow -> Wgraj nowa wersje -> Ustaw jako aktywna)" -ForegroundColor Yellow
        Write-Host "  - token jest nieprawidlowy, wycofany albo wygasl" -ForegroundColor Yellow
        Write-Host "  - certyfikat serwera nie jest zaufany na tej maszynie" -ForegroundColor Yellow
        Blad $_.Exception.Message
    }

    # Skrot jest zabezpieczeniem dodatkowym - polaczenie idzie po TLS, ale to
    # wychwytuje uszkodzenie pliku w transporcie albo pomylke w magazynie wydan.
    $oczekiwany = $odpowiedz.Headers["X-CMDB-SHA256"]
    if ($oczekiwany) {
        $faktyczny = (Get-FileHash -Path $agent -Algorithm SHA256).Hash.ToLower()
        if ($faktyczny -ne $oczekiwany.ToLower()) {
            Blad "skrot pobranego agenta sie nie zgadza (oczekiwano $oczekiwany, jest $faktyczny)"
        }
        Write-Host "    skrot SHA-256 zgodny"
        Write-Host ("    wersja: " + $odpowiedz.Headers["X-CMDB-Version"])
    } else {
        Write-Host "    UWAGA: serwer nie podal skrotu - nie moge sprawdzic pliku" -ForegroundColor Yellow
    }

    # --- ikona w zasobniku (opcjonalna) -------------------------------------
    # Instalator kopiuje ja, jesli lezy OBOK agenta. Agent trafia tu do
    # katalogu tymczasowego, wiec bez pobrania ikona nie mialaby skad sie
    # tam wziasc - i instalacja z serwera nigdy by jej nie zakladala.
    #
    # Jej brak nie jest bledem: agent zbiera dane i raportuje bez niej,
    # a nie kazda firma wgrywa ten wariant.
    if (-not $NoTray) {
        $ikona = Join-Path $roboczy "cmdb-agent-tray.exe"
        try {
            Invoke-WebRequest -Uri "$Server/download/agent-windows-tray.exe" `
                -Headers @{ Authorization = "Bearer $Token" } `
                -OutFile $ikona -UseBasicParsing | Out-Null
            Write-Host "    pobrano ikone w zasobniku"
        } catch {
            Remove-Item $ikona -ErrorAction SilentlyContinue
            Write-Host "    ikona w zasobniku niedostepna - instaluje bez niej" -ForegroundColor DarkYellow
        }
    }

    # --- wlasciwy instalator ------------------------------------------------
    Krok "Pobieram instalator"
    $instalator = Join-Path $roboczy "install-agent.ps1"
    Invoke-WebRequest -Uri "$Server/download/install-agent.ps1" `
        -OutFile $instalator -UseBasicParsing

    $argumenty = @{
        ServerUrl     = $Server
        Token         = $Token
        AgentExe      = $agent
        IntervalHours = $IntervalHours
    }
    if ($CaBundle)      { $argumenty.CaBundle = $CaBundle }
    if ($NoProcessList) { $argumenty.NoProcessList = $true }
    if ($NoTray)        { $argumenty.NoTray = $true }
    if ($Silent)        { $argumenty.Silent = $true }

    Krok "Instaluje"
    & $instalator @argumenty
} finally {
    Remove-Item -Path $roboczy -Recurse -Force -ErrorAction SilentlyContinue
}
