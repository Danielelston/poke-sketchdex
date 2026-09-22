#!/bin/bash
# Nightly SQLite backup for PokeSketchDex — sqlite-safe copy + rsync to unraid.
set -euo pipefail
DB=/opt/pokesketch/data/pokesketch.db
DEST_LOCAL=/opt/pokesketch/data/backups
DEST_REMOTE=root@192.168.51.15:/mnt/user/backups/pokesketchdex/
STAMP=$(date +%Y%m%d-%H%M%S)

mkdir -p "$DEST_LOCAL"

# Use sqlite3 .backup for a consistent snapshot even while the bot is writing.
sqlite3 "$DB" ".backup '$DEST_LOCAL/pokesketch-$STAMP.db'"

# Keep the last 14 local snapshots.
ls -1t "$DEST_LOCAL"/pokesketch-*.db 2>/dev/null | tail -n +15 | xargs -r rm -f

# Push tonight's snapshot to unraid (offsite from the LXC).
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -i /root/.ssh/id_ed25519 root@192.168.51.15 'mkdir -p /mnt/user/backups/pokesketchdex' 2>/dev/null || true
rsync -a -e 'ssh -i /root/.ssh/id_ed25519 -o StrictHostKeyChecking=accept-new' "$DEST_LOCAL/pokesketch-$STAMP.db" "$DEST_REMOTE"

echo "[$(date -Is)] Backup complete: pokesketch-$STAMP.db"
