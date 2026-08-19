<#
.SYNOPSIS
    Usuwa agenta CMDB z maszyny.

.DESCRIPTION
    Kasuje zadanie harmonogramu, autostart ikony w zasobniku i pliki programu.
    Katalog danych z poswiadczeniem usuwany jest tylko z parametrem -RemoveData
    - bez niego ponowna instalacja odtworzy istniejaca rejestracje zamiast
    tworzyc duplikat maszyny w bazie.

    Odinstalowanie agenta nie kasuje maszyny w CMDB - zasob zostaje w bazie
    z data ostatniego kontaktu. Jest to zamierzone: historia inwentarza nie
    powinna znikac razem z agentem.

.EXAMPLE
    .\uninstall-agent.ps1
    .\uninstall-agent.ps1 -RemoveData
#>
[CmdletBinding()]
param(
    [string] $InstallDir = "$env:ProgramFiles\CMDB Agent",
    [string] $DataDir    = "$env:ProgramData\CMDB",
    [string] $TaskName   = "CMDB Agent",
    [switch] $RemoveData,
    # Wywolywane z instalatora, ktory sam usuwa swoje pliki.
    [switch] $KeepFiles
)

$ErrorActionPreference = "Stop"

$identity = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Skrypt wymaga uprawnien administratora."
}

# --- zadanie harmonogramu ---------------------------------------------------
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Usunieto zadanie: $TaskName"
}

# --- ikona w zasobniku ------------------------------------------------------
Remove-ItemProperty -Path "HKLM:\Software\Microsoft\Windows\CurrentVersion\Run" `
                    -Name "CMDB Agent Tray" -ErrorAction SilentlyContinue

# Ikona trzyma otwarty plik exe - bez zamkniecia usuwanie katalogu sie nie uda.
Get-Process -Name "cmdb-agent-tray" -ErrorAction SilentlyContinue | ForEach-Object {
    $_ | Stop-Process -Force -ErrorAction SilentlyContinue
    Write-Host "Zatrzymano ikone agenta (PID $($_.Id))"
}
Start-Sleep -Milliseconds 500

# --- pliki programu ---------------------------------------------------------
if (-not $KeepFiles -and (Test-Path $InstallDir)) {
    Remove-Item $InstallDir -Recurse -Force
    Write-Host "Usunieto katalog programu: $InstallDir"
}

# --- dane -------------------------------------------------------------------
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
