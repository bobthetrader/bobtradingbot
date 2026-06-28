#!/bin/bash
# deploy.sh - Safe deploy with pre-deploy data backup and git tag
# Usage: bash scripts/deploy.sh
# Run from: /home/botuser/bobtradingbot on the server

set -e

REPO_DIR="/home/botuser/bobtradingbot"
BACKUP_DIR="/home/botuser/backups"
VOLUME_NAME="bobtradingbot_tradingbot_data"
KEEP_BACKUPS=5
TIMESTAMP=$(date +%Y%m%d-%H%M%S)

cd "$REPO_DIR"

echo "=== Deploy started: $TIMESTAMP ==="

# 1. Tag current git state as last known-good before pulling new code
CURRENT_HASH=$(git rev-parse --short HEAD)
TAG_NAME="pre-deploy-$TIMESTAMP"
git tag "$TAG_NAME" 2>/dev/null && echo "Tagged current state: $TAG_NAME ($CURRENT_HASH)" || echo "Git tag skipped"

# 2. Back up Docker data volume
mkdir -p "$BACKUP_DIR"
BACKUP_FILE="$BACKUP_DIR/data-$TIMESTAMP.tar.gz"
echo "Backing up Docker volume $VOLUME_NAME..."
docker run --rm \
    -v "${VOLUME_NAME}:/data:ro" \
    -v "$BACKUP_DIR:/backup" \
    alpine tar czf "/backup/data-$TIMESTAMP.tar.gz" -C /data . 2>/dev/null \
    && echo "Backup saved: $BACKUP_FILE ($(du -sh "$BACKUP_FILE" | cut -f1))" \
    || echo "WARNING: backup failed - continuing anyway"

# 3. Prune old backups, keep last N
echo "Pruning old backups (keeping last $KEEP_BACKUPS)..."
ls -t "$BACKUP_DIR"/data-*.tar.gz 2>/dev/null | tail -n +$((KEEP_BACKUPS + 1)) | xargs -r rm -v

# 4. Pull latest code
echo "Pulling latest code..."
git pull

# 5. Rebuild and restart
echo "Rebuilding and restarting containers..."
docker compose up --build -d

echo ""
echo "=== Deploy complete ==="
echo "  Backup:   $BACKUP_FILE"
echo "  Git tag:  $TAG_NAME"
echo "  Rollback: bash scripts/rollback.sh $TAG_NAME"
