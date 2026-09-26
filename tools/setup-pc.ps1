[CmdletBinding()]
param(
    [string]$Destination = "$env:USERPROFILE\GitHub\media-intake-prototype",
    [switch]$ScheduledTask,
    [int]$IntervalMinutes = 30
)

$ErrorActionPreference = 'Stop'
$sync = Join-Path $Destination 'tools\sync-to-pc.ps1'

if (-not (Test-Path $sync)) {
    $parent = Split-Path -Parent $Destination
    if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
    git clone --origin origin --branch main --single-branch 'https://github.com/unwit1/media-intake-prototype.git' $Destination
}

& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $sync -RunTests

if ($ScheduledTask) {
    if ($IntervalMinutes -lt 5 -or $IntervalMinutes -gt 1440) { throw 'IntervalMinutes must be between 5 and 1440.' }
    $taskName = 'Media Intake prototype sync'
    $action = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$sync`" -RunTests"
    schtasks.exe /Create /F /SC MINUTE /MO $IntervalMinutes /TN $taskName /TR $action | Out-Host
    Write-Host "Scheduled task '$taskName' created."
}

