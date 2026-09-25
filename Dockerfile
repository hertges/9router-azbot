# 9Router (files from prebuilt image) + AzBot (this repo) in one container.
# Fits a single lowest-spec Sevalla instance: 9router owns $PORT (health
# probe), AzBot runs alongside as a supervised background process.
#
# Base is python:3.12-bookworm-slim, NOT the 9router image: its Alpine edge
# ships python 3.14, which pyrogram 2.x cannot import (asyncio.get_event_loop
# removal) and tgcrypto 1.2.5 has no wheels for. Node 22 + 9Router files are
# copied in from their official images instead. 3.12 is AzBot's pinned
# runtime (same as its own Dockerfile).
FROM decolua/9router:0.5.86 AS nine
FROM node:22-bookworm-slim AS nodebin
FROM python:3.12-bookworm-slim

# AzBot runtime (Debian names) + C toolchain for tgcrypto (no cp312 wheels,
# builds from source — purged again after pip install).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg aria2 p7zip-full zip wget curl ca-certificates gcc python3-dev libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Node 22 for 9Router, from the official image.
COPY --from=nodebin /usr/local/bin/node /usr/local/bin/node
COPY --from=nodebin /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -sf /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -sf /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && node --version

# 9Router app: standalone build + custom-server.js + open-sse + node_modules.
COPY --from=nine /app /app

COPY azbot/requirements.txt /tmp/azbot-requirements.txt
RUN pip3 install --no-cache-dir -r /tmp/azbot-requirements.txt \
    && rm /tmp/azbot-requirements.txt \
    && apt-get purge -y gcc python3-dev libffi-dev \
    && apt-get autoremove -y \
    && python3 -c "import pyrogram, tgcrypto; print('pyrogram+tgcrypto ok')"

COPY azbot/ /app/azbot/
COPY entrypoint.sh /entrypoint-combined.sh
RUN chmod +x /entrypoint-combined.sh \
    && rm -rf /app/azbot/__pycache__ /app/azbot/bot_pkg/__pycache__

# Low-RAM defaults for a 0.5-1 GB box. Override in Sevalla dashboard.
ENV QUEUE_WORKERS=1 \
    UPLOAD_WORKERS=1 \
    DRIVE_UPLOAD_CONCURRENCY=1 \
    HANDLER_WORKERS=8 \
    LOG_LEVEL=INFO

ENTRYPOINT ["/entrypoint-combined.sh"]
