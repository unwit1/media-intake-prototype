[CmdletBinding()]
param(
    [string]$RepoUrl = 'https://github.com/unwit1/media-intake-prototype.git',
    [string]$Destination = "$env:USERPROFILE\GitHub\media-intake-prototype",
    [switch]$RunTests
)

$ErrorActionPreference = 'Stop'

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw 'Git is required. Install Git for Windows, then run this script again.'
}

$parent = Split-Path -Parent $Destination
if ($parent -and -not (Test-Path $parent)) {
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
}

if (-not (Test-Path (Join-Path $Destination '.git'))) {
    git clone --origin origin --branch main --single-branch $RepoUrl $Destination
} else {
    git -C $Destination fetch --prune origin main
    git -C $Destination merge --ff-only origin/main
}

if ($RunTests) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        py -3 -m unittest discover -s (Join-Path $Destination 'tests') -v
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        python -m unittest discover -s (Join-Path $Destination 'tests') -v
    } else {
        throw 'Python 3 is required to run the test suite.'
    }
}

Write-Host "Media Intake is synced at $Destination"

