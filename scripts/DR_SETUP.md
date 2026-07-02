# Disaster Recovery — cold standby on the local PC

Goal: keep a fresh full-state snapshot of the Hetzner server on your PC, **not
running**, and stand up an identical bot locally with one command if the server
dies.

**What's snapshotted:** the live Docker data volume (`bot_status`, paper balance,
positions, AI params, journals, `scalper_observations`) + the Postgres DB.
**Not snapshotted (already on the PC):** code (git-mirrored) and `.env`.

Snapshot freshness = last nightly run. Trades between the last snapshot and a
server death are not recovered (fine for paper; in live mode the exchange stays
the source of truth for balance/holdings).

---

## 1. Server — nightly snapshot (run once to install)

```bash
# from your PC, with the admin key:
scp scripts/server_backup.sh root@178.105.159.157:/home/botuser/server_backup.sh
ssh root@178.105.159.157 "chmod +x /home/botuser/server_backup.sh && mkdir -p /home/botuser/backups/dr"
```

Add the cron (server): `crontab -e` →
```
# 09:20 — full DR snapshot (before the 09:30 extract / 09:45 pull)
20 9 * * * /home/botuser/server_backup.sh >> /home/botuser/backups/dr/backup.log 2>&1
```

Test it now:
```bash
ssh root@178.105.159.157 "/home/botuser/server_backup.sh && ls -lh /home/botuser/backups/dr/"
```

## 2. Server — add the `backup` gatekeeper command

The restricted automation key runs `/home/botuser/bot_auto.sh` via
`command="..."` in `authorized_keys`. Add a `backup` case so the PC can pull the
snapshot (base64 = binary-safe over SSH). Edit `/home/botuser/bot_auto.sh`,
inside its `case "$SSH_ORIGINAL_COMMAND" in` block:

```bash
  backup)
      exec base64 -w0 /home/botuser/backups/dr/latest.tar.gz
      ;;
```

(Leave the existing `extract)` and `pull)` cases as-is.) The key still can't run
anything else.

## 3. Your PC — nightly pull (Task Scheduler)

Create Basic Task → Daily → **09:50** (after the 09:20 server snapshot):
- Program: `powershell.exe`
- Arguments: `-NonInteractive -ExecutionPolicy Bypass -File "D:\Tradingbot\scripts\pull_dr_backup.ps1"`

Test the pull + verify contents:
```powershell
powershell -ExecutionPolicy Bypass -File D:\Tradingbot\scripts\pull_dr_backup.ps1
tar tzf D:\Tradingbot\backups\dr\latest.tar.gz   # expect tradingbot_data.tar.gz (+ postgres.sql.gz)
```

## 4. Failover (server is down)

```powershell
powershell -ExecutionPolicy Bypass -File D:\Tradingbot\scripts\restore_failover.ps1
```
Overwrites local state with the snapshot, rebuilds from current code, starts the
bot. Dashboard: http://localhost:8080. Prompts before overwriting.

---

## 5. Flip local to cold standby (do this AFTER a successful test restore)

Today the local bot runs 24/7 as a divergent paper instance (doubles API load).
Once DR is proven, make it dormant so it only runs on failover.

Change the existing **TradingBotAutoUpdate** task (09:30) command from:
```
cd D:\tradingbot; git pull; docker compose up --build -d
```
to (build everything so images are ready, but only run the local-only FTSE bot):
```
cd D:\tradingbot; git pull; docker compose build; docker compose up -d ftsebot; docker compose stop tradingbot postgres
```

Then stop the trading bot now:
```powershell
cd D:\Tradingbot; docker compose stop tradingbot postgres
```

Notes:
- The FTSE bot (`ftsebot_local`, port 8081) is local-only and keeps running.
- `daily_backtest.ps1` (09:35) reads from the **server** volume over SSH, so it
  is unaffected by the local tradingbot being stopped.
- To go back to a hot local instance, revert the task and `docker compose up -d`.
