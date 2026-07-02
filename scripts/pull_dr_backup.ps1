# pull_dr_backup.ps1 - pull the latest DR snapshot from the server to this PC.
# Does NOT start anything: it only refreshes the local failover snapshot.
#
# Run nightly via Task Scheduler AFTER the server's 09:20 backup cron, e.g. 09:50:
#   Program:   powershell.exe
#   Arguments: -NonInteractive -ExecutionPolicy Bypass -File "D:\Tradingbot\scripts\pull_dr_backup.ps1"
#
# Uses the restricted automation key + a new gatekeeper "backup" command that
# base64-streams /home/botuser/backups/dr/latest.tar.gz (base64 = binary-safe
# over SSH stdout). See scripts/DR_SETUP.md for the gatekeeper snippet.

$ErrorActionPreference = "Stop"
$SERVER = "root@178.105.159.157"
$KEY    = "C:\Users\rober\.ssh\id_ed25519_botauto"
$DEST   = "D:\Tradingbot\backups\dr"
$LOGDIR = "D:\Tradingbot\scripts\logs"

New-Item -ItemType Directory -Force -Path $DEST, $LOGDIR | Out-Null
$log = Join-Path $LOGDIR "dr_pull_$(Get-Date -Format 'yyyy-MM-dd').log"
function Log($m) { $l = "$(Get-Date -Format 'HH:mm:ss')  $m"; Write-Host $l; Add-Content $log $l -Encoding UTF8 }

Log "=== DR pull starting ==="
$b64 = & ssh -i $KEY -o StrictHostKeyChecking=no $SERVER "backup"
if ($LASTEXITCODE -ne 0 -or -not $b64) {
    Log "ERROR: ssh 'backup' failed (exit $LASTEXITCODE). Is the gatekeeper 'backup' command installed and did the 09:20 server cron run?"
    exit 1
}

try {
    $bytes = [Convert]::FromBase64String(($b64 -join ""))
} catch {
    Log "ERROR: could not decode base64 stream - $($_.Exception.Message)"
    exit 1
}

# Write atomically: temp then move, so a failed pull never clobbers a good snapshot
$tmp   = Join-Path $DEST "latest.tar.gz.tmp"
$final = Join-Path $DEST "latest.tar.gz"
[IO.File]::WriteAllBytes($tmp, $bytes)
Move-Item -Force $tmp $final
$mb = [math]::Round($bytes.Length / 1048576, 1)
Log "DR snapshot pulled: $final ($mb MB)"
Log "=== Done ==="
