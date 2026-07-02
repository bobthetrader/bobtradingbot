# restore_failover.ps1 - FAILOVER: stand up an identical bot locally from the
# latest server DR snapshot. Run this ONLY when the server is down.
#
# It OVERWRITES the local bot's Docker volume + Postgres with the server's last
# snapshot, then rebuilds from current code and starts the containers.
#
#   powershell -ExecutionPolicy Bypass -File D:\Tradingbot\scripts\restore_failover.ps1
#
# State is restored as of the last nightly snapshot - trades the server made
# between the snapshot and its death are not included (fine for paper; in live
# mode the exchange remains the source of truth for balance/holdings).

$ErrorActionPreference = "Stop"
$ROOT = "D:\Tradingbot"
$DR   = "$ROOT\backups\dr\latest.tar.gz"
$VOL  = "tradingbot_tradingbot_data"       # local compose project = tradingbot
$PG   = "tradingbot_postgres"

if (-not (Test-Path $DR)) {
    Write-Error "No DR snapshot at $DR - run pull_dr_backup.ps1 first."
    exit 1
}

Write-Host "WARNING: this OVERWRITES the local bot state with the server snapshot." -ForegroundColor Yellow
$ans = Read-Host "Type RESTORE to proceed"
if ($ans -ne "RESTORE") { Write-Host "Aborted."; exit 0 }

Set-Location $ROOT
Write-Host "Pulling latest code..."
git pull

# Unpack the DR bundle to a staging dir
$stage = Join-Path $env:TEMP "dr_restore"
Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $stage | Out-Null
tar xzf $DR -C $stage
if (-not (Test-Path "$stage\tradingbot_data.tar.gz")) {
    Write-Error "Snapshot is missing tradingbot_data.tar.gz - aborting."
    exit 1
}

# Stop the trading bot (leave FTSE bot alone; ensure the volume + postgres exist)
Write-Host "Stopping local tradingbot (FTSE bot left running)..."
docker compose stop tradingbot 2>$null
docker volume create $VOL | Out-Null

# 1. Restore the data volume (wipe then extract)
Write-Host "Restoring data volume $VOL..."
$rm = "rm -rf /data/* /data/..?* /data/.[!.]* 2>/dev/null; tar xzf /backup/tradingbot_data.tar.gz -C /data"
docker run --rm -v "${VOL}:/data" -v "${stage}:/backup:ro" alpine sh -c $rm

# 2. Restore Postgres if the snapshot has a dump
if (Test-Path "$stage\postgres.sql.gz") {
    Write-Host "Starting Postgres..."
    docker compose up -d postgres
    for ($i = 0; $i -lt 30; $i++) {
        docker exec $PG pg_isready -U tradingbot -d tradingbot 2>$null | Out-Null
        if ($?) { break }
        Start-Sleep 2
    }
    Write-Host "Restoring Postgres dump..."
    docker cp "$stage\postgres.sql.gz" "${PG}:/tmp/dr.sql.gz"
    docker exec $PG sh -c "gunzip -c /tmp/dr.sql.gz | psql -U tradingbot -d tradingbot -q"
    if (-not $?) { Write-Host "WARN: Postgres restore reported errors (bot still runs without full DB)." -ForegroundColor Yellow }
} else {
    Write-Host "No Postgres dump in snapshot - skipping DB restore."
}

# 3. Rebuild + start everything from current code
Write-Host "Rebuilding and starting containers..."
docker compose up --build -d

Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
Write-Host ""
Write-Host "=== Failover complete - local bot is running the server's last snapshot ===" -ForegroundColor Green
Write-Host "Dashboard: http://localhost:8080"
