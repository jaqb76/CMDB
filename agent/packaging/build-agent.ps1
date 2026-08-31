<#
.SYNOPSIS
    Buduje agenta CMDB do pojedynczego pliku cmdb-agent.exe.

.DESCRIPTION
    Powstaje jeden cmdb-agent.exe: bez argumentow dziala w tray,
    z argumentami (run, enroll, status, configure) wykonuje wybrana operacje.

    Sam agent nie ma zaleznosci zewnetrznych; ikona uzywa pystray i Pillow,
    ktore PyInstaller wpakuje do pliku - na stacjach koncowych nadal nie
    trzeba niczego instalowac.

    Zgodnosc buildu sprawdz na najstarszej wspieranej wersji Windows.

.EXAMPLE
    .\build-agent.ps1
    .\build-agent.ps1 -Installer
    .\build-agent.ps1 -Installer -SignCertThumbprint 1A2B3C...
#>
[CmdletBinding()]
param(
    [string] $OutputDir,
    # Podpisanie pliku jest mocno zalecane: bez niego SmartScreen i czesc
    # systemow EDR beda blokowac uruchomienie agenta.
    [string] $SignCertThumbprint,
    [string] $TimestampUrl = "http://timestamp.digicert.com",
    # Zgodnosc ze starymi skryptami; GUI jest teraz czescia jednego EXE.
    [switch] $NoTray,
    # Opcjonalna kopia historyczna; domyslnie tylko jeden plik programu.
    [switch] $Archive,
    # Buduje takze CMDB-Agent-Setup.exe - wymaga zainstalowanego Inno Setup 6.
    [switch] $Installer
)

$ErrorActionPreference = "Stop"

# $PSScriptRoot bywa pusty w bloku param(), a wtedy "$PSScriptRoot\..\dist"
# rozwija sie do "\..\dist" i pliki laduja w katalogu glownym dysku.
# Sciezki liczymy dopiero tutaj, gdzie zmienna jest juz ustawiona.
$scriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$agentRoot = (Resolve-Path (Join-Path $scriptDir "..")).Path
if (-not $OutputDir) { $OutputDir = Join-Path $agentRoot "dist" }
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$OutputDir = (Resolve-Path $OutputDir).Path

Write-Host "== Budowanie agenta CMDB ==" -ForegroundColor Cyan

python -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)"
if ($LASTEXITCODE -ne 0) { throw "Wymagany Python 3.9 lub nowszy" }

python -m pip install --quiet --upgrade pyinstaller
if ($LASTEXITCODE -ne 0) { throw "Nie udalo sie zainstalowac PyInstallera" }

if ($NoTray) { Write-Warning "-NoTray nie rozdziela juz buildu. Aby wylaczyc autostart ikony, uzyj -NoTray przy instalacji." }
python -m pip install --quiet -r (Join-Path $agentRoot "requirements-gui.txt")
if ($LASTEXITCODE -ne 0) { throw "Nie udalo sie zainstalowac zaleznosci interfejsu" }

Push-Location $agentRoot
try {
    # Embed the same C mark in Explorer/taskbar/setup, not only in Tk windows.
    $brandDir = Join-Path $OutputDir "build"
    New-Item -ItemType Directory -Force -Path $brandDir | Out-Null
    $brandIcon = Join-Path $brandDir "cmdb-agent.ico"
    python -c "import sys; from cmdb_agent.gui.appearance import icon_image; icon_image(size=256).save(sys.argv[1], sizes=[(16,16),(32,32),(48,48),(64,64),(256,256)])" $brandIcon
    if ($LASTEXITCODE -ne 0) { throw "Nie udalo sie utworzyc ikony C" }
    # Jedyny build: subsystem WINDOWS nie tworzy okna konsoli nawet na moment.
    python -m PyInstaller `
        --onefile `
        --name cmdb-agent `
        --distpath $OutputDir `
        --workpath "$OutputDir\build" `
        --specpath "$OutputDir\build" `
        --windowed `
        --icon $brandIcon `
        --noupx `
        --paths . `
        --hidden-import pystray._win32 `
        packaging\agent_entry.py
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller zakonczyl sie bledem (cmdb-agent)" }

}
finally {
    Pop-Location
}

$exe = Join-Path $OutputDir "cmdb-agent.exe"
if (-not (Test-Path $exe)) { throw "Nie powstal plik $exe" }

# --- metadane w pliku -------------------------------------------------------
# PyInstaller pakuje kod w skompresowane archiwum, wiec numeru wersji nie da
# sie z gotowego pliku odczytac. Serwer musialby go uruchomic, a uruchamianie
# wgranego pliku na serwerze to po prostu wykonywanie obcego kodu. Dopisujemy
# wiec metadane na koncu pliku: PE i ELF ignoruja dane za wlasciwa zawartoscia,
# wiec program dziala tak samo, a serwer odczytuje wersje bez uruchamiania.
#
# Kolejnosc jest istotna: podpis Authenticode obejmuje caly plik, wiec metadane
# musza byc dopisane PRZED podpisaniem.
$wersjaZrodel = (Select-String -Path (Join-Path $agentRoot "cmdb_agent\__init__.py") `
                               -Pattern '__version__\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
Write-Host "Wersja ze zrodel: $wersjaZrodel" -ForegroundColor Cyan

function Add-Metadane($sciezka, $system) {
    $meta = [ordered]@{
        version   = $wersjaZrodel
        os_family = $system
        entry_mode = "unified"
        built_at  = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    } | ConvertTo-Json -Compress
    $stopka = "`n<<<CMDB-AGENT-META>>>$meta<<<KONIEC>>>"
    $bajty = [System.Text.Encoding]::UTF8.GetBytes($stopka)
    $strumien = [System.IO.File]::Open($sciezka, "Append", "Write")
    try { $strumien.Write($bajty, 0, $bajty.Length) } finally { $strumien.Close() }
}

Add-Metadane $exe "windows"
Write-Host "  dopisano metadane wersji do plikow" -ForegroundColor Cyan

$artifacts = @($exe)

function Invoke-Sign($path) {
    $cert = Get-ChildItem Cert:\CurrentUser\My, Cert:\LocalMachine\My |
            Where-Object { $_.Thumbprint -eq $SignCertThumbprint } | Select-Object -First 1
    if (-not $cert) { throw "Nie znaleziono certyfikatu o odcisku $SignCertThumbprint" }
    Set-AuthenticodeSignature -FilePath $path -Certificate $cert `
        -TimestampServer $TimestampUrl -HashAlgorithm SHA256 | Out-Null
    Write-Host "  podpisano: $(Split-Path $path -Leaf)"
}

if ($SignCertThumbprint) {
    Write-Host "Podpisuje pliki certyfikatem $SignCertThumbprint" -ForegroundColor Cyan
    $artifacts | ForEach-Object { Invoke-Sign $_ }
}
else {
    Write-Warning "Pliki nie zostaly podpisane."
    Write-Host "  SmartScreen ostrzeze przy pierwszym uruchomieniu, a czesc systemow EDR moze plik zablokowac." -ForegroundColor DarkYellow
    Write-Host "  Podpisywanie: .\build-agent.ps1 -SignCertThumbprint <odcisk>" -ForegroundColor DarkYellow
    Write-Host "  Skad wziac certyfikat: docs/agent-windows.md, sekcja Podpisywanie" -ForegroundColor DarkYellow
}

# --- instalator graficzny ---------------------------------------------------
if ($Installer) {
    $iscc = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1

    if (-not $iscc) {
        Write-Warning "Nie znaleziono Inno Setup 6 (ISCC.exe) - pomijam budowanie instalatora."
        Write-Warning "Pobierz z https://jrsoftware.org/isdl.php albo zainstaluj: winget install JRSoftware.InnoSetup"
    }
    else {
        Write-Host "Buduje instalator..." -ForegroundColor Cyan
        & $iscc "/DAppVersion=$wersjaZrodel" "/DBuildDir=$OutputDir" (Join-Path $scriptDir "cmdb-agent.iss")
        if ($LASTEXITCODE -ne 0) { throw "Inno Setup zakonczyl sie bledem" }
        $setup = Get-ChildItem (Join-Path $OutputDir "CMDB-Agent-Setup-*.exe") |
                 Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($setup) {
            if ($SignCertThumbprint) { Invoke-Sign $setup.FullName }
            $artifacts += $setup.FullName
        }
    }
}


# --- opcjonalna historia ---------------------------------------------------
$zwersjonowane = @()
if ($Archive) {
    $historyDir = Join-Path $OutputDir "history"
    New-Item -ItemType Directory -Force -Path $historyDir | Out-Null
    $kopia = Join-Path $historyDir ("cmdb-agent-{0}.exe" -f $wersjaZrodel)
    Copy-Item -Path $exe -Destination $kopia -Force
    $zwersjonowane += $kopia
}
if ($zwersjonowane.Count -gt 0) {
    Write-Host ("  zapisano kopie z numerem wersji ({0})" -f $wersjaZrodel) -ForegroundColor Cyan
    $artifacts += $zwersjonowane
}
Write-Host ""
Write-Host "Gotowe:" -ForegroundColor Green
foreach ($item in $artifacts) {
    $info = Get-Item $item
    Write-Host ("  {0,-32} {1,7:N2} MB  SHA-256 {2}" -f `
        $info.Name, ($info.Length / 1MB), (Get-FileHash $item -Algorithm SHA256).Hash)
}
