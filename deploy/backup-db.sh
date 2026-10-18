#!/bin/bash
# Nightly SQLite backup for PokeSketchDex — local snapshot and optional remote copy.
set -euo pipefail
umask 077
DB=/opt/pokesketch/data/pokesketch.db
DEST_LOCAL=/opt/pokesketch/data/backups
STAMP=$(date +%Y%m%d-%H%M%S)

if [ "${BACKUP_REQUIRE_REMOTE:-0}" = 1 ] && [ -z "${BACKUP_REMOTE_DEST:-}" ]; then
    echo "BACKUP_REMOTE_DEST is required when BACKUP_REQUIRE_REMOTE=1" >&2
    exit 1
fi

mkdir -p "$DEST_LOCAL"

# Use sqlite3 .backup for a consistent snapshot even while the bot is writing.
sqlite3 "$DB" ".backup '$DEST_LOCAL/pokesketch-$STAMP.db'"

# Keep the last 14 local snapshots.
ls -1t "$DEST_LOCAL"/pokesketch-*.db 2>/dev/null | tail -n +15 | xargs -r rm -f

# Configure BACKUP_REMOTE_DEST in /etc/pokesketch-backup.env for an off-host copy.
# The destination directory must already exist on the remote host.
if [ -n "${BACKUP_REMOTE_DEST:-}" ]; then
    if [ -n "${BACKUP_SSH_KEY:-}" ]; then
        case "$BACKUP_SSH_KEY" in
            *[!a-zA-Z0-9_./-]*) echo "BACKUP_SSH_KEY may contain only letters, digits, _, ., / and -" >&2; exit 1 ;;
        esac
        rsync -a -e "ssh -i $BACKUP_SSH_KEY -o BatchMode=yes -o StrictHostKeyChecking=yes" \
            "$DEST_LOCAL/pokesketch-$STAMP.db" "$BACKUP_REMOTE_DEST"
    else
        rsync -a -e 'ssh -o BatchMode=yes -o StrictHostKeyChecking=yes' \
            "$DEST_LOCAL/pokesketch-$STAMP.db" "$BACKUP_REMOTE_DEST"
    fi
else
    echo "[$(date -Is)] WARNING: BACKUP_REMOTE_DEST unset; backup is local only" >&2
fi

echo "[$(date -Is)] Backup complete: pokesketch-$STAMP.db"
