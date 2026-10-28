# Build and audit release/PTKit.exe plus source and Windows release archives.
# -Install creates shortcuts for the current user; it is optional.
param([switch]$Install)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$projectPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $projectPython)) {
    throw 'Run setup_env.ps1 first, or create .venv and install .[dev,build].'
}
& $projectPython -m pip install -e '.[build]'
if ($LASTEXITCODE -ne 0) { throw 'Build dependency installation failed.' }
& $projectPython tools\build_release.py
if ($LASTEXITCODE -ne 0) { throw 'Build, portability test or privacy audit failed.' }
if ($Install) {
    $releaseExe = (Resolve-Path -LiteralPath 'release\PTKit.exe').Path
    $shortcutShell = New-Object -ComObject WScript.Shell
    foreach ($shortcutFolder in @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('Desktop'))) {
        $shortcut = $shortcutShell.CreateShortcut((Join-Path $shortcutFolder 'PTKit.lnk'))
        $shortcut.TargetPath = $releaseExe
        $shortcut.WorkingDirectory = Split-Path -Parent $releaseExe
        $shortcut.IconLocation = "$releaseExe,0"
        $shortcut.Description = 'PTKit - Packet Tracer Toolkit'
        $shortcut.Save()
    }
}
Write-Host 'Ready: release\PTKit-windows-x64.zip is the recommended GitHub release asset.'
