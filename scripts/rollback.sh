#!/bin/bash
# rollback.sh - Restore code and optionally data to a pre-deploy snapshot
# Usage:
#   bash scripts/rollback.sh                    # list available snapshots
#   bash scripts/rollback.sh pre-deploy-<ts>    # rollback code only
#   bash scripts/rollback.sh pre-deploy-<ts> --data  # rollback code + data

set -e

REPO_DIR="/home/botuser/bobtradingbot"
BACKUP_DIR="/home/botuser/backups"
VOLUME_NAME="bobtradingbot_tradingbot_data"

cd "$REPO_DIR"

if [ -z "$1" ]; then
    echo "Available git tags (pre-deploy snapshots):"
    git tag --sort=-creatordate | grep "^pre-deploy-" | head -10
    echo ""
    echo "Available data backups:"
    ls -lht "$BACKUP_DIR"/data-*.tar.gz 2>/dev/null | head -10
    echo ""
    echo "Usage: bash scripts/rollback.sh <tag> [--data]"
    exit 0
fi

TAG="$1"
RESTORE_DATA="$2"

# Verify tag exists
git rev-parse "$TAG" >/dev/null 2>&1 || { echo "ERROR: tag '$TAG' not found"; exit 1; }

echo "=== Rollback to $TAG ==="

# 1. Stop containers
echo "Stopping containers..."
docker compose stop tradingbot

# 2. Restore code
echo "Restoring code to $TAG..."
git checkout "$TAG"
echo "Code restored"

# 3. Optionally restore data volume
if [ "$RESTORE_DATA" = "--data" ]; then
    # Find matching backup by timestamp in tag name
    TS=$(echo "$TAG" | sed 's/pre-deploy-//')
    BACKUP_FILE="$BACKUP_DIR/data-$TS.tar.gz"
    if [ -f "$BACKUP_FILE" ]; then
        echo "Restoring data volume from $BACKUP_FILE..."
        docker run --rm \
            -v "${VOLUME_NAME}:/data" \
            -v "$BACKUP_DIR:/backup:ro" \
            alpine sh -c "rm -rf /data/* && tar xzf /backup/data-$TS.tar.gz -C /data"
        echo "Data volume restored"
    else
        echo "WARNING: No matching data backup found for $TS"
        echo "Available backups:"
        ls "$BACKUP_DIR"/data-*.tar.gz 2>/dev/null | tail -5
    fi
fi

# 4. Rebuild and restart
echo "Rebuilding and restarting..."
docker compose up --build -d

echo ""
echo "=== Rollback complete ==="
[ "$RESTORE_DATA" = "--data" ] && echo "  Code + data restored to: $TAG" || echo "  Code restored to: $TAG (data unchanged)"
echo "  To return to latest: git checkout main && docker compose up --build -d"
