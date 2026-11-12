# Registers a Windows scheduled task that syncs Garmin data in the background, so the
# dashboard and the Claude Desktop MCP server stay current without the web app running.
# Runs at sign-in and every 2 hours, silently (pythonw), and logs to data\sync.log.
#   Install:  .\scripts\schedule-sync.ps1
#   Remove:   .\scripts\schedule-sync.ps1 -Remove

param([switch]$Remove, [int]$Days = 7, [int]$EveryHours = 2)

$ErrorActionPreference = "Stop"
$taskName = "Garmin Insights sync"
$projectRoot = Split-Path -Parent $PSScriptRoot

if ($Remove) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host "Removed the '$taskName' task."
    return
}

$python = (Get-Command python -ErrorAction Stop).Source
if (Test-Path "$projectRoot\.venv\Scripts\python.exe") { $python = "$projectRoot\.venv\Scripts\python.exe" }
$pythonw = Join-Path (Split-Path $python) "pythonw.exe"

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "cli.py sync --days $Days --unattended" -WorkingDirectory $projectRoot
$atLogon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5) -RepetitionInterval (New-TimeSpan -Hours $EveryHours)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $atLogon, $repeat -Settings $settings `
    -Principal $principal -Description "Syncs Garmin Connect data into $projectRoot\data\garmin.db" -Force | Out-Null

Write-Host "Registered '$taskName': at sign-in and every $EveryHours hours, syncing the last $Days days."
Write-Host "Results are appended to $projectRoot\data\sync.log"
