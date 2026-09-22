#!/bin/bash
# Nightly SQLite backup for PokeSketchDex — sqlite-safe copy + rsync to backup host.
set -euo pipefail
DB=/opt/pokesketch/data/pokesketch.db
DEST_LOCAL=/opt/pokesketch/data/backups
DEST_REMOTE=backup-user@backup-host.example:/path/to/backups/
STAMP=$(date +%Y%m%d-%H%M%S)

mkdir -p "$DEST_LOCAL"

# Use sqlite3 .backup for a consistent snapshot even while the bot is writing.
sqlite3 "$DB" ".backup '$DEST_LOCAL/pokesketch-$STAMP.db'"

# Keep the last 14 local snapshots.
ls -1t "$DEST_LOCAL"/pokesketch-*.db 2>/dev/null | tail -n +15 | xargs -r rm -f

# Push tonight's snapshot to backup host (offsite from the LXC).
ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -i /path/to/backup-ssh-key backup-user@backup-host.example 'mkdir -p /path/to/backups' 2>/dev/null || true
rsync -a -e 'ssh -i /path/to/backup-ssh-key -o StrictHostKeyChecking=accept-new' "$DEST_LOCAL/pokesketch-$STAMP.db" "$DEST_REMOTE"

echo "[$(date -Is)] Backup complete: pokesketch-$STAMP.db"
