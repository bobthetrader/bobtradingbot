# daily_backtest.ps1 — runs at 9:35am via Windows Task Scheduler
#
# What it does:
#   1. SCPs today's trade data from the server
#   2. Runs the backtester locally (your PC does the heavy lifting)
#   3. Backtester commits+pushes backtest_recommendations.json to git
#   4. Server's 9:45am cron (server_daily_pull.sh) picks it up
#
# SETUP — one time only:
#   1. Set SERVER_IP below to your Hetzner server IP
#   2. Set SSH_KEY to your private key path (the one you use to SSH to the server)
#   3. Register this script as a Windows Task:
#      Open Task Scheduler -> Create Basic Task -> Daily -> 09:35
#      Action: Start a program
#        Program:    powershell.exe
#        Arguments:  -NonInteractive -ExecutionPolicy Bypass -File "D:\Tradingbot\scripts\daily_backtest.ps1"
#   4. On the server, add the two cron jobs in server_daily_pull.sh (see that file)

$SERVER_IP  = "YOUR_SERVER_IP"        # <-- fill in your Hetzner server IP
$SSH_KEY    = "C:\Users\rober\.ssh\id_rsa"   # <-- path to your SSH private key
$BOT_DIR    = "D:\Tradingbot"
$BACKUP_DIR = "$BOT_DIR\backtest\data"
$LOG_DIR    = "$BOT_DIR\scripts\logs"
$LOG_FILE   = "$LOG_DIR\backtest_$(Get-Date -Format 'yyyy-MM-dd').log"

New-Item -ItemType Directory -Force -Path $LOG_DIR | Out-Null

function Log($msg) {
    $line = "$(Get-Date -Format 'HH:mm:ss')  $msg"
    Write-Host $line
    Add-Content -Path $LOG_FILE -Value $line
}

Log "=== Daily backtest starting ==="

# ── Step 1: Pull latest trade data from server ────────────────────────────────
Log "Pulling scalper_trades.jsonl from server..."
$scpResult = & scp -i $SSH_KEY -o StrictHostKeyChecking=no `
    "botuser@${SERVER_IP}:/home/botuser/backup/scalper_trades.jsonl" `
    "$BACKUP_DIR\scalper_trades.jsonl" 2>&1

if ($LASTEXITCODE -ne 0) {
    Log "ERROR: scp failed — $scpResult"
    Log "Aborting. Check server IP, SSH key, and that server 9:30am cron ran."
    exit 1
}

$lines = (Get-Content "$BACKUP_DIR\scalper_trades.jsonl" | Measure-Object -Line).Lines
Log "Downloaded $lines trade records"

# ── Step 2: Run backtester ────────────────────────────────────────────────────
Log "Running backtester..."
$env:PYTHONIOENCODING = "utf-8"
$btResult = & py "$BOT_DIR\backtest\scalper_backtest.py" --no-sync 2>&1
$btResult | ForEach-Object { Log "  $_" }

if ($LASTEXITCODE -ne 0) {
    Log "ERROR: backtester exited with code $LASTEXITCODE"
    exit 1
}

Log "=== Daily backtest complete ==="
Log "Server will pick up recommendations at 9:45am via git pull"
