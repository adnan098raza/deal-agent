<#
.SYNOPSIS
  Registers the deal agent in Windows Task Scheduler:
  starts at log-on, runs hidden (pythonw.exe, no console window) and is restarted if it stops.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\setup_autostart.ps1          # install + start now
  powershell -ExecutionPolicy Bypass -File .\setup_autostart.ps1 -Remove  # uninstall
  powershell -ExecutionPolicy Bypass -File .\setup_autostart.ps1 -Profile it          # 2nd agent (.env.it) as task DealAgent-IT
  powershell -ExecutionPolicy Bypass -File .\setup_autostart.ps1 -Profile it -Remove

.NOTES
  Restart strategy: Task Scheduler's "restart on failure" (every minute, up to 999 times) plus a
  15-minute watchdog trigger. While the agent runs, extra triggers are ignored (IgnoreNew), and
  the agent itself refuses to start twice (lock file), so it is safe to run this script again.
  Logs: %LOCALAPPDATA%\DealAgent\agent.log
#>
param(
    [switch]$Remove,
    [Alias("Profile")][string]$AgentProfile = "",   # not $Profile: that is a PowerShell built-in
    [string]$TaskName = ""
)
$ErrorActionPreference = "Stop"
$Project = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonW = Join-Path $Project ".venv\Scripts\pythonw.exe"
$AgentProfile = $AgentProfile.ToLower()
if (-not $TaskName) { $TaskName = if ($AgentProfile) { "DealAgent-$($AgentProfile.ToUpper())" } else { "DealAgent" } }
$EnvFile = if ($AgentProfile) { ".env.$AgentProfile" } else { ".env" }
$AgentArgs = if ($AgentProfile) { "-m deal_agent --profile $AgentProfile start" } else { "-m deal_agent start" }
$LogName = if ($AgentProfile) { "agent-$AgentProfile.log" } else { "agent.log" }

if ($Remove) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed scheduled task '$TaskName'."
    return
}

if (-not (Test-Path $PythonW)) {
    throw "pythonw.exe not found at $PythonW. Create the venv first: python -m venv .venv; .venv\Scripts\pip install -r requirements.txt"
}
if (-not (Test-Path (Join-Path $Project $EnvFile))) {
    throw "$EnvFile not found in $Project - create it first (copy .env.example)."
}

$User = "$env:USERDOMAIN\$env:USERNAME"
$Action = New-ScheduledTaskAction -Execute $PythonW -Argument $AgentArgs -WorkingDirectory $Project
$AtLogOn = New-ScheduledTaskTrigger -AtLogOn -User $User
$Watchdog = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) -RepetitionInterval (New-TimeSpan -Minutes 15)
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
$Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger @($AtLogOn, $Watchdog) `
    -Settings $Settings -Principal $Principal -Force `
    -Description "AI shopping deal agent: python $AgentArgs" | Out-Null

Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 3
$Info = Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo
Write-Host "Registered '$TaskName' (runs at log-on, restarts on failure)."
Write-Host ("State: {0}   Last result: {1}" -f (Get-ScheduledTask -TaskName $TaskName).State, $Info.LastTaskResult)
if (-not $AgentProfile) { Write-Host "Dashboard: http://127.0.0.1:8765" }
Write-Host "Log: $env:LOCALAPPDATA\DealAgent\$LogName"
