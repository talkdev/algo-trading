$Base = "C:\Users\Administrator\Desktop\algo-trading"
$StartDate = [datetime]"2026-09-08"
$EndDate = (Get-Date).Date

$Holidays = @(
    "2026-01-26",
    "2026-03-03",
    "2026-03-26",
    "2026-03-31",
    "2026-04-03",
    "2026-04-14",
    "2026-05-01",
    "2026-05-28",
    "2026-06-26",
    "2026-08-27",
    "2026-09-14",
    "2026-10-02",
    "2026-10-20",
    "2026-11-08",
    "2026-11-10",
    "2026-11-24",
    "2026-12-25"
)

Write-Host ""
Write-Host "=============================================="
Write-Host " NIFTY PARALLEL BACKTEST RUNNER"
Write-Host "=============================================="
Write-Host ""

$Date = $StartDate

while ($Date -le $EndDate) {

    $DateString = $Date.ToString("yyyy-MM-dd")
    $Day = $Date.DayOfWeek

    if (
        $Day -ne "Saturday" -and
        $Day -ne "Sunday" -and
        $Holidays -notcontains $DateString
    ) {

        Write-Host "Starting backtest: $DateString"

        $Command = "cd /d `"$Base`" && python backtest_engine.py --from $DateString --to $DateString"

        Start-Process `
            -FilePath "cmd.exe" `
            -ArgumentList "/k", $Command `
            -WorkingDirectory $Base `
            -WindowStyle Maximized
    }

    $Date = $Date.AddDays(1)
}

Write-Host ""
Write-Host "All valid trading-day backtests launched."
Write-Host ""