# Daily live-session launcher (foreground).
# Sequence: holiday/weekend skip -> upstox_token.py -> main.py
#           -> split_db_per_day.py -> robocopy data -> GDrive
#           -> 10 min review -> window closes (cmd /c)
# Keep this window open to watch token refresh, the cycle loop, and EOD.
#
# Scheduler note: do NOT block forever on Read-Host after exit — a leftover
# Running task + MultipleInstances=IgnoreNew skips the next morning trigger.

$ErrorActionPreference = "Continue"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$WorkDir = Split-Path -Parent $ScriptDir
Set-Location -LiteralPath $WorkDir

$Py = "C:\Users\Administrator\AppData\Local\Programs\Python\Python313\python.exe"
if (-not (Test-Path -LiteralPath $Py)) {
    $Py = "python"
}

$Lock = Join-Path $env:TEMP "nifty_algo_daily_session.lock"
$LaunchLog = Join-Path $WorkDir "logs\scheduler_launch.log"
New-Item -ItemType Directory -Force -Path (Join-Path $WorkDir "logs") | Out-Null

function Write-LaunchLog([string]$msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $msg"
    Write-Host $line
    # Shared append + retries: Task Scheduler / a second launcher / an
    # editor can briefly lock scheduler_launch.log (Add-Content fails hard).
    for ($i = 0; $i -lt 5; $i++) {
        try {
            $fs = [System.IO.File]::Open(
                $LaunchLog,
                [System.IO.FileMode]::Append,
                [System.IO.FileAccess]::Write,
                [System.IO.FileShare]::ReadWrite
            )
            try {
                $sw = New-Object System.IO.StreamWriter($fs, [System.Text.Encoding]::UTF8)
                try { $sw.WriteLine($line) }
                finally { $sw.Dispose() }
            }
            finally { $fs.Dispose() }
            return
        }
        catch {
            Start-Sleep -Milliseconds (100 * ($i + 1))
        }
    }
}

function Unlock {
    if (Test-Path -LiteralPath $Lock) {
        Remove-Item -LiteralPath $Lock -Force -ErrorAction SilentlyContinue
    }
}

function Test-LockAlive {
    if (-not (Test-Path -LiteralPath $Lock)) { return $false }
    try {
        $raw = Get-Content -LiteralPath $Lock -Raw -ErrorAction Stop
        if ($raw -match 'pid=(\d+)') {
            $pidLock = [int]$Matches[1]
            $proc = Get-Process -Id $pidLock -ErrorAction SilentlyContinue
            if ($null -ne $proc) { return $true }
        }
    } catch {}
    # Stale lock (process gone) — clear it so the morning run can proceed.
    Write-LaunchLog "Removing stale lock: $Lock"
    Unlock
    return $false
}

Write-LaunchLog "Launcher start (pid=$PID user=$env:USERNAME session=$($env:SESSIONNAME))"

if (Test-LockAlive) {
    Write-Host "Another daily session launcher is already running."
    Write-Host "Close that window or delete: $Lock"
    Write-LaunchLog "ABORT: live lock held by another process"
    Write-Host ""
    # Timed pause so a scheduled leftover does not block forever.
    Start-Sleep -Seconds 30
    exit 0
}
Set-Content -LiteralPath $Lock -Value "$(Get-Date -Format o) pid=$PID" -Encoding ascii

$code = 0
try {
    Write-Host "===== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ====="
    Write-Host "Working dir: $WorkDir"
    Write-Host "User:       $env:USERNAME"
    Write-Host "Python:     $Py"
    Write-Host "Leave this window open to monitor the session."
    Write-Host ""

    $pyHoliday = 'from datetime import date; from core import ExpiryCalendar; import sys; d=date.today(); sys.exit(2 if ExpiryCalendar.is_holiday(d) else 0)'
    & $Py -c $pyHoliday
    $holidayCode = $LASTEXITCODE
    if ($holidayCode -eq 2) {
        Write-Host "Weekend or NSE holiday (nse_holidays.json) - not starting the engine."
        Write-LaunchLog "SKIP holiday/weekend"
        $code = 0
    }
    elseif ($holidayCode -ne 0) {
        Write-Host "Holiday check failed (exit $holidayCode) - aborting."
        Write-LaunchLog "ABORT holiday check exit=$holidayCode"
        $code = 1
    }
    else {
            Write-Host "[1/4] Refreshing Upstox access token..."
        & $Py "upstox_token.py"
        if ($LASTEXITCODE -ne 0) {
            Write-Host "upstox_token.py FAILED (exit $LASTEXITCODE) - not starting main.py"
            Write-LaunchLog "ABORT upstox_token exit=$LASTEXITCODE"
            $code = $LASTEXITCODE
        }
        else {
            Write-Host ""
            Write-Host "[2/4] Starting main.py..."
            Write-LaunchLog "Starting main.py"
            & $Py "main.py"
            $code = $LASTEXITCODE
            Write-Host ""
            Write-Host "main.py exited with code $code"
            Write-LaunchLog "main.py exited code=$code"

            # Always split after a live session attempt so per_day shards
            # pick up today's tape (read-only; refuses if writer lock held).
            Write-Host ""
            # Only today's shard after a live session — full-library fingerprint
            # of ~40 days was taking minutes (worse after Drive robocopy).
            $today = Get-Date -Format "yyyy-MM-dd"
            Write-Host "[3/4] Splitting today's shard (split_db_per_day.py --dates $today)..."
            Write-LaunchLog "Starting split_db_per_day.py --dates $today"
            & $Py "split_db_per_day.py" "--dates" $today
            $splitCode = $LASTEXITCODE
            if ($splitCode -ne 0) {
                Write-Host "split_db_per_day.py FAILED (exit $splitCode) — live db was not written; fix shards later"
                Write-LaunchLog "split_db_per_day FAILED exit=$splitCode"
                if ($code -eq 0) { $code = $splitCode }
            }
            else {
                Write-Host "split_db_per_day.py finished"
                Write-LaunchLog "split_db_per_day finished"
            }

            # Post-session backup to Google Drive (engine is closed; skip WAL/SHM).
            Write-Host ""
            Write-Host "[4/4] Syncing data\ to Google Drive..."
            $gSrc = Join-Path $WorkDir "data"
            $gDst = "G:\My Drive\algo-trading-data"
            if (-not (Test-Path -LiteralPath "G:\My Drive")) {
                Write-Host "G:\My Drive not available — skipping Drive sync"
                Write-LaunchLog "SKIP gdrive sync (G:\My Drive missing)"
            }
            else {
                New-Item -ItemType Directory -Force -Path $gDst | Out-Null
                Write-LaunchLog "Starting robocopy data -> $gDst"
                & robocopy $gSrc $gDst /MIR /Z /R:2 /W:5 `
                    /XD __pycache__ `
                    /XF *.db-wal *.db-shm *.split_snap.*
                # robocopy: 0-7 = success/partial; >=8 = failure
                $robo = $LASTEXITCODE
                if ($robo -ge 8) {
                    Write-Host "robocopy FAILED (exit $robo) — Drive sync incomplete"
                    Write-LaunchLog "robocopy FAILED exit=$robo"
                    if ($code -eq 0) { $code = $robo }
                }
                else {
                    Write-Host "Drive sync finished (robocopy exit $robo)"
                    Write-LaunchLog "robocopy finished exit=$robo"
                }
            }
        }
    }
}
finally {
    Unlock
}

Write-Host ""
Write-Host "Done. Window stays open 10 minutes for review, then closes."
Write-LaunchLog "Launcher end code=$code"
# Timed hold: keeps output readable without leaving the Scheduled Task Running overnight.
Start-Sleep -Seconds 600
exit $code
