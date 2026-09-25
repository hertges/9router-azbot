#!/bin/sh
# Combined entrypoint: 9Router (foreground, owns $PORT) + AzBot (supervised).
set -e

DATA_DIR="${DATA_DIR:-/app/data}"
mkdir -p "$DATA_DIR/azbot" "$DATA_DIR/db" 2>/dev/null || true

# AzBot keeps settings/cookies/indexes on the persistent disk so redeploys
# don't wipe Drive auth or /settings. Downloads stay ephemeral.
if [ ! -L /app/azbot/data ]; then
    rm -rf /app/azbot/data
    ln -sfn "$DATA_DIR/azbot" /app/azbot/data
fi
mkdir -p /app/azbot/downloads
chown -R node:node /app/data /app/azbot 2>/dev/null || true

# AzBot supervisor: restart on crash/exit. If API_ID/API_HASH/BOT_TOKEN are
# missing it exits(1) here until you set them in the Sevalla dashboard.
(
while true; do
    python3 /app/azbot/bot.py || true
    echo "[azbot] exited, restarting in 15s..."
    sleep 15
done
) &

# 9Router in foreground. Next standalone honors $PORT, which Sevalla injects.
cd /app
exec node custom-server.js
