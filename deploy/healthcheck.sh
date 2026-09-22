#!/bin/bash
# Basic health check: service must be active, and the daily job must have
# succeeded for today's date (checked shortly after the scheduled post time).
set -euo pipefail
LOG_TAG=pokesketch-healthcheck

if ! systemctl is-active --quiet pokesketch; then
    logger -t "$LOG_TAG" "ALERT: pokesketch.service is not active"
    systemctl status pokesketch --no-pager | logger -t "$LOG_TAG"
    exit 1
fi

TODAY=$(date +%Y-%m-%d)
COUNT=$(journalctl -u pokesketch --since "today" 2>/dev/null | grep -c "posted daily" || true)
if [ "${COUNT:-0}" -gt 0 ]; then
    logger -t "$LOG_TAG" "OK: $COUNT daily post(s) found in today's log ($TODAY)"
else
    logger -t "$LOG_TAG" "ALERT: no successful daily post found in today's log ($TODAY)"
    exit 1
fi
