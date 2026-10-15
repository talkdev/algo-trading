# Register a visible Mon-Fri 08:30 task that runs Misc\run_daily_session.ps1
# in YOUR interactive desktop session (not Session 0).
#
#   Set-ExecutionPolicy -Scope Process Bypass -Force
#   cd C:\Users\Administrator\Desktop\algo-trading
#   powershell -ExecutionPolicy Bypass -File .\Misc\register_daily_session.ps1

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$WorkDir = Split-Path -Parent $ScriptDir
$Launcher = Join-Path $ScriptDir "run_daily_session.ps1"
$TaskName = "NiftyAlgo-Daily830Interactive"
$UserId = "$env:USERDOMAIN\$env:USERNAME"

if (-not (Test-Path -LiteralPath $Launcher)) {
    throw "Launcher not found: $Launcher"
}

$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-ExecutionPolicy Bypass -NoProfile -File `"$Launcher`"" `
    -WorkingDirectory $WorkDir

$Trigger0830 = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday `
    -At 8:30am

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
    -Trigger $Trigger0830 `
    -Principal $Principal `
    -Settings $Settings `
    -Force | Out-Null

Write-Host "Registered task: $TaskName"
Write-Host "  User:     $UserId  (Interactive - visible window)"
Write-Host "  Trigger:  Mon-Fri 08:30"
Write-Host "  Script:   $Launcher"
Write-Host "  WorkDir:  $WorkDir"
Write-Host ""
Write-Host "Test now:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Query:     Get-ScheduledTask -TaskName '$TaskName' | Get-ScheduledTaskInfo"
Write-Host ""
Write-Host "VPS: lock (Win+L) instead of Sign out so 08:30 can show a window."
Write-Host "Do NOT set 'Run whether user is logged on or not' - that hides the UI."
