# Register a visible Mon-Fri 08:00 task that runs Misc\run_daily_session.ps1
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

# Visible console: cmd /c shows the window for the whole session, then closes
# after run_daily_session.ps1's 10-minute review sleep + exit.
# (cmd /k left a leftover prompt and kept the task "Running".)
# PowerShell -File alone can end up with no useful window after RDP reconnect.
$Action = New-ScheduledTaskAction `
    -Execute "cmd.exe" `
    -Argument "/c powershell.exe -ExecutionPolicy Bypass -NoProfile -File `"$Launcher`"" `
    -WorkingDirectory $WorkDir

# 08:00 Mon-Fri (token + warmup before open)
$Trigger0800 = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday `
    -At 8:00am

# Backup: if the box was signed out at 08:00, start at next interactive logon.
$TriggerLogon = New-ScheduledTaskTrigger -AtLogOn -User $UserId

$Principal = New-ScheduledTaskPrincipal `
    -UserId $UserId `
    -LogonType Interactive `
    -RunLevel Limited

# Use Parallel at register time (this host's PowerShell enum has no StopExisting).
# PID lock in run_daily_session.ps1 prevents two live main.py copies. Then patch
# XML to StopExisting + disable idle-stop so a leftover "Press Enter" window
# cannot make IgnoreNew skip tomorrow's 08:00 trigger.
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances Parallel `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 0 `
    -WakeToRun

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger @($Trigger0800, $TriggerLogon) `
    -Principal $Principal `
    -Settings $Settings `
    -Force | Out-Null

try {
    $xml = [xml](Export-ScheduledTask -TaskName $TaskName)
    $nsm = New-Object System.Xml.XmlNamespaceManager($xml.NameTable)
    $nsm.AddNamespace("t", "http://schemas.microsoft.com/windows/2004/02/mit/task")
    $idle = $xml.SelectSingleNode("//t:IdleSettings/t:StopOnIdleEnd", $nsm)
    if ($idle) { $idle.InnerText = "false" }
    $multi = $xml.SelectSingleNode("//t:MultipleInstancesPolicy", $nsm)
    if ($multi) { $multi.InnerText = "StopExisting" }
    Register-ScheduledTask -TaskName $TaskName -Xml $xml.OuterXml -Force | Out-Null
} catch {
    Write-Host "Warning: could not patch idle/multi settings: $_"
}

# Enable Task Scheduler history (Operational log) if disabled.
try {
    wevtutil set-log Microsoft-Windows-TaskScheduler/Operational /enabled:true 2>$null
} catch {}

Write-Host "Registered task: $TaskName"
Write-Host "  User:     $UserId  (Interactive - visible window)"
Write-Host "  Triggers: Mon-Fri 08:00 + At logon"
Write-Host "  Action:   cmd /c powershell -File $Launcher  (auto-closes after 10 min review)"
Write-Host "  WorkDir:  $WorkDir"
Write-Host "  Instances: StopExisting (won't skip if yesterday's window still open)"
Write-Host ""
Write-Host "Test now:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Query:     Get-ScheduledTask -TaskName '$TaskName' | Get-ScheduledTaskInfo"
Write-Host "Launch log: $WorkDir\logs\scheduler_launch.log"
Write-Host ""
Write-Host "VPS: lock (Win+L) instead of Sign out so 08:00 can show a window."
Write-Host "Do NOT set 'Run whether user is logged on or not' - that hides the UI."
