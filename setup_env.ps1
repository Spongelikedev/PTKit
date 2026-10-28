# Create a local virtual environment, install development dependencies and test.
param([switch]$SkipTests)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Creating .venv failed. Install Python 3.10+ with Tkinter and add Python to PATH.' }
}
$projectPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
& $projectPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw 'Updating pip failed.' }
& $projectPython -m pip install -e '.[dev]'
if ($LASTEXITCODE -ne 0) { throw 'Installing dependencies failed.' }
if (-not $SkipTests) {
    & $projectPython -m pytest -q -m 'not samples'
    if ($LASTEXITCODE -ne 0) { throw 'Tests failed.' }
}
Write-Host 'Ready. Launch with: .\.venv\Scripts\python.exe gui.py'
