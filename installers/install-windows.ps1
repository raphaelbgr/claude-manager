# claude-manager installer for Windows
# Usage: irm .../install-windows.ps1 | iex

Write-Host "Installing claude-manager..." -ForegroundColor Cyan

# Check Python
$py = Get-Command python3 -ErrorAction SilentlyContinue
if (-not $py) {
    $py = Get-Command python -ErrorAction SilentlyContinue
}
if (-not $py) {
    Write-Host "Installing Python via winget..."
    winget install Python.Python.3.12 --accept-package-agreements --accept-source-agreements
    # Refresh PATH
    $env:PATH = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
    $py = Get-Command python -ErrorAction SilentlyContinue
}

$installDir = "$env:USERPROFILE\.claude-manager"

# Clone or update
if (Test-Path $installDir) {
    Set-Location $installDir
    git pull
} else {
    git clone https://github.com/raphaelbgr/claude-manager.git $installDir
}
Set-Location $installDir

# Create venv
python -m venv .venv
& .\.venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -e ".[all]"

# Desktop shortcut
$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut("$env:USERPROFILE\Desktop\Claude Manager.lnk")
$Shortcut.TargetPath = "$installDir\.venv\Scripts\python.exe"
$Shortcut.Arguments = "-m src.main"
$Shortcut.WorkingDirectory = $installDir
$Shortcut.Description = "Claude Manager - Fleet Session Manager"
$Shortcut.Save()

Write-Host ""
Write-Host "claude-manager installed!" -ForegroundColor Green
Write-Host "  Desktop shortcut: ~/Desktop/Claude Manager.lnk"
Write-Host "  Web: http://localhost:44740"
