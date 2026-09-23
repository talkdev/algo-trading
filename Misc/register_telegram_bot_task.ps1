# Ensure TelegramAlgoBot is a single instance: StopExisting on re-trigger,
# and bot_controller.py itself kills any leftover bot_controller.py PIDs on start.
#
# Usage:
#   Set-ExecutionPolicy -Scope Process Bypass -Force
#   .\Misc\register_telegram_bot_task.ps1

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$WorkDir   = Split-Path -Parent $ScriptDir
$BotScript = Join-Path $WorkDir "bot_controller.py"
$Python    = "C:\Users\Administrator\AppData\Local\Programs\Python\Python313\python.exe"
$TaskName  = "TelegramAlgoBot"

if (-not (Test-Path $BotScript)) {
    throw "bot_controller.py not found: $BotScript"
}
if (-not (Test-Path $Python)) {
    $Python = (Get-Command python -ErrorAction Stop).Source
}

$Action = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument "`"$BotScript`"" `
    -WorkingDirectory $WorkDir

# Boot + keep trying if the VPS was off
$TriggerBoot = New-ScheduledTaskTrigger -AtStartup

# SYSTEM service account (existing task). Run with highest privileges.
$Principal = New-ScheduledTaskPrincipal `
    -UserId "SYSTEM" `
    -LogonType ServiceAccount `
    -RunLevel Highest

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $TriggerBoot `
    -Principal $Principal `
    -Settings $Settings `
    -Force | Out-Null

Write-Host "Registered task: $TaskName"
Write-Host "  MultipleInstances: IgnoreNew (scheduler will not start a 2nd task run)"
Write-Host "  Action: $Python $BotScript"
Write-Host "  bot_controller.py kills any leftover bot_controller.py PIDs on start"
Write-Host ""
Write-Host "Restart now:  Restart-ScheduledTask -TaskName '$TaskName'"
Write-Host "  (or: Stop-ScheduledTask then Start-ScheduledTask)"
