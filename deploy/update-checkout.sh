#!/usr/bin/env bash
# Update an existing checkout only when its history can fast-forward.
# A rewritten upstream must be migrated deliberately after preserving local state.
set -euo pipefail

APP_DIR="${1:-/opt/pokesketch}"
if ! git -C "$APP_DIR" diff --quiet || ! git -C "$APP_DIR" diff --cached --quiet; then
    echo "Refusing to update $APP_DIR: tracked changes are present" >&2
    exit 1
fi

git -C "$APP_DIR" fetch origin master
if ! git -C "$APP_DIR" merge-base --is-ancestor HEAD origin/master; then
    echo "Refusing to update $APP_DIR: local and remote history diverged." >&2
    echo "Back up the database and preserve local files, then perform the one-time history rewrite migration." >&2
    exit 1
fi

git -C "$APP_DIR" merge --ff-only origin/master
