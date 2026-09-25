# 9Router (prebuilt image) + AzBot (this repo) in one container.
# Fits a single lowest-spec Sevalla instance: 9router owns $PORT (health
# probe), AzBot runs alongside as a supervised background process.
FROM decolua/9router:0.5.86

USER root

# AzBot runtime (Alpine package names) + C toolchain for tgcrypto.
RUN apk add --no-cache python3 py3-pip ffmpeg aria2 p7zip zip su-exec \
        gcc musl-dev python3-dev libffi-dev

COPY azbot/requirements.txt /tmp/azbot-requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/azbot-requirements.txt \
    && rm /tmp/azbot-requirements.txt \
    && apk del gcc musl-dev python3-dev \
    && python3 -c "import pyrogram; print('pyrogram ok')"

COPY azbot/ /app/azbot/
COPY entrypoint.sh /entrypoint-combined.sh
RUN chmod +x /entrypoint-combined.sh \
    && rm -rf /app/azbot/__pycache__ /app/azbot/bot_pkg/__pycache__ \
    && chown -R node:node /app/azbot

# Low-RAM defaults for a 0.5-1 GB box. Override in Sevalla dashboard.
ENV QUEUE_WORKERS=1 \
    UPLOAD_WORKERS=1 \
    DRIVE_UPLOAD_CONCURRENCY=1 \
    HANDLER_WORKERS=8 \
    LOG_LEVEL=INFO

ENTRYPOINT ["/entrypoint-combined.sh"]
