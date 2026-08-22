<#
.SYNOPSIS
    Buduje agenta CMDB do pojedynczego pliku cmdb-agent.exe.

.DESCRIPTION
    Powstaja dwa pliki:
      cmdb-agent.exe      - agent wiersza polecen (chodzi jako zadanie SYSTEM),
      cmdb-agent-tray.exe - ikona w zasobniku ze statusem i ustawieniami.

    Sam agent nie ma zaleznosci zewnetrznych; ikona uzywa pystray i Pillow,
    ktore PyInstaller wpakuje do pliku - na stacjach koncowych nadal nie
    trzeba niczego instalowac.

    Buduj na tej wersji Windows, ktora jest najstarsza w flocie - plik
    zbudowany na Windows 11 dziala na starszych, ale nie odwrotnie.

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
    # Pomija budowanie ikony w zasobniku (np. dla serwerow bez pulpitu).
    [switch] $NoTray,
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

# Ikona w zasobniku potrzebuje pystray i Pillow; sam agent nie ma zaleznosci.
if (-not $NoTray) {
    python -m pip install --quiet -r (Join-Path $agentRoot "requirements-gui.txt")
    if ($LASTEXITCODE -ne 0) { throw "Nie udalo sie zainstalowac zaleznosci interfejsu" }
}

Push-Location $agentRoot
try {
    # 1. Agent wiersza polecen - to on chodzi jako zadanie SYSTEM.
    python -m PyInstaller `
        --onefile `
        --name cmdb-agent `
        --distpath $OutputDir `
        --workpath "$OutputDir\build" `
        --specpath "$OutputDir\build" `
        --console `
        --noupx `
        --paths . `
        --exclude-module tkinter `
        --exclude-module pystray `
        --exclude-module PIL `
        packaging\agent_entry.py
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller zakonczyl sie bledem (cmdb-agent)" }

    # 2. Ikona w zasobniku - --windowed, zeby nie migalo okno konsoli.
    if (-not $NoTray) {
        python -m PyInstaller `
            --onefile `
            --name cmdb-agent-tray `
            --distpath $OutputDir `
            --workpath "$OutputDir\build" `
            --specpath "$OutputDir\build" `
            --windowed `
            --noupx `
            --hidden-import pystray._win32 `
            packaging\tray_entry.py
        if ($LASTEXITCODE -ne 0) { throw "PyInstaller zakonczyl sie bledem (cmdb-agent-tray)" }
    }
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
        built_at  = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    } | ConvertTo-Json -Compress
    $stopka = "`n<<<CMDB-AGENT-META>>>$meta<<<KONIEC>>>"
    $bajty = [System.Text.Encoding]::UTF8.GetBytes($stopka)
    $strumien = [System.IO.File]::Open($sciezka, "Append", "Write")
    try { $strumien.Write($bajty, 0, $bajty.Length) } finally { $strumien.Close() }
}

Add-Metadane $exe "windows"
$trayExeSciezka = Join-Path $OutputDir "cmdb-agent-tray.exe"
if (Test-Path $trayExeSciezka) { Add-Metadane $trayExeSciezka "windows" }
Write-Host "  dopisano metadane wersji do plikow" -ForegroundColor Cyan

$artifacts = @($exe)
$trayExe = Join-Path $OutputDir "cmdb-agent-tray.exe"
if (Test-Path $trayExe) { $artifacts += $trayExe }

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
    Write-Warning "Pliki nie zostaly podpisane - bez podpisu SmartScreen i czesc systemow EDR beda blokowac agenta."
}

# --- instalator graficzny ---------------------------------------------------
if ($Installer) {
    $iscc = @(
        "$env:ProgramFiles(x86)\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1

    if (-not $iscc) {
        Write-Warning "Nie znaleziono Inno Setup 6 (ISCC.exe) - pomijam budowanie instalatora."
        Write-Warning "Pobierz z https://jrsoftware.org/isdl.php albo zainstaluj: winget install JRSoftware.InnoSetup"
    }
    else {
        Write-Host "Buduje instalator..." -ForegroundColor Cyan
        & $iscc (Join-Path $scriptDir "cmdb-agent.iss")
        if ($LASTEXITCODE -ne 0) { throw "Inno Setup zakonczyl sie bledem" }
        $setup = Get-ChildItem (Join-Path $OutputDir "CMDB-Agent-Setup-*.exe") |
                 Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($setup) {
            if ($SignCertThumbprint) { Invoke-Sign $setup.FullName }
            $artifacts += $setup.FullName
        }
    }
}


# --- kopie z numerem wersji -------------------------------------------------
# Pliki bez numeru wersji zostaja, bo ich nazwy sa nosne: ikona w zasobniku
# szuka agenta po "cmdb-agent.exe" obok siebie, instalator kopiuje wlasnie
# taka nazwe do Program Files, a skrypt Inno Setup tez ja zaklada.
# Kopie z wersja sluza do trzymania historii - kolejny build nie kasuje juz
# poprzedniego, wiec da sie wrocic do dowolnego wydanego agenta.
$zwersjonowane = @()
foreach ($plik in @($exe, $trayExe)) {
    if (-not (Test-Path $plik)) { continue }
    $bazowa = [System.IO.Path]::GetFileNameWithoutExtension($plik)
    $rozsz  = [System.IO.Path]::GetExtension($plik)
    $kopia  = Join-Path $OutputDir ("{0}-{1}{2}" -f $bazowa, $wersjaZrodel, $rozsz)
    Copy-Item -Path $plik -Destination $kopia -Force
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
