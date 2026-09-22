<#
.SYNOPSIS
    Registers a daily Windows Scheduled Task that runs anomaly.py --all, so
    the anomalies table (and therefore the dashboard) stays current without
    having to run it by hand.

.DESCRIPTION
    Unlike the logger's task, this one is a short-lived script that's meant
    to run once a day and exit -- so it uses a Daily trigger, not "at logon".
    --all is used (not just today) since it's cheap (~seconds for a 14-day
    lookback) and idempotent (each date's anomalies are replaced, not
    duplicated), so it's simpler than reasoning about UTC-vs-local "today".

.PARAMETER TaskName
    Name of the Scheduled Task. Default: LaptopMysteryDetectiveAnomalyDetection

.PARAMETER At
    Local time to run daily, HH:mm. Default: 02:00 (off-hours).

.EXAMPLE
    .\setup_daily_anomaly_task.ps1
    .\setup_daily_anomaly_task.ps1 -At "03:30"
#>
param(
    [string]$TaskName = "LaptopMysteryDetectiveAnomalyDetection",
    [string]$ProjectRoot = (Resolve-Path "$PSScriptRoot\..").Path,
    [string]$PythonExe = "$((Resolve-Path "$PSScriptRoot\..").Path)\venv\Scripts\pythonw.exe",
    [string]$At = "02:00"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $PythonExe)) {
    Write-Error "Python executable not found at '$PythonExe'. Create the venv first: py -3.12 -m venv venv"
    exit 1
}

$outLog = Join-Path $PSScriptRoot "anomaly_output.log"
$errLog = Join-Path $PSScriptRoot "anomaly_error.log"
$script = Join-Path $ProjectRoot "anomaly.py"

if (-not (Test-Path $outLog)) { New-Item -ItemType File -Path $outLog | Out-Null }
if (-not (Test-Path $errLog)) { New-Item -ItemType File -Path $errLog | Out-Null }

$cmdArgs = "/c `"`"$PythonExe`" `"$script`" --all >> `"$outLog`" 2>> `"$errLog`"`""

$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $cmdArgs -WorkingDirectory $ProjectRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Runs Laptop Mystery Detective's anomaly.py --all once a day so the dashboard stays current." `
    -Force | Out-Null

Write-Host "Registered scheduled task '$TaskName' (daily at $At)."
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State

Write-Host ""
Write-Host "Run it once now to verify:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Check output:               Get-Content '$outLog' -Tail 20"
Write-Host "Remove it:                  Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
