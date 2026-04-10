# Create Start Menu + Desktop shortcuts for claude-manager on Windows with
# the bundled .ico as the launcher icon. Re-run after moving the repo.
$ErrorActionPreference = 'Stop'

$RepoDir = if ($env:REPO_DIR) { $env:REPO_DIR } else { "$env:USERPROFILE\git\claude-manager" }
$IconPath = Join-Path $RepoDir 'assets\icon.ico'
$PyExe = Join-Path $RepoDir '.venv\Scripts\pythonw.exe'
$PyFallback = Join-Path $RepoDir '.venv\Scripts\python.exe'

if (-not (Test-Path $RepoDir)) {
    Write-Error "claude-manager not found at $RepoDir"
    exit 1
}
if (-not (Test-Path $IconPath)) {
    Write-Error "Icon not found at $IconPath"
    exit 1
}
if (-not (Test-Path $PyExe)) {
    if (Test-Path $PyFallback) {
        $PyExe = $PyFallback
    } else {
        Write-Error "venv python not found (looked for pythonw.exe and python.exe under $RepoDir\.venv\Scripts)"
        exit 1
    }
}

$StartMenu = [Environment]::GetFolderPath('StartMenu')
$StartMenuDir = Join-Path $StartMenu 'Programs'
if (-not (Test-Path $StartMenuDir)) { New-Item -ItemType Directory -Path $StartMenuDir | Out-Null }
$StartShortcut = Join-Path $StartMenuDir 'claude-manager.lnk'

$DesktopDir = [Environment]::GetFolderPath('Desktop')
$DesktopShortcut = Join-Path $DesktopDir 'claude-manager.lnk'

$WshShell = New-Object -ComObject WScript.Shell
foreach ($target in @($StartShortcut, $DesktopShortcut)) {
    $link = $WshShell.CreateShortcut($target)
    $link.TargetPath = $PyExe
    $link.Arguments = '-m src.main --bind 0.0.0.0 --port 44740'
    $link.WorkingDirectory = $RepoDir
    $link.IconLocation = "$IconPath,0"
    $link.Description = 'Fleet session manager for Claude Code and psmux'
    $link.WindowStyle = 7  # minimized (pywebview makes its own window)
    $link.Save()
    Write-Host "Installed: $target"
}

Write-Host ""
Write-Host "Launch from Start Menu (type 'claude-manager') or the desktop shortcut."
