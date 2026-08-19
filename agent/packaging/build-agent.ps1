<#
.SYNOPSIS
    Buduje agenta CMDB do pojedynczego pliku cmdb-agent.exe.

.DESCRIPTION
    Agent nie ma zaleznosci zewnetrznych, wiec PyInstaller pakuje tylko
    biblioteke standardowa - plik wychodzi maly i nie wymaga Pythona na
    stacjach koncowych.

    Buduj na tej wersji Windows, ktora jest najstarsza w flocie - plik
    zbudowany na Windows 11 dziala na starszych, ale nie odwrotnie.

.EXAMPLE
    .\build-agent.ps1
    .\build-agent.ps1 -SignCertThumbprint 1A2B3C...
#>
[CmdletBinding()]
param(
    [string] $OutputDir = "$PSScriptRoot\..\dist",
    # Podpisanie pliku jest mocno zalecane: bez niego SmartScreen i czesc
    # systemow EDR beda blokowac uruchomienie agenta.
    [string] $SignCertThumbprint,
    [string] $TimestampUrl = "http://timestamp.digicert.com"
)

$ErrorActionPreference = "Stop"
$agentRoot = Resolve-Path "$PSScriptRoot\.."

Write-Host "== Budowanie agenta CMDB ==" -ForegroundColor Cyan

python -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)"
if ($LASTEXITCODE -ne 0) { throw "Wymagany Python 3.9 lub nowszy" }

python -m pip install --quiet --upgrade pyinstaller
if ($LASTEXITCODE -ne 0) { throw "Nie udalo sie zainstalowac PyInstallera" }

Push-Location $agentRoot
try {
    python -m PyInstaller `
        --onefile `
        --name cmdb-agent `
        --distpath $OutputDir `
        --workpath "$OutputDir\build" `
        --specpath "$OutputDir\build" `
        --console `
        --noupx `
        cmdb_agent\main.py
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller zakonczyl sie bledem" }
}
finally {
    Pop-Location
}

$exe = Join-Path $OutputDir "cmdb-agent.exe"
if (-not (Test-Path $exe)) { throw "Nie powstal plik $exe" }

if ($SignCertThumbprint) {
    Write-Host "Podpisuje plik certyfikatem $SignCertThumbprint" -ForegroundColor Cyan
    $cert = Get-ChildItem Cert:\CurrentUser\My, Cert:\LocalMachine\My |
            Where-Object { $_.Thumbprint -eq $SignCertThumbprint } | Select-Object -First 1
    if (-not $cert) { throw "Nie znaleziono certyfikatu o odcisku $SignCertThumbprint" }
    Set-AuthenticodeSignature -FilePath $exe -Certificate $cert `
        -TimestampServer $TimestampUrl -HashAlgorithm SHA256 | Out-Null
}
else {
    Write-Warning "Plik nie zostal podpisany - rozwazenie podpisu jest zalecane przed wdrozeniem."
}

$hash = (Get-FileHash $exe -Algorithm SHA256).Hash
Write-Host ""
Write-Host "Gotowe: $exe" -ForegroundColor Green
Write-Host "Rozmiar: $([math]::Round((Get-Item $exe).Length / 1MB, 2)) MB"
Write-Host "SHA-256: $hash"
