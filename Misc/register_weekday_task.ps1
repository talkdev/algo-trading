# Register interactive Mon-Fri 08:00 + At-logon task for NiftyAlgo.
# Runs in YOUR desktop session (visible CMD). Does NOT use Session 0
# ("run whether user is logged on or not") - that would hide the window.
#
# Usage (PowerShell, as the VPS user you monitor with):
#   Set-ExecutionPolicy -Scope Process Bypass -Force
#   cd C:\Users\Administrator\Desktop\algo-trading
#   .\Misc\register_weekday_task.ps1

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$WorkDir   = Split-Path -Parent $ScriptDir
$Launcher  = Join-Path $WorkDir "run_weekday_open.cmd"
$TaskName  = "NiftyAlgo-WeekdayInteractive"
$UserId    = "$env:USERDOMAIN\$env:USERNAME"

if (-not (Test-Path $Launcher)) {
    throw "Launcher not found: $Launcher"
}

# Visible CMD that stays open (/k). WorkingDirectory = repo root (main.py / upstox_token.py).
$Action = New-ScheduledTaskAction `
    -Execute "cmd.exe" `
    -Argument "/k `"$Launcher`"" `
    -WorkingDirectory $WorkDir

# 08:00 Mon-Fri
$Trigger0800 = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 8:00am

# Also at interactive logon (covers "was logged off at 08:00")
$TriggerLogon = New-ScheduledTaskTrigger -AtLogOn -User $UserId

# Interactive only - window appears on this user's desktop after unlock/login
$Principal = New-ScheduledTaskPrincipal `
    -UserId $UserId `
    -LogonType Interactive `
    -RunLevel Limited

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 0

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger @($Trigger0800, $TriggerLogon) `
    -Principal $Principal `
    -Settings $Settings `
    -Force | Out-Null

Write-Host "Registered task: $TaskName"
Write-Host "  User:     $UserId  (Interactive)"
Write-Host "  Triggers: Mon-Fri 08:00 + At logon"
Write-Host "  Action:   cmd /k $Launcher"
Write-Host "  WorkDir:  $WorkDir"
Write-Host ""
Write-Host "Test now:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Query:     Get-ScheduledTask -TaskName '$TaskName' | Get-ScheduledTaskInfo"
Write-Host ""
Write-Host "IMPORTANT for VPS:"
Write-Host "  - LOCK the session (Win+L) instead of Sign out - 08:00 still fires and the CMD is there when you unlock."
Write-Host "  - If you Sign out, 08:00 is skipped; the At-logon trigger starts the CMD when you next log in."
Write-Host "  - Do NOT switch the task to 'Run whether user is logged on or not' - that hides the window (Session 0)."
