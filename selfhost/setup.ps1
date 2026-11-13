# Trading Alerts setup for Windows.
#
#   Right-click setup.ps1 > Run with PowerShell, or in PowerShell:
#   powershell -ExecutionPolicy Bypass -File setup.ps1            asks what you'd like to do
#   ... -File setup.ps1 -Web            install, then open it in your browser
#   ... -File setup.ps1 -Service        keep it running in the background (starts when you log in)
#   ... -File setup.ps1 -Desktop        build the desktop app (needs Rust and Node.js)
#   ... -File setup.ps1 -Docker         run it in Docker
#   ... -File setup.ps1 -RemoveService
#
# Your strategies and settings go in %USERPROFILE%\TradingAlerts (change with -Folder).
param(
    [switch]$Web, [switch]$Service, [switch]$Desktop, [switch]$Docker, [switch]$RemoveService, [switch]$InstallOnly,
    [string]$Folder = (Join-Path $env:USERPROFILE "TradingAlerts"),
    [int]$Port = 8321
)
$ErrorActionPreference = "Stop"

$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Engine = Join-Path $Here "engine"
if (-not (Test-Path $Engine)) { $Engine = Join-Path (Split-Path -Parent $Here) "engine" }
$DesktopDir = Join-Path $Here "desktop"
$AppDir = Join-Path $env:LOCALAPPDATA "TradingAlerts"
$Venv = Join-Path $AppDir "venv"
$Py = Join-Path $Venv "Scripts\python.exe"
$PyW = Join-Path $Venv "Scripts\pythonw.exe"
$TaskName = "Trading Alerts"

function Say($t) { Write-Host $t }
function Bold($t) { Write-Host $t -ForegroundColor Cyan }
function Fail($t) { Write-Host $t -ForegroundColor Red; exit 1 }

function Find-Python {
    foreach ($cmd in @("py -3.14", "py -3.13", "py -3.12", "py -3.11", "python")) {
        $parts = $cmd.Split(" ")
        try {
            $ok = & $parts[0] $parts[1..9] -c "import sys; print(sys.version_info >= (3, 11))" 2>$null
            if ($ok -eq "True") { return $parts }
        } catch { }
    }
    return $null
}

function Install-Engine {
    if (-not (Test-Path (Join-Path $Engine "pyproject.toml"))) { Fail "Can't find the engine folder next to this script ($Engine)." }
    if (-not (Test-Path $Py)) {
        $python = Find-Python
        if (-not $python) {
            Say "Trading Alerts needs Python 3.11 or newer."
            Say "Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then run this again."
            exit 1
        }
        Bold "Setting up Python packages (one-off, takes a minute or two)..."
        New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
        & $python[0] $python[1..9] -m venv $Venv
        if ($LASTEXITCODE -ne 0) { Fail "Couldn't create a Python environment." }
    }
    & $Py -m pip install --quiet --upgrade pip
    & $Py -m pip install --quiet --upgrade "$Engine[yahoo]"
    if ($LASTEXITCODE -ne 0) { Fail "Installing the Python packages failed (see above)." }
    if (-not (Test-Path (Join-Path $Folder "config.yaml"))) {
        & $Py -m tradealerts init $Folder | Out-Null
        Say "Your strategies and settings will be kept in $Folder"
    }
    # A shortcut on the desktop that starts it and opens the browser
    $start = Join-Path $AppDir "Start Trading Alerts.cmd"
    Set-Content -Path $start -Encoding ASCII -Value @"
@echo off
cd /d "$Folder"
start "" http://localhost:$Port
"$Py" -m tradealerts serve --port $Port
"@
}

function Open-Browser {
    $url = "http://localhost:$Port"
    for ($i = 0; $i -lt 60; $i++) {
        try { Invoke-WebRequest -UseBasicParsing "$url/api/session" -TimeoutSec 1 | Out-Null; break } catch { Start-Sleep -Milliseconds 500 }
    }
    Start-Process $url
}

function Do-Web {
    Install-Engine
    Bold "Starting Trading Alerts at http://localhost:$Port (this computer only). Close this window to stop it."
    Say "Next time, double-click: $(Join-Path $AppDir 'Start Trading Alerts.cmd')"
    Start-Job -ScriptBlock { param($u) for ($i = 0; $i -lt 60; $i++) { try { Invoke-WebRequest -UseBasicParsing "$u/api/session" -TimeoutSec 1 | Out-Null; break } catch { Start-Sleep -Milliseconds 500 } }; Start-Process $u } -ArgumentList "http://localhost:$Port" | Out-Null
    Set-Location $Folder
    & $Py -m tradealerts serve --port $Port
}

function Do-Service {
    Install-Engine
    $action = New-ScheduledTaskAction -Execute $PyW -Argument "-m tradealerts serve --port $Port" -WorkingDirectory $Folder
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Description "Trading Alerts: backtests, forward tests and alerts" | Out-Null
    Start-ScheduledTask -TaskName $TaskName
    Bold "Trading Alerts is running in the background at http://localhost:$Port"
    Say "It starts again by itself when you log in. Remove it with: setup.ps1 -RemoveService"
    Open-Browser
}

function Do-RemoveService {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Say "The background task is removed. Your strategies and settings in $Folder are untouched."
}

function Do-Desktop {
    if (-not (Test-Path $DesktopDir)) { Fail "The desktop folder isn't next to this script." }
    Install-Engine
    $missing = $false
    if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) {
        Say "- Rust isn't installed. Get it from https://rustup.rs (choose the default options, which also install the Visual Studio build tools)."
        $missing = $true
    }
    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
        Say "- Node.js isn't installed. Get the LTS version from https://nodejs.org"
        $missing = $true
    }
    if ($missing) { Fail "Install the missing pieces above, open a new PowerShell window, then run setup.ps1 -Desktop again." }
    Bold "Building the desktop app. The first build takes 5 to 15 minutes..."
    & $Py -m pip install --quiet pyinstaller
    $env:PATH = (Join-Path $Venv "Scripts") + ";" + $env:PATH
    Push-Location $DesktopDir
    try {
        npm ci --no-audit --no-fund
        if ($LASTEXITCODE -ne 0) { Fail "npm install failed (see above)." }
        npm run build
        if ($LASTEXITCODE -ne 0) { Fail "The build failed (see above)." }
    } finally { Pop-Location }
    Bold "Done. Your installer is in:"
    Say "  $(Join-Path $DesktopDir 'src-tauri\target\release\bundle')  (the setup .exe or the .msi)"
}

function Do-Docker {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Fail "Docker isn't installed. See https://docs.docker.com/desktop/setup/install/windows-install/" }
    $envFile = Join-Path $Here ".env"
    if (-not (Test-Path $envFile)) {
        $chars = [char[]]"ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
        $pw = -join (1..24 | ForEach-Object { $chars | Get-Random })
        Set-Content -Path $envFile -Encoding ASCII -Value "TRADEALERTS_PASSWORD=$pw"
        Bold "Your web interface password is: $pw"
        Say "(It's saved in $envFile. Change it there and restart to choose your own.)"
    }
    New-Item -ItemType Directory -Force -Path (Join-Path $Here "workspace") | Out-Null
    Push-Location $Here
    try { docker compose up -d --build } finally { Pop-Location }
    Bold "Running at http://localhost:$Port"
}

if ($Web) { Do-Web }
elseif ($Service) { Do-Service }
elseif ($Desktop) { Do-Desktop }
elseif ($Docker) { Do-Docker }
elseif ($RemoveService) { Do-RemoveService }
elseif ($InstallOnly) { Install-Engine; Say "Installed." }
else {
    Bold "Trading Alerts setup"
    Say ""
    Say "  1) Use it in your web browser (starts now, on this computer only)"
    Say "  2) Keep it running in the background, so forward tests and alerts carry on (starts when you log in)"
    Say "  3) Build the desktop app (needs Rust and Node.js; about 10 minutes)"
    Say "  4) Run it in Docker"
    Say "  5) Remove the background task"
    Say ""
    switch (Read-Host "Choose 1-5") {
        "1" { Do-Web } "2" { Do-Service } "3" { Do-Desktop } "4" { Do-Docker } "5" { Do-RemoveService }
        default { Fail "Choose a number from 1 to 5." }
    }
}
