# daily_backtest.ps1 - runs daily via Windows Task Scheduler (after the 10:50 DR pull)
#
# 2026-07-16: journal now comes from the LOCAL DR snapshot instead of a new
# restricted-SSH command - the nightly backup already contains the full data
# volume, so no extra server access is needed. Extracts trade_events_paper.jsonl
# from backups\dr\latest.tar.gz and runs backtest/journal_analysis.py.
# REPORT ONLY - no recommendations push-back (that fed the retired scalper AI loop).
#
# Prerequisite: "TradingBot DR Pull" task (10:50) has refreshed backups\dr\latest.tar.gz.
# Schedule this task AFTER it (11:05) or the report runs on yesterday's snapshot.

$BOT_DIR   = "D:\Tradingbot"
$SNAPSHOT  = "$BOT_DIR\backups\dr\latest.tar.gz"
$DATA_DIR  = "$BOT_DIR\backtest\data"
$LOG_DIR   = "$BOT_DIR\scripts\logs"
$LOG_FILE  = "$LOG_DIR\backtest_$(Get-Date -Format 'yyyy-MM-dd').log"
$WORK_DIR  = "$env:TEMP\journal_extract"

New-Item -ItemType Directory -Force -Path $LOG_DIR  | Out-Null
New-Item -ItemType Directory -Force -Path $DATA_DIR | Out-Null

function Log($msg) {
    $line = "$(Get-Date -Format 'HH:mm:ss')  $msg"
    Write-Host $line
    Add-Content -Path $LOG_FILE -Value $line -Encoding UTF8
}

Log "=== Daily journal analysis starting ==="

# Step 1: Extract the trade journal from the local DR snapshot
if (-not (Test-Path $SNAPSHOT)) {
    Log "ERROR: DR snapshot not found at $SNAPSHOT"
    Log "Check the TradingBot DR Pull task (10:50) ran."
    exit 1
}

$ageHours = ((Get-Date) - (Get-Item $SNAPSHOT).LastWriteTime).TotalHours
if ($ageHours -gt 26) {
    Log ("WARNING: snapshot is {0:N1}h old - DR pull may have failed; report will be stale" -f $ageHours)
}

Log ("Extracting trade_events_paper.jsonl from DR snapshot ({0:N1}h old)..." -f $ageHours)
if (Test-Path $WORK_DIR) { Remove-Item -Recurse -Force $WORK_DIR }
New-Item -ItemType Directory -Force -Path $WORK_DIR | Out-Null

tar -xzf $SNAPSHOT -C $WORK_DIR "./tradingbot_data.tar.gz"
if ($LASTEXITCODE -ne 0) { Log "ERROR: failed to unpack DR bundle"; exit 1 }

tar -xzf "$WORK_DIR\tradingbot_data.tar.gz" -C $WORK_DIR "./trade_events_paper.jsonl"
if ($LASTEXITCODE -ne 0) { Log "ERROR: trade_events_paper.jsonl not found in snapshot"; exit 1 }

Copy-Item "$WORK_DIR\trade_events_paper.jsonl" "$DATA_DIR\trade_events_paper.jsonl" -Force
Remove-Item -Recurse -Force $WORK_DIR

$lines = (Get-Content "$DATA_DIR\trade_events_paper.jsonl" | Measure-Object -Line).Lines
Log "Extracted $lines journal records"

# Step 2: Run the journal analyzer (report only)
Log "Running journal analysis..."
$env:PYTHONIOENCODING = "utf-8"
$btOut = & py "$BOT_DIR\backtest\journal_analysis.py" 2>&1
$btOut | ForEach-Object { Log "  $_" }

if ($LASTEXITCODE -ne 0) {
    Log "ERROR: analyzer exited with code $LASTEXITCODE"
    exit 1
}

# Keep a dated copy so view_backtest.bat can list report history
$REPORTS_DIR = "$BOT_DIR\backtest\reports"
New-Item -ItemType Directory -Force -Path $REPORTS_DIR | Out-Null
Copy-Item "$BOT_DIR\backtest\journal_report.html" "$REPORTS_DIR\journal_$(Get-Date -Format 'yyyy-MM-dd').html" -Force

Log "Report: $BOT_DIR\backtest\journal_report.html"
Log "=== Done ==="
