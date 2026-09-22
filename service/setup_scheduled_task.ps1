<#
.SYNOPSIS
    Registers the Laptop Mystery Detective logger as a Windows Scheduled Task
    that starts at logon and runs indefinitely (logger.py loops internally).

.DESCRIPTION
    Safe to re-run: uses -Force to update an existing registration idempotently.
    Does NOT run every N seconds -- logger.py itself contains the sampling loop,
    so this task fires once at logon and lets the process run forever.

.PARAMETER TaskName
    Name of the Scheduled Task. Default: LaptopMysteryDetectiveLogger

.PARAMETER PythonExe
    Path to the interpreter to run. Default: venv\Scripts\pythonw.exe (windowless).

.PARAMETER IntervalSec
    Sampling interval passed to logger.py --interval. Default: 45.

.EXAMPLE
    .\setup_scheduled_task.ps1
    .\setup_scheduled_task.ps1 -IntervalSec 30
#>
param(
    [string]$TaskName = "LaptopMysteryDetectiveLogger",
    [string]$ProjectRoot = (Resolve-Path "$PSScriptRoot\..").Path,
    [string]$PythonExe = "$((Resolve-Path "$PSScriptRoot\..").Path)\venv\Scripts\pythonw.exe",
    [double]$IntervalSec = 45
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $PythonExe)) {
    Write-Error "Python executable not found at '$PythonExe'. Create the venv first: py -3.12 -m venv venv"
    exit 1
}

$outLog = Join-Path $PSScriptRoot "logger_output.log"
$errLog = Join-Path $PSScriptRoot "logger_error.log"
$loggerScript = Join-Path $ProjectRoot "logger.py"

if (-not (Test-Path $outLog)) { New-Item -ItemType File -Path $outLog | Out-Null }
if (-not (Test-Path $errLog)) { New-Item -ItemType File -Path $errLog | Out-Null }

$cmdArgs = "/c `"`"$PythonExe`" `"$loggerScript`" --interval $IntervalSec >> `"$outLog`" 2>> `"$errLog`"`""

$action = New-ScheduledTaskAction `
    -Execute "cmd.exe" `
    -Argument $cmdArgs `
    -WorkingDirectory $ProjectRoot

$trigger = New-ScheduledTaskTrigger -AtLogOn

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Runs Laptop Mystery Detective's background telemetry logger at user logon." `
    -Force | Out-Null

Write-Host "Registered scheduled task '$TaskName'."
Write-Host "  Python:   $PythonExe"
Write-Host "  Script:   $loggerScript --interval $IntervalSec"
Write-Host "  Stdout:   $outLog"
Write-Host "  Stderr:   $errLog"
Write-Host ""

Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State

Write-Host ""
Write-Host "Starting it now so you can verify it works without logging off/on..."
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 3
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State

Write-Host ""
Write-Host "Check it's writing:   Get-Content '$outLog' -Wait -Tail 10"
Write-Host "Stop it:              Stop-ScheduledTask -TaskName '$TaskName'"
Write-Host "Remove it:            Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
