# setup_tasks.ps1 v4 - ASCII only, no special characters
# Run as Administrator

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# --- Configuration ---
$SCRIPT_DIR     = "C:\Users\Administrator\Desktop\algo-trading\full-system"
$LAUNCH_MAIN    = Join-Path $SCRIPT_DIR "launch_main.vbs"
$LAUNCH_JOURNAL = Join-Path $SCRIPT_DIR "launch_journal.vbs"
$USERNAME       = "Administrator"
$TASK_MAIN      = "NiftyAlgo_MainEngine"
$TASK_JOURNAL   = "NiftyAlgo_DecisionJournal"

# --- Verify VBS launchers exist ---
foreach ($f in @($LAUNCH_MAIN, $LAUNCH_JOURNAL)) {
    if (-not (Test-Path $f)) {
        Write-Host "ERROR: Launcher not found: $f" -ForegroundColor Red
        Write-Host "       Create the .vbs launcher files first." -ForegroundColor Red
        exit 1
    }
}

Write-Host "=== Setting up Task Scheduler ===" -ForegroundColor Cyan
Write-Host "Main engine task  : $TASK_MAIN" -ForegroundColor White
Write-Host "Journal task      : $TASK_JOURNAL" -ForegroundColor White
Write-Host "User              : $USERNAME" -ForegroundColor White
Write-Host ""

# --- Helper: remove existing task ---
function Remove-TaskIfExists {
    param([string]$TaskName)
    try {
        $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($existing) {
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
            Write-Host "  Removed existing task: $TaskName" -ForegroundColor Yellow
        }
    }
    catch { }
}

# --- TASK 1: Main Engine at 09:00 Mon-Fri ---
Write-Host "[1] Creating main engine task ($TASK_MAIN)..." -ForegroundColor Cyan
Remove-TaskIfExists $TASK_MAIN

$mainAction = New-ScheduledTaskAction `
    -Execute "wscript.exe" `
    -Argument "`"$LAUNCH_MAIN`"" `
    -WorkingDirectory $SCRIPT_DIR

$mainTrigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
    -At "09:00AM"

$mainSettings = New-ScheduledTaskSettingsSet
$mainSettings.ExecutionTimeLimit        = "PT8H"
$mainSettings.MultipleInstances         = "IgnoreNew"
$mainSettings.StartWhenAvailable        = $true
$mainSettings.RunOnlyIfNetworkAvailable = $true
$mainSettings.WakeToRun                 = $false
$mainSettings.Enabled                   = $true

$mainPrincipal = New-ScheduledTaskPrincipal `
    -UserId    $USERNAME `
    -LogonType Interactive `
    -RunLevel  Highest

$mainTask = New-ScheduledTask `
    -Action      $mainAction `
    -Trigger     $mainTrigger `
    -Settings    $mainSettings `
    -Principal   $mainPrincipal `
    -Description "NIFTY algo engine. Mon-Fri 09:00. Auto-kills 15:30. Skips NSE holidays."

Register-ScheduledTask `
    -TaskName    $TASK_MAIN `
    -InputObject $mainTask `
    -Force | Out-Null

Write-Host "  Created  : $TASK_MAIN" -ForegroundColor Green
Write-Host "  Launcher : $LAUNCH_MAIN" -ForegroundColor Green
Write-Host "  Schedule : Mon-Fri at 09:00 AM" -ForegroundColor Green
Write-Host "  Auto-kill: 15:30 IST (inside run_main.ps1)" -ForegroundColor Green

# --- TASK 2: Decision Journal at 16:00 Mon-Fri ---
Write-Host ""
Write-Host "[2] Creating journal task ($TASK_JOURNAL)..." -ForegroundColor Cyan
Remove-TaskIfExists $TASK_JOURNAL

$journalAction = New-ScheduledTaskAction `
    -Execute "wscript.exe" `
    -Argument "`"$LAUNCH_JOURNAL`"" `
    -WorkingDirectory $SCRIPT_DIR

$journalTrigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
    -At "04:00PM"

$journalSettings = New-ScheduledTaskSettingsSet
$journalSettings.ExecutionTimeLimit        = "PT1H"
$journalSettings.MultipleInstances         = "IgnoreNew"
$journalSettings.StartWhenAvailable        = $true
$journalSettings.RunOnlyIfNetworkAvailable = $true
$journalSettings.WakeToRun                 = $false
$journalSettings.Enabled                   = $true

$journalPrincipal = New-ScheduledTaskPrincipal `
    -UserId    $USERNAME `
    -LogonType Interactive `
    -RunLevel  Highest

$journalTask = New-ScheduledTask `
    -Action      $journalAction `
    -Trigger     $journalTrigger `
    -Settings    $journalSettings `
    -Principal   $journalPrincipal `
    -Description "NIFTY decision journal. Mon-Fri 16:00. Skips NSE holidays. Auto-exits."

Register-ScheduledTask `
    -TaskName    $TASK_JOURNAL `
    -InputObject $journalTask `
    -Force | Out-Null

Write-Host "  Created  : $TASK_JOURNAL" -ForegroundColor Green
Write-Host "  Launcher : $LAUNCH_JOURNAL" -ForegroundColor Green
Write-Host "  Schedule : Mon-Fri at 04:00 PM" -ForegroundColor Green
Write-Host "  Auto-exit: Yes (script exits when done)" -ForegroundColor Green

# --- Verify both tasks ---
Write-Host ""
Write-Host "=== Verification ===" -ForegroundColor Cyan

foreach ($taskName in @($TASK_MAIN, $TASK_JOURNAL)) {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($task) {
        $info = Get-ScheduledTaskInfo -TaskName $taskName -ErrorAction SilentlyContinue
        Write-Host ""
        Write-Host "  $taskName" -ForegroundColor Green
        Write-Host "    State     : $($task.State)" -ForegroundColor White
        Write-Host "    Executor  : $($task.Actions[0].Execute)" -ForegroundColor White
        if ($info) {
            Write-Host "    Next run  : $($info.NextRunTime)" -ForegroundColor White
        }
    }
    else {
        Write-Host "  $taskName : NOT FOUND" -ForegroundColor Red
    }
}

# --- Quick test: launch main window directly ---
Write-Host ""
Write-Host "=== Quick Test ===" -ForegroundColor Cyan
Write-Host "Testing launch_main.vbs directly..." -ForegroundColor Yellow

try {
    $testProc = Start-Process `
        -FilePath "wscript.exe" `
        -ArgumentList "`"$LAUNCH_MAIN`"" `
        -WorkingDirectory $SCRIPT_DIR `
        -PassThru

    Start-Sleep -Seconds 4

    if (-not $testProc.HasExited) {
        Write-Host "  SUCCESS: Window opened (PID=$($testProc.Id))" -ForegroundColor Green
        Write-Host "  The run_main.ps1 window should be visible on your desktop." -ForegroundColor Green
        Write-Host "  Close it manually or wait for 15:30 auto-kill." -ForegroundColor Yellow
    }
    else {
        Write-Host "  Window closed quickly (exit=$($testProc.ExitCode))" -ForegroundColor Yellow
        Write-Host "  Check: is today a weekend or holiday?" -ForegroundColor Yellow
        Write-Host "  Check: does run_main.ps1 exist at $SCRIPT_DIR?" -ForegroundColor Yellow

        $logFile = Join-Path $SCRIPT_DIR "data\scheduler_main_$((Get-Date).ToString('yyyy-MM-dd')).log"
        if (Test-Path $logFile) {
            Write-Host ""
            Write-Host "  Last 10 lines of log:" -ForegroundColor Cyan
            Get-Content $logFile -Tail 10 | ForEach-Object {
                Write-Host "    $_" -ForegroundColor Gray
            }
        }
    }
}
catch {
    Write-Host "  Test error: $_" -ForegroundColor Red
}

# --- Final summary ---
Write-Host ""
Write-Host "=== Setup Complete ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "Files required in $SCRIPT_DIR :" -ForegroundColor White
Write-Host "  launch_main.vbs      launches run_main.ps1 in visible CMD window" -ForegroundColor Green
Write-Host "  launch_journal.vbs   launches run_journal.ps1 in visible CMD window" -ForegroundColor Green
Write-Host "  run_main.ps1         holiday check + starts main.py + kills at 15:30" -ForegroundColor Green
Write-Host "  run_journal.ps1      holiday check + runs decision_journal.py + exits" -ForegroundColor Green
Write-Host ""
Write-Host "Daily schedule:" -ForegroundColor White
Write-Host "  09:00  wscript launch_main.vbs    -> CMD window -> run_main.ps1 -> main.py" -ForegroundColor Green
Write-Host "  15:30  run_main.ps1 kills main.py automatically" -ForegroundColor Green
Write-Host "  16:00  wscript launch_journal.vbs -> CMD window -> run_journal.ps1 -> exits" -ForegroundColor Green
Write-Host ""
Write-Host "Useful commands:" -ForegroundColor White
Write-Host "  Start-ScheduledTask  -TaskName 'NiftyAlgo_MainEngine'      # trigger now" -ForegroundColor Gray
Write-Host "  Start-ScheduledTask  -TaskName 'NiftyAlgo_DecisionJournal' # trigger now" -ForegroundColor Gray
Write-Host "  Disable-ScheduledTask -TaskName 'NiftyAlgo_MainEngine'     # pause" -ForegroundColor Gray
Write-Host "  Enable-ScheduledTask  -TaskName 'NiftyAlgo_MainEngine'     # resume" -ForegroundColor Gray
Write-Host "  Unregister-ScheduledTask -TaskName 'NiftyAlgo_MainEngine' -Confirm:`$false" -ForegroundColor Gray
Write-Host ""
Write-Host "IMPORTANT:" -ForegroundColor Red
Write-Host "  1. Machine must be LOGGED IN at 09:00 for window to appear." -ForegroundColor Red
Write-Host "  2. Refresh Upstox token in env.txt before 09:00 each morning." -ForegroundColor Red
Write-Host "  3. Do NOT lock the screen - use screensaver instead." -ForegroundColor Red