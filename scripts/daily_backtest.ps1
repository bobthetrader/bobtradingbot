# daily_backtest.ps1 - runs at 9:35am via Windows Task Scheduler
#
# 2026-07-15: switched from the retired scalper backtest to the long/short
# JOURNAL analysis. Pulls trade_events_paper.jsonl via the restricted-key
# "journal" command and runs backtest/journal_analysis.py. REPORT ONLY -
# no recommendations push-back (that fed the retired scalper AI loop).
#
# Server prerequisites (one-time, already installed if this works):
#   - 09:30 cron extracts trade_events_paper.jsonl to /home/botuser/backup/
#   - /home/botuser/bot_auto.sh has a "journal" case streaming that file
#
# SECURITY: uses id_ed25519_botauto - restricted key, fixed commands only.

$SERVER      = "root@178.105.159.157"
$SSH_KEY     = "C:\Users\rober\.ssh\id_ed25519_botauto"
$BOT_DIR     = "D:\Tradingbot"
$DATA_DIR    = "$BOT_DIR\backtest\data"
$LOG_DIR     = "$BOT_DIR\scripts\logs"
$LOG_FILE    = "$LOG_DIR\backtest_$(Get-Date -Format 'yyyy-MM-dd').log"

New-Item -ItemType Directory -Force -Path $LOG_DIR  | Out-Null
New-Item -ItemType Directory -Force -Path $DATA_DIR | Out-Null

function Log($msg) {
    $line = "$(Get-Date -Format 'HH:mm:ss')  $msg"
    Write-Host $line
    Add-Content -Path $LOG_FILE -Value $line -Encoding UTF8
}

Log "=== Daily journal analysis starting ==="

# Step 1: Pull the main-bot trade journal from the server via restricted key
Log "Pulling trade_events_paper.jsonl from server..."
$dest   = "$DATA_DIR\trade_events_paper.jsonl"
$sshOut = & ssh -i $SSH_KEY -o StrictHostKeyChecking=no $SERVER "journal" 2>&1

if ($LASTEXITCODE -ne 0) {
    Log "ERROR: ssh journal failed - $sshOut"
    Log "Check server IP, SSH key, gatekeeper 'journal' case, and the 9:30 cron."
    exit 1
}

$sshOut | Out-File -FilePath $dest -Encoding UTF8
$lines = (Get-Content $dest | Measure-Object -Line).Lines
Log "Downloaded $lines journal records"

# Step 2: Run the journal analyzer (report only)
Log "Running journal analysis..."
$env:PYTHONIOENCODING = "utf-8"
$btOut = & py "$BOT_DIR\backtest\journal_analysis.py" 2>&1
$btOut | ForEach-Object { Log "  $_" }

if ($LASTEXITCODE -ne 0) {
    Log "ERROR: analyzer exited with code $LASTEXITCODE"
    exit 1
}

Log "Report: $BOT_DIR\backtest\journal_report.html"
Log "=== Done ==="
