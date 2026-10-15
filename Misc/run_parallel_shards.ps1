# Launch one independent OS process per per_day shard (FORCE_LOTS=1).
# Usage: powershell -File Misc\run_parallel_shards.ps1 -OutDir Misc\replay_runs\fresh_fix1
param(
    [Parameter(Mandatory = $true)][string]$OutDir,
    [int]$ForceLots = 1
)

$Root = Split-Path -Parent $PSScriptRoot
if (-not $Root) { $Root = "C:\Users\Administrator\Desktop\algo-trading" }
Set-Location $Root

$OutPath = Join-Path $Root $OutDir
New-Item -ItemType Directory -Force -Path $OutPath | Out-Null

$Dates = @(
    "2026-09-08","2026-09-09","2026-09-10","2026-09-11","2026-09-15",
    "2026-09-16","2026-09-17","2026-09-18","2026-09-21","2026-09-22",
    "2026-09-23","2026-09-24","2026-09-25","2026-09-28","2026-09-29",
    "2026-09-30","2026-10-01","2026-10-05","2026-10-06","2026-10-07",
    "2026-10-08"
)

$procs = @()
foreach ($d in $Dates) {
    $db = Join-Path $Root "data\per_day\nifty_algo_$d.db"
    if (-not (Test-Path $db)) {
        Write-Host "SKIP missing shard $d"
        continue
    }
    $out = Join-Path $OutPath "$d.txt"
    $err = Join-Path $OutPath "$d`_err.txt"
    $csv = Join-Path $OutPath "$d.csv"
    $args = @(
        "-u", "backtest_engine.py",
        "--db", $db,
        "--from", $d, "--to", $d,
        "--force-lots", "$ForceLots",
        "--trade-report", "off",
        "--csv", $csv,
        "--allow-unfaithful"
    )
    Write-Host "START $d -> $out"
    $p = Start-Process -FilePath "python" -ArgumentList $args `
        -WorkingDirectory $Root `
        -RedirectStandardOutput $out `
        -RedirectStandardError $err `
        -PassThru -WindowStyle Hidden
    $procs += [PSCustomObject]@{ Date = $d; Pid = $p.Id; Proc = $p }
}

Write-Host ("Launched {0} day processes" -f $procs.Count)
$procs | ForEach-Object { "{0} pid={1}" -f $_.Date, $_.Pid }
# Wait for all
foreach ($x in $procs) {
    Write-Host ("waiting {0} ..." -f $x.Date)
    $x.Proc.WaitForExit()
    Write-Host ("DONE {0} exit={1}" -f $x.Date, $x.Proc.ExitCode)
}
Write-Host "ALL DAYS COMPLETE -> $OutPath"
