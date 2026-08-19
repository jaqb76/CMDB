<#
.SYNOPSIS
    Usuwa agenta CMDB z maszyny.

.DESCRIPTION
    Kasuje zadanie harmonogramu i pliki programu. Katalog danych z
    poswiadczeniem usuwany jest tylko z parametrem -RemoveData - bez niego
    ponowna instalacja odtworzy istniejaca rejestracje zamiast tworzyc
    duplikat maszyny w bazie.

    Pamietaj, ze odinstalowanie agenta nie kasuje maszyny w CMDB - zasob
    zostaje w bazie z data ostatniego kontaktu, co jest zamierzone
    (historia inwentarza nie powinna znikac po odinstalowaniu agenta).

.EXAMPLE
    .\uninstall-agent.ps1
    .\uninstall-agent.ps1 -RemoveData
#>
[CmdletBinding()]
param(
    [string] $InstallDir = "$env:ProgramFiles\CMDB Agent",
    [string] $DataDir    = "$env:ProgramData\CMDB",
    [string] $TaskName   = "CMDB Agent",
    [switch] $RemoveData
)

$ErrorActionPreference = "Stop"

$identity = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Skrypt wymaga uprawnien administratora."
}

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Usunieto zadanie: $TaskName"
}

if (Test-Path $InstallDir) {
    Remove-Item $InstallDir -Recurse -Force
    Write-Host "Usunieto katalog programu: $InstallDir"
}

if ($RemoveData) {
    if (Test-Path $DataDir) {
        Remove-Item $DataDir -Recurse -Force
        Write-Host "Usunieto katalog danych wraz z poswiadczeniem: $DataDir"
    }
    Write-Host "Uwaga: ponowna instalacja zarejestruje maszyne od nowa." -ForegroundColor Yellow
}
else {
    Write-Host "Zachowano $DataDir (poswiadczenie agenta). Uzyj -RemoveData, aby je skasowac."
}

Write-Host "Agent odinstalowany." -ForegroundColor Green
