[CmdletBinding()]
param([switch]$Clean)

$ErrorActionPreference = 'Stop'
if (-not (Get-Command py -ErrorAction SilentlyContinue)) { throw 'Python 3 is required.' }
if ($Clean) { Remove-Item -Recurse -Force -ErrorAction SilentlyContinue build, dist }
py -3 -m pip install --upgrade pyinstaller
py -3 -m PyInstaller --noconfirm --clean --onefile --windowed --name MediaIntake run_gui.py
Write-Host "Portable executable: $((Resolve-Path 'dist\MediaIntake.exe').Path)"

