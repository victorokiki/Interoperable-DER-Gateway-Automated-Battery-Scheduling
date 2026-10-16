# Loads deploy/frankensystem.env and runs ONE of: the read-only check, the utility server, or the gateway.
#
#   .\deploy\run_frankensystem.ps1 check     # reads the battery and prints PASS / WARN / FAIL. Writes nothing.
#   .\deploy\run_frankensystem.ps1 server    # our simulated utility server + dashboard (window 1, leave it running)
#   .\deploy\run_frankensystem.ps1 gateway   # the gateway (window 2). Needs the server running first.
#
# Run it from the project folder, with the virtual environment active (.venv\Scripts\Activate.ps1).
param([Parameter(Mandatory = $true)][ValidateSet("check", "server", "gateway")][string]$What)

$envFile = Join-Path $PSScriptRoot "frankensystem.env"
if (-not (Test-Path $envFile)) {
    Write-Host "Missing deploy\frankensystem.env. Copy deploy\frankensystem.env.example to it and fill in the BMS address." -ForegroundColor Red
    exit 1
}
Get-Content $envFile | ForEach-Object {
    $line = $_.Trim()
    if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
        $name, $value = $line.Split("=", 2)
        Set-Item -Path "env:$($name.Trim())" -Value $value.Trim()
    }
}
if ($env:MODBUS_HOST -like "CHANGE_ME*") {
    Write-Host "MODBUS_HOST is still CHANGE_ME. Put the BMS IP address Invinity gave you in deploy\frankensystem.env." -ForegroundColor Red
    exit 1
}

function Get-Gateways {
    Get-CimInstance Win32_Process | Where-Object { $_.Name -match "python" -and $_.CommandLine -match "-m capstone(\s|$)" }
}

if ($What -eq "check") {
    # Prefer the BMS's read-only monitoring port (MONITOR_PORT, 5030 in SPC100644 Rev 05): the BMS itself refuses
    # writes there and several clients may connect. The control port (MODBUS_PORT) takes ONE client at a time.
    $port = if ($env:MONITOR_PORT) { $env:MONITOR_PORT } else { $env:MODBUS_PORT }
    python -m capstone.bms_probe --host $env:MODBUS_HOST --port $port --unit $env:MODBUS_UNIT_ID
    exit $LASTEXITCODE
}

if ($What -eq "server") {
    python simulators\fake_grid_server.py
    exit $LASTEXITCODE
}

# --- gateway ---------------------------------------------------------------------------------------------
# The BMS control port accepts ONE client. A second gateway would keep knocking the first one off.
$running = @(Get-Gateways)
if ($running.Count -gt 0) {
    $ids = ($running | ForEach-Object { $_.ProcessId }) -join ", "
    Write-Host "A gateway is already running (PID $ids)." -ForegroundColor Red
    Write-Host "The BMS control port takes only ONE client, so two gateways fight over it. Stop the old one first" -ForegroundColor Red
    Write-Host "(Ctrl+C in its window, or close that window), then run this again." -ForegroundColor Red
    exit 1
}
# The gateway takes its schedule from the utility server on this laptop, so that must be running first.
$serverUp = (Test-NetConnection -ComputerName 127.0.0.1 -Port 5001 -WarningAction SilentlyContinue).TcpTestSucceeded
if (-not $serverUp) {
    Write-Host "The utility server is not running on this laptop (port 5001)." -ForegroundColor Red
    Write-Host "Open another PowerShell window here, activate the venv, and run:  .\deploy\run_frankensystem.ps1 server" -ForegroundColor Red
    exit 1
}

if ($env:BMS_READ_ONLY -ne "1") {
    Write-Host "BMS_READ_ONLY is OFF: this gateway can command the battery." -ForegroundColor Yellow
    $answer = Read-Host "Is an Invinity engineer beside you, and did they say yes? Type YES to continue"
    if ($answer -ne "YES") { Write-Host "Not started."; exit 1 }
}
python -m capstone
