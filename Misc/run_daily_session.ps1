# Daily live-session launcher (foreground).
# Sequence: holiday/weekend skip -> upstox_token.py -> main.py
# Keep this window open to watch token refresh, the cycle loop, and EOD.
#
# Do not start this while another copy is already running.

$ErrorActionPreference = "Continue"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$WorkDir = Split-Path -Parent $ScriptDir
Set-Location -LiteralPath $WorkDir

$Py = "C:\Users\Administrator\AppData\Local\Programs\Python\Python313\python.exe"
if (-not (Test-Path -LiteralPath $Py)) {
    $Py = "python"
}

$Lock = Join-Path $env:TEMP "nifty_algo_daily_session.lock"
function Unlock {
    if (Test-Path -LiteralPath $Lock) {
        Remove-Item -LiteralPath $Lock -Force -ErrorAction SilentlyContinue
    }
}

if (Test-Path -LiteralPath $Lock) {
    Write-Host "Another daily session launcher is already running."
    Write-Host "Close that window or delete: $Lock"
    Write-Host ""
    Read-Host "Press Enter to close"
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
        $code = 0
    }
    elseif ($holidayCode -ne 0) {
        Write-Host "Holiday check failed (exit $holidayCode) - aborting."
        $code = 1
    }
    else {
        Write-Host "[1/2] Refreshing Upstox access token..."
        & $Py "upstox_token.py"
        if ($LASTEXITCODE -ne 0) {
            Write-Host "upstox_token.py FAILED (exit $LASTEXITCODE) - not starting main.py"
            $code = $LASTEXITCODE
        }
        else {
            Write-Host ""
            Write-Host "[2/2] Starting main.py..."
            & $Py "main.py"
            $code = $LASTEXITCODE
            Write-Host ""
            Write-Host "main.py exited with code $code"
        }
    }
}
finally {
    Unlock
}

Write-Host ""
Write-Host "Done. This window stays open so you can read the log."
Read-Host "Press Enter to close"
exit $code
