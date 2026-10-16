# Loads deploy/frankensystem.env and runs ONE of: the read-only check, or the gateway.
#
#   .\deploy\run_frankensystem.ps1 check     # reads the battery and prints PASS / WARN / FAIL. Writes nothing.
#   .\deploy\run_frankensystem.ps1 gateway   # runs the gateway with the settings in the file
#
# Run it from the project folder, with the virtual environment active (.venv\Scripts\Activate.ps1).
param([Parameter(Mandatory = $true)][ValidateSet("check", "gateway")][string]$What)

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

if ($What -eq "check") {
    # Prefer the BMS's read-only monitoring port (MONITOR_PORT, 5030 in SPC100644 Rev 05): the BMS itself refuses
    # writes there and several clients may connect. The control port (MODBUS_PORT) takes ONE client at a time.
    $port = if ($env:MONITOR_PORT) { $env:MONITOR_PORT } else { $env:MODBUS_PORT }
    python -m capstone.bms_probe --host $env:MODBUS_HOST --port $port --unit $env:MODBUS_UNIT_ID
    exit $LASTEXITCODE
}

if ($env:BMS_READ_ONLY -ne "1") {
    Write-Host "BMS_READ_ONLY is OFF: this gateway can command the battery." -ForegroundColor Yellow
    $answer = Read-Host "Is an Invinity engineer beside you, and did they say yes? Type YES to continue"
    if ($answer -ne "YES") { Write-Host "Not started."; exit 1 }
}
python -m capstone
