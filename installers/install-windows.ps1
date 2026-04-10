# claude-manager installer for Windows
# Usage: irm .../install-windows.ps1 | iex

Write-Host "Installing claude-manager..." -ForegroundColor Cyan

# Check Python — prefer 3.12, accept 3.11-3.13
$py = $null
foreach ($ver in @("3.12", "3.11", "3.13")) {
    $candidate = Get-Command "py" -ErrorAction SilentlyContinue
    if ($candidate) {
        # Use py launcher to select version
        $testVer = & py "-$ver" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
        if ($testVer -eq $ver) {
            $py = "py -$ver"
            break
        }
    }
}
if (-not $py) {
    $candidate = Get-Command python3 -ErrorAction SilentlyContinue
    if (-not $candidate) { $candidate = Get-Command python -ErrorAction SilentlyContinue }
    if ($candidate) {
        $ver = & $candidate.Source -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
        $major, $minor = $ver -split '\.'
        if ([int]$major -ge 3 -and [int]$minor -ge 11 -and [int]$minor -lt 14) {
            $py = $candidate.Source
        }
    }
}
if (-not $py) {
    Write-Host "Installing Python 3.12 via winget..."
    winget install Python.Python.3.12 --accept-package-agreements --accept-source-agreements
    $env:PATH = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
    $py = "py -3.12"
}

Write-Host "Using Python: $py" -ForegroundColor Yellow

$installDir = "$env:USERPROFILE\.claude-manager"

# Clone or update
if (Test-Path $installDir) {
    Set-Location $installDir
    git pull
} else {
    git clone https://github.com/raphaelbgr/claude-manager.git $installDir
}
Set-Location $installDir

# Create venv with the selected Python
& cmd /c "$py -m venv .venv"
& .\.venv\Scripts\Activate.ps1
pip install --upgrade pip

# Install base dependencies first (always works)
pip install -e "."
Write-Host "  Base dependencies installed." -ForegroundColor Green

# Try desktop extras (pywebview + Pillow) — may fail on some Python versions
Write-Host "Installing desktop extras..." -ForegroundColor Yellow
pip install -e ".[desktop]" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  Desktop extras failed (pywebview). GUI will fall back to browser mode." -ForegroundColor Yellow
    Write-Host "  To fix: install Python 3.12 and recreate venv." -ForegroundColor Yellow
}

# Try TUI extras
pip install -e ".[tui]" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  TUI extras failed. --tui mode unavailable." -ForegroundColor Yellow
}

# Try system tray extras (pystray)
pip install pystray Pillow 2>$null

# Desktop shortcut — uses cmd /k to keep window open
$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut("$env:USERPROFILE\Desktop\Claude Manager.lnk")
$Shortcut.TargetPath = "cmd.exe"
$Shortcut.Arguments = "/k cd /d `"$installDir`" && .venv\Scripts\python.exe -m src --enable-gui"
$Shortcut.WorkingDirectory = $installDir
$Shortcut.Description = "Claude Manager - Fleet Session Manager"
# Set icon if available
if (Test-Path "$installDir\assets\icon.ico") {
    $Shortcut.IconLocation = "$installDir\assets\icon.ico"
}
$Shortcut.Save()

# Also create a headless service shortcut (API only, no window)
$Shortcut2 = $WshShell.CreateShortcut("$env:USERPROFILE\Desktop\Claude Manager (API).lnk")
$Shortcut2.TargetPath = "$installDir\.venv\Scripts\pythonw.exe"
$Shortcut2.Arguments = "-m src --api-only"
$Shortcut2.WorkingDirectory = $installDir
$Shortcut2.Description = "Claude Manager - API Server (headless)"
if (Test-Path "$installDir\assets\icon.ico") {
    $Shortcut2.IconLocation = "$installDir\assets\icon.ico"
}
$Shortcut2.Save()

Write-Host ""
Write-Host "claude-manager installed!" -ForegroundColor Green
Write-Host "  Desktop shortcut: ~/Desktop/Claude Manager.lnk"
Write-Host "  API-only shortcut: ~/Desktop/Claude Manager (API).lnk"
Write-Host "  Web: http://localhost:44740"
