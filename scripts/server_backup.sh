#!/bin/bash
# server_backup.sh — nightly full-state DR snapshot for failover to the local PC.
#
# Bundles the live Docker data volume + a Postgres dump into ONE archive that the
# local PC pulls (pull_dr_backup.ps1) and can restore to stand up an identical
# instance if the server dies (restore_failover.ps1). Code + .env are NOT included
# (code is git-mirrored; .env already lives on the PC).
#
# Install (on the Hetzner server), run once:
#   scp scripts/server_backup.sh botuser@SERVER:/home/botuser/server_backup.sh
#   ssh botuser@SERVER "chmod +x /home/botuser/server_backup.sh && mkdir -p /home/botuser/backups/dr"
#   ssh botuser@SERVER "crontab -e"   # add:
#     # 09:20 — full DR snapshot (before the 09:30 extract / 09:45 pull)
#     20 9 * * * /home/botuser/server_backup.sh >> /home/botuser/backups/dr/backup.log 2>&1
set -e

BACKUP_DIR="/home/botuser/backups/dr"
VOLUME_NAME="bobtradingbot_tradingbot_data"   # server compose project = bobtradingbot
PG_CONTAINER="tradingbot_postgres"
PG_USER="tradingbot"
PG_DB="tradingbot"
KEEP=5
TS=$(date +%Y%m%d-%H%M%S)

mkdir -p "$BACKUP_DIR"
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

# 1. Live data volume (bot_status, balance, positions, ai params, journals, observations)
echo "[$(date '+%F %T')] tarring volume $VOLUME_NAME"
docker run --rm -v "${VOLUME_NAME}:/data:ro" -v "$STAGE:/out" \
    alpine tar czf /out/tradingbot_data.tar.gz -C /data .

# 2. Postgres dump (best-effort — the bot runs without it; --clean makes restore idempotent)
if docker ps --format '{{.Names}}' | grep -q "^${PG_CONTAINER}$"; then
    echo "[$(date '+%F %T')] pg_dump $PG_DB"
    docker exec "$PG_CONTAINER" pg_dump -U "$PG_USER" --clean --if-exists "$PG_DB" \
        | gzip > "$STAGE/postgres.sql.gz" \
        || echo "WARN: pg_dump failed — snapshot will have no DB"
else
    echo "WARN: $PG_CONTAINER not running — snapshot will have no DB"
fi

# 3. Bundle into one archive + update the 'latest' pointer the PC pulls
ARCHIVE="$BACKUP_DIR/dr-$TS.tar.gz"
tar czf "$ARCHIVE" -C "$STAGE" .
ln -sf "$ARCHIVE" "$BACKUP_DIR/latest.tar.gz"
echo "[$(date '+%F %T')] DR snapshot: $ARCHIVE ($(du -sh "$ARCHIVE" | cut -f1))"

# 4. Prune old snapshots, keep last N (never touches the symlink target — newest is kept)
ls -t "$BACKUP_DIR"/dr-*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -v
