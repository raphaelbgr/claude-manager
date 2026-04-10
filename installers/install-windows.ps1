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

# Check for psmux (required for tmux-equivalent session management on Windows)
$psmux = Get-Command psmux -ErrorAction SilentlyContinue
if ($psmux) {
    $psmuxVer = & psmux --version 2>$null
    Write-Host "  psmux found: $psmuxVer" -ForegroundColor Green
} else {
    Write-Host "  psmux not found — tmux session management will be unavailable on this machine." -ForegroundColor Yellow
    Write-Host "  Install: winget install psmux  (or download from https://github.com/psmux/psmux)" -ForegroundColor Yellow
}

# Check for SSH (required for fleet connectivity)
$ssh = Get-Command ssh -ErrorAction SilentlyContinue
if ($ssh) {
    Write-Host "  SSH found: $($ssh.Source)" -ForegroundColor Green
} else {
    Write-Host "  SSH not found — remote machine access will be unavailable." -ForegroundColor Yellow
    Write-Host "  Enable: Settings > Apps > Optional Features > OpenSSH Client" -ForegroundColor Yellow
}

# Check for Git Bash (used by SSH -t for remote session resume)
$gitBash = Get-Command bash -ErrorAction SilentlyContinue
if (-not $gitBash) {
    $gitBashPath = "C:\Program Files\Git\bin\bash.exe"
    if (Test-Path $gitBashPath) { $gitBash = $gitBashPath }
}
if ($gitBash) {
    Write-Host "  Git Bash found (used for remote SSH sessions)." -ForegroundColor Green
} else {
    Write-Host "  Git Bash not found — remote session resume may not work." -ForegroundColor Yellow
    Write-Host "  Install Git for Windows: winget install Git.Git" -ForegroundColor Yellow
}

# Desktop shortcut — pythonw.exe (no console window)
$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut("$env:USERPROFILE\Desktop\Claude Manager.lnk")
$Shortcut.TargetPath = "$installDir\.venv\Scripts\pythonw.exe"
$Shortcut.Arguments = "-m src --enable-desktop"
$Shortcut.WorkingDirectory = $installDir
$Shortcut.Description = "Claude Manager - Fleet Session Manager"
if (Test-Path "$installDir\assets\icon.ico") {
    $Shortcut.IconLocation = "$installDir\assets\icon.ico"
}
$Shortcut.Save()

# API-only shortcut (headless daemon, no window)
$Shortcut2 = $WshShell.CreateShortcut("$env:USERPROFILE\Desktop\Claude Manager (API).lnk")
$Shortcut2.TargetPath = "$installDir\.venv\Scripts\pythonw.exe"
$Shortcut2.Arguments = "-m src --daemon"
$Shortcut2.WorkingDirectory = $installDir
$Shortcut2.Description = "Claude Manager - API Server (background daemon)"
if (Test-Path "$installDir\assets\icon.ico") {
    $Shortcut2.IconLocation = "$installDir\assets\icon.ico"
}
$Shortcut2.Save()

Write-Host ""
Write-Host "claude-manager installed!" -ForegroundColor Green
Write-Host "  Desktop shortcut: ~/Desktop/Claude Manager.lnk (GUI, no console)"
Write-Host "  API-only shortcut: ~/Desktop/Claude Manager (API).lnk (background daemon)"
Write-Host "  Web: http://localhost:44740"
Write-Host ""
Write-Host "  CLI usage:" -ForegroundColor Yellow
Write-Host "    claude-manager --daemon     Start as background service"
Write-Host "    claude-manager               Start with native desktop window (default)"
Write-Host "    claude-manager --tui        Start terminal UI"
