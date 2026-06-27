# daily_backtest.ps1 - runs at 9:35am via Windows Task Scheduler
#
# SETUP - one time only:
#   1. Register in Task Scheduler -> Create Basic Task -> Daily -> 09:35
#      Program:   powershell.exe
#      Arguments: -NonInteractive -ExecutionPolicy Bypass -File "D:\Tradingbot\scripts\daily_backtest.ps1"
#   2. On the server, two cron jobs must be active (already installed):
#      30 9 * * *  extract scalper_trades.jsonl from Docker volume to /home/botuser/backup/
#      45 9 * * *  server_daily_pull.sh - git pull + copy recommendations to Docker volume
#
# SECURITY: uses id_ed25519_botauto - a restricted key that can ONLY run the two
# automation commands (extract/pull). Cannot be used for general server access.
# Keep id_ed25519 (your admin key) with a passphrase for manual SSH sessions.

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

Log "=== Daily backtest starting ==="

# Step 1: Pull latest trade data from server via restricted key
Log "Pulling scalper_trades.jsonl from server..."
$dest   = "$DATA_DIR\scalper_trades.jsonl"
$sshOut = & ssh -i $SSH_KEY -o StrictHostKeyChecking=no $SERVER "extract" 2>&1

if ($LASTEXITCODE -ne 0) {
    Log "ERROR: ssh extract failed - $sshOut"
    Log "Check server IP, SSH key, and that the 9:30am server cron ran."
    exit 1
}

$sshOut | Out-File -FilePath $dest -Encoding UTF8
$lines = (Get-Content $dest | Measure-Object -Line).Lines
Log "Downloaded $lines trade records"

# Step 2: Run backtester
Log "Running backtester..."
$env:PYTHONIOENCODING = "utf-8"
$btOut = & py "$BOT_DIR\backtest\scalper_backtest.py" --no-sync 2>&1
$btOut | ForEach-Object { Log "  $_" }

if ($LASTEXITCODE -ne 0) {
    Log "ERROR: backtester exited with code $LASTEXITCODE"
    exit 1
}

# Step 3: Trigger server pull via restricted key (runs git pull + copies recs to volume)
Log "Triggering server recommendations pull..."
$pullOut = & ssh -i $SSH_KEY -o StrictHostKeyChecking=no $SERVER "pull" 2>&1
$pullOut | ForEach-Object { Log "  $_" }

Log "=== Done ==="
