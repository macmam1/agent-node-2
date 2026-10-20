#!/bin/bash
# keep-alive script for sleep-prone platforms (Vercel, Render, Belmo free tier)
# Runs as a background process to prevent container from sleeping
#
# Usage: ./keepalive.sh [target_url] [interval_seconds]
# or: nohup ./keepalive.sh &

TARGET="${1:-http://localhost:${PORT:-8080}/health}"
INTERVAL="${2:-300}"  # 5 minutes default
LOGFILE="/var/log/agent-node/keepalive.log"

# Only run if ENABLE_KEEPALIVE=true
if [ "${ENABLE_KEEPALIVE:-true}" != "true" ]; then
    echo "[$(date)] keepalive disabled (ENABLE_KEEPALIVE != true)" >> "$LOGFILE"
    exit 0
fi

echo "[$(date)] keep-alive started — target: $TARGET, interval: ${INTERVAL}s" >> "$LOGFILE"

while true; do
    STATUS=$(curl -sf -o /dev/null -w "%{http_code}" "$TARGET" 2>/dev/null || echo "000")
    echo "[$(date)] Health check: $STATUS" >> "$LOGFILE"

    # If health check fails, container may be sleeping
    # The ping itself wakes the container
    if [ "$STATUS" = "000" ]; then
        echo "[$(date)] WARNING: Container appears to be sleeping — will retry next cycle" >> "$LOGFILE"
    fi

    sleep "$INTERVAL"
done
