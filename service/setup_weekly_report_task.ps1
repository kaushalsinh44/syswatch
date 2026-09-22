<#
.SYNOPSIS
    Registers a weekly Windows Scheduled Task that runs report.py to generate
    the Markdown case report for the week that just ended, into reports/.

.DESCRIPTION
    Runs Mondays, after the daily anomaly-detection task has had a chance to
    finalize the previous day's (Sunday's) anomalies -- see setup_daily_anomaly_task.ps1.

.PARAMETER TaskName
    Name of the Scheduled Task. Default: LaptopMysteryDetectiveWeeklyReport

.PARAMETER At
    Local time to run on Mondays, HH:mm. Default: 02:30 (after the 02:00 daily anomaly task).

.EXAMPLE
    .\setup_weekly_report_task.ps1
#>
param(
    [string]$TaskName = "LaptopMysteryDetectiveWeeklyReport",
    [string]$ProjectRoot = (Resolve-Path "$PSScriptRoot\..").Path,
    [string]$PythonExe = "$((Resolve-Path "$PSScriptRoot\..").Path)\venv\Scripts\pythonw.exe",
    [string]$At = "02:30"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $PythonExe)) {
    Write-Error "Python executable not found at '$PythonExe'. Create the venv first: py -3.12 -m venv venv"
    exit 1
}

$outLog = Join-Path $PSScriptRoot "report_output.log"
$errLog = Join-Path $PSScriptRoot "report_error.log"
$script = Join-Path $ProjectRoot "report.py"

if (-not (Test-Path $outLog)) { New-Item -ItemType File -Path $outLog | Out-Null }
if (-not (Test-Path $errLog)) { New-Item -ItemType File -Path $errLog | Out-Null }

$cmdArgs = "/c `"`"$PythonExe`" `"$script`" >> `"$outLog`" 2>> `"$errLog`"`""

$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $cmdArgs -WorkingDirectory $ProjectRoot
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At $At
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Runs Laptop Mystery Detective's report.py weekly to generate the case report for the prior week." `
    -Force | Out-Null

Write-Host "Registered scheduled task '$TaskName' (Mondays at $At)."
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State

Write-Host ""
Write-Host "Run it once now to verify:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Check output:               Get-Content '$outLog' -Tail 20"
Write-Host "Reports land in:            $ProjectRoot\reports\"
Write-Host "Remove it:                  Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
