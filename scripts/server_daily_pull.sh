#!/bin/bash
# server_daily_pull.sh — runs at 9:45am on the Hetzner server
#
# Pulls the latest backtest_recommendations.json from git and copies it
# into the Docker data volume so the live bot reads it on its next AI trigger.
#
# SETUP — run this once on the server to install both cron jobs:
#
#   scp scripts/server_daily_pull.sh botuser@YOUR_SERVER_IP:/home/botuser/server_daily_pull.sh
#   ssh botuser@YOUR_SERVER_IP "chmod +x /home/botuser/server_daily_pull.sh"
#   ssh botuser@YOUR_SERVER_IP "crontab -e"
#
# Then add these two lines to crontab:
#
#   # 9:30am — extract trade data so local PC can pull it for backtesting
#   30 9 * * * docker run --rm -v tradingbot_tradingbot_data:/data alpine cat /data/scalper_trades.jsonl > /home/botuser/backup/scalper_trades.jsonl 2>/home/botuser/backup/extract.log
#
#   # 9:45am — pull latest backtest recommendations from git into Docker volume
#   45 9 * * * /home/botuser/server_daily_pull.sh >> /home/botuser/backup/pull.log 2>&1
#
# Also create the backup directory once:
#   ssh botuser@YOUR_SERVER_IP "mkdir -p /home/botuser/backup"

set -e
LOG_PREFIX="[$(date '+%Y-%m-%d %H:%M:%S')]"
REPO="/home/botuser/bobtradingbot"
RECS_FILE="backtest_recommendations.json"
VOLUME="tradingbot_tradingbot_data"

echo "$LOG_PREFIX Starting daily recommendations pull"

# Pull latest code + recommendations from git
cd "$REPO"
git pull --quiet
echo "$LOG_PREFIX git pull complete"

# Copy recommendations into the Docker data volume if the file exists
if [ -f "$REPO/$RECS_FILE" ]; then
    docker run --rm -i \
        -v "${VOLUME}:/data" \
        alpine sh -c "cat > /data/${RECS_FILE}" \
        < "$REPO/$RECS_FILE"
    echo "$LOG_PREFIX Copied $RECS_FILE into Docker volume"
else
    echo "$LOG_PREFIX $RECS_FILE not found in repo yet — skipping copy"
fi

echo "$LOG_PREFIX Done — bot will use new recommendations on next AI trigger"
