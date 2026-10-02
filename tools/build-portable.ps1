[CmdletBinding()]
param([switch]$Clean)

$ErrorActionPreference = 'Stop'
if (-not (Get-Command py -ErrorAction SilentlyContinue)) { throw 'Python 3 is required.' }
if ($Clean) {
    if (Test-Path -LiteralPath '.\build') {
        Remove-Item -LiteralPath '.\build' -Recurse -Force
    }
    foreach ($artifact in @('.\dist\FanFicFare.exe', '.\dist\MediaIntake.exe',
                            '.\dist\MediaIntake-Kindle.exe')) {
        if (Test-Path -LiteralPath $artifact) {
            Remove-Item -LiteralPath $artifact -Force
        }
    }
}
py -3 (Join-Path $PSScriptRoot 'install_kindling.py')
py -3 -m pip install --upgrade pyinstaller -r .\requirements-fanfiction.txt `
    -r .\requirements-browser.txt -r .\requirements-books.txt
py -3 -m PyInstaller --noconfirm --clean --onefile --console --name FanFicFare `
    --collect-all fanficfare .\tools\fanficfare_cli.py
py -3 -m PyInstaller --noconfirm --clean --onefile --windowed --name MediaIntake `
    --collect-all webview --collect-all pypdf --hidden-import clr `
    --exclude-module cefpython3 --exclude-module PyQt5 --exclude-module PyQt6 `
    --exclude-module PySide2 --exclude-module PySide6 --exclude-module gi `
    --add-binary 'dist\FanFicFare.exe;.' `
    --add-binary 'tools\kindling-cli.exe;tools' `
    --add-data 'licenses\Kindling-MIT.txt;licenses' `
    --add-data 'licenses\pypdf-BSD-3-Clause.txt;licenses' `
    --add-data 'media_intake\data;media_intake\data' run_gui.py
Write-Host "Portable executable: $((Resolve-Path 'dist\MediaIntake.exe').Path)"

